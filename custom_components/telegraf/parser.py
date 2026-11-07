"""Parse Telegraf JSON serializer output (spec §3).

This module must not import homeassistant so it stays trivially testable.
"""

from __future__ import annotations

import json
import logging
import math
import re
import reprlib
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "telegraf"
MAX_STATE_LENGTH = 255
MAX_NAME_LENGTH = 255
# Bounds on a series identity (spec §3.3): the tag set ends up in every
# field's key, entity name, dispatcher signal and stored record.
MAX_TAGS = 64
MAX_SERIES_LENGTH = 4096
# Same as HA's http MAX_CLIENT_SIZE (asserted in tests; not imported, to keep
# this module free of homeassistant imports).
MAX_BODY_SIZE = 16 * 1024 * 1024
MAX_INT_DIGITS = 20
INT_MIN = -(2**63)
INT_MAX = 2**64 - 1
GZIP_MAGIC = b"\x1f\x8b"

# C0 controls, DEL and C1 controls: never valid in names (spec §3.3), so
# they cannot forge log lines or hide in entity names.
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")

# Tags whose value changes at runtime for the same device (spec §3.3). Tags
# are part of a sensor's identity (§5.1), so keeping them would create a
# new set of entities on every change. Some are kept as a text field.
VOLATILE_TAGS: dict[str, frozenset[str]] = {
    # A mount is identified by its path; the rest describes it and changes
    # (device name across reboots, ro/rw remount, label, reformat).
    "disk": frozenset({"device", "fstype", "label", "mode"}),
    "smart_device": frozenset({"power", "enabled"}),
    "smart_attribute": frozenset({"power", "enabled", "fail"}),
    "mdstat": frozenset({"ActivityState", "Devices"}),
    # Set by the user for the host device's link (spec §5.1).
    "system": frozenset({"ha_configuration_url"}),
}
# Tags that only locate a device (kernel name, controller slot) and can
# change across reboots; dropped when a stable serial number identifies it.
VOLATILE_WITH_SERIAL: dict[str, frozenset[str]] = {
    "smart_device": frozenset({"device", "device_type"}),
    "smart_attribute": frozenset({"device", "device_type"}),
}
# The tag holding that serial; it makes the series a drive device (§5.1).
SERIAL_TAG = "serial_no"
TAGS_AS_FIELDS: dict[str, tuple[str, ...]] = {
    "smart_attribute": ("fail",),
    "mdstat": ("ActivityState",),
    "system": ("ha_configuration_url",),
}

_REPR = reprlib.Repr(maxstring=80, maxother=80, maxlist=4, maxdict=4, maxlevel=2)

type Value = float | int | str


class LogValue:
    """Lazily rendered, truncated repr of an untrusted value for log messages.

    Rendering only happens if the record is emitted, and always as a repr, so
    newlines cannot forge log lines and huge values cannot flood the log.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        """Wrap a value."""
        self.value = value

    def __repr__(self) -> str:
        """Return the truncated repr."""
        return _REPR.repr(self.value)

    __str__ = __repr__


class PayloadError(ValueError):
    """The request body is not a usable Telegraf JSON payload."""


class PayloadTooLargeError(PayloadError):
    """The decompressed request body exceeds the size limit."""


@dataclass(frozen=True, slots=True)
class Metric:
    """A single field of a Telegraf metric."""

    host: str
    measurement: str
    tags: tuple[tuple[str, str], ...]
    field: str
    value: Value
    # The stable sensor key (spec §5.1). Built here, during parsing in the
    # executor, rather than by the hub on the event loop.
    key: str = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Compute the key."""
        object.__setattr__(
            self, "key", make_key(self.host, self.measurement, self.tags, self.field)
        )

    @property
    def numeric(self) -> bool:
        """Return True if the value is numeric."""
        return not isinstance(self.value, str)


