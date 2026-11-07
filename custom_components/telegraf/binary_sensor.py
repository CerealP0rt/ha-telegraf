"""Binary sensors for Telegraf: metric fields (§5.4.2), Connectivity (§5.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from . import TelegrafConfigEntry
from .const import HOST_KIND_CONNECTIVITY
from .entity import (
    TelegrafHostEntity,
    TelegrafMetricEntity,
    async_setup_host_entities,
    async_setup_metric_entities,
)
from .hub import HostInfo, SensorInfo, TelegrafHub
from .units import binary_is_on, binary_traits

# Push-only entities: no polling or service calls to serialize.
PARALLEL_UPDATES = 0


def _is_binary(info: SensorInfo) -> bool:
    """Metric fields shown as binary sensors (spec §5.4.2)."""
    return binary_traits(info.measurement, info.field, info.numeric) is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelegrafConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add Connectivity per host and binary metric fields, now and later."""
    async_setup_host_entities(hass, entry, async_add_entities, TelegrafHostConnectivity)
    async_setup_metric_entities(
        hass, entry, async_add_entities, _is_binary, TelegrafBinarySensor
    )


class TelegrafBinarySensor(TelegrafMetricEntity, BinarySensorEntity):
    """A numeric metric field with two states, e.g. drive health (§5.4.2)."""

    def __init__(
        self, hub: TelegrafHub, info: SensorInfo, device_info: DeviceInfo | None
    ) -> None:
        """Initialize with the field's binary traits."""
        super().__init__(hub, info, device_info)
        traits = binary_traits(info.measurement, info.field, info.numeric)
        assert traits is not None
        self._traits = traits
        if traits.device_class:
            self._attr_device_class = BinarySensorDeviceClass(traits.device_class)
        # State names and icons only; the name stays the code-built one.
        self._attr_translation_key = traits.translation_key

    @property
    def is_on(self) -> bool | None:
        """Return the state for the latest value."""
        value = self._info.value
        if not isinstance(value, int | float):
            return None
        return binary_is_on(self._traits, value)


@dataclass
class LastSeenData(ExtraStoredData):
    """When the host last sent data, kept across restarts (spec §5.7)."""

    last_seen: datetime

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {"last_seen": self.last_seen.isoformat()}


class TelegrafHostConnectivity(TelegrafHostEntity, BinarySensorEntity, RestoreEntity):
    """On while the host keeps sending data within connectivity_timeout."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, hub: TelegrafHub, host: HostInfo) -> None:
        """Initialize for one host."""
        super().__init__(hub, host, HOST_KIND_CONNECTIVITY)

    @property
    def available(self) -> bool:
        """Unknown until the host is seen (or its last-seen restored)."""
        return self._host.connected is not None

    @property
    def is_on(self) -> bool:
        """Return True while the host is connected."""
        return bool(self._host.connected)

    @property
    def extra_restore_state_data(self) -> LastSeenData | None:
        """Store the host's last-seen time for the next start."""
        if self._host.last_seen is None:
            return None
        return LastSeenData(self._host.last_seen)

    async def async_added_to_hass(self) -> None:
        """Listen for updates and restore the last-seen time across restarts.

        Restore data without a usable, timezone-aware time is ignored: a
        naive one could not be compared with the current time (spec §5.7).
        """
        await super().async_added_to_hass()
        if (
            self._host.last_seen is None
            and (data := await self.async_get_last_extra_data()) is not None
            and isinstance(raw := data.as_dict().get("last_seen"), str)
            and (last_seen := dt_util.parse_datetime(raw)) is not None
            and last_seen.tzinfo is not None
        ):
            self._hub.async_restore_last_seen(self._host.host, last_seen)
