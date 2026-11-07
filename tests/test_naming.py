"""Tests for entity names (spec §5.2)."""

from __future__ import annotations

import pytest

from custom_components.telegraf import naming
from custom_components.telegraf.identity import drive_serial
from custom_components.telegraf.naming import drive_name, entity_name

SMART = (
    ("device", "nvme0"),
    ("model", "Samsung SSD 980 PRO 1TB"),
    ("serial_no", "S5GXNF0R983593Y"),
)
DISK_ROOT = (("path", "/"),)  # other disk tags are volatile (§3.3)


@pytest.mark.parametrize(
    ("measurement", "tags", "field", "expected"),
    [
        ("cpu", (("cpu", "cpu-total"),), "usage_active", "Processor use"),
        ("cpu", (("cpu", "cpu-total"),), "usage_user", "Processor use (user)"),
        ("cpu", (("cpu", "cpu3"),), "usage_idle", "Processor idle cpu3"),
        ("cpu", (), "usage_guest_nice", "Processor use (guest nice)"),
        ("mem", (), "used_percent", "Memory usage"),
        ("mem", (), "available", "Memory available"),
        ("mem", (), "swap_free", "Memory swap free"),
        ("swap", (), "free", "Swap free"),
        ("mem", (), "page_tables", "Memory page tables"),
        ("swap", (), "used_percent", "Swap usage"),
        ("system", (), "load1", "Load (1 min)"),
        ("system", (), "load15", "Load (15 min)"),
        ("system", (), "uptime", "Uptime"),
        ("system", (), "kernel_version", "Kernel version"),
        ("kernel", (), "boot_time", "Last boot"),
        ("kernel", (), "disk_pages_in", "Kernel disk pages in"),
        ("disk", DISK_ROOT, "used_percent", "Disk usage /"),
        ("disk", DISK_ROOT, "free", "Disk free /"),
        ("disk", (("path", "/boot/efi"),), "total", "Disk total /boot/efi"),
        ("diskio", (("name", "sda"),), "read_bytes", "Disk read sda"),
        ("net", (("interface", "eth0"),), "bytes_recv", "Network in eth0"),
        ("net", (("interface", "all"),), "tcp_insegs", "Network tcp insegs"),
        ("temp", (("sensor", "cpu_thermal"),), "temp", "Temperature cpu_thermal"),
        (
            "sensors",
            (("chip", "k10temp-pci-00c3"), ("feature", "tctl")),
            "temp_input",
            "Temperature k10temp-pci-00c3 tctl",
        ),
        (
            "sensors",
            (("chip", "nct6798-isa-0290"), ("feature", "fan2")),
            "fan_input",
            "Fan speed nct6798-isa-0290 fan2",
        ),
        (
            "sensors",
            (("chip", "it8728-isa-0a30"), ("feature", "temp1")),
            "temp1_input",
            "Temperature it8728-isa-0a30 temp1",
        ),
        (
            "sensors",
            (("chip", "c"), ("feature", "f")),
            "temp1_alarm",
            "Sensor temp1 alarm c f",
        ),
        (
            "smart_device",
            SMART,
            "temp_c",
            "Temperature",
        ),
        (
            "smart_device",
            SMART,
            "percentage_used",
            "Life used",
        ),
        (
            "smart_device",
            SMART,
            "warning_temperature_time",
            "Warning temperature time",
        ),
        # Behind RAID controllers `device` is the same for every disk; the
        # serial identifies it (and the drive device names it).
        (
            "smart_device",
            (("device", "0"), ("device_type", "megaraid,1"), ("serial_no", "WX1234")),
            "exit_status",
            "SMART exit status",
        ),
        # Without a serial (drive in standby) the device name is used.
        (
            "smart_device",
            (("device", "sda"),),
            "exit_status",
            ("Drive SMART exit status sda"),
        ),
        (
            "smart_attribute",
            (
                ("flags", "-O--CK"),
                ("id", "194"),
                ("model", "WDC WD40EFRX"),
                ("name", "Temperature_Celsius"),
                ("serial_no", "WD-WCC7K1AB7XKL"),
            ),
            "raw_value",
            "Attribute raw value Temperature_Celsius",
        ),
        (
            "sensors",
            (("chip", "amdgpu-pci-0300"), ("feature", "ppt")),
            "power_average",
            "Power (average) amdgpu-pci-0300 ppt",
        ),
        ("mdstat", (("Name", "md0"),), "BlocksSyncedPct", "RAID sync progress md0"),
        ("reboot_needed", (), "value", "Reboot required"),
        ("mdstat", (("Name", "md0"),), "ActivityState", "RAID state md0"),
        # Unknown measurements keep the generic name.
        ("my_app", (("region", "eu"),), "requests", "my_app eu requests"),
        (
            "docker_container_cpu",
            (),
            "usage_percent",
            "docker_container_cpu usage_percent",
        ),
    ],
)
def test_entity_name(measurement: str, tags: tuple, field: str, expected: str) -> None:
    """§5.2: label, then the identifying tag values."""
    assert entity_name(measurement, tags, field) == expected


def test_user_sample_names_are_unique() -> None:
    """A real host's series get distinct names per device (§5.1, §5.2)."""
    series = [
        ("disk", DISK_ROOT, "used_percent"),
        (
            "disk",
            (("path", "/boot/efi"),),
            "used_percent",
        ),
        ("sensors", (("chip", "nvme-pci-0400"), ("feature", "sensor_1")), "temp_input"),
        ("sensors", (("chip", "nvme-pci-0500"), ("feature", "sensor_1")), "temp_input"),
        ("smart_device", SMART, "temp_c"),
        (
            "smart_device",
            (("device", "sda"), ("model", "WD"), ("serial_no", "1")),
            "temp_c",
        ),
    ]
    names = [
        (
            drive_name(s[1]) if drive_serial(s[0], s[1]) else "host",
            entity_name(*s),
        )
        for s in series
    ]
    assert len(set(names)) == len(names)


def test_every_labelled_measurement_has_a_prefix() -> None:
    """Unlabelled fields of a known measurement still get a readable name."""
    assert set(naming._LABELS) <= set(naming._PREFIX)


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (SMART, "Samsung SSD 980 PRO 1TB 593Y"),
        ((("device", "0"), ("serial_no", "WX1234")), "Drive 1234"),
        ((("model", "M"), ("serial_no", "AB")), "M AB"),
    ],
)
def test_drive_name(tags: tuple, expected: str) -> None:
    """§5.1: a drive device is named by model plus serial tail."""
    assert drive_name(tags) == expected
