"""Shared entity base classes for Telegraf."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TelegrafConfigEntry
from .const import (
    MANUFACTURER,
    signal_host_update,
    signal_new_hosts,
    signal_new_sensors,
    signal_sensor_update,
)
from .hub import HostInfo, SensorInfo, TelegrafHub
from .identity import (
    device_identifier,
    drive_device_identifier,
    drive_serial,
    host_unique_id,
    unique_id_for,
)
from .naming import drive_name, entity_name
from .units import enabled_by_default, host_device_details, is_diagnostic


def host_device_info(hub: TelegrafHub, host: str) -> DeviceInfo:
    """Return the device of a Telegraf host (spec §5.1).

    Host info fields not received since setup are left out, so the registry
    keeps the values it has.
    """
    info = hub.hosts.get(host)
    return DeviceInfo(
        identifiers={device_identifier(hub.entry_id, host)},
        name=host,
        manufacturer=MANUFACTURER,
        **host_device_details(info.details if info else {}),  # type: ignore[typeddict-item]
    )


def drive_device_info(
    entry_id: str,
    host: str,
    serial: str,
    tags: tuple[tuple[str, str], ...],
    host_device_id: str,
) -> DeviceInfo:
    """Return the device of a SMART drive, linked to its host (spec §5.1)."""
    return DeviceInfo(
        identifiers={drive_device_identifier(entry_id, host, serial)},
        name=drive_name(tags),
        model=dict(tags).get("model"),
        serial_number=serial,
        via_device_id=host_device_id,
    )


class TelegrafEntity(Entity):
    """An entity on a host's device that writes its state on a hub signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hub: TelegrafHub,
        host: str,
        unique_id: str,
        signal: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize with the hub, the device (default: host's) and signal."""
        self._hub = hub
        self._signal = signal
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info or host_device_info(hub, host)

    async def async_added_to_hass(self) -> None:
        """Write the state whenever the hub signals a change."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._signal, self.async_write_ha_state)
        )


class TelegrafHostEntity(TelegrafEntity):
    """A per-host diagnostic entity (spec §5.7), named by its kind."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: TelegrafHub, host: HostInfo, kind: str) -> None:
        """Initialize for one host; kind is part of the unique_id."""
        super().__init__(
            hub,
            host.host,
            host_unique_id(hub.entry_id, host.host, kind),
            signal_host_update(hub.entry_id, host.host),
        )
        self._host = host
        self._attr_translation_key = kind


@callback
def async_setup_host_entities(
    hass: HomeAssistant,
    entry: TelegrafConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[TelegrafHub, HostInfo], Entity],
) -> None:
    """Add a host entity per known host, now and as hosts are discovered."""
    hub = entry.runtime_data

    @callback
    def _add(hosts: list[HostInfo]) -> None:
        async_add_entities(factory(hub, host) for host in hosts)

    _add(list(hub.hosts.values()))
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_hosts(entry.entry_id), _add)
    )


@callback
def metric_device_info(
    hass: HomeAssistant, hub: TelegrafHub, info: SensorInfo
) -> DeviceInfo | None:
    """Return a drive's device (spec §5.1); None for the host's device."""
    if (serial := drive_serial(info.measurement, info.tags)) is None:
        return None
    # via_device_id needs the host device's registry id, so make sure it
    # exists before the drive's entities are added.
    host_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=hub.entry_id, **host_device_info(hub, info.host)
    )
    return drive_device_info(hub.entry_id, info.host, serial, info.tags, host_device.id)


class TelegrafMetricEntity(TelegrafEntity):
    """An entity for one Telegraf metric field (spec §5)."""

    def __init__(
        self, hub: TelegrafHub, info: SensorInfo, device_info: DeviceInfo | None
    ) -> None:
        """Initialize from the hub's sensor info, on the given device."""
        super().__init__(
            hub,
            info.host,
            unique_id_for(hub.entry_id, info.key),
            signal_sensor_update(hub.entry_id, info.key),
            device_info,
        )
        self._info = info
        self._attr_name = entity_name(info.measurement, info.tags, info.field)
        self._attr_entity_registry_enabled_default = enabled_by_default(
            info.measurement, info.tags, info.field
        )
        if is_diagnostic(info.measurement, info.field):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Return True while the metric is fresh (spec §6.2)."""
        return self._info.available


@callback
def async_setup_metric_entities(
    hass: HomeAssistant,
    entry: TelegrafConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    wants: Callable[[SensorInfo], bool],
    factory: Callable[[TelegrafHub, SensorInfo, DeviceInfo | None], Entity],
) -> None:
    """Add the platform's metric entities, now and as sensors are discovered.

    `wants` picks the metric fields of this platform (spec §5.4.2).
    """
    hub = entry.runtime_data

    @callback
    def _add(infos: list[SensorInfo]) -> None:
        async_add_entities(
            factory(hub, info, metric_device_info(hass, hub, info))
            for info in infos
            if wants(info)
        )

    _add(list(hub.sensors.values()))
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_sensors(entry.entry_id), _add)
    )
