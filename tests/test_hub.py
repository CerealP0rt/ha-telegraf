"""Tests for hub updates, filters, entity cap and staleness (§6.1, §6.2, §7.2)."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.telegraf.const import (
    CONF_EXCLUDE_MEASUREMENTS,
    CONF_INCLUDE_MEASUREMENTS,
    CONF_MAX_ENTITIES,
    CONF_STALE_TIMEOUT,
    DOMAIN,
)

from .common import (
    ALMOST_STALE,
    CPU_USER,
    DRIVE_HEALTH,
    NVME,
    PAST_STALE_CHECK,
    SYSTEM_INFO,
    add_entry,
    drive_device,
    host_device,
    metric,
    metric_sensors,
    post,
    setup_entry,
    storage,
    store_key,
    stored_sensor,
)


async def test_update_existing_sensor(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.1: known keys update state without new entities."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("cpu", {"usage_user": 10.0}, cpu="cpu-total"))
    await post(client, metric("cpu", {"usage_user": 20.0}, cpu="cpu-total"))
    await hass.async_block_till_done()
    assert hass.states.get(CPU_USER).state == "20.0"
    assert len(metric_sensors(hass)) == 1


async def test_repeated_series_written_once(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§4.1/§6.1: a series repeated in one batch writes its state once (last value)."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 0.0}))
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    batch = {"metrics": [metric("system", {"load1": v}) for v in (1.0, 2.0, 3.0)]}
    assert await post(client, batch) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    writes = [
        e.data["new_state"].state
        for e in events
        if e.data["entity_id"] == "sensor.nas_load_1_min"
    ]
    assert writes == ["3.0"]


async def test_stale_unavailable(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """§6.2: no data for stale_timeout -> unavailable; new data -> available."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()

    freezer.tick(ALMOST_STALE)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "1.0"

    freezer.tick(PAST_STALE_CHECK)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == STATE_UNAVAILABLE

    await post(client, metric("system", {"load1": 2.0}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "2.0"


async def test_stale_disabled(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """§6.2: stale_timeout = 0 never marks sensors unavailable."""
    entry = add_entry(hass, {CONF_STALE_TIMEOUT: 0})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "1.0"


async def test_include_exclude_filters(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§7.2: excluding a measurement removes existing sensors and devices."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("cpu", {"usage_user": 1.0}, cpu="cpu-total"),
                metric("system", {"load1": 1.0}, host="pi"),
            ]
        },
    )
    await hass.async_block_till_done()
    assert len(metric_sensors(hass)) == 2

    hass.config_entries.async_update_entry(
        entry, options={CONF_EXCLUDE_MEASUREMENTS: ["cpu*"]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(CPU_USER) is None
    assert entity_registry.async_get("sensor.pi_load_1_min") is not None
    hosts = {
        d.name
        for d in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    }
    assert hosts == {"pi"}

    client = await hass_client_no_auth()
    await post(client, metric("cpu", {"usage_user": 1.0}, cpu="cpu-total"))
    await hass.async_block_till_done()
    assert entity_registry.async_get(CPU_USER) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_max_entities_cap(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§6.1: discovery stops at max_entities (host entities count) and warns once."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 4})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    for _ in range(2):
        await post(client, metric("m", {"a": 1, "b": 2, "c": 3, "d": 4}))
    await hass.async_block_till_done()
    # 3 sensors + Connectivity = 4.
    assert len(metric_sensors(hass)) == 3
    assert entry.runtime_data.entity_count == 4
    assert caplog.text.count("reached the limit of 4 entities") == 1
    # Logs name the entry by its title, like the webhook handler does.
    assert f"Telegraf entry {entry.title} reached" in caplog.text


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_max_entities_bounds_hosts(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """§6.1 (review S3): a new host costs 2, so the cap also bounds devices."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 5})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    hosts = [metric("m", {"a": 1}, host=f"h{i}") for i in range(100)]
    await post(client, {"metrics": hosts})
    # A known host still has room for 1 more sensor.
    await post(client, metric("m", {"b": 1, "c": 1, "d": 1}, host="h0"))
    await hass.async_block_till_done()
    assert set(entry.runtime_data.hosts) == {"h0", "h1"}
    assert len(entry.runtime_data.sensors) == 3
    assert entry.runtime_data.entity_count == 5


async def test_max_entities_counts_disabled_sensors(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.6/§6.1: sensors registered disabled still count toward the cap."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 4})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("custom", {"a": 1, "b": 2, "c": 3, "d": 4}))
    await hass.async_block_till_done()
    sensors = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.entity_category is None
    ]
    assert len(sensors) == 3
    assert all(e.disabled_by is er.RegistryEntryDisabler.INTEGRATION for e in sensors)


async def test_max_entities_lowered_keeps_stored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§6.1: lowering the cap never drops stored sensors; it blocks new ones."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 3})
    hass_storage[store_key(entry.entry_id)] = storage(
        entry.entry_id,
        [stored_sensor(field=f) for f in ("load1", "load5", "load15")],
    )
    await setup_entry(hass, entry)
    assert len(entry.runtime_data.sensors) == 3
    client = await hass_client_no_auth()
    await post(client, metric("system", {"n_cpus": 4}))
    await hass.async_block_till_done()
    assert len(entry.runtime_data.sensors) == 3
    assert "reached the limit of 3 entities" in caplog.text


async def test_max_entities_warning_rearmed(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§6.1: the cap warning repeats after a reload and after device removal."""
    assert await async_setup_component(hass, "config", {})
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 2})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    message = "reached the limit of 2 entities"

    async def _post_two() -> None:
        await post(client, metric("system", {"load1": 1.0, "load5": 1.0}))
        await hass.async_block_till_done()

    await _post_two()
    await _post_two()
    assert caplog.text.count(message) == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    await _post_two()
    assert caplog.text.count(message) == 2

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    await _post_two()
    assert caplog.text.count(message) == 3


def _cap_issue(
    issue_registry: ir.IssueRegistry, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    """Return the entry's entity cap repair issue (spec §6.1), if any."""
    return issue_registry.async_get_issue(DOMAIN, f"entity_cap_{entry.entry_id}")


async def _hit_cap(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> MockConfigEntry:
    """Set up an entry with a cap of 2 and send it 3 entities' worth."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 2})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0, "load5": 1.0}))
    await hass.async_block_till_done()
    return entry


async def test_cap_repair_issue_created(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1: hitting the cap raises a non-persistent warning repair issue."""
    entry = await _hit_cap(hass, hass_client_no_auth)
    issue = _cap_issue(issue_registry, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert not issue.is_persistent
    assert issue.translation_key == "entity_cap_reached"
    assert issue.translation_placeholders == {
        "title": entry.title,
        "max_entities": "2",
    }


async def test_no_cap_repair_issue_below_cap(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1: reaching the cap exactly (nothing dropped) raises no issue."""
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 2})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()
    assert entry.runtime_data.entity_count == 2
    assert _cap_issue(issue_registry, entry) is None


async def test_cap_repair_issue_cleared_by_raising_limit(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1: raising the limit in the options reloads and clears the issue."""
    entry = await _hit_cap(hass, hass_client_no_auth)
    assert _cap_issue(issue_registry, entry) is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_ENTITIES: 100}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.max_entities == 100
    assert _cap_issue(issue_registry, entry) is None


async def test_cap_repair_issue_recreated_after_reload(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1: a reload clears the issue; hitting the cap again recreates it."""
    entry = await _hit_cap(hass, hass_client_no_auth)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _cap_issue(issue_registry, entry) is None

    client = await hass_client_no_auth()
    await post(client, metric("system", {"load15": 1.0}))
    await hass.async_block_till_done()
    assert _cap_issue(issue_registry, entry) is not None


async def test_cap_repair_issue_deleted_on_unload_and_removal(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1: an unloaded entry has no issue, and none leaks after removal."""
    entry = await _hit_cap(hass, hass_client_no_auth)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _cap_issue(issue_registry, entry) is None

    entry = await _hit_cap(hass, hass_client_no_auth)
    assert _cap_issue(issue_registry, entry) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _cap_issue(issue_registry, entry) is None
    assert not [i for i in issue_registry.issues.values() if i.domain == DOMAIN]


async def test_cap_repair_issue_deleted_on_device_removal(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """§6.1, §6.4: removing a device resets the cap and deletes the issue."""
    assert await async_setup_component(hass, "config", {})
    entry = await _hit_cap(hass, hass_client_no_auth)
    assert _cap_issue(issue_registry, entry) is not None

    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {
            "type": "config/device_registry/remove",
            "device_id": host_device(device_registry, entry).id,
        }
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert not entry.runtime_data.cap_reached
    assert _cap_issue(issue_registry, entry) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_include_filter(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """§7.2: include globs (case-sensitive) with exclude taking precedence."""
    entry = add_entry(
        hass,
        {
            CONF_INCLUDE_MEASUREMENTS: ["cpu*", "disk"],
            CONF_EXCLUDE_MEASUREMENTS: ["cpu_extra"],
        },
    )
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("cpu", {"usage_user": 1.0}, cpu="cpu-total"),
                metric("cpu_extra", {"x": 1}),
                metric("disk", {"used": 1}),
                metric("Disk", {"used": 1}),
                metric("system", {"load1": 1.0}),
            ]
        },
    )
    await hass.async_block_till_done()
    assert metric_sensors(hass) == [
        "sensor.nas_disk_use",
        CPU_USER,
    ]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_filtered_drive_device_removed(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§7.2/§6.3: a drive whose sensors are all filtered loses its device."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("system", {"load1": 1.0}),
                metric("smart_device", {"temp_c": 34}, **NVME),
            ]
        },
    )
    await hass.async_block_till_done()
    assert drive_device(device_registry, entry, "S5GX593Y") is not None

    hass.config_entries.async_update_entry(
        entry, options={CONF_EXCLUDE_MEASUREMENTS: ["smart_*"]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert drive_device(device_registry, entry, "S5GX593Y") is None
    assert host_device(device_registry, entry) is not None


async def test_host_info_respects_filters(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§7.2: an excluded system measurement sets no device info."""
    entry = add_entry(hass, {CONF_EXCLUDE_MEASUREMENTS: ["system"]})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(
        client,
        {"metrics": [metric("system", SYSTEM_INFO), metric("mem", {"used": 1})]},
    )
    await hass.async_block_till_done()
    assert host_device(device_registry, entry).model is None


async def test_filtered_binary_metric_removed(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§7.2: filter-dropped binary metric sensors leave no registry entry."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("smart_device", {"health_ok": True}, **NVME))
    await hass.async_block_till_done()
    assert entity_registry.async_get(DRIVE_HEALTH) is not None

    hass.config_entries.async_update_entry(
        entry, options={CONF_EXCLUDE_MEASUREMENTS: ["smart_*"]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(DRIVE_HEALTH) is None
