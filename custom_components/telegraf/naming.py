"""Entity names (spec §5.2), styled after HA's System Monitor integration.

Pure module (no homeassistant imports). A name is a readable label for the
field followed by the values of the identifying tags, e.g. ``Disk usage /``
or ``Network in eth0``. Purely descriptive tags (disk `fstype`, SMART
`model`, ...) are left out; every other tag value is kept so names stay
unique.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from .identity import drive_serial
from .parser import SERIAL_TAG

# Label prefix per known measurement, for fields without their own label.
_PREFIX: dict[str, str] = {
    "cpu": "Processor",
    "mem": "Memory",
    "swap": "Swap",
    "system": "System",
    "kernel": "Kernel",
    "disk": "Disk",
    "diskio": "Disk",
    "net": "Network",
    "temp": "Temperature",
    "sensors": "Sensor",
    "smart_device": "Drive",
    "smart_attribute": "Drive attribute",
    "mdstat": "RAID",
    "reboot_needed": "Reboot",
}

# (field pattern, label) per measurement; first match wins.
_LABELS: dict[str, tuple[tuple[str, str], ...]] = {
    "cpu": (
        ("usage_active", "Processor use"),
        ("usage_idle", "Processor idle"),
        ("usage_iowait", "Processor I/O wait"),
        ("usage_user", "Processor use (user)"),
        ("usage_system", "Processor use (system)"),
        ("usage_nice", "Processor use (nice)"),
        ("usage_irq", "Processor use (IRQ)"),
        ("usage_softirq", "Processor use (soft IRQ)"),
        ("usage_steal", "Processor steal"),
        ("usage_guest", "Processor use (guest)"),
        ("usage_guest_nice", "Processor use (guest nice)"),
    ),
    "mem": (
        ("used_percent", "Memory usage"),
        ("available_percent", "Memory available (%)"),
        ("used", "Memory use"),
        ("free", "Memory free"),
        ("available", "Memory available"),
        ("total", "Memory total"),
        # swap_* fall back to "Memory swap ...": "Swap ..." is inputs.swap.
    ),
    "swap": (
        ("used_percent", "Swap usage"),
        ("used", "Swap use"),
        ("free", "Swap free"),
        ("total", "Swap total"),
        ("in", "Swap in"),
        ("out", "Swap out"),
    ),
    "system": (
        ("load1", "Load (1 min)"),
        ("load5", "Load (5 min)"),
        ("load15", "Load (15 min)"),
        ("uptime", "Uptime"),
        ("uptime_format", "Uptime (text)"),
        ("kernel_version", "Kernel version"),
        ("n_cpus", "Logical processors"),
        ("n_physical_cpus", "Physical processors"),
        ("n_users", "Users"),
        ("n_unique_users", "Unique users"),
    ),
    "kernel": (
        ("boot_time", "Last boot"),
        ("context_switches", "Context switches"),
        ("interrupts", "Interrupts"),
        ("processes_forked", "Processes forked"),
        ("entropy_avail", "Entropy available"),
    ),
    "disk": (
        ("used_percent", "Disk usage"),
        ("used", "Disk use"),
        ("free", "Disk free"),
        ("total", "Disk total"),
        ("inodes_used_percent", "Disk inode usage"),
        ("inodes_used", "Disk inodes used"),
        ("inodes_free", "Disk inodes free"),
        ("inodes_total", "Disk inodes total"),
    ),
    "diskio": (
        ("read_bytes", "Disk read"),
        ("write_bytes", "Disk write"),
        ("reads", "Disk reads"),
        ("writes", "Disk writes"),
        ("read_time", "Disk read time"),
        ("write_time", "Disk write time"),
        ("io_time", "Disk I/O time"),
        ("io_util", "Disk utilization"),
        ("io_await", "Disk wait"),
        ("io_svctm", "Disk service time"),
        ("iops_in_progress", "Disk I/O in progress"),
    ),
    "net": (
        ("bytes_recv", "Network in"),
        ("bytes_sent", "Network out"),
        ("packets_recv", "Packets in"),
        ("packets_sent", "Packets out"),
        ("err_in", "Network errors in"),
        ("err_out", "Network errors out"),
        ("drop_in", "Network drops in"),
        ("drop_out", "Network drops out"),
        ("speed", "Link speed"),
    ),
    "temp": (("temp", "Temperature"),),
    # inputs.sensors: both `temp_input` and `temp1_input` styles (§5.4).
    "sensors": (
        ("temp_input", "Temperature"),
        ("temp[0-9]*_input", "Temperature"),
        ("temp_crit", "Temperature critical"),
        ("temp[0-9]*_crit", "Temperature critical"),
        ("temp_max", "Temperature max"),
        ("temp[0-9]*_max", "Temperature max"),
        ("fan_input", "Fan speed"),
        ("fan[0-9]*_input", "Fan speed"),
        ("in_input", "Voltage"),
        ("in[0-9]*_input", "Voltage"),
        ("curr_input", "Current"),
        ("curr[0-9]*_input", "Current"),
        ("power_input", "Power"),
        ("power[0-9]*_input", "Power"),
        ("power_average", "Power (average)"),
        ("power[0-9]*_average", "Power (average)"),
    ),
    "smart_device": (
        ("temp_c", "Drive temperature"),
        ("health_ok", "Drive health"),
        ("power_on_hours", "Drive power-on time"),
        ("power_cycle_count", "Drive power cycles"),
        ("unsafe_shutdowns", "Drive unsafe shutdowns"),
        ("available_spare", "Drive available spare"),
        ("available_spare_threshold", "Drive available spare threshold"),
        ("percentage_used", "Drive life used"),
        ("percent_lifetime_remain", "Drive life remaining"),
        ("media_errors", "Drive media errors"),
        ("error_log_entries", "Drive error log entries"),
        ("critical_warning", "Drive critical warning"),
        ("exit_status", "Drive SMART exit status"),
        ("reallocated_sectors_count", "Drive reallocated sectors"),
        ("pending_sector_count", "Drive pending sectors"),
        ("uncorrectable_sector_count", "Drive uncorrectable sectors"),
        ("udma_crc_errors", "Drive CRC errors"),
    ),
    "reboot_needed": (("value", "Reboot required"),),
    "mdstat": (
        ("ActivityState", "RAID state"),
        ("BlocksTotal", "RAID size"),
        ("BlocksSynced", "RAID synced"),
        ("BlocksSyncedPct", "RAID sync progress"),
        ("BlocksSyncedSpeed", "RAID sync speed"),
        ("BlocksSyncedFinishTime", "RAID sync time remaining"),
        ("DisksTotal", "RAID disks"),
        ("DisksActive", "RAID disks active"),
        ("DisksDown", "RAID disks down"),
        ("DisksFailed", "RAID disks failed"),
        ("DisksSpare", "RAID disks spare"),
    ),
}

# Descriptive tags left out of names (SMART: see entity_name). disk tags
# other than `path` never get here: they are volatile (parser, §3.3).
_HIDDEN_TAGS: dict[str, frozenset[str]] = {
    "smart_device": frozenset({"model", SERIAL_TAG, "wwn", "capacity"}),
    "smart_attribute": frozenset(
        {"model", SERIAL_TAG, "wwn", "capacity", "id", "flags"}
    ),
}
# Characters of the serial number shown after the model.
SERIAL_TAIL = 4
# Tag values that mean "the whole host" and add nothing to a name.
_HIDDEN_TAG_VALUES: frozenset[tuple[str, str, str]] = frozenset(
    {("cpu", "cpu", "cpu-total"), ("net", "interface", "all")}
)


def _label(measurement: str, field: str) -> str | None:
    for pattern, label in _LABELS.get(measurement, ()):
        if fnmatchcase(field, pattern):
            return label
    if (prefix := _PREFIX.get(measurement)) is not None:
        return f"{prefix} {field.replace('_', ' ')}"
    return None


def drive_name(tags: tuple[tuple[str, str], ...]) -> str:
    """Return the name of a drive device: model plus serial tail (§5.1)."""
    values = dict(tags)
    tail = values[SERIAL_TAG][-SERIAL_TAIL:]
    if model := values.get("model"):
        return f"{model} {tail}"
    return f"Drive {tail}"


def _drive_label(label: str) -> str:
    """Drop the `Drive` prefix: the drive device's name says it (§5.2)."""
    rest = label.removeprefix(_PREFIX["smart_device"] + " ")
    return rest[:1].upper() + rest[1:]


def entity_name(measurement: str, tags: tuple[tuple[str, str], ...], field: str) -> str:
    """Return the entity name of a metric field (spec §5.2)."""
    label = _label(measurement, field)
    if label is None:
        # Unknown measurement: measurement, tag values, field.
        return " ".join([measurement, *(value for _, value in tags), field])
    hidden = _HIDDEN_TAGS.get(measurement, frozenset())
    if drive_serial(measurement, tags) is not None:
        # On its own drive device (spec §5.1), which names the drive; the
        # kernel device name can change across reboots.
        hidden = hidden | {"device", "device_type"}
        label = _drive_label(label)
    values = [
        value
        for key, value in tags
        if key not in hidden and (measurement, key, value) not in _HIDDEN_TAG_VALUES
    ]
    return " ".join([label, *values])
