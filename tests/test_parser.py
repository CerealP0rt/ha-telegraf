"""Tests for the Telegraf JSON parser (spec §3, §5.1)."""

from __future__ import annotations

import gzip
import json
import logging

import pytest

from custom_components.telegraf.parser import (
    DEFAULT_HOST,
    MAX_BODY_SIZE,
    MAX_SERIES_LENGTH,
    MAX_TAGS,
    Metric,
    PayloadError,
    PayloadTooLargeError,
    decode_body,
    make_key,
    parse_payload,
)

from .common import metric


def _body(doc) -> bytes:
    return json.dumps(doc).encode()


def test_batch_fixture(batch_body: bytes) -> None:
    """§3.1: the batch form yields one Metric per field."""
    metrics = parse_payload(batch_body)
    assert len(metrics) == 3 + 3 + 3 + 3 + 4 + 1
    cpu = next(m for m in metrics if m.field == "usage_idle")
    assert cpu == Metric("nas", "cpu", (("cpu", "cpu-total"),), "usage_idle", 97.1)
    assert cpu.numeric


def test_single_metric_form() -> None:
    """§3.2: a bare metric object is a batch of one."""
    metrics = parse_payload(
        _body({"name": "system", "tags": {"host": "h"}, "fields": {"load1": 1}})
    )
    assert metrics == [Metric("h", "system", (), "load1", 1)]


def test_gzip_body(batch_body: bytes) -> None:
    """§3.4: gzip bodies are decompressed."""
    assert parse_payload(gzip.compress(batch_body)) == parse_payload(batch_body)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe\x00",
        b"\x1f\x8bbroken gzip",
        b"[]",
        b'"string"',
        b'{"foo": 1}',
        b'{"metrics": {"not": "a list"}}',
    ],
)
def test_invalid_payloads(body: bytes) -> None:
    """§4.1: unusable documents raise PayloadError."""
    with pytest.raises(PayloadError):
        parse_payload(body)


def test_host_defaults_and_is_removed_from_tags() -> None:
    """§3.3: missing/empty host falls back; host is not part of tags."""
    metrics = parse_payload(
        _body(
            {
                "metrics": [
                    {"name": "a", "fields": {"x": 1}},
                    {"name": "b", "tags": {"host": ""}, "fields": {"x": 1}},
                    {
                        "name": "c",
                        "tags": {"host": "h", "z": "1", "a": "2"},
                        "fields": {"x": 1},
                    },
                ]
            }
        )
    )
    assert [m.host for m in metrics] == [DEFAULT_HOST, DEFAULT_HOST, "h"]
    assert metrics[2].tags == (("a", "2"), ("z", "1"))


def test_value_conversion() -> None:
    """§3.3: value type table."""
    fields = {
        "int": 3,
        "float": 1.5,
        "true": True,
        "false": False,
        "text": "x" * 300,
        "null": None,
        "obj": {"a": 1},
        "arr": [1],
    }
    metrics = {
        m.field: m for m in parse_payload(_body({"name": "m", "fields": fields}))
    }
    assert metrics["int"].value == 3
    assert metrics["float"].value == 1.5
    assert metrics["true"].value == 1
    assert metrics["false"].value == 0
    assert metrics["true"].numeric
    assert metrics["text"].value == "x" * 255
    assert not metrics["text"].numeric
    assert set(metrics) == {"int", "float", "true", "false", "text"}


def test_non_finite_floats_skipped() -> None:
    """§3.3: NaN/Inf (allowed by Python's json) are skipped."""
    body = b'{"name": "m", "fields": {"a": NaN, "b": Infinity, "c": 1.0}}'
    assert [m.field for m in parse_payload(body)] == ["c"]


def test_invalid_metrics_skipped_rest_kept() -> None:
    """§3.3: invalid metrics in a batch are skipped individually."""
    doc = {
        "metrics": [
            "nope",
            {"fields": {"x": 1}},
            {"name": "", "fields": {"x": 1}},
            {"name": "nofields"},
            {"name": "ok", "fields": {"x": 1}},
        ]
    }
    assert [m.measurement for m in parse_payload(_body(doc))] == ["ok"]


def test_key_format() -> None:
    """§5.1: key is a compact JSON array with sorted tags."""
    assert make_key("h", "disk", (("device", "sda"), ("path", "/")), "used") == (
        '["h","disk",[["device","sda"],["path","/"]],"used"]'
    )
    m = Metric("h", "system", (), "load1", 1)
    assert m.key == '["h","system",[],"load1"]'


