"""Tests for the config and options flows (spec §7)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.telegraf.const import (
    CONF_CONNECTIVITY_TIMEOUT,
    CONF_EXCLUDE_MEASUREMENTS,
    CONF_INCLUDE_MEASUREMENTS,
    CONF_LOCAL_ONLY,
    CONF_MAX_ENTITIES,
    CONF_STALE_TIMEOUT,
    DOMAIN,
    MAX_TIMEOUT,
    MIN_MAX_ENTITIES,
    options_with_defaults,
)

from .common import WEBHOOK_ID, add_and_setup_entry, post


async def test_user_flow_creates_entry_with_lan_url(hass: HomeAssistant) -> None:
    """§7.1: the flow creates an entry and shows the internal webhook URL."""
    await async_process_ha_core_config(
        hass,
        {
            "internal_url": "http://192.168.1.10:8123",
            "external_url": "https://example.duckdns.org",
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "NAS"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NAS"
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    assert len(webhook_id) >= 32

    url = result["description_placeholders"]["webhook_url"]
    assert url == f"http://192.168.1.10:8123/api/webhook/{webhook_id}"
    assert url in result["description_placeholders"]["telegraf_config"]
    snippet = result["description_placeholders"]["telegraf_config"]
    assert 'data_format = "json"' in snippet
    # §4.1: rejected batches are not retried forever.
    assert "non_retryable_statuscodes = [400, 413]" in snippet


async def test_user_flow_requires_name(hass: HomeAssistant) -> None:
    """§7.1: the name is stripped; an empty one re-shows the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_NAME: "name_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "  NAS "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NAS"


async def test_user_flow_never_uses_external_url(hass: HomeAssistant) -> None:
    """§4/§7.1: with only an external URL configured, fall back to the IP URL."""
    await async_process_ha_core_config(
        hass, {"external_url": "https://example.duckdns.org"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Telegraf"}
    )
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    # HA's LAN IP, which the test harness mocks as 10.10.10.10.
    assert result["description_placeholders"]["webhook_url"] == (
        f"http://10.10.10.10:8123/api/webhook/{webhook_id}"
    )


async def test_multiple_entries_allowed(hass: HomeAssistant) -> None:
    """§7.1: several receivers can coexist with distinct webhooks."""
    ids = set()
    for name in ("a", "b"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: name}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        ids.add(result["data"][CONF_WEBHOOK_ID])
    assert len(ids) == 2


async def test_options_flow(hass: HomeAssistant) -> None:
    """§7.2: options are cleaned, coerced to int, and shown with the URL."""
    entry = await add_and_setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["webhook_url"].endswith(
        f"/api/webhook/{WEBHOOK_ID}"
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_INCLUDE_MEASUREMENTS: [" cpu ", "", "disk*"],
            CONF_EXCLUDE_MEASUREMENTS: [],
            CONF_STALE_TIMEOUT: 60.0,
            CONF_CONNECTIVITY_TIMEOUT: 30.0,
            CONF_MAX_ENTITIES: 10.0,
            CONF_LOCAL_ONLY: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_INCLUDE_MEASUREMENTS: ["cpu", "disk*"],
        CONF_EXCLUDE_MEASUREMENTS: [],
        CONF_STALE_TIMEOUT: 60,
        CONF_CONNECTIVITY_TIMEOUT: 30,
        CONF_MAX_ENTITIES: 10,
        CONF_LOCAL_ONLY: False,
    }
    # OptionsFlowWithReload: the entry was reloaded with a hub using them.
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.stale_timeout == 60
    assert hub.connectivity_timeout == 30
    assert hub.max_entities == 10
    assert hub.measurement_allowed("disk1")
    assert not hub.measurement_allowed("mem")


async def test_options_flow_suggests_current_options(hass: HomeAssistant) -> None:
    """§7.2: the form is pre-filled with the entry's current options."""
    options = {
        CONF_INCLUDE_MEASUREMENTS: ["cpu"],
        CONF_EXCLUDE_MEASUREMENTS: ["disk*"],
        CONF_STALE_TIMEOUT: 60,
        CONF_CONNECTIVITY_TIMEOUT: 0,
        CONF_MAX_ENTITIES: 10,
        CONF_LOCAL_ONLY: False,
    }
    entry = await add_and_setup_entry(hass, options)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested == options


async def test_options_flow_empty_input_stores_defaults(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
    batch_body: bytes,
) -> None:
    """§7.2: submitting no fields stores every option's default."""
    entry = await add_and_setup_entry(hass, {CONF_MAX_ENTITIES: 10})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == options_with_defaults({})
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED

    # The reloaded entry no longer applies the old cap of 10 entities.
    client = await hass_client_no_auth()
    assert await post(client, batch_body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entries) > 10


@pytest.mark.parametrize(
    "user_input",
    [
        {CONF_MAX_ENTITIES: MIN_MAX_ENTITIES - 1},
        {CONF_STALE_TIMEOUT: MAX_TIMEOUT + 1},
        {CONF_CONNECTIVITY_TIMEOUT: MAX_TIMEOUT + 1},
        {CONF_STALE_TIMEOUT: -1},
    ],
    ids=[
        "max-entities-below-min",
        "stale-above-max",
        "connectivity-above-max",
        "negative-timeout",
    ],
)
async def test_options_flow_rejects_out_of_range(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> None:
    """§7.2: values outside the documented ranges are refused, options kept."""
    entry = await add_and_setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert entry.options == {}
    assert entry.data[CONF_WEBHOOK_ID] == WEBHOOK_ID


async def test_no_url_available_fallback(hass: HomeAssistant) -> None:
    """§7.1: without any usable URL, a placeholder LAN URL is shown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.telegraf.config_flow.webhook.async_generate_url",
        side_effect=NoURLAvailableError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Telegraf"}
        )
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    assert result["description_placeholders"]["webhook_url"] == (
        f"http://<home-assistant-ip>:8123/api/webhook/{webhook_id}"
    )
