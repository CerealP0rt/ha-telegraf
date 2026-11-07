# Telegraf for Home Assistant — Functional Specification

Status: draft v0.1 · Domain: `telegraf` · Distribution: Marketplace (HACS) custom repository

This document is the source of truth for behavior. Code and tests follow it;
when behavior changes, this file changes in the same commit. Section numbers
(§) are referenced from tests.

---

## §1 Goals and non-goals

**Goals**

- Receive metrics pushed by [Telegraf](https://github.com/influxdata/telegraf)
  and expose them as Home Assistant `sensor` entities without per-metric
  configuration.
- Setup entirely through the HA UI (config flow); Telegraf needs only a
  standard `[[outputs.http]]` block.
- Keep the ingestion endpoint **LAN-only** — never reachable through
  Nabu Casa / Home Assistant Cloud.
- Survive HA restarts: discovered entities keep their IDs and history.

**Non-goals**

- Pulling data from HA into Telegraf (HA → Telegraf direction).
- Any transport other than HTTP push (no MQTT, no Prometheus scraping).
- Influx line protocol input (JSON only).
- Long-term storage — HA's recorder/statistics handle history.
- Writing back to Telegraf or controlling it.

## §2 Data flow

```
Telegraf inputs ──► [[outputs.http]] (data_format = "json", batch)
                         │  POST, optional gzip
                         ▼
        http://<ha-lan>:8123/api/webhook/<webhook_id>
                         │  HA webhook component, local_only=True
                         ▼
               handler (custom_components/telegraf/__init__.py)
                         │  parser.parse_payload(bytes) -> list[Metric]
                         ▼
               TelegrafHub (hub.py, entry.runtime_data)
                 ├─ known key  → update value, signal entity
                 ├─ new key    → filters + cap → persist → add entity
                 └─ any metric → host connectivity
                         ▼
               one device per host (entity.py), carrying:
                 ├─ TelegrafSensor per metric field (sensor.py)
                 └─ Connectivity binary sensor (binary_sensor.py), per host, §5.7
```

One config entry = one webhook = one independent set of devices/entities.
Several entries may exist (e.g. one per Telegraf instance).

## §3 Payload format

Telegraf's JSON serializer, as used by `outputs.http` (`use_batch_format`
defaults to `true`).

### §3.1 Batch form (primary)

```json
{
  "metrics": [
    {
      "name": "cpu",
      "tags": {"cpu": "cpu-total", "host": "nas"},
      "fields": {"usage_idle": 97.1, "usage_user": 1.9},
      "timestamp": 1760090400
    }
  ]
}
```

### §3.2 Single-metric form

A bare metric object (`{"name", "tags", "fields", "timestamp"}`) is accepted
as a batch of one (produced with `use_batch_format = false`).

### §3.3 Parsing rules

- **Names**: the measurement `name`, tag keys, tag values and field names must
  be valid Unicode strings (no lone surrogates) of 1–255 characters without
  control characters (C0 `U+0000`–`U+001F`, DEL, C1 `U+0080`–`U+009F`).
- `name` — required and valid; otherwise the metric is skipped.
- `tags` — optional object; missing or not an object = `{}`. Number and
  boolean tag values are converted with `str()`; array and object values are
  invalid; `null` and empty-string values are dropped (treated as absent). Any invalid tag key or value — including an
  integer literal longer than 20 digits — skips the whole metric, because
  dropping it could merge distinct series.
- **Series size**: a metric with more than 64 tags (counted as sent,
  including `host` and `null`/empty ones) is skipped, as is one whose
  series identity — the key of §5.1 without the field, i.e.
  `["<host>","<measurement>",[[k, v], ...],""]` — exceeds 4096 characters.
  The tag set is repeated in every field's key, entity name and stored
  record, so it must stay bounded. Stored records (§6.3) that exceed these
  bounds are skipped on load.
- **Volatile tags**: a few known tags change value at runtime for the same
  device. Since tags are part of a sensor's identity (§5.1), keeping them
  would create a new set of entities on every change and leave the old ones
  unavailable. They are removed after the 64-tag count check and before the
  4096-character identity check (which thus applies to the identity
  actually used), for these measurements only:
  | Measurement | Removed tags | Why |
  |---|---|---|
  | `disk` | `device`, `fstype`, `label`, `mode` | the mount point (`path`) identifies a disk; the device name can change across reboots, `mode` on an ro/rw remount, `label` and `fstype` with the filesystem |
  | `smart_device` | `power`, `enabled` | power mode (ACTIVE/IDLE/STANDBY) of spinning disks; SMART on/off |
  | `smart_attribute` | `power`, `enabled`, `fail` | as above; WHEN_FAILED (`-`, `FAILING_NOW`, `In_the_past`) |
  | `mdstat` | `ActivityState`, `Devices` | active/check/resync/recover; member list on disk failure or replacement |
  | `system` | `ha_configuration_url` | the host device's link, not a series (§5.1) |

  `mdstat` `ActivityState`, `smart_attribute` `fail` and `system`
  `ha_configuration_url` are kept as a text field of the same name (e.g. the sensor `RAID state md0`, §5.2), unless
  the metric already has a field with that name.

  For `smart_device` and `smart_attribute`, `device` (kernel name such as
  `sda`/`nvme0`, which can change across reboots) and `device_type`
  (controller slot, e.g. `megaraid,1`) are also removed **when the metric
  has a `serial_no` tag**: the serial identifies the drive. Without a
  serial (a drive in standby reports none) they are kept.
