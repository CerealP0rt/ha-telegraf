"""Tests for the webhook endpoint and its handler (spec §3, §4, §4.1)."""

from __future__ import annotations

import asyncio
import gzip
import logging
import threading
from http import HTTPStatus
from unittest.mock import patch

import pytest
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.telegraf.const import (
    CONF_LOCAL_ONLY,
    CONF_MAX_ENTITIES,
    DOMAIN,
)
from custom_components.telegraf.hub import (
    TelegrafHub,
)
from custom_components.telegraf.parser import parse_payload

from .common import (
    CPU_USER,
    WEBHOOK_ID,
    add_entry,
    metric,
    metric_sensors,
    post,
    setup_entry,
)


async def test_webhook_registered_local_only(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """§4: the webhook is registered local_only and POST-only."""
    with patch.object(webhook, "async_register", wraps=webhook.async_register) as reg:
        await setup_entry(hass, entry)
    reg.assert_called_once()
    assert reg.call_args.kwargs["local_only"] is True
    assert reg.call_args.kwargs["allowed_methods"] == ["POST"]


async def test_single_metric_and_gzip(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    batch_body: bytes,
) -> None:
    """§3.2/§3.4: single-metric form and gzip bodies are accepted."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert (
        await post(
            client,
            gzip.compress(batch_body),
            {"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        == HTTPStatus.NO_CONTENT
    )
    # Gzip without the header: handler-side fallback.
    assert await post(client, gzip.compress(batch_body)) == HTTPStatus.NO_CONTENT
    assert (
        await post(client, metric("cpu", {"usage_user": 5.0}, cpu="cpu-total"))
        == HTTPStatus.NO_CONTENT
    )
    await hass.async_block_till_done()
    assert hass.states.get(CPU_USER).state == "5.0"


async def test_payload_parsed_off_event_loop(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    batch_body: bytes,
) -> None:
    """§4.1: parsing (up to 16 MiB) runs in the executor, not the event loop."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    threads = []

    def _parse(body: bytes):
        threads.append(threading.current_thread())
        return parse_payload(body)

    with patch("custom_components.telegraf.parse_payload", _parse):
        assert await post(client, batch_body) == HTTPStatus.NO_CONTENT
    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()


@pytest.mark.parametrize("body", [b"garbage", b"[]", b'{"foo": 1}'])
async def test_bad_payload_400(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    body: bytes,
) -> None:
    """§4.1: unusable bodies get 400 and create nothing."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    assert await post(client, body) == HTTPStatus.BAD_REQUEST
    assert metric_sensors(hass) == []


async def test_partially_invalid_batch_204(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§4.1: invalid metrics are skipped, the rest processed, 204 returned."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    body = {"metrics": ["bad", {"name": "x"}, metric("system", {"load1": 1.0})]}
    assert await post(client, body) == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert metric_sensors(hass) == ["sensor.nas_load_1_min"]


async def test_cloud_request_rejected(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4: requests through HA Cloud are dropped by local_only."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    with patch(
        "homeassistant.components.webhook.is_cloud_connection", return_value=True
    ):
        assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.OK
    await hass.async_block_till_done()
    assert metric_sensors(hass) == []
    assert f"Received remote request for local webhook {WEBHOOK_ID}" in caplog.text


async def test_non_local_ip_rejected(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4: requests from non-local addresses are dropped by local_only."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    with patch(
        "homeassistant.components.webhook.network_util.is_local", return_value=False
    ):
        assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.OK
    await hass.async_block_till_done()
    assert metric_sensors(hass) == []
    assert f"Received remote request for local webhook {WEBHOOK_ID}" in caplog.text


async def test_local_only_off_registers_without_ip_check(
    hass: HomeAssistant,
) -> None:
    """§4, §7.2: with the option off, HA core's local-only check is skipped."""
    entry = add_entry(hass, {CONF_LOCAL_ONLY: False})
    with patch.object(webhook, "async_register", wraps=webhook.async_register) as reg:
        await setup_entry(hass, entry)
    assert reg.call_args.kwargs["local_only"] is False


async def test_local_only_off_accepts_non_local_ip(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4: with the option off, a global source address is processed."""
    entry = add_entry(hass, {CONF_LOCAL_ONLY: False})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    with patch(
        "homeassistant.components.webhook.network_util.is_local", return_value=False
    ):
        assert (
            await post(client, metric("system", {"load1": 1.0}))
            == HTTPStatus.NO_CONTENT
        )
    await hass.async_block_till_done()
    assert metric_sensors(hass) == ["sensor.nas_load_1_min"]
    assert "Received remote request" not in caplog.text


async def test_local_only_off_still_rejects_cloud(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4: with the option off, requests through HA Cloud are still dropped."""
    entry = add_entry(hass, {CONF_LOCAL_ONLY: False})
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    with patch("custom_components.telegraf.is_cloud_connection", return_value=True):
        assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.OK
    await hass.async_block_till_done()
    assert metric_sensors(hass) == []
    ours = [r for r in caplog.records if r.name == "custom_components.telegraf"]
    assert [r.getMessage() for r in ours] == [
        "Rejected Telegraf request through Home Assistant Cloud for Telegraf"
    ]


async def test_broken_gzip_with_header_400(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4.1: aiohttp decompression errors become 400, not a logged 200."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    status = await post(
        client, b"\x1f\x8bnot really gzip", {"Content-Encoding": "gzip"}
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert "Error processing webhook" not in caplog.text


async def test_oversized_body_413(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4.1: bodies over HA's 16 MiB limit get 413 without a traceback."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    body = b'{"metrics": [], "pad": "' + b"x" * (17 * 1024 * 1024) + b'"}'
    assert await post(client, body) == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert "Error processing webhook" not in caplog.text


@pytest.mark.parametrize(
    "headers", [{"Content-Encoding": "gzip"}, {}], ids=["aiohttp", "fallback"]
)
async def test_oversized_gzip_413(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    headers: dict[str, str],
) -> None:
    """§3.4/§4.1: gzip expanding past 16 MiB is 413 with or without the header."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    body = gzip.compress(
        b'{"metrics": [], "pad": "' + b"x" * (17 * 1024 * 1024) + b'"}'
    )
    assert await post(client, body, headers) == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert "Error processing webhook" not in caplog.text


async def test_unexpected_parser_error_400(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4.1: even an unexpected parser exception is a 400, never raised to HA."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    with patch("custom_components.telegraf.parse_payload", side_effect=RecursionError):
        status = await post(client, metric("system", {"load1": 1.0}))
    assert status == HTTPStatus.BAD_REQUEST
    assert "Error processing webhook" not in caplog.text


async def test_deep_nesting_400(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§4.1: pathological nesting is a 400, not a RecursionError traceback."""
    await setup_entry(hass, entry)
    client = await hass_client_no_auth()
    body = b"[" * 200_000 + b"]" * 200_000
    assert await post(client, body) == HTTPStatus.BAD_REQUEST


async def test_webhook_503_until_started(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """§4.1: during setup the webhook already exists and answers 503."""
    assert await async_setup_component(hass, "webhook", {})
    client = await hass_client_no_auth()
    loading, release = asyncio.Event(), asyncio.Event()
    load = TelegrafHub.async_load

    async def _slow_load(self):
        loading.set()
        await release.wait()
        return await load(self)

    with patch.object(TelegrafHub, "async_load", _slow_load):
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        await loading.wait()
        status = await post(client, metric("system", {"load1": 1.0}))
        assert status == HTTPStatus.SERVICE_UNAVAILABLE
        release.set()
        assert await setup
    await hass.async_block_till_done()
    assert await post(client, metric("system", {"load1": 1.0})) == HTTPStatus.NO_CONTENT


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_integration_never_logs_webhook_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    batch_body: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4: no log record of the integration contains the webhook id."""
    caplog.set_level(logging.DEBUG, logger="custom_components.telegraf")
    assert await async_setup_component(hass, "config", {})
    entry = add_entry(hass, {CONF_MAX_ENTITIES: 5})
    await setup_entry(hass, entry)
    ws = await hass_ws_client(hass)
    for body, headers in (
        (batch_body, {}),  # also hits the cap
        (gzip.compress(batch_body), {}),
        (b"garbage", {}),
        (b"\x1f\x8bbroken", {}),
        (b"\x1f\x8bbroken", {"Content-Encoding": "gzip"}),
        (gzip.compress(b" " * (17 * 1024 * 1024)), {}),
        ({"metrics": ["bad", {"name": "bad\nname", "fields": {"f": 1}}]}, {}),
        (metric("system", {"load1": "text"}), {}),
    ):
        # Fresh connection each time: the server may close it after an error.
        await post(await hass_client_no_auth(), body, headers)
    await hass.async_block_till_done()
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_nas"), entry.entry_id
    )
    await ws.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    assert (await ws.receive_json())["success"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    ours = [
        r for r in caplog.records if r.name.startswith("custom_components.telegraf")
    ]
    assert ours, "expected the integration to log something"
    assert not [r for r in ours if WEBHOOK_ID in r.getMessage()]