def make_key(
    host: str, measurement: str, tags: tuple[tuple[str, str], ...], field: str
) -> str:
    """Build the injective sensor key: a compact JSON array (spec §5.1)."""
    return json.dumps(
        [host, measurement, [list(tag) for tag in tags], field],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def series_within_limits(
    host: str, measurement: str, tags: tuple[tuple[str, str], ...]
) -> bool:
    """Return True if a series identity is small enough to track (spec §3.3)."""
    return (
        len(tags) <= MAX_TAGS
        and len(make_key(host, measurement, tags, "")) <= MAX_SERIES_LENGTH
    )


def decode_body(body: bytes, max_size: int = MAX_BODY_SIZE) -> Any:
    """Decode a (possibly gzip-compressed) UTF-8 JSON request body."""
    if body.startswith(GZIP_MAGIC):
        body = _gunzip(body, max_size)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as err:
        raise PayloadError(f"body is not UTF-8: {err}") from err
    try:
        return json.loads(text, parse_int=_parse_int)
    except (ValueError, RecursionError) as err:
        raise PayloadError(f"invalid JSON: {err}") from err


def _gunzip(body: bytes, max_size: int) -> bytes:
    """Decompress gzip with a size limit (guards against gzip bombs)."""
    decompressor = zlib.decompressobj(wbits=31)
    try:
        data = decompressor.decompress(body, max_size)
    except zlib.error as err:
        raise PayloadError(f"invalid gzip body: {err}") from err
    if decompressor.unconsumed_tail:
        raise PayloadTooLargeError(f"decompressed body exceeds {max_size} bytes")
    if not decompressor.eof:
        raise PayloadError("truncated gzip body")
    if decompressor.unused_data:
        # A second gzip member or trailing garbage would otherwise be dropped
        # silently while the request is answered 204.
        raise PayloadError("trailing data after gzip body")
    return data


class _TooLongInt:
    """Stands in for an integer literal too long to parse (spec §3.3).

    Not None: a None tag value means "absent", which would merge series.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<integer literal too long>"


TOO_LONG_INT = _TooLongInt()


def _parse_int(literal: str) -> int | _TooLongInt:
    """Parse a JSON integer; absurdly long literals become TOO_LONG_INT."""
    if len(literal.lstrip("-")) > MAX_INT_DIGITS:
        return TOO_LONG_INT
    return int(literal)


def parse_payload(body: bytes) -> list[Metric]:
    """Parse a request body into metrics.

    Raises PayloadError if the body as a whole is unusable; individual invalid
    metrics or fields are skipped.
    """
    return parse_document(decode_body(body))


def parse_document(doc: Any) -> list[Metric]:
    """Parse an already-decoded JSON document (batch or single metric)."""
    if isinstance(doc, dict) and isinstance(doc.get("metrics"), list):
        raw_metrics = doc["metrics"]
    elif isinstance(doc, dict) and "name" in doc and "fields" in doc:
        raw_metrics = [doc]
    else:
        raise PayloadError("expected an object with a 'metrics' list or a metric")

    result: list[Metric] = []
    for raw in raw_metrics:
        result.extend(_parse_metric(raw))
    return result


def valid_name(value: Any) -> bool:
    """Return True for a non-empty, bounded, UTF-8 encodable string."""
    if not isinstance(value, str) or not value or len(value) > MAX_NAME_LENGTH:
        return False
    return _encodable(value) and not _CONTROL_CHARS.search(value)


def _encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _parse_metric(raw: Any) -> list[Metric]:
    if not isinstance(raw, dict):
        _LOGGER.debug("Skipping non-object metric: %s", LogValue(raw))
        return []
    name = raw.get("name")
    fields = raw.get("fields")
    if not valid_name(name):
        _LOGGER.debug("Skipping metric with invalid name: %s", LogValue(name))
        return []
    if not isinstance(fields, dict):
        _LOGGER.debug("Skipping metric %r without fields", name)
        return []

    tags = _parse_tags(name, raw.get("tags"))
    if tags is None:
        return []
    host = tags.pop("host", DEFAULT_HOST)
    fields = _drop_volatile_tags(name, tags, fields)
    sorted_tags = tuple(sorted(tags.items()))
    if not series_within_limits(host, name, sorted_tags):
        _LOGGER.debug("Skipping metric %r with oversized tag set", name)
        return []

    metrics: list[Metric] = []
    for field, raw_value in fields.items():
        value = _convert_value(raw_value)
        if value is None or not valid_name(field):
            _LOGGER.debug(
                "Skipping field %r.%s value %s",
                name,
                LogValue(field),
                LogValue(raw_value),
            )
            continue
        metrics.append(Metric(host, name, sorted_tags, field, value))
    return metrics


def _volatile_keys(measurement: str, tag_keys: Iterable[str]) -> frozenset[str]:
    """Return the volatile tag keys for a tag set (spec §3.3)."""
    volatile = VOLATILE_TAGS.get(measurement, frozenset())
    if measurement in VOLATILE_WITH_SERIAL and SERIAL_TAG in tag_keys:
        volatile |= VOLATILE_WITH_SERIAL[measurement]
    return volatile


def _drop_volatile_tags(
    name: str, tags: dict[str, str], fields: dict[str, Any]
) -> dict[str, Any]:
    """Remove volatile tags (spec §3.3); return fields plus tags kept as fields."""
    volatile = _volatile_keys(name, tags)
    if not volatile:
        return fields
    extra = {
        key: tags[key]
        for key in TAGS_AS_FIELDS.get(name, ())
        if key in tags and key not in fields
    }
    for key in volatile:
        tags.pop(key, None)
    return {**fields, **extra} if extra else fields


def _parse_tags(name: str, raw_tags: Any) -> dict[str, str] | None:
    """Return the valid tags, or None if the whole metric must be skipped."""
    tags: dict[str, str] = {}
    if not isinstance(raw_tags, dict):
        return tags
    if len(raw_tags) > MAX_TAGS:
        _LOGGER.debug("Skipping metric %r with %d tags", name, len(raw_tags))
        return None
    for tag_key, raw_tag_value in raw_tags.items():
        if raw_tag_value is None:
            continue
        # Only scalars: str() of a (possibly deeply nested) array or object
        # could overflow the stack, and TOO_LONG_INT must not become a string.
        if not isinstance(raw_tag_value, str | int | float):
            _LOGGER.debug(
                "Skipping metric %r with invalid tag %s", name, LogValue(tag_key)
            )
            return None
        tag_value = str(raw_tag_value)
        if tag_value == "":
            continue
        if not (valid_name(tag_key) and valid_name(tag_value)):
            _LOGGER.debug(
                "Skipping metric %r with invalid tag %s", name, LogValue(tag_key)
            )
            return None
        tags[tag_key] = tag_value
    return tags


def _convert_value(value: Any) -> Value | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if INT_MIN <= value <= INT_MAX else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        value = value[:MAX_STATE_LENGTH]
        return value if _encodable(value) else None
    return None