- `fields` — required object; a metric with no usable fields yields nothing.
  Invalid field names skip just that field.
- `timestamp` — ignored for state; HA's receive time is used as "last seen".
  (Its units/format depend on `json_timestamp_units` / `json_timestamp_format`
  and cannot be interpreted reliably.)
- `host` tag — becomes the device; removed from the tag set used for naming.
  Missing/empty `host` → `"telegraf"`.
- Field values:
  | JSON type | Result |
  |---|---|
  | int within int64/uint64 range (−2⁶³ … 2⁶⁴−1) | numeric sensor value (stays int) |
  | float (finite) | numeric sensor value (stays float: JSON `1.0` shows as `1.0`) |
  | bool | numeric `1` / `0` |
  | string (valid Unicode) | non-numeric (text) sensor value, truncated to 255 chars |
  | null, object, array, NaN/±Inf, out-of-range int, int literal > 20 digits, string with lone surrogates | field skipped |
- Invalid individual metrics or fields are skipped (logged at debug);
  the rest of the batch is still processed. Untrusted input in log
  messages is always a truncated `repr` (no raw newlines, bounded length).

### §3.4 Transport details

- Body must be UTF-8 JSON (other encodings → 400).
- Body may be gzip-compressed (`content_encoding = "gzip"`). Decompression
  is handled by aiohttp (capped at HA's 16 MiB request limit); as a fallback,
  a body starting with the gzip magic bytes `1f 8b` is decompressed by the
  handler with the same 16 MiB cap (gzip-bomb protection). Exceeding the
  cap answers 413 on either path (§4.1).
- The fallback accepts exactly one gzip member: a second member or trailing
  bytes after it make the body invalid (400) rather than being dropped.
- Only `POST` is accepted (`allowed_methods=["POST"]`).

## §4 Webhook and security model

- Registered with
  `webhook.async_register(hass, DOMAIN, entry.title, webhook_id, handler,
  local_only=<option>, allowed_methods=["POST"])`, where `<option>` is the
  `local_only` option (§7.2, default `true`).
- **`local_only` on (default):** HA core drops requests that:
  - arrive through a Home Assistant Cloud (Nabu Casa) connection
    (`is_cloud_connection`), or
  - come from a non-local IP address (`network.is_local`: loopback,
    `10/8`, `172.16/12`, `192.168/16`, `fd00::/8`, link-local).
  HA answers such requests with **200 and no processing**, logging
  `Received remote request for local webhook <id>` at warning level.
- **`local_only` off:** registered with `local_only=False`, so HA core does
  no IP check, and neither does the integration: any source address is
  accepted. Requests through Nabu Casa are **still dropped**, by the
  integration's handler (`is_cloud_connection`), with **200 and no
  processing** and a warning naming the entry title (not the webhook ID).
  Meant for networks where Telegraf reaches HA over global IPv6 addresses,
  which `network.is_local` does not count as local; the user accepts that
  anyone who can reach HA directly and knows the URL can post metrics.
- Nabu Casa is blocked in both modes.
- The integration **never** creates a cloudhook
  (`cloud.async_create_cloudhook`).
- The webhook ID (generated by `webhook.async_generate_id()`) is the only
  credential. The integration never logs it and redacts it in diagnostics.
  HA core's webhook component does log it (e.g. for rejected remote
  requests), so treat HA logs as sensitive.
- Reverse proxies: if HA sits behind one, `http: use_x_forwarded_for` and
  `trusted_proxies` must be configured, otherwise every request appears to
  come from the proxy's address.
- Unknown webhook IDs and rejected requests are handled by HA core, not by
  this integration.

### §4.1 Response codes (from the handler)

| Situation | Status |
|---|---|
| Valid JSON, ≥0 metrics processed (including all skipped) | 204 |
| Body is not valid JSON / not UTF-8 / gzip broken / nested too deeply | 400 |
| JSON top level is neither an object with `metrics` list nor an object with `name` and `fields` | 400 |
| Body (after decompression) larger than 16 MiB | 413 |
| Entry is setting up, unloading or reloading (hub not running) | 503 (Telegraf retries) |

Any unexpected exception while parsing is also answered 400 (logged at
debug), so nothing reaches HA core's webhook error handler, which would
log the webhook ID and answer 200.

The webhook is registered at the very start of entry setup and stays
registered until the platforms have unloaded, so requests during setup and
unload get 503. One gap remains during a reload — between the old entry's
unregistration and the new one's registration (no I/O in between, so it is
short). There HA core itself answers **200** for the unknown webhook, so
Telegraf considers that batch delivered, and core logs the webhook ID at
info level. Reloads happen on every options save and ~30 s after an entity
is enabled.