def test_key_is_injective() -> None:
    """§5.1: separators inside values cannot make distinct series collide."""
    a = parse_payload(_body({"name": "m", "tags": {"a": "1,b=2"}, "fields": {"f": 1}}))
    b = parse_payload(
        _body({"name": "m", "tags": {"a": "1", "b": "2"}, "fields": {"f": 1}})
    )
    c = parse_payload(
        _body({"name": "y", "tags": {"host": "x|cpu"}, "fields": {"f": 1}})
    )
    d = parse_payload(
        _body({"name": "cpu|y", "tags": {"host": "x"}, "fields": {"f": 1}})
    )
    assert len({a[0].key, b[0].key, c[0].key, d[0].key}) == 4


def test_gzip_bomb_rejected() -> None:
    """§3.4: decompressed size is capped."""
    bomb = gzip.compress(b"[" + b" " * 2_000_000 + b"]")
    with pytest.raises(PayloadTooLargeError, match="exceeds"):
        decode_body(bomb, max_size=1_000_000)
    with pytest.raises(PayloadError, match="truncated"):
        decode_body(gzip.compress(b"{}")[:-8])


@pytest.mark.parametrize(
    "body",
    [
        gzip.compress(b'{"metrics": []}') + gzip.compress(b'{"metrics": []}'),
        gzip.compress(b'{"metrics": []}') + b"garbage",
    ],
    ids=["second-member", "trailing-garbage"],
)
def test_gzip_trailing_data_rejected(body: bytes) -> None:
    """§3.4: data after the gzip member is an error, not silently dropped."""
    with pytest.raises(PayloadError, match="trailing"):
        decode_body(body)


def test_max_body_size_matches_home_assistant() -> None:
    """§3.4: the fallback gzip cap equals HA's request size limit."""
    from homeassistant.components.http.server import MAX_CLIENT_SIZE  # noqa: PLC0415

    assert MAX_BODY_SIZE == MAX_CLIENT_SIZE


@pytest.mark.parametrize(
    "body",
    [
        "{}".encode("utf-16"),
        b"[" * 200_000 + b"]" * 200_000,
    ],
)
def test_non_utf8_and_deep_nesting_rejected(body: bytes) -> None:
    """§4.1: non-UTF-8 and pathological nesting are payload errors."""
    with pytest.raises(PayloadError):
        parse_payload(body)


def test_lone_surrogates_skipped() -> None:
    """§3.3: strings that are not valid Unicode are skipped, rest kept."""
    body = (
        b'{"metrics": ['
        b'{"name": "cpu", "tags": {"host": "nas"},'
        b' "fields": {"\\ud800x": 1.0, "ok": 2.0, "txt": "\\udc00"}},'
        b'{"name": "\\ud800", "fields": {"x": 1}},'
        b'{"name": "m", "tags": {"t": "\\ud800"}, "fields": {"x": 1}}'
        b"]}"
    )
    assert [(m.measurement, m.field) for m in parse_payload(body)] == [("cpu", "ok")]


def test_integer_bounds() -> None:
    """§3.3: ints outside int64/uint64 or absurdly long literals are skipped."""
    body = (
        b'{"name": "m", "fields": {"max": 18446744073709551615,'
        b' "min": -9223372036854775808, "big": 18446744073709551616,'
        b' "huge": ' + b"9" * 5000 + b"}}"
    )
    values = {m.field: m.value for m in parse_payload(body)}
    assert values == {"max": 2**64 - 1, "min": -(2**63)}


def test_overlong_integer_tag_skips_metric() -> None:
    """§3.3: an unparseable int tag is invalid, not absent (no series merge)."""
    long_a, long_b = b"1" * 30, b"9" * 30
    body = (
        b'{"metrics": ['
        b'{"name": "m", "tags": {"id": ' + long_a + b'}, "fields": {"f": 1}},'
        b'{"name": "m", "tags": {"id": ' + long_b + b'}, "fields": {"f": 2}},'
        b'{"name": "m", "tags": {"id": 7}, "fields": {"f": 3}}]}'
    )
    assert parse_payload(body) == [Metric(DEFAULT_HOST, "m", (("id", "7"),), "f", 3)]


def test_overlong_names_and_empty_tags() -> None:
    """§3.3: names over 255 chars skip; empty tag values are dropped."""
    long = "x" * 256
    doc = {
        "metrics": [
            {"name": long, "fields": {"a": 1}},
            {"name": "m", "tags": {"t": long}, "fields": {"a": 1}},
            {"name": "m", "fields": {long: 1, "ok": 1}},
            {"name": "d", "tags": {"path": "", "dev": "sda"}, "fields": {"u": 1}},
        ]
    }
    metrics = parse_payload(_body(doc))
    assert [(m.measurement, m.field) for m in metrics] == [("m", "ok"), ("d", "u")]
    assert metrics[1].tags == (("dev", "sda"),)


