"""Telegraf hub: known sensors, discovery, persistence and staleness (spec §6)."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONNECTIVITY_TIMEOUT,
    CONF_EXCLUDE_MEASUREMENTS,
    CONF_INCLUDE_MEASUREMENTS,
    CONF_MAX_ENTITIES,
    CONF_STALE_TIMEOUT,
    DOMAIN,
    HOST_ENTITY_COUNT,
    STALE_CHECK_INTERVAL,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    options_with_defaults,
    signal_host_update,
    signal_new_hosts,
    signal_new_sensors,
    signal_sensor_update,
)
from .identity import device_identifier, drive_serial
from .parser import (
    LogValue,
    Metric,
    Value,
    make_key,
    series_within_limits,
    valid_name,
)
from .units import host_device_details, is_host_info

_LOGGER = logging.getLogger(__name__)


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the persisted sensor list of a config entry."""
    await _store(hass, entry_id).async_remove()


async def async_remove_host_from_store(
    hass: HomeAssistant, entry_id: str, host: str, serial: str | None = None
) -> bool:
    """Forget a host's (or one drive's) sensors while not loaded (spec §6.4).

    Return True if the host has stored sensors left. Records are read like
    the hub loads them (§6.3); invalid ones belong to no device and are
    kept as they are, for the next load to skip with a warning.
    """
    store = _store(hass, entry_id)
    data = await store.async_load()
    if not isinstance(data, dict) or not isinstance(data.get("sensors"), list):
        return False
    kept: list[Any] = []
    host_left = False
    for raw in data["sensors"]:
        try:
            info = SensorInfo.from_storage(raw)
        except ValueError:
            kept.append(raw)
            continue
        if info.on_device(host, serial):
            continue
        kept.append(raw)
        host_left = host_left or info.on_device(host)
    await store.async_save({**data, "sensors": kept})
    return host_left


def cap_issue_id(entry_id: str) -> str:
    """Return the repair issue id for an entry's entity cap (spec §6.1)."""
    return f"entity_cap_{entry_id}"


