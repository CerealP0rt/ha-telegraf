"""Tests for unit heuristics (spec §5.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS

from custom_components.telegraf import units
from custom_components.telegraf.units import (
    SensorTraits,
    boot_time,
    enabled_by_default,
    traits_for,
)


@pytest.mark.parametrize(
    ("measurement", "field", "expected"),
    [
        (
            "net",
            "bytes_recv",
            SensorTraits("B", "data_size", "total_increasing", "GiB"),
        ),
        ("net", "packets_sent", SensorTraits(None, None, "total_increasing")),
        ("net", "drop_in", SensorTraits(None, None, "total_increasing")),
        (
            "diskio",
            "write_bytes",
            SensorTraits("B", "data_size", "total_increasing", "GiB"),
        ),
        ("diskio", "reads", SensorTraits(None, None, "total_increasing")),
        ("diskio", "io_util", SensorTraits("%", None, "measurement")),
        ("diskio", "io_await", SensorTraits("ms", "duration", "measurement")),
        ("diskio", "io_svctm", SensorTraits("ms", "duration", "measurement")),
        ("net", "speed", SensorTraits("Mbit/s", "data_rate", "measurement")),
        ("diskio", "io_time", SensorTraits("ms", "duration", "total_increasing")),
        ("system", "uptime", SensorTraits("s", "duration", "measurement", "d")),
        # Rules 13/13a: inputs.kernel.
        ("kernel", "boot_time", SensorTraits(None, "uptime", None)),
        ("kernel", "interrupts", SensorTraits(None, None, "total_increasing")),
        ("kernel", "context_switches", SensorTraits(None, None, "total_increasing")),
        ("kernel", "processes_forked", SensorTraits(None, None, "total_increasing")),
        ("kernel", "entropy_avail", SensorTraits(None, None, "measurement")),
        ("temp", "temp", SensorTraits("°C", "temperature", "measurement")),
        ("sensors", "temp_input", SensorTraits("°C", "temperature", "measurement")),
        ("sensors", "temp_crit", SensorTraits("°C", "temperature", "measurement")),
        ("sensors", "fan_input", SensorTraits("rpm", None, "measurement")),
        ("sensors", "in_input", SensorTraits("V", "voltage", "measurement")),
        ("sensors", "curr_input", SensorTraits("A", "current", "measurement")),
        ("sensors", "power_average", SensorTraits("W", "power", "measurement")),
        ("temp", "fan_input", SensorTraits(None, None, "measurement")),
        # remove_numbers = false
        ("sensors", "temp1_input", SensorTraits("°C", "temperature", "measurement")),
        ("sensors", "fan1_input", SensorTraits("rpm", None, "measurement")),
        ("sensors", "fan12_min", SensorTraits("rpm", None, "measurement")),
        ("sensors", "in0_input", SensorTraits("V", "voltage", "measurement")),
        ("sensors", "curr1_input", SensorTraits("A", "current", "measurement")),
        ("sensors", "power1_average", SensorTraits("W", "power", "measurement")),
        ("sensors", "intrusion0_alarm", SensorTraits(None, None, "measurement")),
        # Rule 6a: flags, not temperatures/voltages/speeds.
        ("sensors", "temp1_alarm", SensorTraits(None, None, "measurement")),
        ("sensors", "temp_crit_alarm", SensorTraits(None, None, "measurement")),
        ("sensors", "temp1_fault", SensorTraits(None, None, "measurement")),
        ("sensors", "in0_beep", SensorTraits(None, None, "measurement")),
        ("sensors", "fan1_alarm", SensorTraits(None, None, "measurement")),
        ("sensors", "curr_alarm", SensorTraits(None, None, "measurement")),
        ("sensors", "fanatic_input", SensorTraits(None, None, "measurement")),
        ("cpu", "usage_idle", SensorTraits("%", None, "measurement")),
        ("disk", "used_percent", SensorTraits("%", None, "measurement")),
        ("whatever", "percent_free", SensorTraits("%", None, "measurement")),
        ("mem", "available", SensorTraits("B", "data_size", "measurement", "GiB")),
        ("disk", "free", SensorTraits("B", "data_size", "measurement", "GiB")),
        (
            "procstat",
            "memory_bytes",
            SensorTraits("B", "data_size", "measurement", "MiB"),
        ),
        (
            "docker_container_mem",
            "usage_bytes",
            SensorTraits("B", "data_size", "measurement", "MiB"),
        ),
        # Rules 7e/7f: cumulative byte counters.
        (
            "docker_container_net",
            "rx_bytes",
            SensorTraits("B", "data_size", "total_increasing", "GiB"),
        ),
        (
            "procstat",
            "write_bytes",
            SensorTraits("B", "data_size", "total_increasing", "GiB"),
        ),
        # Rule 9a: usage_* is a percentage only for cpu.
        ("cpu", "usage_system", SensorTraits("%", None, "measurement")),
        (
            "docker_container_cpu",
            "usage_total",
            SensorTraits(None, None, "measurement"),
        ),
        (
            "docker_container_cpu",
            "usage_system",
            SensorTraits(None, None, "measurement"),
        ),
        (
            "docker_container_cpu",
            "usage_in_usermode",
            SensorTraits(None, None, "measurement"),
        ),
        (
            "docker_container_cpu",
            "usage_percent",
            SensorTraits("%", None, "measurement"),
        ),
        ("system", "load1", SensorTraits(None, None, "measurement")),
        ("disk", "inodes_used", SensorTraits(None, None, "measurement")),
        ("disk", "inodes_used_percent", SensorTraits("%", None, "measurement")),
        # Rules 10a/10b: more inputs.mem byte fields; huge pages are counts.
        ("mem", "swap_free", SensorTraits("B", "data_size", "measurement", "GiB")),
        ("mem", "swap_total", SensorTraits("B", "data_size", "measurement", "GiB")),
        ("mem", "committed_as", SensorTraits("B", "data_size", "measurement", "GiB")),
        ("mem", "dirty", SensorTraits("B", "data_size", "measurement", "MiB")),
        ("mem", "slab", SensorTraits("B", "data_size", "measurement", "MiB")),
        ("mem", "active_file", SensorTraits("B", "data_size", "measurement", "MiB")),
        ("mem", "huge_pages_free", SensorTraits(None, None, "measurement")),
        ("mem", "available_percent", SensorTraits("%", None, "measurement")),
        # Rules 11-11d: inputs.smart smart_device.
        ("smart_device", "temp_c", SensorTraits("°C", "temperature", "measurement")),
        ("smart_device", "available_spare", SensorTraits("%", None, "measurement")),
        ("smart_device", "percentage_used", SensorTraits("%", None, "measurement")),
        (
            "smart_device",
            "wear_leveling_count",
            SensorTraits("%", None, "measurement"),
        ),
        (
            "smart_device",
            "power_on_hours",
            SensorTraits("h", "duration", "total_increasing", "d"),
        ),
        (
            "smart_device",
            "critical_temperature_time",
            SensorTraits("min", "duration", "total_increasing"),
        ),
        (
            "smart_device",
            "power_cycle_count",
            SensorTraits(None, None, "total_increasing"),
        ),
        (
            "smart_device",
            "udma_crc_errors",
            SensorTraits(None, None, "total_increasing"),
        ),
        # Current counts can go down: no counter.
        (
            "smart_device",
            "pending_sector_count",
            SensorTraits(None, None, "measurement"),
        ),
        ("smart_device", "health_ok", SensorTraits(None, None, "measurement")),
        ("smart_device", "critical_warning", SensorTraits(None, None, "measurement")),
        ("smart_attribute", "temp_c", SensorTraits(None, None, "measurement")),
        # Rules 12-12c: inputs.mdstat.
        (
            "mdstat",
            "BlocksTotal",
            SensorTraits("KiB", "data_size", "measurement", "GiB"),
        ),
        (
            "mdstat",
            "BlocksSynced",
            SensorTraits("KiB", "data_size", "measurement", "GiB"),
        ),
        ("mdstat", "BlocksSyncedPct", SensorTraits("%", None, "measurement")),
        (
            "mdstat",
            "BlocksSyncedSpeed",
            SensorTraits("KiB/s", "data_rate", "measurement", "MiB/s"),
        ),
        (
            "mdstat",
            "BlocksSyncedFinishTime",
            SensorTraits("min", "duration", "measurement"),
        ),
        ("mdstat", "DisksDown", SensorTraits(None, None, "measurement")),
    ],
)
def test_numeric_rules(measurement: str, field: str, expected: SensorTraits) -> None:
    """Numeric fields map per the §5.4 table."""
    assert traits_for(measurement, field, numeric=True) == expected


def test_text_has_no_traits() -> None:
    """Text sensors get no unit and no state class."""
    assert traits_for("cpu", "usage_idle", numeric=False) == SensorTraits()


def test_rule_order_net_before_generic_bytes() -> None:
    """net bytes are counters, not the generic *_bytes measurement rule."""
    assert traits_for("net", "bytes_sent", numeric=True).state_class == (
        "total_increasing"
    )


def test_values_are_valid_ha_enum_members() -> None:
    """Every string used must be a valid HA enum value."""
    for rule in units._RULES:
        if rule.traits.device_class:
            SensorDeviceClass(rule.traits.device_class)
        if rule.traits.state_class:
            SensorStateClass(rule.traits.state_class)


# The §5.6 table, written out independently of the code so a change to
# units.ENABLED_BY_DEFAULT must be a deliberate spec change.
SPEC_ENABLED = {
    "cpu": ("usage_user", "usage_system"),
    "mem": ("used_percent", "available"),
    "swap": ("used_percent",),
    "system": ("load1", "load5", "load15", "kernel_version"),
    "kernel": ("boot_time",),
    "reboot_needed": ("value",),
    "smart_device": (
        "temp_c",
        "health_ok",
        "percentage_used",
        "percent_lifetime_remain",
    ),
    "temp": ("temp",),
    "sensors": ("temp_input", "temp[0-9]*_input"),
}


def test_allowlist_matches_spec_table() -> None:
    """§5.6: the allowlist is exactly the spec table."""
    assert units.ENABLED_BY_DEFAULT == SPEC_ENABLED


@pytest.mark.parametrize(
    ("measurement", "tags", "field", "enabled"),
    [
        ("cpu", (("cpu", "cpu-total"),), "usage_idle", False),  # 100 - busy
        ("cpu", (("cpu", "cpu-total"),), "usage_user", True),
        ("cpu", (("cpu", "cpu-total"),), "usage_system", True),
        ("cpu", (), "usage_user", True),  # no cpu tag = total
        ("cpu", (("cpu", "cpu-total"),), "usage_iowait", False),
        ("cpu", (("cpu", "cpu0"),), "usage_idle", False),  # per-core
        ("mem", (), "used_percent", True),
        ("mem", (), "available", True),
        ("mem", (), "cached", False),
        ("swap", (), "used_percent", True),
        ("swap", (), "in", False),
        # Per-disk / per-interface measurements are all off (many instances).
        ("disk", (("path", "/"),), "used_percent", False),
        ("disk", (("path", "/"),), "free", False),
        ("disk", (("path", "/"),), "inodes_used", False),
        ("diskio", (("name", "sda"),), "read_bytes", False),
        ("diskio", (("name", "sda"),), "write_bytes", False),
        ("diskio", (("name", "sda"),), "io_util", False),
        ("net", (("interface", "eth0"),), "bytes_recv", False),
        ("net", (("interface", "eth0"),), "bytes_sent", False),
        ("net", (("interface", "eth0"),), "packets_recv", False),
        ("net", (("interface", "all"),), "tcp_insegs", False),
        ("system", (), "load1", True),
        ("system", (), "load5", True),
        ("system", (), "load15", True),
        ("system", (), "uptime", False),
        ("kernel", (), "boot_time", True),
        ("kernel", (), "interrupts", False),
        ("system", (), "n_cpus", False),
        ("system", (), "uptime_format", False),
        ("temp", (("sensor", "cpu_thermal"),), "temp", True),
        ("sensors", (("feature", "core_0"),), "temp_input", True),
        ("sensors", (("feature", "temp1"),), "temp1_input", True),
        ("sensors", (("feature", "core_0"),), "temp_crit", False),
        ("sensors", (("feature", "fan1"),), "fan_input", False),
        # Anything not listed, including custom inputs, starts disabled.
        ("docker_container_cpu", (), "usage_percent", False),
        ("my_custom", (), "anything", False),
    ],
)
def test_enabled_by_default(
    measurement: str, tags: tuple, field: str, enabled: bool
) -> None:
    """§5.6: only the short allowlist starts enabled."""
    assert enabled_by_default(measurement, tags, field) is enabled


@pytest.mark.parametrize(
    ("measurement", "field", "suggested"),
    [
        ("net", "bytes_sent", "GiB"),
        ("diskio", "read_bytes", "GiB"),
        ("mem", "total", "GiB"),
        ("swap", "total", "GiB"),
        ("swap", "out", "GiB"),
        ("disk", "used", "GiB"),
        ("docker_container_mem", "usage_bytes", "MiB"),
        ("procstat", "memory_rss_bytes", "MiB"),
    ],
)
def test_suggested_units(measurement: str, field: str, suggested: str | None) -> None:
    """§5.5: suggested display units per rule."""
    assert traits_for(measurement, field, numeric=True).suggested_unit == suggested


def test_units_valid_for_device_class() -> None:
    """Every unit and suggested unit is one HA accepts for the device class."""
    uptime = [r for r in units._RULES if r.traits.device_class == "uptime"]
    assert all(r.traits == SensorTraits(None, "uptime", None) for r in uptime)
    for rule in units._RULES:
        traits = rule.traits
        if traits.device_class in (None, "uptime"):
            continue
        allowed = DEVICE_CLASS_UNITS[SensorDeviceClass(traits.device_class)]
        assert traits.unit in allowed, rule
        if traits.suggested_unit:
            assert traits.suggested_unit in allowed, rule


BOOT = datetime(2026, 11, 4, 11, 45, 25, tzinfo=UTC)
BOOT_EPOCH = 1793792725


def test_boot_time_converts_epoch_seconds() -> None:
    """§5.4.1: kernel.boot_time is epoch seconds."""
    assert boot_time(BOOT_EPOCH) == BOOT
    assert boot_time(BOOT_EPOCH + 0.9) == BOOT


@pytest.mark.parametrize("value", [0, -1, 1e300, 10**30])
def test_boot_time_invalid_value(value: float) -> None:
    """Non-positive or out-of-range values give no boot time."""
    assert boot_time(value) is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (units._MEM_LARGE_BYTES, SensorTraits("B", "data_size", "measurement", "GiB")),
        (units._MEM_BYTES, SensorTraits("B", "data_size", "measurement", "MiB")),
        (units._SMART_COUNTERS, SensorTraits(None, None, "total_increasing")),
        (
            (
                "available_spare",
                "available_spare_threshold",
                "percentage_used",
                "wear_leveling_count",
                "percent_lifetime_remain",
                "media_wearout_indicator",
            ),
            SensorTraits("%", None, "measurement"),
        ),
        (
            ("warning_temperature_time", "critical_temperature_time"),
            SensorTraits("min", "duration", "total_increasing"),
        ),
    ],
)
def test_every_listed_field_gets_its_rule(fields, expected) -> None:
    """§5.4 rules 10a-11d: every listed field maps as the table says."""
    measurement = (
        "mem"
        if fields in (units._MEM_LARGE_BYTES, units._MEM_BYTES)
        else ("smart_device")
    )
    for field in fields:
        assert traits_for(measurement, field, numeric=True) == expected, field


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            {
                "os": "linux",
                "platform": "opensuse-microos",
                "platform_family": "",
                "platform_version": "20261024",
                "arch": "x86_64",
            },
            {
                "model": "opensuse-microos",
                "sw_version": "20261024",
                "hw_version": "x86_64",
            },
        ),
        ({"os": "linux", "platform": ""}, {"model": "linux"}),
        (
            {"ha_configuration_url": "https://microos:9090"},
            {"configuration_url": "https://microos:9090"},
        ),
        ({"ha_configuration_url": "javascript:alert(1)"}, {}),
        ({"ha_configuration_url": "ftp://nas/"}, {}),
        ({"ha_configuration_url": "https://"}, {}),
        ({"ha_configuration_url": "http://[::1"}, {}),
        # Refused by HA's device registry (ValueError), spec §5.1.
        ({"ha_configuration_url": "http://nas:99999/"}, {}),
        ({"ha_configuration_url": "http://nas:abc/"}, {}),
        ({"ha_configuration_url": "http://xn--a/"}, {}),
        ({"ha_configuration_url": "http://\u00ad/"}, {}),
        (
            {"ha_configuration_url": "http://[::1]:8080/"},
            {"configuration_url": "http://[::1]:8080/"},
        ),
        (
            {"ha_configuration_url": "http://ünïcode/"},
            {"configuration_url": "http://ünïcode/"},
        ),
        ({"platform_family": "debian"}, {}),
        ({}, {}),
    ],
)
def test_host_device_details(values: dict, expected: dict) -> None:
    """§5.1: platform (else os) is the model, then sw/hw versions."""
    assert units.host_device_details(values) == expected


def test_host_info_and_diagnostic_fields() -> None:
    """§5.1/§5.6: system's OS fields go to the device; kernel is diagnostic."""
    for field in ("os", "platform", "platform_family", "platform_version", "arch"):
        assert units.is_host_info("system", field)
    assert not units.is_host_info("system", "kernel_version")
    assert not units.is_host_info("my_app", "os")
    assert units.is_diagnostic("system", "kernel_version")
    assert not units.is_diagnostic("system", "load1")


