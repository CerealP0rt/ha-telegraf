"""The Telegraf integration: receive Telegraf metrics on a webhook."""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import is_cloud_connection

from .const import (
    CONF_LOCAL_ONLY,
    DOMAIN,
    options_with_defaults,
)
from .hub import (
    SensorInfo,
    TelegrafHub,
    async_delete_cap_issue,
    async_remove_host_from_store,
    async_remove_store,
    loaded_hub,
)
from .identity import (
    device_identifier,
    drive_device_identifier,
    drive_from_device_identifier,
    drive_serial,
    host_from_device_identifier,
    unique_id_for,
)
from .parser import PayloadError, PayloadTooLargeError, parse_payload

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type TelegrafConfigEntry = ConfigEntry[TelegrafHub]


async def async_setup_entry(hass: HomeAssistant, entry: TelegrafConfigEntry) -> bool:
    """Set up a Telegraf webhook receiver from a config entry."""
    hub = TelegrafHub(hass, entry)
    # Register first: until the hub starts, requests get 503 (Telegraf
    # retries) instead of HA core's 200 for unknown webhooks (spec §4.1).
    _async_register_webhook(hass, entry, hub)
    dropped = await hub.async_load()
    _async_remove_dropped(hass, entry, hub, dropped)
    entry.runtime_data = hub
    # Only once loaded, so a failed load never flushes an empty list. Runs
    # before the webhook unregistration (on_unload callbacks are LIFO).
    entry.async_on_unload(hub.async_shutdown)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hub.async_start()
    return True


@callback
def _async_register_webhook(
    hass: HomeAssistant, entry: TelegrafConfigEntry, hub: TelegrafHub
) -> None:
    """Register the entry's webhook; unregister it when the entry unloads."""

    async def _handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Parse a Telegraf POST and feed the hub (spec §4.1).

        Never raises: HA core would log the webhook id with a traceback and
        answer 200, which Telegraf treats as delivered.
        """
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            _LOGGER.debug("Rejected oversized Telegraf payload for %s", entry.title)
            return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except Exception as err:  # aiohttp payload/decoding errors
            _LOGGER.debug("Unreadable Telegraf payload for %s: %s", entry.title, err)
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        try:
            # Up to 16 MiB of JSON: parse off the event loop (spec §4.1).
            metrics = await hass.async_add_executor_job(parse_payload, body)
        except Exception as err:  # never raise into HA (spec §4.1)
            # PayloadError messages carry no payload content; for anything
            # unexpected (e.g. RecursionError) only the type is logged.
            _LOGGER.debug(
                "Rejected Telegraf payload for %s: %s",
                entry.title,
                err if isinstance(err, PayloadError) else type(err).__name__,
            )
            if isinstance(err, PayloadTooLargeError):
                return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        if not hub.running:
            # Entry is starting, unloading or reloading; let Telegraf retry.
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        hub.async_process(metrics)
        return web.Response(status=HTTPStatus.NO_CONTENT)

    async def _handle_webhook_off_cloud(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Drop Nabu Casa requests, which HA core only does for local_only (§4)."""
        if is_cloud_connection(hass):
            # 200 like HA core's local_only rejection.
            _LOGGER.warning(
                "Rejected Telegraf request through Home Assistant Cloud for %s",
                entry.title,
            )
            return web.Response(status=HTTPStatus.OK)
        return await _handle_webhook(hass, webhook_id, request)

    webhook_id = entry.data[CONF_WEBHOOK_ID]
    # local_only keeps the endpoint off Nabu Casa and non-local IPs. With the
    # option off there is no IP check, but Nabu Casa stays blocked (spec §4).
    local_only = options_with_defaults(entry.options)[CONF_LOCAL_ONLY]
    webhook.async_register(
        hass,
        DOMAIN,
        entry.title,
        webhook_id,
        _handle_webhook if local_only else _handle_webhook_off_cloud,
        local_only=local_only,
        allowed_methods=["POST"],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))


async def async_unload_entry(hass: HomeAssistant, entry: TelegrafConfigEntry) -> bool:
    """Pause the hub, then unload the platforms (spec §6.3).

    The hub pauses first so a request still in flight cannot add entities to
    a platform that is being torn down (it gets 503). Only when the platforms
    are gone do the on_unload callbacks unregister the webhook and flush the
    store; if unloading fails, the hub resumes and the webhook keeps working.
    """
    hub = entry.runtime_data
    hub.async_pause()
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hub.async_resume()
        return False
    return True


async def async_remove_entry(hass: HomeAssistant, entry: TelegrafConfigEntry) -> None:
    """Delete the persisted sensor list and any cap repair issue (§6.3, §6.1)."""
    async_delete_cap_issue(hass, entry.entry_id)
    await async_remove_store(hass, entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TelegrafConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a host or drive device from the UI (spec §6.4).

    Goes through the hub while it holds the sensor list, since its saves
    would overwrite a direct store edit; edits the store when no hub does
    (entry disabled, failed or unloaded); refuses while a hub is about to
    load or has just flushed (setup/unload in progress).
    """
    hub = loaded_hub(entry)
    if hub is None and entry.state in (
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    ):
        return False
    for identifier in device.identifiers:
        if drive := drive_from_device_identifier(entry.entry_id, identifier):
            if hub is not None:
                host_left = hub.async_remove_drive(*drive)
            else:
                host_left = await async_remove_host_from_store(
                    hass, entry.entry_id, *drive
                )
            if not host_left:
                # The host's last sensors went with the drive (cf. §6.3).
                _async_remove_host_device(hass, entry, drive[0])
            continue
        if (host := host_from_device_identifier(entry.entry_id, identifier)) is None:
            continue
        if hub is not None:
            hub.async_remove_host(host)
        else:
            await async_remove_host_from_store(hass, entry.entry_id, host)
        _async_remove_drive_devices(hass, entry, host)
    return True


@callback
def _async_remove_host_device(
    hass: HomeAssistant, entry: TelegrafConfigEntry, host: str
) -> None:
    """Remove a host device that has no sensors left (spec §6.4)."""
    dev_reg = dr.async_get(hass)
    if device := dev_reg.async_get_device_by_identifier(
        device_identifier(entry.entry_id, host), entry.entry_id
    ):
        dev_reg.async_remove_device(device.id)


@callback
def _async_remove_drive_devices(
    hass: HomeAssistant, entry: TelegrafConfigEntry, host: str
) -> None:
    """Remove the drive devices of a removed host (spec §6.4)."""
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if any(
            (drive := drive_from_device_identifier(entry.entry_id, identifier))
            and drive[0] == host
            for identifier in device.identifiers
        ):
            dev_reg.async_remove_device(device.id)


@callback
def _async_remove_dropped(
    hass: HomeAssistant,
    entry: TelegrafConfigEntry,
    hub: TelegrafHub,
    dropped: list[SensorInfo],
) -> None:
    """Remove registry entries of dropped sensors (§7.2, §6.3) and empty hosts."""
    if not dropped:
        return
    ent_reg = er.async_get(hass)
    for info in dropped:
        # Metric fields are sensors or binary sensors (spec §5.4.2).
        for platform in PLATFORMS:
            if entity_id := ent_reg.async_get_entity_id(
                platform, DOMAIN, unique_id_for(entry.entry_id, info.key)
            ):
                ent_reg.async_remove(entity_id)

    dev_reg = dr.async_get(hass)
    remaining = {
        identifier
        for info in hub.sensors.values()
        for identifier in _device_identifiers(entry.entry_id, info)
    }
    for identifier in {
        identifier
        for info in dropped
        for identifier in _device_identifiers(entry.entry_id, info)
    } - remaining:
        if device := dev_reg.async_get_device_by_identifier(identifier, entry.entry_id):
            dev_reg.async_remove_device(device.id)


def _device_identifiers(entry_id: str, info: SensorInfo) -> list[tuple[str, str]]:
    """Return the devices a sensor needs: its host's, and its drive's (§5.1)."""
    identifiers = [device_identifier(entry_id, info.host)]
    if (serial := drive_serial(info.measurement, info.tags)) is not None:
        identifiers.append(drive_device_identifier(entry_id, info.host, serial))
    return identifiers
