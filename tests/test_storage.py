"""Tests for the persisted sensor list (spec §6.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    ConfigEntryState,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
)

from custom_components.telegraf.const import (
    CONF_INCLUDE_MEASUREMENTS,
    STORAGE_SAVE_DELAY,
)
from custom_components.telegraf.identity import (
    unique_id_for,
)
from custom_components.telegraf.parser import make_key

from .common import (
    CPU_USER,
    add_entry,
    metric,
    metric_sensors,
    post,
    setup_entry,
    storage,
    store_key,
    stored_sensor,
)


async def test_reload_restores_entities_unavailable(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.2/§6.3: after a reload, stored sensors return unavailable until data."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == STATE_UNAVAILABLE

    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 3.0}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "3.0"


async def test_startup_from_storage(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.3: sensors persisted by a previous run are recreated at startup."""
    hass_storage[store_key(entry.entry_id)] = storage(
        entry.entry_id,
        [
            stored_sensor(
                measurement="cpu", tags=[["cpu", "cpu-total"]], field="usage_user"
            ),
            {"garbage": True},
            # §3.3: oversized tag sets are not rebuilt from storage either.
            stored_sensor(tags=[[f"t{i}", "v"] for i in range(65)]),
        ],
    )
    await setup_entry(hass, entry)
    assert hass.states.get(CPU_USER).state == STATE_UNAVAILABLE
    assert er.async_get(hass).async_get(CPU_USER).unique_id == unique_id_for(
        entry.entry_id, make_key("nas", "cpu", (("cpu", "cpu-total"),), "usage_user")
    )

    client = await hass_client_no_auth()
    await post(client, metric("cpu", {"usage_user": 4.0}, cpu="cpu-total"))
    await hass.async_block_till_done()
    assert hass.states.get(CPU_USER).state == "4.0"
    assert metric_sensors(hass) == [CPU_USER]


@pytest.mark.parametrize(
    "data",
    [["not", "an", "object"], {"sensors": "not a list"}, "garbage"],
    ids=["list", "sensors-not-list", "string"],
)
async def test_corrupt_store_ignored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    data: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§6.3: an unusable store is treated as empty, with a warning."""
    entry = add_entry(hass)
    hass_storage[store_key(entry.entry_id)] = {
        **storage(entry.entry_id, []),
        "data": data,
    }
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.sensors == {}
    assert "Ignoring invalid stored sensor list" in caplog.text


async def test_invalid_stored_records_skipped(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """§6.3: records the parser would reject are skipped, even with globs set."""
    entry = add_entry(hass, {CONF_INCLUDE_MEASUREMENTS: ["sys*"]})
    hass_storage[store_key(entry.entry_id)] = storage(
        entry.entry_id,
        [
            {**stored_sensor(), "host": None},
            {**stored_sensor(), "numeric": "false"},
            {**stored_sensor(), "measurement": 5},
            {**stored_sensor(), "field": "bad\nfield"},
            stored_sensor(tags=[["only-key"]]),
            stored_sensor(tags=[["k", 1]]),
            stored_sensor(tags=[["k", "a"], ["k", "b"]]),
            stored_sensor(tags="not a list"),
            "not an object",
            stored_sensor(field="load5", tags=[["z", "1"], ["a", "2"]]),
        ],
    )
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    infos = list(entry.runtime_data.sensors.values())
    # Tags are normalized to the parser's sorted order (§5.1).
    assert [(i.field, i.tags) for i in infos] == [("load5", (("a", "2"), ("z", "1")))]


async def test_remove_entry_deletes_storage(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_storage: dict[str, Any],
) -> None:
    """§6.3: removing the entry deletes its store."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("system", {"load1": 1.0}))
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert store_key(entry.entry_id) in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(entry.entry_id) not in hass_storage


async def test_debounced_save_shape(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """§6.3: discoveries are persisted by the debounced save in the spec shape."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    await post(client, metric("net", {"speed": 1}, interface="eth0", vlan="10"))
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=STORAGE_SAVE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[store_key(entry.entry_id)]["data"] == {
        "sensors": [
            {
                "host": "nas",
                "measurement": "net",
                "tags": [["interface", "eth0"], ["vlan", "10"]],
                "field": "speed",
                "numeric": True,
            }
        ]
    }
