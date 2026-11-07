"""Brand images shipped with the integration (served by HA's brands API)."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from PIL import Image

from custom_components.telegraf.const import DOMAIN

BRAND = Path(__file__).parent.parent / "custom_components" / "telegraf" / "brand"


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_icon(name: str, size: int) -> None:
    """Square PNG with transparency, at the size HA's brand guidelines expect."""
    with Image.open(BRAND / name) as image:
        assert image.format == "PNG"
        assert image.size == (size, size)
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0  # transparent rounded corner


async def test_integration_has_branding(hass: HomeAssistant) -> None:
    """HA's loader finds the brand folder, so the icon replaces the placeholder."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding
