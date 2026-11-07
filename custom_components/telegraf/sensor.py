"""Sensor platform for Telegraf (spec §5)."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TelegrafConfigEntry
from .const import SUGGESTED_DISPLAY_PRECISION
from .entity import TelegrafMetricEntity, async_setup_metric_entities
from .hub import SensorInfo, TelegrafHub
from .parser import Value
from .units import binary_traits, boot_time, icon_key, traits_for

# Push-only entities: no polling or service calls to serialize.
PARALLEL_UPDATES = 0


def _is_sensor(info: SensorInfo) -> bool:
    """Every metric field that is not a binary sensor (spec §5.4.2)."""
    return binary_traits(info.measurement, info.field, info.numeric) is None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelegrafConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add stored sensors now and discovered sensors as they appear."""
    async_setup_metric_entities(
        hass, entry, async_add_entities, _is_sensor, TelegrafSensor
    )


class TelegrafSensor(TelegrafMetricEntity, SensorEntity):
    """A single Telegraf metric field."""

    def __init__(
        self, hub: TelegrafHub, info: SensorInfo, device_info: DeviceInfo | None
    ) -> None:
        """Initialize with the unit and classes of the field (spec §5.4)."""
        super().__init__(hub, info, device_info)
        traits = traits_for(info.measurement, info.field, info.numeric)
        self._attr_native_unit_of_measurement = traits.unit
        if traits.suggested_unit:
            self._attr_suggested_unit_of_measurement = traits.suggested_unit
            self._attr_suggested_display_precision = SUGGESTED_DISPLAY_PRECISION
        self._attr_device_class = (
            SensorDeviceClass(traits.device_class) if traits.device_class else None
        )
        self._attr_state_class = (
            SensorStateClass(traits.state_class) if traits.state_class else None
        )
        # Icon only (icons.json); the name stays the code-built one (§5.4.3).
        self._attr_translation_key = icon_key(
            info.measurement, info.field, traits.device_class
        )
        # Spec §5.4.1: kernel.boot_time (epoch seconds) as a datetime.
        self._is_boot_time = self._attr_device_class is SensorDeviceClass.UPTIME

    @property
    def native_value(self) -> Value | datetime | None:
        """Return the latest value (a datetime for a boot time, §5.4.1)."""
        value = self._info.value
        if self._is_boot_time:
            return boot_time(value) if isinstance(value, int | float) else None
        return value
