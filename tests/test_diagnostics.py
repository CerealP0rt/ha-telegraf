"""Tests for diagnostics (spec §7.3)."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from .common import add_entry, post, setup_entry


async def test_diagnostics_redacts_webhook(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    batch_body: bytes,
    snapshot: SnapshotAssertion,
) -> None:
    """§4/§7.3: the webhook id is redacted; the hub summary is included."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = add_entry(hass, webhook_id="secret_id")
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    status = await post(client, batch_body, webhook_id="secret_id")
    assert status == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert "secret_id" not in str(diag)
    assert diag == snapshot


async def test_diagnostics_entry_not_loaded(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """§7.3: diagnostics of an unloaded entry have no hub block, still redacted."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = add_entry(hass, webhook_id="secret_id")
    await setup_entry(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert "secret_id" not in str(diag)
    assert diag["hub"] is None


async def test_diagnostics_after_failed_setup(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """§7.3: a hub left behind by a failed setup is not reported."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = add_entry(hass, webhook_id="secret_id")
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("boom"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["hub"] is None
