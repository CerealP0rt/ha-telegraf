"""Persisted identities of Telegraf entities and devices (spec §5.1, §5.7).

Everything here ends up in HA's entity and device registries, so the
formulas are frozen: changing one re-IDs existing entities (new entity_ids,
lost history). That includes the quirks — sensor keys (parser.make_key) are
compact JSON, while host unique_ids use json.dumps' default separators;
both keep non-ASCII characters (ensure_ascii=False). tests/test_identity.py
pins literal values.
"""

from __future__ import annotations

import hashlib
import json

from .const import DOMAIN
from .parser import SERIAL_TAG, VOLATILE_WITH_SERIAL

_HASH_LENGTH = 16


def _hash_id(entry_id: str, key: str) -> str:
    return f"{entry_id}_{hashlib.sha1(key.encode()).hexdigest()[:_HASH_LENGTH]}"


def unique_id_for(entry_id: str, key: str) -> str:
    """Return the unique_id of a metric sensor from its key (spec §5.1)."""
    return _hash_id(entry_id, key)


def host_unique_id(entry_id: str, host: str, kind: str) -> str:
    """Return the unique_id of a per-host entity (spec §5.7)."""
    return _hash_id(entry_id, json.dumps(["host", host, kind], ensure_ascii=False))


def device_identifier(entry_id: str, host: str) -> tuple[str, str]:
    """Return the device registry identifier of a host (spec §5.1)."""
    return (DOMAIN, f"{entry_id}_{host}")


def host_from_device_identifier(
    entry_id: str, identifier: tuple[str, str]
) -> str | None:
    """Return the host of one of this entry's device identifiers, else None."""
    domain, value = identifier
    prefix = f"{entry_id}_"
    if domain != DOMAIN or not value.startswith(prefix):
        return None
    return value.removeprefix(prefix)


def drive_serial(measurement: str, tags: tuple[tuple[str, str], ...]) -> str | None:
    """Return the serial of a SMART series with its own drive device (§5.1).

    These are the measurements whose location tags a serial replaces in
    the sensor key (parser.VOLATILE_WITH_SERIAL, §3.3).
    """
    if measurement not in VOLATILE_WITH_SERIAL:
        return None
    return dict(tags).get(SERIAL_TAG) or None


def _drive_prefix(entry_id: str) -> str:
    # `:` after the entry_id (a ULID) never matches a host identifier's `_`.
    return f"{entry_id}:drive:"


def drive_device_identifier(entry_id: str, host: str, serial: str) -> tuple[str, str]:
    """Return the device registry identifier of a drive (spec §5.1)."""
    return (
        DOMAIN,
        _drive_prefix(entry_id) + json.dumps([host, serial], ensure_ascii=False),
    )


def drive_from_device_identifier(
    entry_id: str, identifier: tuple[str, str]
) -> tuple[str, str] | None:
    """Return (host, serial) of one of this entry's drive identifiers, else None."""
    domain, value = identifier
    prefix = _drive_prefix(entry_id)
    if domain != DOMAIN or not value.startswith(prefix):
        return None
    try:
        parts = json.loads(value.removeprefix(prefix))
    except ValueError:
        return None
    if not (
        isinstance(parts, list)
        and len(parts) == 2
        and all(isinstance(part, str) for part in parts)
    ):
        return None
    return parts[0], parts[1]
