"""Unit/device class/state class heuristics and enable policy (spec §5.4-§5.6).

Pure module: values are the plain strings HA uses for the corresponding enums
(``SensorDeviceClass``/``SensorStateClass`` are StrEnums) so this file does not
import homeassistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class SensorTraits:
    """Unit, device class, state class and display unit for a sensor."""

    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    # HA converts the native `unit` value into this unit (user-changeable).
    suggested_unit: str | None = None


@dataclass(frozen=True, slots=True)
class _Rule:
    measurements: frozenset[str] | None  # None = any measurement
    field_patterns: tuple[str, ...]
    traits: SensorTraits

    def matches(self, measurement: str, field: str) -> bool:
        if self.measurements is not None and measurement not in self.measurements:
            return False
        return any(fnmatchcase(field, pattern) for pattern in self.field_patterns)


_BYTES = "B"
_KIB = "KiB"
_MIB = "MiB"
_GIB = "GiB"
_DATA_SIZE = "data_size"
_DURATION = "duration"
_UPTIME = "uptime"
_TEMPERATURE = "temperature"
_MEASUREMENT = "measurement"
_TOTAL_INCREASING = "total_increasing"

# inputs.mem fields in bytes besides rule 10's (huge_pages_free/_total are
# page counts). Large ones are shown in GiB, the rest in MiB.
_MEM_LARGE_BYTES = (
    "swap_total",
    "swap_free",
    "commit_limit",
    "committed_as",
    "vmalloc_total",
    "high_total",
    "low_total",
)
_MEM_BYTES = (
    "swap_cached",
    "dirty",
    "high_free",
    "low_free",
    "huge_page_size",
    "mapped",
    "page_tables",
    "shared",
    "slab",
    "sreclaimable",
    "sunreclaim",
    "vmalloc_chunk",
    "vmalloc_used",
    "write_back",
    "write_back_tmp",
    # collect_extended = true
    "active_anon",
    "active_file",
    "inactive_anon",
    "inactive_file",
    "unevictable",
    "percpu",
)
# inputs.smart smart_device lifetime counters (never decrease).
_SMART_COUNTERS = (
    "power_cycle_count",
    "unsafe_shutdowns",
    "media_errors",
    "error_log_entries",
    "spin_retry_count",
    "end_to_end_error",
    "uncorrectable_errors",
    "command_timeout",
    "realloc_event_count",
    "udma_crc_errors",
)

# Order matters: first match wins.
# inputs.sensors fields come as `fan_input` (remove_numbers = true, the
# default) or `fan1_input` (remove_numbers = false); both forms are matched.
_RULES: tuple[_Rule, ...] = (
    _Rule(
        frozenset({"net"}),
        ("bytes_sent", "bytes_recv"),
        SensorTraits(_BYTES, _DATA_SIZE, _TOTAL_INCREASING, _GIB),
    ),
    _Rule(
        frozenset({"net"}),
        ("packets_*", "err_*", "drop_*"),
        SensorTraits(None, None, _TOTAL_INCREASING),
    ),
    _Rule(
        frozenset({"diskio"}),
        ("read_bytes", "write_bytes"),
        SensorTraits(_BYTES, _DATA_SIZE, _TOTAL_INCREASING, _GIB),
    ),
    _Rule(
        frozenset({"diskio"}),
        ("reads", "writes", "merged_reads", "merged_writes"),
        SensorTraits(None, None, _TOTAL_INCREASING),
    ),
    _Rule(
        frozenset({"diskio"}),
        ("io_util",),
        SensorTraits("%", None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"diskio"}),
        ("io_await", "io_svctm"),
        SensorTraits("ms", _DURATION, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"net"}),
        ("speed",),
        SensorTraits("Mbit/s", "data_rate", _MEASUREMENT),
    ),
    _Rule(
        frozenset({"diskio"}),
        ("*_time",),
        SensorTraits("ms", _DURATION, _TOTAL_INCREASING),
    ),
    # Shown in days; the boot time comes from kernel.boot_time (rule 13).
    _Rule(
        frozenset({"system"}),
        ("uptime",),
        SensorTraits("s", _DURATION, _MEASUREMENT, "d"),
    ),
    # lm-sensors alarm/beep/fault flags (0/1) before the unit rules below,
    # which would otherwise give e.g. `temp1_alarm` °C (0 °C -> 32 °F).
    _Rule(
        frozenset({"sensors"}),
        ("*_alarm", "*_beep", "*_fault"),
        SensorTraits(None, None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"temp", "sensors"}),
        ("temp*",),
        SensorTraits("°C", _TEMPERATURE, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"sensors"}),
        ("fan_*", "fan[0-9]*_*"),
        SensorTraits("rpm", None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"sensors"}),
        ("in_*", "in[0-9]*_*"),
        SensorTraits("V", "voltage", _MEASUREMENT),
    ),
    _Rule(
        frozenset({"sensors"}),
        ("curr_*", "curr[0-9]*_*"),
        SensorTraits("A", "current", _MEASUREMENT),
    ),
    _Rule(
        frozenset({"sensors"}),
        ("power_*", "power[0-9]*_*"),
        SensorTraits("W", "power", _MEASUREMENT),
    ),
    # Cumulative byte counters of common inputs, before the generic
    # (measurement) byte rule.
    _Rule(
        frozenset({"docker_container_net"}),
        ("rx_bytes", "tx_bytes"),
        SensorTraits(_BYTES, _DATA_SIZE, _TOTAL_INCREASING, _GIB),
    ),
    _Rule(
        frozenset({"procstat"}),
        ("read_bytes", "write_bytes"),
        SensorTraits(_BYTES, _DATA_SIZE, _TOTAL_INCREASING, _GIB),
    ),
    # Generic byte fields before the generic percent rule, so e.g.
    # `usage_bytes` is a size, not a percentage.
    _Rule(
        None,
        ("*_bytes", "bytes_*"),
        SensorTraits(_BYTES, _DATA_SIZE, _MEASUREMENT, _MIB),
    ),
    _Rule(
        None,
        ("*_percent", "percent_*"),
        SensorTraits("%", None, _MEASUREMENT),
    ),
    # Only inputs.cpu: elsewhere (e.g. docker's usage_total, usage_system)
    # usage_* fields are nanosecond counters, not percentages.
    _Rule(
        frozenset({"cpu"}),
        ("usage_*",),
        SensorTraits("%", None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"mem", "swap", "disk"}),
        (
            "total",
            "free",
            "used",
            "available",
            "cached",
            "buffered",
            "active",
            "inactive",
            "in",
            "out",
        ),
        SensorTraits(_BYTES, _DATA_SIZE, _MEASUREMENT, _GIB),
    ),
    _Rule(
        frozenset({"mem"}),
        _MEM_LARGE_BYTES,
        SensorTraits(_BYTES, _DATA_SIZE, _MEASUREMENT, _GIB),
    ),
    _Rule(
        frozenset({"mem"}),
        _MEM_BYTES,
        SensorTraits(_BYTES, _DATA_SIZE, _MEASUREMENT, _MIB),
    ),
    # inputs.smart (smart_device).
    _Rule(
        frozenset({"smart_device"}),
        ("temp_c",),
        SensorTraits("°C", _TEMPERATURE, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"smart_device"}),
        (
            "available_spare",
            "available_spare_threshold",
            "percentage_used",
            "wear_leveling_count",
            "percent_lifetime_remain",
            "media_wearout_indicator",
        ),
        SensorTraits("%", None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"smart_device"}),
        ("power_on_hours",),
        SensorTraits("h", _DURATION, _TOTAL_INCREASING, "d"),
    ),
    _Rule(
        frozenset({"smart_device"}),
        ("warning_temperature_time", "critical_temperature_time"),
        SensorTraits("min", _DURATION, _TOTAL_INCREASING),
    ),
    _Rule(
        frozenset({"smart_device"}),
        _SMART_COUNTERS,
        SensorTraits(None, None, _TOTAL_INCREASING),
    ),
    # inputs.mdstat: blocks are 1 KiB, speed is K/sec, finish is minutes.
    _Rule(
        frozenset({"mdstat"}),
        ("BlocksTotal", "BlocksSynced"),
        SensorTraits(_KIB, _DATA_SIZE, _MEASUREMENT, _GIB),
    ),
    _Rule(
        frozenset({"mdstat"}),
        ("BlocksSyncedPct",),
        SensorTraits("%", None, _MEASUREMENT),
    ),
    _Rule(
        frozenset({"mdstat"}),
        ("BlocksSyncedSpeed",),
        SensorTraits("KiB/s", "data_rate", _MEASUREMENT, "MiB/s"),
    ),
    _Rule(
        frozenset({"mdstat"}),
        ("BlocksSyncedFinishTime",),
        SensorTraits("min", _DURATION, _MEASUREMENT),
    ),
    # inputs.kernel: boot_time is /proc/stat btime (epoch seconds).
    _Rule(
        frozenset({"kernel"}),
        ("boot_time",),
        SensorTraits(None, _UPTIME, None),
    ),
    _Rule(
        frozenset({"kernel"}),
        ("context_switches", "interrupts", "processes_forked"),
        SensorTraits(None, None, _TOTAL_INCREASING),
    ),
)

_NUMERIC_DEFAULT = SensorTraits(None, None, _MEASUREMENT)
_TEXT = SensorTraits()


def traits_for(measurement: str, field: str, numeric: bool) -> SensorTraits:
    """Return the sensor traits for a measurement field."""
    if not numeric:
        return _TEXT
    for rule in _RULES:
        if rule.matches(measurement, field):
            return rule.traits
    return _NUMERIC_DEFAULT


def boot_time(value: float) -> datetime | None:
    """Return the boot time for an epoch-seconds value (spec §5.4.1).

    Small drift (clock steps) is suppressed by HA's uptime device class.
    """
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except OverflowError, OSError, ValueError:
        return None


# Spec §5.6: a short allowlist of core sensors starts enabled. Everything
# else, including every measurement not listed here (custom inputs too), is
# created disabled; the user enables what they want to track. Per-disk and
# per-interface measurements (disk, diskio, net) are deliberately absent:
# hosts can have many of them.
ENABLED_BY_DEFAULT: dict[str, tuple[str, ...]] = {
    "cpu": ("usage_user", "usage_system"),  # cpu-total only
    "mem": ("used_percent", "available"),
    "swap": ("used_percent",),
    "system": ("load1", "load5", "load15", "kernel_version"),
    "kernel": ("boot_time",),
    "reboot_needed": ("value",),
    # Few per host, and the drive's key values (on its own device, §5.1).
    "smart_device": (
        "temp_c",
        "health_ok",
        "percentage_used",
        "percent_lifetime_remain",
    ),
    "temp": ("temp",),
    "sensors": ("temp_input", "temp[0-9]*_input"),
}


def enabled_by_default(
    measurement: str, tags: tuple[tuple[str, str], ...], field: str
) -> bool:
    """Return True only for the core sensors of spec §5.6."""
    if measurement == "cpu" and dict(tags).get("cpu", "cpu-total") != "cpu-total":
        return False  # per-core CPU stats
    return any(
        fnmatchcase(field, pattern)
        for pattern in ENABLED_BY_DEFAULT.get(measurement, ())
    )


# Text fields shown on the host device instead of as sensors (spec §5.1).
HOST_INFO_FIELDS: dict[str, frozenset[str]] = {
    "system": frozenset(
        {
            "os",
            "platform",
            "platform_family",
            "platform_version",
            "arch",
            # From the tag of the same name (parser.TAGS_AS_FIELDS).
            "ha_configuration_url",
        }
    ),
}

# Sensors with entity_category diagnostic (spec §5.6).
DIAGNOSTIC: dict[str, frozenset[str]] = {
    "system": frozenset({"kernel_version"}),
    # smartctl's exit code bitmask and the vendor's fixed spare threshold.
    "smart_device": frozenset({"exit_status", "available_spare_threshold"}),
}


@dataclass(frozen=True, slots=True)
class BinaryTraits:
    """A numeric field shown as a binary sensor (spec §5.4.2)."""

    device_class: str | None
    # True: on when the value is 0 (e.g. health_ok = 0 is a problem).
    on_when_zero: bool
    # For state names and icons from strings.json/icons.json.
    translation_key: str | None = None


_PROBLEM = "problem"
BINARY: dict[str, dict[str, BinaryTraits]] = {
    "smart_device": {
        # smartctl's overall health self-assessment (true = passed).
        "health_ok": BinaryTraits(_PROBLEM, on_when_zero=True),
        # NVMe critical warning bitmask: any bit set is a problem.
        "critical_warning": BinaryTraits(_PROBLEM, on_when_zero=False),
    },
    # The documented inputs.exec check for a pending reboot (spec §8).
    "reboot_needed": {
        "value": BinaryTraits(None, on_when_zero=False, translation_key="reboot"),
    },
}


def binary_traits(measurement: str, field: str, numeric: bool) -> BinaryTraits | None:
    """Return the binary sensor traits of a numeric field, else None (§5.4.2)."""
    if not numeric:
        return None
    return BINARY.get(measurement, {}).get(field)


def binary_is_on(traits: BinaryTraits, value: float) -> bool:
    """Return the binary sensor state for a value (spec §5.4.2)."""
    return (value == 0) is traits.on_when_zero


def is_host_info(measurement: str, field: str) -> bool:
    """Return True for a field that describes the host device (spec §5.1)."""
    return field in HOST_INFO_FIELDS.get(measurement, frozenset())


def is_diagnostic(measurement: str, field: str) -> bool:
    """Return True for a sensor in the diagnostic category (spec §5.6)."""
    return field in DIAGNOSTIC.get(measurement, frozenset())


def host_device_details(values: dict[str, str]) -> dict[str, str]:
    """Map host info fields to device registry fields (spec §5.1).

    `platform` (else `os`) is the model, `platform_version` the software
    version, `arch` the hardware version and `ha_configuration_url` (only
    http/https) the device's link; empty values are left out.
    """
    details = {
        "model": values.get("platform") or values.get("os"),
        "sw_version": values.get("platform_version"),
        "hw_version": values.get("arch"),
        "configuration_url": _http_url(values.get("ha_configuration_url")),
    }
    return {key: value for key, value in details.items() if value}


def _http_url(value: str | None) -> str | None:
    """Return value if it is an absolute http(s) URL, else None.

    Also rejects what HA's device registry would refuse with a ValueError
    (spec §5.1): an invalid port, or a host that is not valid IDNA.
    """
    if not value:
        return None
    try:
        url = urlsplit(value)
        url.port  # noqa: B018 - raises ValueError for an invalid port
        if url.scheme not in ("http", "https") or not url.hostname:
            return None
        url.hostname.encode("idna").decode("idna")
    except ValueError:  # includes UnicodeError
        return None
    return value


# Icons (spec §5.4.3), after HA's System Monitor: one per subject, as an
# icon translation key (entity.sensor.<key> in icons.json). Only for sensors
# without a device class or with a data size/rate class; others
# (temperature, duration, uptime, voltage, ...) keep HA's class icon.
_ICON_CLASSES = frozenset({None, _DATA_SIZE, "data_rate"})
# (field pattern, key) per measurement, first match wins; "*" last.
_ICONS: dict[str, tuple[tuple[str, str], ...]] = {
    "cpu": (("*", "cpu"),),
    "system": (
        ("n_users", "users"),
        ("n_unique_users", "users"),
        ("kernel_version", "kernel_version"),
        ("uptime_format", "uptime_format"),
        ("*", "cpu"),
    ),
    "kernel": (("*", "cpu"),),
    "mem": (("swap_*", "disk"), ("*", "memory")),
    "swap": (("*", "disk"),),
    "disk": (("*", "disk"),),
    "diskio": (("*", "disk"),),
    "net": (("*", "network"),),
    "sensors": (("fan*", "fan"),),
    "smart_device": (
        ("power_cycle_count", "power_cycle_count"),
        ("unsafe_shutdowns", "unsafe_shutdowns"),
        ("*", "disk"),
    ),
    "smart_attribute": (("*", "disk"),),
    "mdstat": (("*", "disk"),),
}
# Every key icon_key can return; icons.json has exactly these sensor keys.
ICON_KEYS = frozenset(key for rules in _ICONS.values() for _, key in rules)


def icon_key(measurement: str, field: str, device_class: str | None) -> str | None:
    """Return the icon translation key of a sensor, else None (spec §5.4.3)."""
    if device_class not in _ICON_CLASSES:
        return None
    for pattern, key in _ICONS.get(measurement, ()):
        if fnmatchcase(field, pattern):
            return key
    return None