def test_non_string_tag_values_converted() -> None:
    """§3.3: non-string tag values are str()-converted; null dropped."""
    doc = {"name": "m", "tags": {"host": "h", "id": 5, "n": None}, "fields": {"x": 1}}
    assert parse_payload(_body(doc))[0].tags == (("id", "5"),)


def test_tag_count_and_series_size_bounded() -> None:
    """§3.3: too many tags or an oversized tag set skips the metric."""

    def metric(tags: dict) -> dict:
        return {"name": "m", "tags": tags, "fields": {"f": 1}}

    at_limit = {"host": "h", **{f"t{i}": "v" for i in range(MAX_TAGS - 1)}}
    over_limit = {**at_limit, "extra": "v"}
    # Null tags still count: the bound is on what was sent.
    over_with_null = {**at_limit, "extra": None}
    long_values = {f"t{i}": "x" * 255 for i in range(MAX_SERIES_LENGTH // 255)}
    metrics = parse_payload(
        _body(
            {
                "metrics": [
                    metric(at_limit),
                    metric(over_limit),
                    metric(over_with_null),
                    metric(long_values),
                    metric({"host": "ok"}),
                ]
            }
        )
    )
    assert [(m.host, len(m.tags)) for m in metrics] == [("h", MAX_TAGS - 1), ("ok", 0)]


def test_control_characters_invalid_in_names() -> None:
    """§3.3: C0/DEL/C1 control characters make a name invalid; values may have them."""

    def metric(name: str, tags: dict, field: str) -> dict:
        return {"name": name, "tags": tags, "fields": {field: "a\nb"}}

    metrics = parse_payload(
        _body(
            {
                "metrics": [
                    metric("bad\nname", {}, "f"),
                    metric("m", {"t": "bad\x85value"}, "f"),
                    metric("m", {"bad\x00key": "v"}, "f"),
                    metric("m", {}, "bad\x7ffield"),
                    metric("ok", {}, "f"),
                ]
            }
        )
    )
    assert metrics == [Metric(DEFAULT_HOST, "ok", (), "f", "a\nb")]


def test_untrusted_values_logged_truncated(caplog: pytest.LogCaptureFixture) -> None:
    """§3.3: skipped input is logged as a short repr on a single line."""
    caplog.set_level(logging.DEBUG, logger="custom_components.telegraf.parser")
    payload = {
        "metrics": [
            {"name": "evil\nFAKE LOG LINE" + "x" * 10_000, "fields": {"f": 1}},
            {"name": "m", "fields": {"f\nFAKE": [1] * 10_000}},
        ]
    }
    assert parse_payload(_body(payload)) == []
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    for message in messages:
        assert "\n" not in message
        assert len(message) < 300


@pytest.mark.parametrize(
    "tag_value",
    [b"[1]", b'{"a": 1}', b"[" * 60_000 + b"]" * 60_000, b"[" + b"1" * 30 + b"]"],
    ids=["array", "object", "deeply-nested", "array-with-long-int"],
)
def test_non_scalar_tag_value_skips_metric(tag_value: bytes) -> None:
    """§3.3: array/object tag values are invalid (no str() of nested data)."""
    body = (
        b'{"metrics": [{"name": "m", "tags": {"t": ' + tag_value + b"}, "
        b'"fields": {"f": 1}}, {"name": "ok", "fields": {"f": 1}}]}'
    )
    assert [m.measurement for m in parse_payload(body)] == ["ok"]


def test_volatile_smart_tags_removed() -> None:
    """§3.3: smart power/enabled tags are not part of the identity."""
    stable = {"device": "sda", "model": "WD Red", "serial_no": "S1"}
    active = metric(
        "smart_device", {"x": 1}, **{**stable, "power": "ACTIVE", "enabled": "Enabled"}
    )
    standby = metric("smart_device", {"x": 1}, **{**stable, "power": "STANDBY"})
    [a] = parse_payload(_body(active))
    [b] = parse_payload(_body(standby))
    # `device` too: the serial identifies the drive.
    assert a.tags == (("model", "WD Red"), ("serial_no", "S1"))
    assert a.key == b.key
    [attr] = parse_payload(
        _body(
            metric("smart_attribute", {"x": 1}, **{"name": "Spin_Up", "power": "IDLE"})
        )
    )
    assert attr.tags == (("name", "Spin_Up"),)


def test_volatile_mdstat_tags_removed_activity_state_kept_as_field() -> None:
    """§3.3: mdstat ActivityState becomes a text field, Devices is dropped."""
    tags = {"Name": "md0", "ActivityState": "checking", "Devices": "sda1,sdb1"}
    metrics = parse_payload(_body(metric("mdstat", {"DisksDown": 0}, **tags)))
    assert {m.tags for m in metrics} == {(("Name", "md0"),)}
    assert {m.field: m.value for m in metrics} == {
        "DisksDown": 0,
        "ActivityState": "checking",
    }


def test_volatile_tag_field_does_not_override_real_field() -> None:
    """§3.3: an existing field with the tag's name wins."""
    doc = metric(
        "mdstat", {"ActivityState": 1}, **{"Name": "md0", "ActivityState": "x"}
    )
    [m] = parse_payload(_body(doc))
    assert (m.field, m.value) == ("ActivityState", 1)


def test_volatile_tags_kept_for_other_measurements() -> None:
    """§3.3: the same tag names stay in the identity elsewhere."""
    [m] = parse_payload(_body(metric("ups", {"x": 1}, **{"power": "on"})))
    assert m.tags == (("power", "on"),)


def test_smart_attribute_fail_tag_kept_as_field() -> None:
    """§3.3: WHEN_FAILED changing to FAILING_NOW keeps the sensors' identity."""
    tags = {"device": "sda", "name": "Reallocated_Sector_Ct", "power": "IDLE"}
    ok = parse_payload(
        _body(metric("smart_attribute", {"value": 100}, **{**tags, "fail": "-"}))
    )
    failing = parse_payload(
        _body(
            metric("smart_attribute", {"value": 5}, **{**tags, "fail": "FAILING_NOW"})
        )
    )
    assert {m.field: m.value for m in failing} == {"value": 5, "fail": "FAILING_NOW"}
    assert {m.key for m in ok} == {m.key for m in failing}
    assert failing[0].tags == (("device", "sda"), ("name", "Reallocated_Sector_Ct"))


def test_smart_device_name_dropped_when_serial_identifies_drive() -> None:
    """§3.3: sda -> sdb across reboots keeps the drive's identity."""
    tags = {"model": "WD Red", "serial_no": "WD-1"}
    [a] = parse_payload(
        _body(metric("smart_device", {"x": 1}, **{**tags, "device": "sda"}))
    )
    [b] = parse_payload(
        _body(
            metric(
                "smart_device",
                {"x": 1},
                **{**tags, "device": "sdb", "device_type": "sat"},
            )
        )
    )
    assert a.key == b.key
    assert a.tags == (("model", "WD Red"), ("serial_no", "WD-1"))


def test_smart_device_name_kept_without_serial() -> None:
    """§3.3: a drive in standby reports no serial; `device` identifies it."""
    [m] = parse_payload(_body(metric("smart_device", {"x": 1}, **{"device": "sda"})))
    assert m.tags == (("device", "sda"),)


def test_disk_identified_by_mount_point() -> None:
    """§3.3: device rename, remount ro/rw or new label keep the mount's key."""
    a = metric(
        "disk",
        {"used": 1},
        **{"device": "sdb1", "fstype": "ext4", "mode": "rw", "path": "/mnt/usb"},
    )
    b = metric(
        "disk",
        {"used": 1},
        **{
            "device": "sdc1",
            "fstype": "ext4",
            "label": "USB",
            "mode": "ro",
            "path": "/mnt/usb",
        },
    )
    [ma] = parse_payload(_body(a))
    [mb] = parse_payload(_body(b))
    assert ma.tags == (("path", "/mnt/usb"),)
    assert ma.key == mb.key


def test_configuration_url_tag_becomes_field() -> None:
    """§3.3/§5.1: system's ha_configuration_url is not part of the identity."""
    tags = {"ha_configuration_url": "https://nas:9090"}
    metrics = parse_payload(_body(metric("system", {"load1": 0.5}, **tags)))
    assert {m.tags for m in metrics} == {()}
    assert {m.field: m.value for m in metrics} == {
        "load1": 0.5,
        "ha_configuration_url": "https://nas:9090",
    }
    # On other measurements it is an ordinary tag.
    [m] = parse_payload(_body(metric("my_app", {"x": 1}, **tags)))
    assert m.tags == (("ha_configuration_url", "https://nas:9090"),)
