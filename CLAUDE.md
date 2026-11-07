# ha-telegraf

Home Assistant custom integration (domain `telegraf`, HACS custom repo) that
receives metrics pushed by Telegraf's `[[outputs.http]]` (JSON serializer) on
a local-only webhook and auto-creates sensor entities, one device per `host`.

**`docs/spec.md` is the source of truth for behavior.** Read the relevant
section before changing behavior, and update the spec in the same commit when
behavior changes. Tests reference spec sections (`§6.2` etc.).

## Layout

```
custom_components/telegraf/
  __init__.py      entry setup/unload/remove, webhook register + handler
  config_flow.py   ConfigFlow (user step) + OptionsFlow; canonical snippet
  const.py         DOMAIN, option keys, defaults, host kinds, signal names
  parser.py        Telegraf JSON -> list[Metric]; NO homeassistant imports
  units.py         unit/class heuristics (§5.4/§5.5) + enable allowlist (§5.6)
  identity.py      unique_ids/device identifiers — FROZEN formulas (§5.1, §5.7)
  naming.py        entity names (§5.2), System Monitor style
  hub.py           TelegrafHub: known sensors/hosts, filters, cap, Store, staleness
  entity.py        entity bases (host, metric), host/drive device info, platform setup helpers
  sensor.py        TelegrafSensor, platform setup
  binary_sensor.py binary metric fields (§5.4.2), Connectivity (§5.7, restores last-seen)
  diagnostics.py   redacted diagnostics
  manifest.json, strings.json, translations/en.json, icons.json
  brand/           icon.png (256 px), icon@2x.png (512 px); source docs/icon.svg
tests/             pytest-homeassistant (HA's own test package); shared helpers in common.py
docs/spec.md       functional spec
hacs.json          HACS metadata
```

## Commands (uv)

```bash
uv sync                                   # create .venv with HA + test deps
uv run pytest                             # run tests
uv run pytest tests/test_parser.py -q     # single file
uv run ruff check . && uv run ruff format .
```

The HA version for tests is pinned by `pytest-homeassistant` in
`pyproject.toml` (exact pin; its version is the HA version it requires, so
bumping it bumps HA and possibly the Python version in `.python-version`).
It may be newer than the **development target**, the `hacs.json`
`homeassistant` minimum (2026.10, the user's instance): code must only use
HA APIs that exist in the target, even when tests run on a newer HA.

## Conventions

- Async-only HA APIs; no blocking I/O in the event loop.
- Per-entry state lives in `entry.runtime_data` (typed `TelegrafConfigEntry`),
  not `hass.data`.
- `parser.py`, `units.py`, `naming.py` and `identity.py` stay pure (no HA imports) so they are trivially
  unit-testable.
- **Security invariant:** the webhook must never be reachable through
  Nabu Casa / HA Cloud. By default it is registered with `local_only=True`;
  the user-facing `local_only` option (spec §4, §7.2) may turn HA's IP check
  off, but then the handler must still reject `is_cloud_connection`
  requests. No IP checks of our own (deliberate decision), never create a
  cloudhook, no shared-secret header (deliberate decision).
- The handler must never raise into HA; partially valid batches get 204 so
  Telegraf does not retry them forever (spec §4.1).
- All user-facing strings go in `strings.json` and are mirrored to
  `translations/en.json`.

## HACS requirements

- Exactly one integration under `custom_components/telegraf/`.
- `manifest.json` must keep: `domain`, `name`, `version`, `documentation`,
  `issue_tracker`, `codeowners`, `config_flow`, `iot_class`, `dependencies`.
- Bump `version` in `manifest.json` (and `pyproject.toml`, then `uv lock`)
  for every release.
- Releasing: commit the bump, then `git tag -a vX.Y.Z` with the release notes
  as the annotation. Pushing the tag runs `.github/workflows/release.yml`:
  a check that the tag equals `v` + the manifest version, then a GitHub
  release (`gh release create --notes-from-tag`; tags with `-` become
  pre-releases). HACS offers that release as the update.
- `hacs.json` at repo root: `name`, minimum `homeassistant`.
- No hassfest/HACS GitHub actions: they pull mutable container images
  (`hassfest:latest`, `hacs/action:main`). Run hassfest by hand when needed,
  pinned to a reviewed image digest. CI actions are pinned to commit SHAs
  and tools to exact versions. Dependabot (`.github/dependabot.yml`)
  proposes action and uv updates after a 7-day cooldown; the uv version in
  `tests.yml` (`setup-uv` `version:`) is bumped by hand.

## Git

- Local-only repo, **no remote — never push**. Committing to `main` is fine.
- One concern per commit, with messages that explain what changed and why.

## Manual testing on a real HA

1. Copy `custom_components/telegraf/` to `<ha-config>/custom_components/`.
2. Restart HA → Settings → Devices & services → Add integration → Telegraf.
3. Paste the shown `[[outputs.http]]` block into Telegraf's config; run
   `telegraf --config <file> --test` then restart the Telegraf service.
4. Smoke test without Telegraf:
   `curl -X POST -H 'Content-Type: application/json' -d @tests/fixtures/batch.json <webhook-url>`
   (must originate from the LAN — see spec §4).