The body is parsed in HA's executor (up to 16 MiB of JSON would otherwise
stall the event loop), including building each field's key (§5.1).
Applying the metrics to the hub runs on the loop and is cheap per metric;
a series that appears several times in one batch (e.g. Telegraf flushing
its buffer after an outage) writes its entity state once, with the last
value.
The handler never raises into HA. Note: Telegraf retries batches answered
with non-2xx; partial-validity payloads therefore get 204 so a single bad
metric cannot wedge Telegraf's buffer. Only **503** is meant to be retried:
400 and 413 will fail again, so the snippet (§8) sets
`non_retryable_statuscodes = [400, 413]` (Telegraf 1.29+) to make Telegraf
drop such batches instead of retrying them forever.

## §5 Entity model

### §5.1 Identity

- **Sensor key**: compact JSON array
  `["<host>","<measurement>",[["k1","v1"],["k2","v2"]],"<field>"]`
  (`ensure_ascii=False`, no spaces), tags sorted by key, `host` and the
  volatile tags of §3.3 excluded.
  JSON escaping makes the key injective: separators inside values (`,` `=`
  `|`) cannot make two series collide.
- **unique_id**: `f"{entry_id}_{sha1(key.encode('utf-8')).hexdigest()[:16]}"`.
- These formulas (and §5.7's) are **frozen**: they are stored in HA's
  registries, so any change re-IDs existing entities. They live in
  `identity.py` (key format: `parser.make_key`) and are pinned by literal
  values in `tests/test_identity.py`.
- **Device**: identifiers `{(DOMAIN, f"{entry_id}_{host}")}`, name = host,
  manufacturer `Telegraf`, linked to the config entry.
- **Host info**: the text fields `os`, `platform`, `platform_family`,
  `platform_version` and `arch` of `system` (Telegraf's `inputs.system`
  with OS info) describe the host device instead of becoming sensors:
  model = `platform` (else `os`), `sw_version` = `platform_version`,
  `hw_version` = `arch` (`platform_family` is not shown; empty values are
  ignored). The user-set tag `ha_configuration_url` on `system` (kept as a
  field of that name, §3.3) becomes the device's `configuration_url` (its
  *Visit* link) if it is an absolute `http`/`https` URL with a valid port
  and an IDNA-valid host; anything else is ignored, as is any value HA's
  device registry still refuses (debug log; the batch is processed, §4.1).
  A change is written to the existing device; a new device gets
  the info from its first entity. The latest values are kept in memory
  only: after a restart the registry keeps the stored values until new
  data arrives (entities never send fields they have not received). Hosts
  without these fields have no model. Excluded `system` (§7.2) sets no
  host info. They still count as a sign of life (§5.7).
- **Drive device**: series of `smart_device`/`smart_attribute` with a
  `serial_no` tag belong to a device of their own instead of the host's:
  identifiers `{(DOMAIN, f"{entry_id}:drive:" + json.dumps([host, serial],
  ensure_ascii=False))}` (default separators, e.g.
  `E1:drive:["nas", "S5GX593Y"]`; the `:` after the entry id never matches
  a host identifier's `_`, and the host keeps the same drive in two hosts
  apart), name = model plus the last 4 characters of the serial
  (`Samsung SSD 980 PRO 1TB 593Y`; `Drive 593Y` without a `model` tag),
  `model` = the `model` tag, `serial_number` = the serial, and
  `via_device_id` = the host device. The host device is created first if
  needed (HA requires the via device to exist). Without a serial (drive in
  standby, some controllers) the series stays on the host device.
  Drives have no Connectivity of their own: their data comes with the
  host's (§5.7).

### §5.2 Naming

- `has_entity_name = True`; device name supplies the host prefix.
- Names follow HA's System Monitor integration (`naming.py`): a readable
  **label** for the field, then the **values of the identifying tags** in
  tag-key order, space-separated, e.g. `Disk usage /`, `Network in eth0`,
  `Load (1 min)`, `Memory usage`.
- Label: from a per-measurement table for known inputs (`cpu`, `mem`,
  `swap`, `system`, `kernel`, `disk`, `diskio`, `net`, `temp`, `sensors`,
  `smart_device`, `smart_attribute`, `mdstat`); a field of a known
  measurement without its own label gets the measurement's prefix plus the
  field with `_` as spaces (`mem.page_tables` → `Memory page tables`).
- Tags left out of names (descriptive, not identifying):
  | Measurement | Hidden tags |
  |---|---|
  | `smart_device` | `model`, `serial_no`, `wwn`, `capacity` (shown as drive, see below) |
  | `smart_attribute` | as `smart_device`, plus `id` and `flags` (`name` identifies) |
  | `cpu` | `cpu` when `cpu-total` |
  | `net` | `interface` when `all` |

  Every other tag value is kept, including unknown tags, so two series of
  a device never share a name in practice. (`disk` needs no hidden tags:
  everything but `path` is volatile, §3.3.)
