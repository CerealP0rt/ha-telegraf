"""Tests that keep the docs in sync with the code."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.telegraf import units
from custom_components.telegraf.config_flow import TELEGRAF_SNIPPET

ROOT = Path(__file__).parent.parent
PLACEHOLDER_URL = "http://<ha-lan-address>:8123/api/webhook/<webhook_id>"


def _config_lines(toml: str) -> list[str]:
    """Return the non-comment lines; docs may add their own ## comments."""
    return [line for line in toml.splitlines() if not line.strip().startswith("##")]


@pytest.mark.parametrize("doc", ["README.md", "docs/spec.md"])
def test_telegraf_snippet_matches_docs(doc: str) -> None:
    """§8: the snippet shown after setup is the one documented."""
    blocks = [
        block
        for block in re.findall(r"```toml\n(.*?)\n```", (ROOT / doc).read_text(), re.S)
        if "[[outputs.http]]" in block
    ]
    assert len(blocks) == 1
    assert _config_lines(blocks[0]) == _config_lines(
        TELEGRAF_SNIPPET.format(url=PLACEHOLDER_URL)
    )


def test_english_translation_mirrors_strings() -> None:
    """translations/en.json is an exact copy of strings.json."""
    component = ROOT / "custom_components" / "telegraf"
    assert json.loads((component / "translations" / "en.json").read_text()) == (
        json.loads((component / "strings.json").read_text())
    )


def test_icon_translation_keys_exist() -> None:
    """§5.4.3: icons.json covers exactly the translation keys the code sets.

    Sensor keys are the ones units.icon_key can return (both ways, so no key
    lacks an icon and no icon is dead); binary sensor keys come from
    strings.json, which also holds their state names.
    """
    component = ROOT / "custom_components" / "telegraf"
    icons = json.loads((component / "icons.json").read_text())["entity"]
    strings = json.loads((component / "strings.json").read_text())["entity"]
    assert set(icons) == {"binary_sensor", "sensor"}
    assert set(icons["sensor"]) == units.ICON_KEYS
    assert all(icon["default"].startswith("mdi:") for icon in icons["sensor"].values())
    assert set(icons["binary_sensor"]) <= set(strings["binary_sensor"])
