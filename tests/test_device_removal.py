"""Tests for removing host and drive devices (spec §6.4)."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import (
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.telegraf import async_remove_config_entry_device
from custom_components.telegraf.const import (
    DOMAIN,
)
from custom_components.telegraf.hub import (
    SensorInfo,
    TelegrafHub,
    async_remove_host_from_store,
)
from custom_components.telegraf.identity import (
    drive_device_identifier,
)

from .common import (
    DRIVE_TEMP,
    NVME,
    add_entry,
    drive_device,
    host_device,
    metric,
    post,
    setup_entry,
    storage,
    store_key,
    stored_sensor,
)


async def test_remove_device_with_corrupt_store(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """§6.4: removal while not loaded tolerates a corrupt store."""
    entry = add_entry(hass)
    hass_storage[store_key(entry.entry_id)] = {
        **storage(entry.entry_id, []),
        "data": {"sensors": ["junk", stored_sensor(), stored_sensor(host="pi")]},
    }
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_nas")},
    )
    assert await async_remove_config_entry_device(hass, entry, device)
    await hass.async_block_till_done()
    assert hass_storage[store_key(entry.entry_id)]["data"]["sensors"] == [
        "junk",
        stored_sensor(host="pi"),
    ]


async def test_remove_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4: deleting a host device forgets it; new data rediscovers it."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min") is None
    assert entry.runtime_data.sensors == {}

    await post(client, metric("system", {"load1": 2.0}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "2.0"


async def test_remove_device_while_entry_disabled(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4: removing a host while the entry is not loaded sticks after setup."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("system", {"load1": 1.0}),
                metric("system", {"load1": 1.0}, host="pi"),
            ]
        },
    )
    await hass.async_block_till_done()
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )

    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    assert (await ws.receive_json())["success"]

    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert {info.host for info in entry.runtime_data.sensors.values()} == {"pi"}
    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
        )
        is None
    )


async def test_remove_device_after_failed_setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    hass_storage: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.3/§6.4: a failed setup's closed hub is bypassed; the store is edited."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    body = {
        "metrics": [
            metric("system", {"load1": 1.0}),
            metric("system", {"load1": 1.0}, host="pi"),
        ]
    }
    assert await post(client, body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    nas = (DOMAIN, f"{entry.entry_id}_nas")
    device = device_registry.async_get_device_by_identifier(nas, entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)

    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("boom"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.runtime_data.closed

    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    stored = hass_storage[store_key(entry.entry_id)]["data"]["sensors"]
    assert stored
    assert {record["host"] for record in stored} == {"pi"}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert {info.host for info in entry.runtime_data.sensors.values()} == {"pi"}
    assert device_registry.async_get_device_by_identifier(nas, entry.entry_id) is None


async def test_remove_device_during_reload_sticks(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4: a removal while the platforms unload is not undone by the flush."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    body = {
        "metrics": [
            metric("system", {"load1": 1.0}),
            metric("system", {"load1": 1.0}, host="pi"),
        ]
    }
    assert await post(client, body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
    unload_platforms = hass.config_entries.async_unload_platforms
    results = []

    async def _unload_platforms(*args):
        assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
        results.append(await async_remove_config_entry_device(hass, entry, device))
        return await unload_platforms(*args)

    with patch.object(hass.config_entries, "async_unload_platforms", _unload_platforms):
        assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert results == [True]
    assert {info.host for info in entry.runtime_data.sensors.values()} == {"pi"}


async def test_remove_device_refused_while_setting_up(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4: before the hub holds the sensor list, removal is refused."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
    assert await hass.config_entries.async_unload(entry.entry_id)

    loading, release = asyncio.Event(), asyncio.Event()
    load = TelegrafHub.async_load

    async def _slow_load(self):
        loading.set()
        await release.wait()
        return await load(self)

    with patch.object(TelegrafHub, "async_load", _slow_load):
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        await loading.wait()
        assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
        assert not await async_remove_config_entry_device(hass, entry, device)
        release.set()
        assert await setup
    assert {info.host for info in entry.runtime_data.sensors.values()} == {"nas"}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_remove_drive_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4: deleting a drive forgets only its sensors; data brings it back."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    batch = {
        "metrics": [
            metric("system", {"load1": 1.0}),
            metric("smart_device", {"temp_c": 34}, **NVME),
        ]
    }
    await post(client, batch)
    await hass.async_block_till_done()

    drive = drive_device(device_registry, entry, "S5GX593Y")
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": drive.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get(DRIVE_TEMP) is None
    assert [i.field for i in entry.runtime_data.sensors.values()] == ["load1"]
    assert host_device(device_registry, entry) is not None

    await post(client, batch)
    await hass.async_block_till_done()
    assert hass.states.get(DRIVE_TEMP).state == "34"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_remove_drive_device_while_entry_disabled(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§6.4/§6.3: with no hub, a drive's records are removed from the store."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    other = {**NVME, "device": "nvme1", "serial_no": "OTHER1"}
    await post(
        client,
        {
            "metrics": [
                metric("smart_device", {"temp_c": 34}, **NVME),
                metric("smart_device", {"temp_c": 35}, **other),
            ]
        },
    )
    await hass.async_block_till_done()
    drive = drive_device(device_registry, entry, "S5GX593Y")

    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": drive.id}
    )
    assert (await ws.receive_json())["success"]

    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert {dict(i.tags)["serial_no"] for i in entry.runtime_data.sensors.values()} == {
        "OTHER1"
    }


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_remove_host_removes_its_drives(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """§6.4: deleting a host also deletes its drive devices and their entities."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("smart_device", {"temp_c": 34}, **NVME),
                metric("smart_device", {"temp_c": 30}, host="pi", **NVME),
            ]
        },
    )
    await hass.async_block_till_done()

    host = host_device(device_registry, entry)
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": host.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert drive_device(device_registry, entry, "S5GX593Y") is None
    assert entity_registry.async_get(DRIVE_TEMP) is None
    # The same drive model and serial on another host is untouched.
    assert device_registry.async_get_device_by_identifier(
        drive_device_identifier(entry.entry_id, "pi", "S5GX593Y"), entry.entry_id
    )
    assert {i.host for i in entry.runtime_data.sensors.values()} == {"pi"}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_remove_last_drive_removes_empty_host(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """§6.4: a host whose only sensors were one drive's goes with it."""
    assert await async_setup_component(hass, "config", {})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    await post(client, metric("smart_device", {"temp_c": 34}, **NVME))
    await hass.async_block_till_done()

    drive = drive_device(device_registry, entry, "S5GX593Y")
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": drive.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert host_device(device_registry, entry) is None
    assert entity_registry.async_get("binary_sensor.nas_connectivity") is None
    assert entry.runtime_data.hosts == {}


def test_sensor_on_device() -> None:
    """§6.4: a host owns all its sensors; a drive only those with its serial."""
    drive = SensorInfo.from_storage(
        stored_sensor(
            measurement="smart_device",
            tags=[["model", "WD"], ["serial_no", "S1"]],
            field="temp_c",
        )
    )
    load = SensorInfo.from_storage(stored_sensor())
    assert drive.on_device("nas")
    assert drive.on_device("nas", "S1")
    assert not drive.on_device("nas", "S2")
    assert not drive.on_device("pi", "S1")
    assert load.on_device("nas")
    assert not load.on_device("nas", "S1")


async def test_remove_from_store_keeps_invalid_records(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """§6.3/§6.4: store edits read records like a load; invalid ones stay."""
    drive = stored_sensor(
        measurement="smart_device", tags=[["serial_no", "S1"]], field="temp_c"
    )
    malformed = stored_sensor(measurement="smart_device", tags=[[["x"], "y"]])
    load = stored_sensor()
    hass_storage[store_key("E1")] = storage("E1", [drive, malformed, load])

    assert await async_remove_host_from_store(hass, "E1", "nas", "S1")
    assert hass_storage[store_key("E1")]["data"]["sensors"] == [malformed, load]
    assert not await async_remove_host_from_store(hass, "E1", "nas")
    assert hass_storage[store_key("E1")]["data"]["sensors"] == [malformed]