- **Drives** (`smart_device`, `smart_attribute`) with a `serial_no` are on
  their drive device (§5.1), which names the drive: the label loses its
  `Drive` prefix and `device`/`device_type` are hidden too, e.g.
  `Temperature` (shown as `Samsung SSD 980 PRO 1TB 593Y Temperature`),
  `Attribute raw value Temperature_Celsius`. Without a serial, the series
  is on the host device with the full label and the `device` tag:
  `Drive SMART exit status sda`.
- Labels never repeat across measurements of one host: `mem.swap_*` are
  `Memory swap …`, because `Swap …` is `inputs.swap`; sensors
  `power*_average` is `Power (average)`, `power*_input` is `Power`.
- Unknown measurements keep the generic form: `measurement`, then tag
  values in tag-key order, then `field`.
- Examples (device `nas`):
  | Series | Name |
  |---|---|
  | `cpu{cpu=cpu-total}.usage_active` | `Processor use` |
  | `cpu{cpu=cpu0}.usage_idle` | `Processor idle cpu0` |
  | `system.load1` | `Load (1 min)` |
  | `kernel.boot_time` | `Last boot` |
  | `disk{path=/}.used_percent` (other tags volatile, §3.3) | `Disk usage /` |
  | `net{interface=eth0}.bytes_recv` | `Network in eth0` |
  | `sensors{chip=k10temp-pci-00c3,feature=tctl}.temp_input` | `Temperature k10temp-pci-00c3 tctl` |
  | `smart_device{device=sda,model=WDC WD40EFRX,serial_no=WD-WCC7K1AB7XKL}.temp_c` | `Temperature` on device `WDC WD40EFRX 7XKL` |
  | `smart_device{device=sda}.exit_status` (standby, no serial) | `Drive SMART exit status sda` |
  | `mdstat{Name=md0}.BlocksSyncedPct` | `RAID sync progress md0` |
  | `my_app{region=eu}.requests` | `my_app eu requests` |
- Names are not part of the identity (§5.1). HA derives the entity_id from
  the name only at first registration, so renaming changes the displayed
  name of existing entities but not their entity_ids.

### §5.3 Value type is fixed at discovery

A sensor is numeric or text depending on the first value seen. Later values
of the other kind are ignored (debug log) — HA does not allow a sensor's
numeric-ness to change.

### §5.4 Units and classes (`units.py`)

Rules are evaluated in order; first match wins. Text sensors get no unit
and no state class. Numeric sensors that match no rule get
`state_class = measurement`, no unit, no device class. Data-size rules, the
mdstat data-rate rule and the uptime also set a suggested display unit (§5.5).

| # | Measurement | Field pattern | Unit | Device class | State class |
|---|---|---|---|---|---|
| 1 | `net` | `bytes_sent`, `bytes_recv` | B | data_size | total_increasing |
| 2 | `net` | `packets_*`, `err_*`, `drop_*` | — | — | total_increasing |
| 3 | `diskio` | `read_bytes`, `write_bytes` | B | data_size | total_increasing |
| 4 | `diskio` | `reads`, `writes`, `merged_reads`, `merged_writes` | — | — | total_increasing |
| 4a | `diskio` | `io_util` | % | — | measurement |
| 4b | `diskio` | `io_await`, `io_svctm` | ms | duration | measurement |
| 4c | `net` | `speed` | Mbit/s | data_rate | measurement |
| 5 | `diskio` | `*_time` | ms | duration | total_increasing |
| 6 | `system` | `uptime` | s | duration | measurement |
| 6a | `sensors` | `*_alarm`, `*_beep`, `*_fault` | — | — | measurement |
| 7 | `temp`, `sensors` | `temp*` | °C | temperature | measurement |
| 7a | `sensors` | `fan_*`, `fan[0-9]*_*` | rpm | — | measurement |
| 7b | `sensors` | `in_*`, `in[0-9]*_*` | V | voltage | measurement |
| 7c | `sensors` | `curr_*`, `curr[0-9]*_*` | A | current | measurement |
| 7d | `sensors` | `power_*`, `power[0-9]*_*` | W | power | measurement |
| 7e | `docker_container_net` | `rx_bytes`, `tx_bytes` | B | data_size | total_increasing |
| 7f | `procstat` | `read_bytes`, `write_bytes` | B | data_size | total_increasing |
| 8 | any | `*_bytes`, `bytes_*` | B | data_size | measurement |
| 9 | any | `*_percent`, `percent_*` | % | — | measurement |
| 9a | `cpu` | `usage_*` | % | — | measurement |
| 10 | `mem`, `swap`, `disk` | `total`, `free`, `used`, `available`, `cached`, `buffered`, `active`, `inactive`, `in`, `out` | B | data_size | measurement |
| 10a | `mem` | `swap_total`, `swap_free`, `commit_limit`, `committed_as`, `vmalloc_total`, `high_total`, `low_total` | B | data_size | measurement |
| 10b | `mem` | other byte fields: `swap_cached`, `dirty`, `high_free`, `low_free`, `huge_page_size`, `mapped`, `page_tables`, `shared`, `slab`, `sreclaimable`, `sunreclaim`, `vmalloc_chunk`, `vmalloc_used`, `write_back`, `write_back_tmp`, `active_anon`, `active_file`, `inactive_anon`, `inactive_file`, `unevictable`, `percpu` | B | data_size | measurement |
| 11 | `smart_device` | `temp_c` | °C | temperature | measurement |
| 11a | `smart_device` | `available_spare`, `available_spare_threshold`, `percentage_used`, `wear_leveling_count`, `percent_lifetime_remain`, `media_wearout_indicator` | % | — | measurement |
| 11b | `smart_device` | `power_on_hours` | h | duration | total_increasing |
| 11c | `smart_device` | `warning_temperature_time`, `critical_temperature_time` | min | duration | total_increasing |
| 11d | `smart_device` | `power_cycle_count`, `unsafe_shutdowns`, `media_errors`, `error_log_entries`, `spin_retry_count`, `end_to_end_error`, `uncorrectable_errors`, `command_timeout`, `realloc_event_count`, `udma_crc_errors` | — | — | total_increasing |
| 12 | `mdstat` | `BlocksTotal`, `BlocksSynced` | KiB | data_size | measurement |
| 12a | `mdstat` | `BlocksSyncedPct` | % | — | measurement |
| 12b | `mdstat` | `BlocksSyncedSpeed` | KiB/s | data_rate | measurement |
| 12c | `mdstat` | `BlocksSyncedFinishTime` | min | duration | measurement |
| 13 | `kernel` | `boot_time` | — | uptime (§5.4.1) | — |
| 13a | `kernel` | `context_switches`, `interrupts`, `processes_forked` | — | — | total_increasing |

