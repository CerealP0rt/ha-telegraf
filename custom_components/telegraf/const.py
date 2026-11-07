"""Constants for the Telegraf integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

DOMAIN: Final = "telegraf"

CONF_INCLUDE_MEASUREMENTS: Final = "include_measurements"
CONF_EXCLUDE_MEASUREMENTS: Final = "exclude_measurements"
CONF_STALE_TIMEOUT: Final = "stale_timeout"
CONF_CONNECTIVITY_TIMEOUT: Final = "connectivity_timeout"
CONF_MAX_ENTITIES: Final = "max_entities"
CONF_LOCAL_ONLY: Final = "local_only"

DEFAULT_NAME: Final = "Telegraf"
# Generous for slow inputs (e.g. inputs.smart every 5-10 min); hosts going
# quiet are caught by the shorter Connectivity timeout (spec §5.7, §6.2).
DEFAULT_STALE_TIMEOUT: Final = 1800
DEFAULT_CONNECTIVITY_TIMEOUT: Final = 300
DEFAULT_MAX_ENTITIES: Final = 500
DEFAULT_LOCAL_ONLY: Final = True

# Per-host entity kinds (spec §5.7). Part of their unique_id: frozen.
HOST_KIND_CONNECTIVITY: Final = "connectivity"
# Per-host entities counted toward max_entities (spec §6.1).
HOST_ENTITY_COUNT: Final = 1

# Options flow bounds (spec §7.2); a new host needs room for 1 + 1 entities.
# Upper bound of both stale_timeout and connectivity_timeout.
MAX_TIMEOUT: Final = 86400
MIN_MAX_ENTITIES: Final = 1 + HOST_ENTITY_COUNT
MAX_MAX_ENTITIES: Final = 10000

# Decimals for sensors with a suggested display unit (spec §5.5).
SUGGESTED_DISPLAY_PRECISION: Final = 2

STALE_CHECK_INTERVAL: Final = timedelta(seconds=30)

STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 10

MANUFACTURER: Final = "Telegraf"


def options_with_defaults(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return the entry options with defaults for unset keys (spec §7.2)."""
    return {
        CONF_INCLUDE_MEASUREMENTS: [],
        CONF_EXCLUDE_MEASUREMENTS: [],
        CONF_STALE_TIMEOUT: DEFAULT_STALE_TIMEOUT,
        CONF_CONNECTIVITY_TIMEOUT: DEFAULT_CONNECTIVITY_TIMEOUT,
        CONF_MAX_ENTITIES: DEFAULT_MAX_ENTITIES,
        CONF_LOCAL_ONLY: DEFAULT_LOCAL_ONLY,
        **options,
    }


def signal_new_sensors(entry_id: str) -> str:
    """Dispatcher signal fired with the newly discovered sensors (SensorInfo)."""
    return f"{DOMAIN}_{entry_id}_new_sensors"


def signal_sensor_update(entry_id: str, key: str) -> str:
    """Dispatcher signal fired when one sensor's value or availability changes."""
    return f"{DOMAIN}_{entry_id}_update_{key}"


def signal_new_hosts(entry_id: str) -> str:
    """Dispatcher signal fired with a list of newly discovered hosts."""
    return f"{DOMAIN}_{entry_id}_new_hosts"


def signal_host_update(entry_id: str, host: str) -> str:
    """Dispatcher signal fired when a host's last-seen/connectivity changes."""
    return f"{DOMAIN}_{entry_id}_host_{host}"
