"""Tests for entry setup, unload and their failure paths (spec §4.1, §6.3)."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
)

from custom_components.telegraf.const import (
    STORAGE_SAVE_DELAY,
)
from custom_components.telegraf.parser import Metric

from .common import (
    add_entry,
    metric,
    metric_sensors,
    post,
    setup_entry,
    store_key,
)


async def test_unload_unregisters_webhook(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§4/§6.3: unloading removes the webhook; posts then create nothing."""
    await setup_entry(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    client = await hass_client_no_auth()
    # HA core answers 200 for unknown webhooks; our handler would answer 204.
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.OK
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min") is None


async def test_request_during_unload_503_and_no_zombie(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """§4.1/§6.3: a paused hub refuses data (503) and does not persist or add."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    hub = entry.runtime_data
    hub.async_pause()

    assert (
        await post(client, metric("mem", {"used": 1})) == HTTPStatus.SERVICE_UNAVAILABLE
    )
    # A request that passed the check before the pause is ignored as well.
    hub.async_process([Metric("nas", "mem", (), "used", 1)])
    freezer.tick(timedelta(seconds=STORAGE_SAVE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hub.sensors == {}
    assert metric_sensors(hass) == []
    assert store_key(entry.entry_id) not in hass_storage


async def test_unload_order(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    hass_storage: dict[str, Any],
) -> None:
    """§6.3: pause, unload platforms, then unregister the webhook and flush."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT
    hub = entry.runtime_data
    calls: list[str] = []
    unload_platforms = hass.config_entries.async_unload_platforms

    async def _unload_platforms(*args):
        calls.append(f"platforms running={hub.running}")
        return await unload_platforms(*args)

    with (
        patch.object(hass.config_entries, "async_unload_platforms", _unload_platforms),
        patch.object(
            webhook,
            "async_unregister",
            side_effect=lambda *_: calls.append("unregister"),
        ),
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert calls == ["platforms running=False", "unregister"]
    assert hub.closed
    assert hass_storage[store_key(entry.entry_id)]["data"]["sensors"]


async def test_failed_unload_keeps_webhook(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.3: if the platforms fail to unload, the webhook keeps working."""
    await setup_entry(hass, entry)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD

    client = await hass_client_no_auth()
    status = await post(client, metric("system", {"load1": 1.0}))
    assert status == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nas_load_1_min").state == "1.0"
    # FAILED_UNLOAD is final until restart; stop the timer for teardown.
    await entry.runtime_data.async_shutdown()


async def test_failed_setup_duplicate_webhook(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.3: a duplicate webhook id fails setup without touching the first entry."""
    await setup_entry(hass, entry)
    duplicate = add_entry(hass)
    assert not await hass.config_entries.async_setup(duplicate.entry_id)
    await hass.async_block_till_done()
    assert duplicate.state is ConfigEntryState.SETUP_ERROR

    client = await hass_client_no_auth()
    status = await post(client, metric("system", {"load1": 1.0}))
    assert status == HTTPStatus.NO_CONTENT


async def test_failed_setup_unregisters_webhook(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§6.3: a setup failure after registration unregisters the webhook again.

    (Lingering timers would also fail this test at teardown.)
    """
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("boom"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.runtime_data.closed

    client = await hass_client_no_auth()
    # HA core answers 200 for unknown webhooks.
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.OK
