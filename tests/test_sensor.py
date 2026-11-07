"""Tests for sensor creation, units, names, drives and host info (spec §5)."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    RELOAD_AFTER_UPDATE_DELAY,
    ConfigEntryState,
)
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
)

from custom_components.telegraf.const import (
    DOMAIN,
)
from custom_components.telegraf.identity import (
    unique_id_for,
)
from custom_components.telegraf.parser import make_key

from .common import (
    CPU_USER,
    DRIVE_HEALTH,
    DRIVE_TEMP,
    NVME,
    SYSTEM_INFO,
    add_entry,
    drive_device,
    host_device,
    metric,
    metric_sensors,
    post,
    setup_entry,
)


async def test_batch_creates_entities_and_devices(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    batch_body: bytes,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§5: one entity per field, one device per host, units applied."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert await post(client, batch_body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    # 17 metric sensors + Connectivity for each of 2 hosts.
    assert len(entities) == 17 + 2

    state = hass.states.get(CPU_USER)
    assert state is not None
    assert state.state == "1.9"
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["state_class"] == "measurement"

    reg_entry = entity_registry.async_get(CPU_USER)
    assert reg_entry.unique_id == unique_id_for(
        entry.entry_id, make_key("nas", "cpu", (("cpu", "cpu-total"),), "usage_user")
    )

    # §5.6: everything outside the short allowlist is registered disabled,
    # including all per-disk and per-interface measurements.
    for entity_id in (
        "sensor.nas_processor_idle",
        "sensor.nas_uptime_text",
        "sensor.nas_network_in_eth0",
        "sensor.nas_packets_in_eth0",
        "sensor.nas_disk_usage",
        "sensor.nas_disk_inodes_used",
    ):
        assert hass.states.get(entity_id) is None
        assert (
            entity_registry.async_get(entity_id).disabled_by
            is er.RegistryEntryDisabler.INTEGRATION
        )
    assert hass.states.get("sensor.nas_memory_usage").state == "41.5"
    assert hass.states.get("sensor.pi_temperature_cpu_thermal").attributes[
        "device_class"
    ] == ("temperature")

    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert sorted(d.name for d in devices) == ["nas", "pi"]
    assert {d.manufacturer for d in devices} == {"Telegraf"}
    # No host info fields in the batch (§5.1).
    assert {d.model for d in devices} == {None}
    nas = next(d for d in devices if d.name == "nas")
    assert nas.identifiers == {(DOMAIN, f"{entry.entry_id}_nas")}
    assert entity_registry.async_get(CPU_USER).device_id == nas.id
    # §5.4.3: the icon comes from icons.json via the translation key (the
    # frontend applies it); the name stays the code-built one.
    cpu_user = entity_registry.async_get(CPU_USER)
    assert cpu_user.translation_key == "cpu"
    assert cpu_user.original_icon is None
    assert cpu_user.original_name == "Processor use (user)"
    assert "icon" not in hass.states.get(CPU_USER).attributes
    temp = entity_registry.async_get("sensor.pi_temperature_cpu_thermal")
    assert temp.translation_key is None  # device class icon


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_units_and_names_when_enabled(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    batch_body: bytes,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.2/§5.5: display units and multi-tag names of (enabled) sensors."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert await post(client, batch_body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    net = hass.states.get("sensor.nas_network_in_eth0")
    assert net.attributes["device_class"] == "data_size"
    assert net.attributes["state_class"] == "total_increasing"
    # §5.5: stored in B, displayed in GiB with 2 decimals.
    assert net.attributes["unit_of_measurement"] == "GiB"
    assert float(net.state) == pytest.approx(123456789 / 2**30)
    reg_opts = entity_registry.async_get(net.entity_id).options
    assert reg_opts["sensor.private"]["suggested_unit_of_measurement"] == "GiB"
    assert reg_opts["sensor"]["suggested_display_precision"] == 2

    disk_id = entity_registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        unique_id_for(
            entry.entry_id,
            make_key(
                "nas",
                "disk",
                (("path", "/"),),
                "used_percent",
            ),
        ),
    )
    assert hass.states.get(disk_id).attributes["friendly_name"] == ("nas Disk usage /")


async def test_kernel_boot_time_sensor(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§5.4.1: kernel.boot_time is an enabled uptime (restart time) sensor."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("kernel", {"boot_time": 1793792725}))
    await hass.async_block_till_done()
    state = hass.states.get("sensor.nas_last_boot")
    assert state.state == "2026-11-04T11:45:25+00:00"
    assert state.attributes["device_class"] == "uptime"
    assert "unit_of_measurement" not in state.attributes
    assert "state_class" not in state.attributes

    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    def changes() -> list[str]:
        return [
            e.data["new_state"].state
            for e in events
            if e.data["entity_id"] == "sensor.nas_last_boot"
        ]

    # A clock step moves btime by a second: HA's uptime class ignores it.
    await post(client, metric("kernel", {"boot_time": 1793792726}))
    await hass.async_block_till_done()
    assert changes() == []

    # Reboot.
    await post(client, metric("kernel", {"boot_time": 1793796325}))
    await hass.async_block_till_done()
    assert changes() == ["2026-11-04T12:45:25+00:00"]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_uptime_is_a_duration_in_days(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§5.4/§5.5: system.uptime is a plain duration statistic shown in days."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"uptime": 129600}))
    await hass.async_block_till_done()
    state = hass.states.get("sensor.nas_uptime")
    assert float(state.state) == 1.5
    assert state.attributes["unit_of_measurement"] == "d"
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["state_class"] == "measurement"


async def test_type_fixed_at_discovery(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§5.3: a numeric sensor ignores later string values."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await post(client, metric("system", {"load1": "oops"}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "1.0"


async def test_same_host_in_two_entries(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§2/§5.1: entries are independent; the same host gets one device each."""
    entries = []
    for webhook_id in ("hook_a", "hook_b"):
        e = add_entry(hass, webhook_id=webhook_id)
        await setup_entry(hass, e)
        entries.append(e)
    client = await hass_client_no_auth()
    for webhook_id in ("hook_a", "hook_b"):
        status = await post(
            client, metric("system", {"load1": 1.0}), webhook_id=webhook_id
        )
        assert status == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    device_ids = set()
    for e in entries:
        devices = dr.async_entries_for_config_entry(device_registry, e.entry_id)
        assert len(devices) == 1
        device_ids.add(devices[0].id)
    assert len(device_ids) == 2
    assert len(metric_sensors(hass)) == 2


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_text_sensor_ignores_numbers(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§5.3: a text sensor ignores later numeric values."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("app", {"status": "up"}))
    await post(client, metric("app", {"status": 5}))
    await hass.async_block_till_done()
    state = hass.states.get("sensor.nas_app_status")
    assert state.state == "up"
    assert "unit_of_measurement" not in state.attributes
    assert "state_class" not in state.attributes


async def test_enable_disabled_entity(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """§5.6: a disabled-by-default sensor works once the user enables it."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("cpu", {"usage_idle": 90.0}, cpu="cpu0"))
    await hass.async_block_till_done()
    entity_id = "sensor.nas_processor_idle_cpu0"
    assert hass.states.get(entity_id) is None

    entity_registry.async_update_entity(entity_id, disabled_by=None)
    # HA reloads the entry a while after an entity is enabled.
    freezer.tick(timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    client = await hass_client_no_auth()
    await post(client, metric("cpu", {"usage_idle": 80.0}, cpu="cpu0"))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "80.0"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_drive_gets_own_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.1/§5.2: a SMART drive with a serial is its own device via the host."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(
        client,
        {
            "metrics": [
                metric("smart_device", {"temp_c": 34, "health_ok": True}, **NVME),
                metric("smart_device", {"exit_status": 0}, device="sda"),
            ]
        },
    )
    await hass.async_block_till_done()

    host = host_device(device_registry, entry)
    drive = drive_device(device_registry, entry, "S5GX593Y")
    assert drive.name == "Samsung SSD 980 PRO 1TB 593Y"
    assert drive.model == "Samsung SSD 980 PRO 1TB"
    assert drive.serial_number == "S5GX593Y"
    assert drive.via_device_id == host.id

    temp = entity_registry.async_get(DRIVE_TEMP)
    assert temp.device_id == drive.id
    assert hass.states.get(DRIVE_TEMP).state == "34"
    health = entity_registry.async_get(DRIVE_HEALTH)
    assert health.device_id == drive.id
    assert (
        hass.states.get(DRIVE_TEMP).attributes["friendly_name"]
        == "Samsung SSD 980 PRO 1TB 593Y Temperature"
    )
    # Without a serial (standby), the series stays on the host device.
    standby = entity_registry.async_get("sensor.nas_drive_smart_exit_status_sda")
    assert standby.device_id == host.id


async def test_host_info_on_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.1/§5.6: OS fields describe the device; kernel is a diagnostic sensor."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", SYSTEM_INFO))
    await hass.async_block_till_done()

    nas = host_device(device_registry, entry)
    assert (nas.model, nas.sw_version, nas.hw_version) == (
        "opensuse-microos",
        "20261024",
        "x86_64",
    )
    assert {i.field for i in entry.runtime_data.sensors.values()} == {
        "kernel_version",
        "load1",
    }
    kernel = entity_registry.async_get("sensor.nas_kernel_version")
    assert kernel.entity_category is er.EntityCategory.DIAGNOSTIC
    assert kernel.disabled_by is None
    assert hass.states.get("sensor.nas_kernel_version").state == "7.3.0-1-default"

    # An update reaches the existing device.
    await post(client, metric("system", {**SYSTEM_INFO, "platform_version": "2"}))
    await hass.async_block_till_done()
    assert host_device(device_registry, entry).sw_version == "2"

    # After a restart, stored sensors do not wipe the device info.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert host_device(device_registry, entry).model == "opensuse-microos"


async def test_smart_values_made_useful(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.4.2/§5.6: health is a problem binary sensor; key values enabled."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    fields = {
        "temp_c": 34,
        "health_ok": True,
        "critical_warning": 0,
        "percentage_used": 1,
        "exit_status": 0,
        "power_on_hours": 427,
    }
    await post(client, metric("smart_device", fields, **NVME))
    await hass.async_block_till_done()

    state = hass.states.get(DRIVE_HEALTH)
    assert state.state == "off"  # no problem
    assert state.attributes["device_class"] == "problem"
    warning = "binary_sensor.samsung_ssd_980_pro_1tb_593y_critical_warning"
    assert entity_registry.async_get(warning).disabled_by is not None
    assert hass.states.get("sensor.samsung_ssd_980_pro_1tb_593y_health") is None
    assert hass.states.get(DRIVE_TEMP).state == "34"
    assert hass.states.get("sensor.samsung_ssd_980_pro_1tb_593y_life_used")
    exit_status = entity_registry.async_get(
        "sensor.samsung_ssd_980_pro_1tb_593y_smart_exit_status"
    )
    assert exit_status.entity_category is er.EntityCategory.DIAGNOSTIC
    assert exit_status.disabled_by is not None

    await post(client, metric("smart_device", {**fields, "health_ok": False}, **NVME))
    await hass.async_block_till_done()
    assert hass.states.get(DRIVE_HEALTH).state == "on"  # problem


async def test_configuration_url_from_system_tag(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§5.1: the system input's ha_configuration_url tag is the device link."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    tags = {"ha_configuration_url": "https://nas:9090"}
    await post(client, metric("system", {"load1": 0.5}, **tags))
    await hass.async_block_till_done()
    assert host_device(device_registry, entry).configuration_url == ("https://nas:9090")
    assert {i.field for i in entry.runtime_data.sensors.values()} == {"load1"}
    assert [i.tags for i in entry.runtime_data.sensors.values()] == [()]

    # Changing the URL updates the device, not the sensors' identity.
    await post(
        client, metric("system", {"load1": 0.5}, ha_configuration_url="http://nas/")
    )
    await hass.async_block_till_done()
    assert host_device(device_registry, entry).configuration_url == "http://nas/"
    assert len(entry.runtime_data.sensors) == 1


@pytest.mark.parametrize("url", ["http://nas:99999/", "http://nas:abc/"])
async def test_invalid_configuration_url_ignored(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
    url: str,
) -> None:
    """§5.1/§4.1: a URL HA's device registry refuses is ignored, data kept."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    # New host: its device is created by the first entity.
    status = await post(
        client, metric("system", {"load1": 0.5}, ha_configuration_url=url)
    )
    await hass.async_block_till_done()
    assert status == HTTPStatus.NO_CONTENT
    assert hass.states.get("sensor.nas_load_1_min").state == "0.5"
    assert host_device(device_registry, entry).configuration_url is None

    # Existing host device: updated in place, keeps its valid link.
    good = "https://nas:9090"
    await post(client, metric("system", {"load1": 0.5}, ha_configuration_url=good))
    await hass.async_block_till_done()
    status = await post(
        client, metric("system", {"load1": 0.7}, ha_configuration_url=url)
    )
    await hass.async_block_till_done()
    assert status == HTTPStatus.NO_CONTENT
    assert hass.states.get("sensor.nas_load_1_min").state == "0.7"
    assert host_device(device_registry, entry).configuration_url == good


async def test_host_info_refused_by_registry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    device_registry: dr.DeviceRegistry,
) -> None:
    """§4.1: a device registry ValueError never fails the batch."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 0.5}))
    await hass.async_block_till_done()

    bad = {"configuration_url": "http://nas:99999/"}
    with patch("custom_components.telegraf.hub.host_device_details", return_value=bad):
        status = await post(
            client, metric("system", {"load1": 0.7}, ha_configuration_url="x")
        )
        await hass.async_block_till_done()
    assert status == HTTPStatus.NO_CONTENT
    assert hass.states.get("sensor.nas_load_1_min").state == "0.7"
    assert host_device(device_registry, entry).configuration_url is None


async def test_reboot_required(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.4.2/§8: the exec check is an enabled binary sensor on the host."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("reboot_needed", {"value": 1}))
    await hass.async_block_till_done()

    entity_id = "binary_sensor.nas_reboot_required"
    reg = entity_registry.async_get(entity_id)
    assert reg.disabled_by is None
    assert reg.translation_key == "reboot"
    assert reg.device_id == host_device(dr.async_get(hass), entry).id
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["friendly_name"] == "nas Reboot required"
    assert "device_class" not in state.attributes

    await post(client, metric("reboot_needed", {"value": 0}))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"
