"""Shared fixtures for the Telegraf integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from .common import add_entry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def batch_body() -> bytes:
    """Return a realistic Telegraf outputs.http batch payload."""
    return (FIXTURES / "batch.json").read_bytes()


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add a config entry (not yet set up)."""
    return add_entry(hass)
