"""Pin the persisted identity formulas (spec §5.1, §5.7).

These literals are stored in users' entity and device registries. If one of
these tests fails, the change would re-ID existing entities: do not update
the literal, fix the code.
"""

from __future__ import annotations

import pytest

from custom_components.telegraf.identity import (
    device_identifier,
    drive_device_identifier,
    drive_from_device_identifier,
    drive_serial,
    host_from_device_identifier,
    host_unique_id,
    unique_id_for,
)
from custom_components.telegraf.parser import make_key


def test_sensor_unique_id_pinned() -> None:
    """§5.1: sha1 of the compact JSON key, first 16 hex digits."""
    key = make_key("nas", "cpu", (("cpu", "cpu-total"),), "usage_idle")
    assert key == '["nas","cpu",[["cpu","cpu-total"]],"usage_idle"]'
    assert unique_id_for("E1", key) == "E1_e7b2c4028dae971b"
    key = make_key("Büro-Pi", "temp", (("sensor", "cpu_thermal"),), "temp")
    assert key == '["Büro-Pi","temp",[["sensor","cpu_thermal"]],"temp"]'
    assert unique_id_for("E1", key) == "E1_6d3b423dff2eb9e0"


def test_host_unique_id_pinned() -> None:
    """§5.7: ensure_ascii=False and default separators, frozen as shipped."""
    assert host_unique_id("E1", "nas", "last_seen") == "E1_a1d1ad96421d5c77"
    assert host_unique_id("E1", "Büro-Pi", "last_seen") == "E1_0107360895df71a4"
    assert host_unique_id("E1", "Büro-Pi", "connectivity") == "E1_93d2378bd7847198"


def test_device_identifier_pinned() -> None:
    """§5.1: the device identifier is the entry id and the raw host name."""
    assert device_identifier("E1", "Büro-Pi") == ("telegraf", "E1_Büro-Pi")


def test_host_from_device_identifier_inverts() -> None:
    """§6.4: the host is recovered from this entry's identifiers only."""
    identifier = device_identifier("E1", "Büro_Pi")
    assert host_from_device_identifier("E1", identifier) == "Büro_Pi"
    assert host_from_device_identifier("E2", identifier) is None
    assert host_from_device_identifier("E1", ("other", "E1_x")) is None


def test_drive_device_identifier_pinned() -> None:
    """§5.1: entry id, `:drive:`, then JSON [host, serial]."""
    assert drive_device_identifier("E1", "Büro-Pi", "S5GX") == (
        "telegraf",
        'E1:drive:["Büro-Pi", "S5GX"]',
    )


def test_drive_from_device_identifier_inverts() -> None:
    """§6.4: (host, serial) recovered; host and drive identifiers never mix."""
    drive = drive_device_identifier("E1", "nas_1", "S:5")
    assert drive_from_device_identifier("E1", drive) == ("nas_1", "S:5")
    assert drive_from_device_identifier("E2", drive) is None
    assert host_from_device_identifier("E1", drive) is None
    assert drive_from_device_identifier("E1", device_identifier("E1", "nas")) is None
    # A host literally named like a drive identifier stays a host.
    tricky = device_identifier("E1", ':drive:["nas", "S"]')
    assert drive_from_device_identifier("E1", tricky) is None
    for value in ("E1:drive:x", "E1:drive:[1, 2]", 'E1:drive:"ab"'):
        assert drive_from_device_identifier("E1", ("telegraf", value)) is None


@pytest.mark.parametrize(
    ("measurement", "tags", "expected"),
    [
        ("smart_device", (("device", "nvme0"), ("serial_no", "S5GX")), "S5GX"),
        ("smart_attribute", (("serial_no", "X"),), "X"),
        ("smart_device", (("device", "sda"),), None),
        ("disk", (("serial_no", "X"),), None),
    ],
)
def test_drive_serial(measurement: str, tags: tuple, expected: str | None) -> None:
    """§5.1: only SMART series with a serial get a drive device."""
    assert drive_serial(measurement, tags) == expected
