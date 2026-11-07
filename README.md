# Telegraf for Home Assistant

Send metrics from [Telegraf](https://github.com/influxdata/telegraf) straight
into Home Assistant. Every metric field turns into a sensor automatically, and
each Telegraf `host` becomes a device. No per-metric configuration is needed.

- **Push-based:** Telegraf's built-in `[[outputs.http]]` posts JSON to a Home
  Assistant webhook.
- **Local only:** by default the webhook only accepts requests from your
  local network, and it is **never** reachable through Nabu Casa / Home
  Assistant Cloud.
- **Auto-discovery:** sensors get sensible units and classes for common inputs
  (cpu, mem, disk, diskio, net, system, temp), so they work with HA's long-term
  statistics.
- **Restart-safe:** discovered sensors keep their entity IDs and history.

Requires Home Assistant 2026.10 or newer.

## Installation

### Marketplace (recommended)

Home Assistant 2026.11 and newer have the Marketplace built in.

1. *Settings → Marketplace → Add from link* → enter
   `CerealP0rt/ha-telegraf`, then *Add*. (The Marketplace asks you to
   connect GitHub first if you haven't yet.)
2. Open **Telegraf** in the list, select *Install*, then restart Home
   Assistant if the repair under *Settings → System → Repairs* asks you to.

On Home Assistant 2026.10, add the repository to HACS as a custom
repository of type *Integration* instead.

### Manual

Copy `custom_components/telegraf/` into `<config>/custom_components/` and
restart Home Assistant.

## Setup

1. *Settings → Devices & services → Add integration → Telegraf*.
2. Pick a name. The dialog then shows your webhook URL and a ready-made
   Telegraf block.
3. Add the block to your Telegraf configuration and restart Telegraf:

```toml
[[outputs.http]]
  ## Must be reachable from Home Assistant's local network (local-only webhook)
  url = "http://<ha-lan-address>:8123/api/webhook/<webhook_id>"
  method = "POST"
  data_format = "json"
  content_encoding = "gzip"
  ## 400/413 will never succeed; retry only 503 (Telegraf 1.29+)
  non_retryable_statuscodes = [400, 413]
  [outputs.http.headers]
    Content-Type = "application/json"
```

`non_retryable_statuscodes` needs Telegraf 1.29 or newer. Older versions
refuse to start with it, so remove that line there; without it, Telegraf
keeps retrying batches that Home Assistant rejected (400/413).

Sensors appear as soon as the first metrics arrive. You can open the URL again
at any time from the integration's *Configure* dialog.

**lm-sensors users:** for `[[inputs.sensors]]`, decide on `remove_numbers`
before you start sending data, and keep it. The default `true` produces
`fan_input`; `false` produces `fan1_input`. Both get the correct units, but
the field name is part of each sensor's identity. Changing the setting later
creates new sensors and leaves the old ones unavailable.

**Tip:** every metric field becomes an entity. Use Telegraf's own filters on
the output (`namepass`, `fieldpass`, `tagexclude`, …) to send only what you
want in Home Assistant.

## Options

| Option | Default | Description |
|---|---|---|
| Include measurements | *(all)* | Glob patterns; only matching measurements create sensors, and existing sensors that no longer match are removed |
| Exclude measurements | *(none)* | Glob patterns; matching measurements are ignored and existing sensors removed |
| Stale timeout | 1800 s | A sensor becomes unavailable when no value arrives for this long; keep it above your longest collection interval (0 = never) |
| Connectivity timeout | 300 s | A host's Connectivity turns off when the host sends nothing at all for this long (0 = never) |
| Maximum number of entities | 500 | Safety cap for auto-discovery; each host's Connectivity counts too |
| Only accept requests from the local network | on | Home Assistant's local-network check. Turn it off if Telegraf reaches Home Assistant over global IPv6 (see *Troubleshooting*). Nabu Casa is blocked either way |

## Entities

For a metric `cpu,cpu=cpu-total,host=nas usage_active=2.1` you get the device
**nas** with the sensor **Processor use** (`sensor.nas_processor_use`). Names
follow Home Assistant's System Monitor: a readable label, then the tag values
that tell series apart, e.g. *Disk usage /*, *Network in eth0*,
*Temperature k10temp-pci-00c3 tctl*, *RAID state md0*. Metrics of inputs
this integration doesn't know are named measurement, tag values, field
(e.g. *my_app eu requests*). Existing entities keep their entity IDs when
names change; only the displayed name follows.

**Drives** (`[[inputs.smart]]`) that report a serial number get a device of
their own, connected via the host and named by model and serial tail, e.g.
**Samsung SSD 980 PRO 1TB 593Y** with *Temperature*, *Health*, … The
serial identifies the drive, since `sda`/`nvme0` can change across reboots.
Deleting a drive's device (e.g. after replacing it) removes only its
sensors.

- Numbers and booleans (as 1/0) become numeric sensors. Strings become text
  sensors. A few flags become binary sensors, e.g. a drive's *Health*
  shows *OK* or *Problem*.
- After a Home Assistant restart, sensors are unavailable until Telegraf sends
  fresh data (usually within seconds).
- Each host also gets a **Connectivity** binary sensor. It turns *off* when
  the host has sent nothing for the connectivity timeout (5 minutes by
  default), so one automation can watch for "server stopped reporting":

  ```yaml
  triggers:
    - trigger: state
      entity_id: binary_sensor.nas_connectivity
      to: "off"
  ```
- Only a **short list of core sensors** starts enabled: CPU, memory and swap
  usage, load, boot time (needs `[[inputs.kernel]]`), kernel version,
  temperatures, and each drive's temperature, health and wear. Per-disk and per-interface metrics
  (`disk`, `diskio`, `net`) start disabled because hosts often have many
  disks and interfaces.
  **Everything else is created disabled**, including any input this
  integration doesn't know (docker, procstat, your own…). Enable what you
  want to track on the device page. To enable many at once, go to
  *Settings → Entities*, filter by *Telegraf*, select them and choose
  *Enable*. See `docs/spec.md` §5.6 for the exact list.
- With `[[inputs.system]]` reporting OS info, the host's device shows the
  platform as model, its version and the architecture (e.g.
  *opensuse-microos*, *20261024*, *x86_64*), and the kernel version is a
  diagnostic sensor. To give the device a *Visit* link (e.g. to Cockpit),
  tag the system input:

  ```toml
  [[inputs.system]]
    [inputs.system.tags]
      ha_configuration_url = "https://microos:9090"
  ```
- A **Reboot required** binary sensor (*Yes*/*No*) shows when an installed
  update needs a reboot. Add this check to Telegraf (on Debian/Ubuntu use
  `/var/run/reboot-required`):

  ```toml
  [[inputs.exec]]
    commands = ["sh -c 'test -e /run/reboot-needed && echo 1 || echo 0'"]
    data_format = "value"
    data_type = "integer"
    name_override = "reboot_needed"
    interval = "1m"
  ```
- You can delete a host's device from the UI. It comes back if that host sends
  data again.

## Security

The webhook ID in the URL is the only credential, so treat the URL like a
password. By default the webhook is registered as *local only*, which means
Home Assistant drops requests that

- come from outside your local network, or
- arrive through Home Assistant Cloud (Nabu Casa) remote access.

Dropped requests are answered with HTTP 200 and logged as
`Received remote request for local webhook`.

If you turn off *Only accept requests from the local network*, there is no
check of the sender's address: anyone who can reach Home Assistant directly
(e.g. through a port forward or public IPv6) and knows the URL can send
metrics. Requests through Nabu Casa are still rejected.

## Troubleshooting

- **No sensors, and the log shows `Received remote request for local webhook`:**
  Home Assistant doesn't see Telegraf's address as local. This is common
  behind a reverse proxy, a VPN or a Docker network. If you use a proxy,
  configure `use_x_forwarded_for` and `trusted_proxies` in the `http:`
  section. To see the address Home Assistant sees, run `curl -i <webhook-url>`
  from the Telegraf machine: the GET is answered with 405 and the log shows
  `… was received from <address>`.
- **Problems with IPv6** (the address above is a global IPv6 address such as
  `2a02:…`, not `fd…`): Home Assistant only counts private addresses as
  local, and Telegraf prefers global IPv6 when the hostname has one. Turn off
  *Only accept requests from the local network* in the integration's
  *Configure* dialog (read *Security* first), or make the hostname resolve
  to an IPv4 or `fd…` address only on your local DNS.
- **Sensor count stopped growing:** the *Maximum number of entities* limit was
  reached (a warning is logged). Raise it or filter in Telegraf.
- **Sensors unavailable:** no data arrived within the stale timeout. Check
  Telegraf's log.
- **Diagnostics:** *Settings → Devices & services → Telegraf → ⋮ → Download
  diagnostics*. The webhook ID is redacted.

## Development

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
```

Behavior is specified in [`docs/spec.md`](docs/spec.md).

## License

Released into the public domain under [The Unlicense](LICENSE).
