"""Tests for the per-host Connectivity entity (spec §5.7)."""

from __future__ import annotations

import logging
from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    async_fire_time_changed,
    async_mock_restore_state_shutdown_restart,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.telegraf.const import (
    CONF_CONNECTIVITY_TIMEOUT,
    CONF_EXCLUDE_MEASUREMENTS,
    CONF_MAX_ENTITIES,
)
from custom_components.telegraf.identity import host_unique_id

from .common import (
    ALMOST_DISCONNECTED,
    ALMOST_STALE,
    PAST_STALE_CHECK,
    add_and_setup_entry,
    add_entry,
    metric,
    post,
    setup_entry,
    storage,
    store_key,
    stored_sensor,
)

CONNECTIVITY = "binary_sensor.nas_connectivity"


async def test_host_entities_created(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.7: a new host gets Connectivity (on) on its device, and nothing else."""
    entry = await add_and_setup_entry(hass)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    assert hass.states.get(CONNECTIVITY).state == STATE_ON
    assert hass.states.get(CONNECTIVITY).attributes["device_class"] == "connectivity"

    reg = entity_registry.async_get(CONNECTIVITY)
    assert reg.entity_category is EntityCategory.DIAGNOSTIC
    assert reg.unique_id == host_unique_id(entry.entry_id, "nas", "connectivity")
    assert reg.device_id == entity_registry.async_get("sensor.nas_load_1_min").device_id
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 2


async def test_stale_host_goes_off_not_unavailable(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After connectivity_timeout the host is off, then on again (§5.7).

    Sensors follow the longer stale_timeout (§6.2).
    """
    await add_and_setup_entry(hass)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    freezer.tick(ALMOST_DISCONNECTED)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_ON

    freezer.tick(PAST_STALE_CHECK)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_OFF
    assert hass.states.get("sensor.nas_load_1_min").state == "1.0"

    assert await post(client, metric("system", {"load1": 2.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_ON


async def test_host_offline_and_back_logged(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§5.7: going off and coming back are each logged once at INFO."""
    caplog.set_level(logging.INFO, logger="custom_components.telegraf")
    await add_and_setup_entry(hass)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()
    offline = "Telegraf host 'nas' of Telegraf stopped sending data"
    back = "Telegraf host 'nas' of Telegraf is sending data again"
    assert offline not in caplog.text
    assert back not in caplog.text  # first data is no comeback

    for _ in range(2):  # later checks while off do not repeat the message
        freezer.tick(ALMOST_DISCONNECTED + PAST_STALE_CHECK)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_OFF
    assert caplog.text.count(offline) == 1
    assert back not in caplog.text

    for value in (2.0, 3.0):
        await post(client, metric("system", {"load1": value}))
        await hass.async_block_till_done()
    assert caplog.text.count(back) == 1
    assert [r.levelno for r in caplog.records if r.message in (offline, back)] == [
        logging.INFO,
        logging.INFO,
    ]


@pytest.mark.parametrize(
    ("options", "ignored"),
    [
        ({CONF_EXCLUDE_MEASUREMENTS: ["mem"]}, metric("mem", {"used": 1})),
        # Exactly full: nas + Connectivity.
        ({CONF_MAX_ENTITIES: 2}, metric("mem", {"used": 1})),
        ({}, metric("system", {"load1": "not a number"})),
    ],
    ids=["filtered", "capped", "type-mismatch"],
)
async def test_any_metric_counts_as_sign_of_life(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
    options: dict,
    ignored: dict,
) -> None:
    """§5.7: metrics that create or update no sensor keep the host connected."""
    entry = await add_and_setup_entry(hass, options)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    freezer.tick(ALMOST_STALE)
    assert await post(client, ignored) == HTTPStatus.NO_CONTENT
    freezer.tick(PAST_STALE_CHECK)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.sensors) == 1
    assert hass.states.get("sensor.nas_load_1_min").state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTIVITY).state == STATE_ON


async def test_host_without_sensors_gets_no_entities(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """§5.7/§7.2: a host with only filtered metrics gets no device or entities."""
    await add_and_setup_entry(hass, {CONF_EXCLUDE_MEASUREMENTS: ["mem"]})
    client = await hass_client_no_auth()
    assert await post(client, metric("mem", {"used": 1})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY) is None


@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(seconds=60), STATE_ON), (timedelta(hours=2), STATE_OFF)],
)
async def test_restart_restores_last_seen(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    age: timedelta,
    expected: str,
) -> None:
    """§5.7: Connectivity restores the last-seen time and derives its state."""
    restored = dt_util.utcnow() - age
    mock_restore_cache_with_extra_data(
        hass, [(State(CONNECTIVITY, STATE_ON), {"last_seen": restored.isoformat()})]
    )
    entry = add_entry(hass)
    hass_storage[store_key(entry.entry_id)] = storage(entry.entry_id, [stored_sensor()])
    await setup_entry(hass, entry)

    assert hass.states.get(CONNECTIVITY).state == expected
    assert entry.runtime_data.hosts["nas"].last_seen == restored
    # Metric sensors still wait for fresh data (spec §6.2).
    assert hass.states.get("sensor.nas_load_1_min").state == STATE_UNAVAILABLE


async def test_last_seen_saved_for_restart(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """§5.7: Connectivity saves the host's last-seen time; a reload restores it."""
    entry = await add_and_setup_entry(hass)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    last_seen = entry.runtime_data.hosts["nas"].last_seen

    data = await async_mock_restore_state_shutdown_restart(hass)
    stored = data.async_get_stored_state(CONNECTIVITY)
    assert stored.extra_data.as_dict() == {"last_seen": last_seen.isoformat()}

    freezer.tick(timedelta(seconds=60))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.hosts["nas"].last_seen == last_seen
    assert hass.states.get(CONNECTIVITY).state == STATE_ON


async def test_restart_without_history_is_unavailable(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """§5.7: with nothing to restore, Connectivity is unavailable until data."""
    entry = add_entry(hass)
    hass_storage[store_key(entry.entry_id)] = storage(entry.entry_id, [stored_sensor()])
    await setup_entry(hass, entry)
    assert hass.states.get(CONNECTIVITY).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    "extra_data",
    [
        {"last_seen": "garbage"},
        {"last_seen": 123},
        {},
        {"last_seen": "2026-11-07T10:00:00"},
    ],
    ids=["invalid", "not-a-string", "missing", "naive"],
)
async def test_restart_ignores_unusable_last_seen(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
    extra_data: dict[str, Any],
) -> None:
    """Unusable restore data counts as nothing to restore (§5.7)."""
    mock_restore_cache_with_extra_data(
        hass, [(State(CONNECTIVITY, STATE_ON), extra_data)]
    )
    entry = add_entry(hass)
    hass_storage[store_key(entry.entry_id)] = storage(entry.entry_id, [stored_sensor()])
    await setup_entry(hass, entry)

    assert entity_registry.async_get(CONNECTIVITY) is not None
    assert hass.states.get(CONNECTIVITY).state == STATE_UNAVAILABLE
    assert entry.runtime_data.hosts["nas"].last_seen is None

    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_ON


async def test_device_removal_removes_host_entities(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """§5.7/§6.4: deleting the host device removes its host entities and state."""
    assert await async_setup_component(hass, "config", {})
    entry = await add_and_setup_entry(hass)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    device_id = entity_registry.async_get(CONNECTIVITY).device_id
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device_id}
    )
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert entity_registry.async_get(CONNECTIVITY) is None
    assert entry.runtime_data.hosts == {}

    assert await post(client, metric("system", {"load1": 2.0})) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_ON


async def test_connectivity_timeout_disabled(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """connectivity_timeout = 0 keeps Connectivity on (§5.7)."""
    await add_and_setup_entry(hass, {CONF_CONNECTIVITY_TIMEOUT: 0})
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTIVITY).state == STATE_ON
    assert hass.states.get("sensor.nas_load_1_min").state == STATE_UNAVAILABLE