@pytest.mark.parametrize(
    ("field", "value", "on"),
    [
        ("health_ok", 1, False),
        ("health_ok", 0, True),
        ("critical_warning", 0, False),
        ("critical_warning", 4, True),
    ],
)
def test_smart_binary_fields(field: str, value: int, on: bool) -> None:
    """§5.4.2: health and critical warning are problem binary sensors."""
    traits = units.binary_traits("smart_device", field, True)
    assert traits is not None
    assert traits.device_class == "problem"
    assert units.binary_is_on(traits, value) is on


def test_binary_only_for_numeric_known_fields() -> None:
    """§5.4.2: text values and other fields stay sensors."""
    assert units.binary_traits("smart_device", "health_ok", False) is None
    assert units.binary_traits("smart_device", "temp_c", True) is None
    assert units.binary_traits("my_app", "health_ok", True) is None


@pytest.mark.parametrize(
    ("measurement", "field", "expected"),
    [
        ("cpu", "usage_user", "cpu"),
        ("system", "load1", "cpu"),
        ("system", "kernel_version", "kernel_version"),
        ("system", "n_users", "users"),
        ("system", "uptime_format", "uptime_format"),
        ("system", "uptime", None),  # duration: HA's class icon
        ("kernel", "boot_time", None),  # uptime class
        ("kernel", "context_switches", "cpu"),
        ("mem", "used_percent", "memory"),
        ("mem", "available", "memory"),  # data size: icon key set
        ("mem", "swap_free", "disk"),
        ("swap", "used_percent", "disk"),
        ("disk", "used_percent", "disk"),
        ("diskio", "read_bytes", "disk"),
        ("diskio", "io_time", None),  # duration
        ("net", "bytes_recv", "network"),
        ("net", "speed", "network"),  # data rate: icon key set
        ("sensors", "fan_input", "fan"),
        ("sensors", "fan2_input", "fan"),
        ("sensors", "temp_input", None),
        ("sensors", "in_input", None),  # voltage
        ("temp", "temp", None),
        ("smart_device", "temp_c", None),
        ("smart_device", "percentage_used", "disk"),
        ("smart_device", "power_on_hours", None),  # duration
        ("smart_device", "power_cycle_count", "power_cycle_count"),
        ("smart_device", "unsafe_shutdowns", "unsafe_shutdowns"),
        ("mdstat", "BlocksSyncedPct", "disk"),
        ("my_app", "requests", None),
    ],
)
def test_icons(measurement: str, field: str, expected: str | None) -> None:
    """§5.4.3: System Monitor style icon keys; device classes keep theirs."""
    traits = units.traits_for(measurement, field, True)
    assert units.icon_key(measurement, field, traits.device_class) == expected


def test_reboot_needed_is_binary() -> None:
    """§5.4.2: the documented exec check is a binary sensor, on for 1."""
    traits = units.binary_traits("reboot_needed", "value", True)
    assert traits is not None
    assert traits.device_class is None
    assert traits.translation_key == "reboot"
    assert units.binary_is_on(traits, 1)
    assert not units.binary_is_on(traits, 0)
