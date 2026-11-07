"""Helpers shared by the Telegraf integration tests."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telegraf.const import (
    DEFAULT_CONNECTIVITY_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
    STALE_CHECK_INTERVAL,
    STORAGE_VERSION,
)
from custom_components.telegraf.identity import drive_device_identifier

WEBHOOK_ID = "telegraf_test_webhook"

CPU_USER = "sensor.nas_processor_use_user"

NVME = {"device": "nvme0", "model": "Samsung SSD 980 PRO 1TB", "serial_no": "S5GX593Y"}
DRIVE_TEMP = "sensor.samsung_ssd_980_pro_1tb_593y_temperature"
DRIVE_HEALTH = "binary_sensor.samsung_ssd_980_pro_1tb_593y_health"

SYSTEM_INFO = {
    "os": "linux",
    "platform": "opensuse-microos",
    "platform_family": "",
    "platform_version": "20261024",
    "arch": "x86_64",
    "kernel_version": "7.3.0-1-default",
    "load1": 0.5,
}

# Staleness timing (spec §6.2, §5.7) with the default stale_timeout:
# ALMOST_STALE after the last value is still fresh; another
# PAST_STALE_CHECK later it is stale and a periodic check has run.
ALMOST_STALE = timedelta(seconds=DEFAULT_STALE_TIMEOUT) - timedelta(seconds=10)
# The same for a host's Connectivity (connectivity_timeout).
ALMOST_DISCONNECTED = timedelta(seconds=DEFAULT_CONNECTIVITY_TIMEOUT) - timedelta(
    seconds=10
)
PAST_STALE_CHECK = STALE_CHECK_INTERVAL + timedelta(seconds=10)


def store_key(entry_id: str) -> str:
    """Return the hass_storage key of an entry's sensor store (spec §6.3)."""
    return f"{DOMAIN}.{entry_id}"


def metric(name: str, fields: dict, /, host: str = "nas", **tags: str) -> dict:
    """Return one Telegraf JSON metric (positional-only, so `name` can be a tag)."""
    return {"name": name, "tags": {"host": host, **tags}, "fields": fields}


def stored_sensor(
    host: str = "nas",
    measurement: str = "system",
    tags: list[list[str]] | None = None,
    field: str = "load1",
    numeric: bool = True,
) -> dict[str, Any]:
    """Return one persisted sensor record (spec §6.3)."""
    return {
        "host": host,
        "measurement": measurement,
        "tags": tags or [],
        "field": field,
        "numeric": numeric,
    }


def storage(entry_id: str, sensors: list[Any]) -> dict[str, Any]:
    """Return a hass_storage record holding the given sensor records."""
    return {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": store_key(entry_id),
        "data": {"sensors": sensors},
    }


def add_entry(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    webhook_id: str = WEBHOOK_ID,
) -> MockConfigEntry:
    """Add a config entry (not yet set up)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_WEBHOOK_ID: webhook_id},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up an added entry and wait until its platforms are ready."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def post(
    client,
    body: bytes | dict[str, Any],
    headers: dict[str, str] | None = None,
    webhook_id: str = WEBHOOK_ID,
) -> int:
    """POST a body (bytes or JSON document) to the webhook; return the status."""
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    resp = await client.post(
        f"/api/webhook/{webhook_id}", data=body, headers=headers or {}
    )
    return resp.status


async def add_and_setup_entry(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Add a config entry with the given options and set it up."""
    entry = add_entry(hass, options)
    await setup_entry(hass, entry)
    return entry


def metric_sensors(hass: HomeAssistant) -> list[str]:
    """Return the entity_ids of metric sensors with a state (no host entities)."""
    ent_reg = er.async_get(hass)
    return sorted(
        entity_id
        for entity_id in hass.states.async_entity_ids("sensor")
        if (reg := ent_reg.async_get(entity_id)) is not None
        and reg.entity_category is None
    )


def drive_device(
    device_registry: dr.DeviceRegistry, entry: MockConfigEntry, serial: str
) -> dr.DeviceEntry | None:
    """Return host nas's drive device with the given serial (spec §5.1)."""
    return device_registry.async_get_device_by_identifier(
        drive_device_identifier(entry.entry_id, "nas", serial), entry.entry_id
    )


def host_device(
    device_registry: dr.DeviceRegistry, entry: MockConfigEntry
) -> dr.DeviceEntry | None:
    """Return the device of host nas (spec §5.1)."""
    return device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
