"""Diagnostics for Telegraf (spec §7.3)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from . import TelegrafConfigEntry
from .hub import loaded_hub

TO_REDACT = {CONF_WEBHOOK_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TelegrafConfigEntry
) -> dict[str, Any]:
    """Return redacted entry data and a hub summary (None if not loaded)."""
    hub = loaded_hub(entry)
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "hub": hub.diagnostics() if hub is not None else None,
    }