Rules 7–7d accept both `inputs.sensors` field styles: `fan_input`
(`remove_numbers = true`, Telegraf's default) and `fan1_input`
(`remove_numbers = false`). The digit is required right after the prefix,
so e.g. `intrusion0_alarm` is not taken for a voltage. Alarm, beep and
fault flags (rule 6a, e.g. `temp1_alarm`, `in0_beep`) are 0/1 values and
get no unit. `usage_*` means percent only for `cpu` (rule 9a); docker's
`usage_total`/`usage_system` are nanosecond counters and match no rule.

#### §5.4.1 Boot time

HA has a device class for "when did it last restart": `uptime`, a datetime
(the frontend shows "3 days ago"; history is a timeline, no statistics).
`inputs.kernel` (Linux) sends it exactly: `kernel.boot_time` is `btime`
from `/proc/stat`, in epoch seconds, shown as that datetime (UTC). It is not
derived from `system.uptime`, which would need guessing (delivery delay,
Telegraf's buffer after an outage); `system.uptime` stays a plain duration
statistic (rule 6), shown in days (§5.5) and disabled by default (§5.6).

- The kernel recomputes `btime` from the wall clock, so a clock step (e.g.
  VMs with host time sync) can move it by a second or so. HA's `uptime`
  device class ignores changes within 60 seconds (its default drift
  tolerance), so the state does not flip; a reboot moves it by the whole
  previous uptime.
- Non-positive or out-of-range values give no value (unknown).
- Not stored; unknown after an HA restart until the next value arrives.

`mem` `huge_pages_free`/`huge_pages_total` are page counts, not bytes.
SMART current counts that can go down (`reallocated_sectors_count`,
`pending_sector_count`, `uncorrectable_sector_count`) and the
`exit_status` bitmask match no rule (`health_ok` and `critical_warning`
are binary sensors, §5.4.2). The
vendor-encoded `*_error_rate` fields too. mdstat `Disks*` are plain counts.

(`swap in/out` are cumulative in Telegraf but listed as measurement for
simplicity; refine later if needed.)

#### §5.4.2 Binary sensors

A few numeric fields are flags; they become **binary sensors** (platform
`binary_sensor`, same key and unique_id formula as §5.1) instead of
sensors. A JSON `true`/`false` arrives as 1/0 (§3.3).

| Measurement | Field | Device class | On (= problem) when |
|---|---|---|---|
| `smart_device` | `health_ok` | problem | value is 0 (smartctl's health check failed) |
| `smart_device` | `critical_warning` | problem | value is not 0 (any NVMe warning bit set) |
| `reboot_needed` | `value` | — (translation key `reboot`: states *Yes*/*No*, icon `mdi:restart`, `mdi:restart-alert` when on) | value is not 0 (the §8 exec check found `/run/reboot-needed`) |

`reboot_needed` is not a Telegraf input but the metric of the exec check
recommended in §8 (`name_override`); its name is `Reboot required` on the
host device. Translation keys only supply state names and icons
(`strings.json`, `icons.json`); entity names stay code-built (§5.2).

A text value for such a field keeps it a (text) sensor (§5.3).
Naming (§5.2), enabled-by-default (§5.6), availability (§6.2) and
registry cleanup (§6.3, §7.2) work as for sensors.

#### §5.4.3 Icons

After HA's System Monitor, sensors get one icon per subject, but only if
they have no device class or a `data_size`/`data_rate` one; all others
(temperature, duration, uptime, voltage, …, and binary sensors) keep HA's
device-class icon. As in System Monitor the icon is not set in code:
`units.icon_key` picks an icon translation key, the sensor sets it as its
`translation_key`, and `icons.json` (`entity.sensor.<key>.default`) maps it
to the icon, which the frontend applies (it is not a state attribute). The
key adds no name: `strings.json` has no entry for it and the explicitly set,
code-built name (§5.2) wins over translations. `icons.json` has exactly the
sensor keys `icon_key` can return.

| Measurement | Key | Icon |
|---|---|---|
| `cpu`, `kernel`, `system` (load, processors, …) | `cpu` | `mdi:cpu-64-bit` |
| `system` `n_users`, `n_unique_users` | `users` | `mdi:account-multiple` |
| `system` `kernel_version` | `kernel_version` | `mdi:chip` |
| `system` `uptime_format` | `uptime_format` | `mdi:clock-outline` |
| `mem` | `memory` | `mdi:memory` |
| `mem` `swap_*`, `swap`, `disk`, `diskio`, `smart_device`, `smart_attribute`, `mdstat` | `disk` | `mdi:harddisk` |
| `smart_device` `power_cycle_count` | `power_cycle_count` | `mdi:power-cycle` |
| `smart_device` `unsafe_shutdowns` | `unsafe_shutdowns` | `mdi:power-plug-off` |
| `net` | `network` | `mdi:server-network` |
| `sensors` `fan*` | `fan` | `mdi:fan` |
| anything else | none | HA's default |

### §5.5 Suggested display units

Data-size and data-rate sensors and the uptime also get a *suggested* display unit with 2 decimals:
**GiB** for rules 1, 3, 7e, 7f, 10, 10a and 12 (byte counters,
`mem`/`swap`/`disk` sizes, RAID sizes) and **MiB** for rules 8 (generic
`*_bytes`) and 10b (smaller `mem` fields). `system.uptime` (rule 6, native
seconds) is shown in **days**. The mdstat sync speed (rule 12b)
is shown in **MiB/s**, the SMART power-on time (rule 11b, native hours) in
**days**. The native value stays in the rule's unit (B, KiB,
KiB/s); HA converts it (and the user can pick another unit per entity). HA
applies suggested units only when an entity is first registered.

### §5.6 Enabled by default: a short allowlist

Telegraf can produce thousands of fields, and no list of "noisy" fields can
anticipate every input. So the rule is inverted: only a **short allowlist**
of core sensors starts enabled; **every other metric sensor**, including all
measurements not listed (custom inputs, docker, procstat, …), is discovered
and registered with `entity_registry_enabled_default = False`. The user
enables what they want to track (device page, or in bulk via *Settings →
Entities*, filter by Telegraf, select, *Enable*); HA then reloads the entry
and the entities receive values. Disabled sensors still count toward
`max_entities`.

| Measurement | Enabled by default (field patterns) |
|---|---|
| `cpu` (only `cpu=cpu-total` or no `cpu` tag) | `usage_user`, `usage_system` |
| `mem` | `used_percent`, `available` |
| `swap` | `used_percent` |
| `system` | `load1`, `load5`, `load15`, `kernel_version` |
| `kernel` | `boot_time` |
| `reboot_needed` (§5.4.2) | `value` |
| `smart_device` | `temp_c`, `health_ok`, `percentage_used`, `percent_lifetime_remain` |
| `temp` | `temp` |
| `sensors` | `temp_input`, `temp[0-9]*_input` |
| anything else | nothing |

`smart_device` has one series per drive, which hosts have few of, and
these are the values a drive's own device (§5.1) is about: temperature,
health and wear (NVMe `percentage_used`, SATA `percent_lifetime_remain`).

`disk`, `diskio` and `net` are deliberately **not** in the list: they produce
one series per disk, partition or network interface (containers, bridges,
VPN tunnels, loop devices, …), so even "useful" fields would multiply.

Fields with `entity_category: diagnostic`: `system.kernel_version` (text,
label `Kernel version`), `smart_device.exit_status` (smartctl's exit code
bitmask) and `smart_device.available_spare_threshold` (fixed by the
vendor).

The per-host Connectivity entity (§5.7) is always enabled. The decision only applies
when an entity is first registered; HA keeps the user's later choice.

### §5.7 Per-host entities

Every host with at least one known sensor gets one extra entity on its
device, `entity_category: diagnostic`:

| Entity | Type | unique_id kind |
|---|---|---|
| **Connectivity** (`binary_sensor.<host>_connectivity`) | binary sensor, device class `connectivity` | `connectivity` |

- unique_id: `f"{entry_id}_{sha1(k.encode('utf-8')).hexdigest()[:16]}"` with
  `k = json.dumps(["host", host, kind], ensure_ascii=False)` — default
  separators, i.e. `["host", "nas", "connectivity"]`, unlike the compact
  sensor key (cannot collide with metric keys, which are 4-element arrays).
- **Sign of life**: *any* metric from a known host updates the host's
  last-seen time (internal, not an entity) and sets Connectivity on —
  including metrics dropped by filters, the cap or a
  type mismatch (§5.3). Hosts with no known sensor (everything filtered)
  get no device and no host entities.
- **Stale**: after `connectivity_timeout` (default 300 s) without any
  data, Connectivity turns **off** (not unavailable, so automations can
  trigger on `off`). `connectivity_timeout = 0` → stays on. It is separate
  from the sensors' longer `stale_timeout` (§6.2): a host sends something
  every collection interval of its fastest input, so it can be declared
  gone long before a slow input's sensors are.
- **Log**: Connectivity turning off logs once at INFO (`Telegraf host
  'nas' of <entry title> stopped sending data`). The next data from a host
  whose Connectivity is off (also when restored off at start) logs
  `... is sending data again`. Nothing is logged when a host is first
  seen, or when its state is restored at start.
- **Restart**: Connectivity (`RestoreEntity`) saves the host's last-seen
  time as extra restore data (`{"last_seen": <ISO 8601>}`) and, at start,
  derives its state from the restored time immediately (on if within
  `connectivity_timeout`, else off). Without anything to restore (new install,
  or Connectivity disabled by the user), Connectivity is unavailable
  until data arrives; restore data without a valid, timezone-aware ISO 8601
  `last_seen` counts as nothing to restore. Metric sensors still follow §6.2.
- Removed together with the host device (§6.4).

## §6 Hub behavior

### §6.1 Discovery

For each parsed metric field:

0. **Host info field** (§5.1) → update the host's device info; no sensor.
1. Compute key (§5.1).
2. **Known key** → store value + `last_seen = now`, fire per-key update
   signal → entity writes state.
3. **New key**:
   - Skip if measurement fails the include/exclude filters (§7.2).
   - Skip if it would push the **entity count** over `max_entities`. The
     entity count is the number of known sensors plus 1 per known host
     (Connectivity, §5.7), so a sensor on a new host costs 2.
     This also bounds the number of hosts/devices. Log a warning
     **once per setup** naming the cap (the flag resets on reload and when a
     host or drive device is removed). Together with the warning, a non-fixable,
     non-persistent repair issue `entity_cap_<entry_id>` (severity warning,
     translation key `entity_cap_reached`, placeholders entry `title` and
     `max_entities`) is created so the user sees it under Settings →
     Repairs. The issue lives exactly as long as the flag: it is deleted
     when a host or drive device is removed (the flag resets) and when the
     hub shuts down (entry unload, so also on reload, options change,
     disable and removal); `async_remove_entry` deletes it as well. A
     reload therefore clears it, and it is created again only if the cap
     is hit again. An unloaded or disabled entry drops nothing, so it has
     no issue; being non-persistent, it does not survive an HA restart
     either. Sensors already stored are never dropped by
     the cap, even if `max_entities` is lowered below the stored count;
     the cap only blocks new discoveries.
   - Otherwise record metadata, schedule a debounced persist (§6.3), fire
     the "new sensors" signal; the sensor platform adds the entity with the
     initial value.

### §6.2 Availability

- An entity is available iff it has received a value since HA start **and**
  `now - last_seen <= stale_timeout`.
- Checked every 30 s by `async_track_time_interval`; entities whose
  availability changed write state.
- `stale_timeout` defaults to 1800 s, so inputs collected every few minutes
  (e.g. `inputs.smart` at 5–10 min) never flicker to unavailable between
  values; it should exceed the longest collection interval. A host that
  stopped reporting is flagged sooner by Connectivity (§5.7).
- `stale_timeout = 0` disables staleness (available forever after the first
  value since start).
- After an HA restart, entities are recreated from storage and are
  **unavailable until their first new value arrives** (no state restore —
  Telegraf pushes every few seconds, and a restored value could be
  arbitrarily old).

### §6.3 Persistence

- `helpers.storage.Store`, key `telegraf.<entry_id>`, version 1.
- Contents: `{"sensors": [{"host", "measurement", "tags": [[k, v], ...],
  "field", "numeric"}, ...]}`. The key (§5.1) is derived from these on load,
  not stored. Records are validated with the parser's rules (§3.3: names,
  tag pairs, series size; `numeric` must be a boolean); invalid ones are
  skipped with a warning, and tags are normalized to sorted order. A store
  that is not an object with a `sensors` list is treated as empty (warning).
- Saved with `async_delay_save` (debounced, ~10 s) on discovery, host
  removal and filter-driven drops; flushed immediately when the entry
  unloads, after which the hub writes nothing.
- **Unload order**: the hub pauses (requests get 503, §4.1), then the
  platforms unload. Only if that succeeds are the webhook unregistered,
  the staleness timer stopped and the store flushed (entry `on_unload`
  callbacks, which also run when setup fails part-way). If the platforms
  fail to unload, the hub resumes and the webhook keeps working.
- Loaded at entry setup before the sensor platform is forwarded.
- Device removal (§6.4) goes through the hub whenever one holds the sensor
  list (loaded, unloading, failed unload), because the hub's saves would
  overwrite a direct store edit. With no hub (entry disabled, failed or
  unloaded) it edits the store directly. While setup or unload is in
  progress and no hub holds the list, the removal is refused.
- Removed in `async_remove_entry`.

### §6.4 Device removal

`async_remove_config_entry_device` allows deleting a host device from the
UI; its sensor keys are dropped from the hub and storage, and its drive
devices (§5.1) are removed with their entities. Deleting a drive device
drops only that drive's sensor keys (same host and serial); if the host
has no sensors left, its device (with Connectivity) is removed as well. If the host or
drive sends data again, its sensors are rediscovered. When the store is
edited directly (§6.3), its records are validated as on load; invalid
records belong to no device and are left for the next load to skip.

## §7 Configuration

### §7.1 Config flow (`user` step)

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `Telegraf` | Becomes the entry title and webhook name; surrounding whitespace is stripped, and an empty name re-shows the form with an error |

On submit: generate webhook ID, create the entry with
`data = {"webhook_id": ...}`, and show the webhook URL plus a ready-to-paste
Telegraf snippet (§8) in the success dialog. The dialog also warns
`inputs.sensors` users to settle on `remove_numbers` before sending data:
changing it later renames fields, which creates new sensors (§5.1, §5.4).

URL: `webhook.async_generate_url(hass, webhook_id, allow_external=False,
allow_ip=True)` — the internal URL, else HA's LAN IP; never the external or
cloud URL, since the webhook is local-only. If no URL can be determined
(`NoURLAvailableError`), show
`http://<home-assistant-ip>:8123/api/webhook/<webhook_id>`.

Multiple entries are allowed; no uniqueness constraint.

### §7.2 Options flow

| Option | Type | Default | Meaning |
|---|---|---|---|
| `include_measurements` | list of glob | `[]` (= all) | Only measurements matching any glob are discovered |
| `exclude_measurements` | list of glob | `[]` | Matching measurements are never discovered (wins over include) |
| `stale_timeout` | int seconds, 0–86400 | `1800` | §6.2: sensors |
| `connectivity_timeout` | int seconds, 0–86400 | `300` | §5.7: host Connectivity |
| `max_entities` | int, 2–10000 | `500` | §6.1 cap on sensors + per-host entities |
| `local_only` | bool | `true` | §4: HA core's local-network IP check; off = no IP check (Nabu Casa still blocked) |

- Globs use `fnmatch.fnmatchcase` against the measurement name.
- The options dialog also shows the webhook URL again.
- Saving options reloads the entry. At setup, stored sensors that no longer
  pass the filters are dropped from storage and their entities removed from
  the entity registry, along with host and drive devices (§5.1) that have
  no sensors left.

### §7.3 Diagnostics

Config entry diagnostics:

- `entry`: `data` and `options` (webhook ID redacted).
- `hub` (`null` for an entry that is not loaded, including after a failed setup): `sensor_count`,
  `entity_count` (counted toward the cap, §6.1), `available_count`,
  `hosts`, `connected_hosts`, `measurements` (sensors per measurement),
  `cap_reached`, `max_entities`, `stale_timeout`, `connectivity_timeout`.

## §8 Telegraf configuration

```toml
[[outputs.http]]
  ## Must be reachable from HA's local network (the webhook is local_only)
  url = "http://<ha-lan-address>:8123/api/webhook/<webhook_id>"
  method = "POST"
  data_format = "json"
  content_encoding = "gzip"
  ## 400/413 will never succeed; retry only 503 (Telegraf 1.29+)
  non_retryable_statuscodes = [400, 413]
  [outputs.http.headers]
    Content-Type = "application/json"
```

`non_retryable_statuscodes` needs Telegraf 1.29 or newer; older versions
reject the unknown option, so remove that line there (§4.1).

For a host boot-time sensor add `[[inputs.kernel]]` (Linux) on the
Telegraf side (§5.4.1).

For a *Reboot required* binary sensor (§5.4.2), check the marker that
openSUSE's `transactional-update`/`zypper` (and Debian/Ubuntu's
`/var/run/reboot-required`) leave behind. The test is cheap, so a short
interval shows the change soon after an update:

```toml
[[inputs.exec]]
  commands = ["sh -c 'test -e /run/reboot-needed && echo 1 || echo 0'"]
  data_format = "value"
  data_type = "integer"
  name_override = "reboot_needed"
  interval = "1m"
```

For a *Visit* link on the host device (§5.1), tag the system input:

```toml
[[inputs.system]]
  [inputs.system.tags]
    ha_configuration_url = "https://myhost:9090"
```

Recommended: limit what is sent with Telegraf's own `namepass` / `fieldpass`
/ `tagexclude` on the output, so HA only sees metrics you want as entities.

## §9 Troubleshooting notes (for README)

- Nothing appears and HA logs `Received remote request for local webhook`:
  Telegraf's source IP isn't considered local (proxy, VPN, Docker network,
  global IPv6) — see §4 reverse-proxy note. To see the address HA sees,
  send a GET (`curl -i <url>`): HA answers 405 and logs the source IP.
  Global IPv6 is common: Go (Telegraf) prefers a global AAAA over IPv4 and
  ULA, so a hostname with a global AAAA makes Telegraf send from its global
  address. Fix in DNS or turn the `local_only` option off (§4).
- Entity count stops growing: `max_entities` reached (warning in log).
- Sensors unavailable: no data for `stale_timeout` seconds.