@callback
def async_delete_cap_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the entity cap repair issue of an entry, if any (spec §6.1)."""
    ir.async_delete_issue(hass, DOMAIN, cap_issue_id(entry_id))


def loaded_hub(entry: ConfigEntry) -> TelegrafHub | None:
    """Return the entry's hub while it holds the sensor list, else None.

    None when the entry is not loaded, and for a closed hub: HA leaves
    runtime_data behind after a setup that failed part-way (whose on_unload
    already flushed and closed the hub), and it is still set while an
    unload's on_unload callbacks run.
    """
    hub: TelegrafHub | None = getattr(entry, "runtime_data", None)
    if hub is None or hub.closed:
        return None
    return hub


@dataclass(slots=True)
class SensorInfo:
    """A discovered sensor and its live state."""

    key: str
    host: str
    measurement: str
    tags: tuple[tuple[str, str], ...]
    field: str
    numeric: bool
    value: Value | None = None
    last_seen: datetime | None = None
    available: bool = False

    def on_device(self, host: str, serial: str | None = None) -> bool:
        """Return True if on the host, or with a serial on that drive (§6.4).

        Without a serial this includes the host's drives: they go with it.
        """
        return self.host == host and (
            serial is None or drive_serial(self.measurement, self.tags) == serial
        )

    def as_storage(self) -> dict[str, Any]:
        """Return the persisted representation (no live state)."""
        return {
            "host": self.host,
            "measurement": self.measurement,
            "tags": [list(tag) for tag in self.tags],
            "field": self.field,
            "numeric": self.numeric,
        }

    @classmethod
    def from_storage(cls, data: Any) -> SensorInfo:
        """Recreate from the persisted representation (spec §6.3).

        Applies the parser's rules (§3.3) so a corrupted or hand-edited store
        cannot inject what a payload could not; raises ValueError if invalid.
        """
        if not isinstance(data, dict):
            raise ValueError("record is not an object")
        host, measurement, field = (
            data.get("host"),
            data.get("measurement"),
            data.get("field"),
        )
        if not (valid_name(host) and valid_name(measurement) and valid_name(field)):
            raise ValueError("invalid host, measurement or field")
        if not isinstance(numeric := data.get("numeric"), bool):
            raise ValueError("numeric is not a bool")
        raw_tags = data.get("tags")
        if not isinstance(raw_tags, list) or not all(
            isinstance(tag, list)
            and len(tag) == 2
            and valid_name(tag[0])
            and valid_name(tag[1])
            for tag in raw_tags
        ):
            raise ValueError("invalid tags")
        tags = tuple(sorted((key, value) for key, value in raw_tags))
        if len({key for key, _ in tags}) != len(tags):
            raise ValueError("duplicate tag key")
        if not series_within_limits(host, measurement, tags):
            raise ValueError("series exceeds the tag limits")
        return cls(
            key=make_key(host, measurement, tags, field),
            host=host,
            measurement=measurement,
            tags=tags,
            field=field,
            numeric=numeric,
        )


@dataclass(slots=True)
class HostInfo:
    """Liveness of one Telegraf host (spec §5.7)."""

    host: str
    last_seen: datetime | None = None
    # None = unknown: no data (and nothing restored) since setup.
    connected: bool | None = None
    # Latest host info fields (spec §5.1), e.g. {"platform": "debian"}.
    details: dict[str, str] = field(default_factory=dict)


def _is_stale(last_seen: datetime, now: datetime, timeout: int) -> bool:
    """Return True if nothing arrived for longer than timeout (0 = never)."""
    return timeout > 0 and (now - last_seen).total_seconds() > timeout


class TelegrafHub:
    """Holds the sensors of one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the hub from the entry options."""
        self.hass = hass
        self.entry_id = entry.entry_id
        self.title = entry.title
        options = options_with_defaults(entry.options)
        self._include: list[str] = list(options[CONF_INCLUDE_MEASUREMENTS])
        self._exclude: list[str] = list(options[CONF_EXCLUDE_MEASUREMENTS])
        self.stale_timeout = int(options[CONF_STALE_TIMEOUT])
        self.connectivity_timeout = int(options[CONF_CONNECTIVITY_TIMEOUT])
        self.max_entities = int(options[CONF_MAX_ENTITIES])
        self.sensors: dict[str, SensorInfo] = {}
        self.hosts: dict[str, HostInfo] = {}
        self.cap_reached = False
        self._store = _store(hass, entry.entry_id)
        self._unsub_interval: CALLBACK_TYPE | None = None
        # Accepting data: False until started, while unloading, and after
        # shutdown (the webhook then answers 503, spec §4.1).
        self.running = False
        # Shut down: the final flush happened; nothing is persisted anymore.
        self.closed = False

    def measurement_allowed(self, measurement: str) -> bool:
        """Apply the include/exclude globs (spec §7.2)."""
        if any(fnmatchcase(measurement, pattern) for pattern in self._exclude):
            return False
        return not self._include or any(
            fnmatchcase(measurement, pattern) for pattern in self._include
        )

    async def async_load(self) -> list[SensorInfo]:
        """Load persisted sensors; return those to remove from the registries.

        That is sensors dropped by the current filters (spec §7.2), so they
        do not linger as orphans. Invalid records are skipped (§6.3).
        """
        data = await self._store.async_load()
        records = data.get("sensors") if isinstance(data, dict) else None
        if data is not None and not isinstance(records, list):
            _LOGGER.warning(
                "Ignoring invalid stored sensor list for %s: %s",
                self.title,
                LogValue(data),
            )
        dropped: list[SensorInfo] = []
        for raw in records if isinstance(records, list) else []:
            try:
                info = SensorInfo.from_storage(raw)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring invalid stored sensor for %s: %s",
                    self.title,
                    LogValue(raw),
                )
                continue
            if self.measurement_allowed(info.measurement):
                self.sensors[info.key] = info
            else:
                dropped.append(info)
        self.hosts = {info.host: HostInfo(info.host) for info in self.sensors.values()}
        if dropped:
            self._async_schedule_save()
        return dropped

    @callback
    def async_start(self) -> None:
        """Start the periodic staleness check and accept data."""
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                self.hass, self._async_check_stale, STALE_CHECK_INTERVAL
            )
        self.running = True

    @callback
    def async_pause(self) -> None:
        """Stop accepting data while the platforms unload (spec §6.3)."""
        self.running = False

    @callback
    def async_resume(self) -> None:
        """Accept data again after a failed unload."""
        if not self.closed:
            self.running = True

    async def async_shutdown(self) -> None:
        """Stop timers and flush the persisted sensor list (entry on_unload)."""
        self.running = False
        # The cap issue describes this hub; it goes with it (spec §6.1).
        self._async_reset_cap()
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        if not self.closed:
            self.closed = True
            await self._store.async_save(self._data_to_save())

    @callback
    def async_process(self, metrics: Iterable[Metric]) -> None:
        """Apply a batch of metrics (spec §6.1)."""
        if not self.running:
            return
        now = dt_util.utcnow()
        new: list[SensorInfo] = []
        # By key: a batch may repeat a series many times (e.g. Telegraf
        # flushing its buffer); each entity writes its state once.
        updated: dict[str, SensorInfo] = {}
        new_hosts: list[HostInfo] = []
        seen_hosts: set[str] = set()
        details: dict[str, dict[str, str]] = {}
        for metric in metrics:
            seen_hosts.add(metric.host)
            if is_host_info(metric.measurement, metric.field):
                # Shown on the device, not as a sensor (spec §5.1).
                if isinstance(metric.value, str) and self.measurement_allowed(
                    metric.measurement
                ):
                    details.setdefault(metric.host, {})[metric.field] = metric.value
                continue
            key = metric.key
            info = self.sensors.get(key)
            if info is None:
                info = self._async_discover(metric)
                if info is None:
                    continue
                new.append(info)
                if info.host not in self.hosts:
                    self.hosts[info.host] = HostInfo(info.host)
                    new_hosts.append(self.hosts[info.host])
            elif info.numeric != metric.numeric:
                _LOGGER.debug(
                    "Ignoring %s value %s for %s sensor %s",
                    "numeric" if metric.numeric else "text",
                    LogValue(metric.value),
                    "numeric" if info.numeric else "text",
                    LogValue(key),
                )
                continue
            else:
                updated[key] = info
            info.value = metric.value
            info.last_seen = now
            info.available = True

        # Any metric from a known host counts as a sign of life (spec §5.7),
        # even if it was filtered, capped or of the wrong type.
        updated_hosts = [self.hosts[h] for h in seen_hosts if h in self.hosts]
        for host in updated_hosts:
            host.last_seen = now
            if host.connected is False:
                _LOGGER.info(
                    "Telegraf host %s of %s is sending data again",
                    LogValue(host.host),
                    self.title,
                )
            host.connected = True
            if (values := details.get(host.host)) and not (
                values.items() <= host.details.items()
            ):
                host.details.update(values)
                self._async_update_host_device(host)

        self._async_dispatch(new_hosts, new, updated, updated_hosts)

    @callback
    def _async_update_host_device(self, host: HostInfo) -> None:
        """Write changed host info to an existing host device (spec §5.1).

        A device that does not exist yet gets the info when its first entity
        is added (entity.host_device_info). Values the registry refuses
        despite units.host_device_details' checks are skipped, so they can
        never fail the batch (spec §4.1).
        """
        dev_reg = dr.async_get(self.hass)
        if device := dev_reg.async_get_device_by_identifier(
            device_identifier(self.entry_id, host.host), self.entry_id
        ):
            details = host_device_details(host.details)
            try:
                dev_reg.async_update_device(device.id, **details)
            except ValueError as err:
                _LOGGER.debug(
                    "Ignoring host info of %s for %s: %s",
                    LogValue(host.host),
                    self.title,
                    LogValue(str(err)),
                )

    @callback
    def _async_dispatch(
        self,
        new_hosts: list[HostInfo],
        new: list[SensorInfo],
        updated: dict[str, SensorInfo],
        updated_hosts: list[HostInfo],
    ) -> None:
        """Signal the platforms and entities about a processed batch."""
        if new_hosts:
            async_dispatcher_send(self.hass, signal_new_hosts(self.entry_id), new_hosts)
        if new:
            self._async_schedule_save()
            async_dispatcher_send(self.hass, signal_new_sensors(self.entry_id), new)
        for info in new:
            # Added with its latest value; no separate update needed.
            updated.pop(info.key, None)
        for key in updated:
            async_dispatcher_send(self.hass, signal_sensor_update(self.entry_id, key))
        for host in updated_hosts:
            async_dispatcher_send(
                self.hass, signal_host_update(self.entry_id, host.host)
            )

    @callback
    def async_restore_last_seen(self, host: str, last_seen: datetime) -> None:
        """Seed a host's last-seen from Connectivity's restored data (spec §5.7)."""
        info = self.hosts.get(host)
        if info is None or info.last_seen is not None:
            return
        info.last_seen = last_seen
        info.connected = not _is_stale(
            last_seen, dt_util.utcnow(), self.connectivity_timeout
        )
        async_dispatcher_send(self.hass, signal_host_update(self.entry_id, host))

    @property
    def entity_count(self) -> int:
        """Entities counted toward max_entities: sensors plus per-host (§6.1)."""
        return len(self.sensors) + HOST_ENTITY_COUNT * len(self.hosts)

    @callback
    def _async_discover(self, metric: Metric) -> SensorInfo | None:
        if not self.measurement_allowed(metric.measurement):
            return None
        # A sensor on a new host also brings that host's per-host entities.
        cost = 1 if metric.host in self.hosts else 1 + HOST_ENTITY_COUNT
        if self.entity_count + cost > self.max_entities:
            if not self.cap_reached:
                self.cap_reached = True
                _LOGGER.warning(
                    "Telegraf entry %s reached the limit of %d entities; new "
                    "metrics are ignored. Raise 'max_entities' in the options or "
                    "filter metrics in Telegraf",
                    self.title,
                    self.max_entities,
                )
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    cap_issue_id(self.entry_id),
                    is_fixable=False,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="entity_cap_reached",
                    translation_placeholders={
                        "title": self.title,
                        "max_entities": str(self.max_entities),
                    },
                )
            return None
        info = SensorInfo(
            key=metric.key,
            host=metric.host,
            measurement=metric.measurement,
            tags=metric.tags,
            field=metric.field,
            numeric=metric.numeric,
        )
        self.sensors[info.key] = info
        return info

    @callback
    def _async_check_stale(self, now: datetime) -> None:
        """Mark sensors unavailable and hosts disconnected (spec §6.2, §5.7)."""
        if not self.running:
            return
        for info in self.sensors.values():
            if (
                info.available
                and info.last_seen is not None
                and _is_stale(info.last_seen, now, self.stale_timeout)
            ):
                info.available = False
                async_dispatcher_send(
                    self.hass, signal_sensor_update(self.entry_id, info.key)
                )
        for host in self.hosts.values():
            if (
                host.connected
                and host.last_seen is not None
                and _is_stale(host.last_seen, now, self.connectivity_timeout)
            ):
                host.connected = False
                _LOGGER.info(
                    "Telegraf host %s of %s stopped sending data",
                    LogValue(host.host),
                    self.title,
                )
                async_dispatcher_send(
                    self.hass, signal_host_update(self.entry_id, host.host)
                )

    @callback
    def async_remove_host(self, host: str) -> None:
        """Forget all sensors of a host (spec §6.4)."""
        for key in [k for k, info in self.sensors.items() if info.on_device(host)]:
            del self.sensors[key]
        self.hosts.pop(host, None)
        self._async_reset_cap()
        self._async_schedule_save()

    @callback
    def async_remove_drive(self, host: str, serial: str) -> bool:
        """Forget the sensors of one drive of a host (spec §6.4).

        Return True if the host has sensors left; if not, it is forgotten too.
        """
        for key in [
            k for k, info in self.sensors.items() if info.on_device(host, serial)
        ]:
            del self.sensors[key]
        self._async_reset_cap()
        self._async_schedule_save()
        if any(info.on_device(host) for info in self.sensors.values()):
            return True
        self.hosts.pop(host, None)
        return False

    @callback
    def _async_reset_cap(self) -> None:
        """Re-arm the cap warning and withdraw its repair issue (spec §6.1)."""
        self.cap_reached = False
        async_delete_cap_issue(self.hass, self.entry_id)

    @callback
    def _async_schedule_save(self) -> None:
        if self.closed:
            return
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {"sensors": [info.as_storage() for info in self.sensors.values()]}

    def diagnostics(self) -> dict[str, Any]:
        """Return a summary for diagnostics (spec §7.3)."""
        return {
            "sensor_count": len(self.sensors),
            "entity_count": self.entity_count,
            "available_count": sum(info.available for info in self.sensors.values()),
            "hosts": sorted(self.hosts),
            "connected_hosts": sorted(
                host for host, info in self.hosts.items() if info.connected
            ),
            "measurements": dict(
                Counter(info.measurement for info in self.sensors.values())
            ),
            "cap_reached": self.cap_reached,
            "max_entities": self.max_entities,
            "stale_timeout": self.stale_timeout,
            "connectivity_timeout": self.connectivity_timeout,
        }
