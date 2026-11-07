"""Config flow for Telegraf (spec §7)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME, CONF_WEBHOOK_ID, SERVER_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_CONNECTIVITY_TIMEOUT,
    CONF_EXCLUDE_MEASUREMENTS,
    CONF_INCLUDE_MEASUREMENTS,
    CONF_LOCAL_ONLY,
    CONF_MAX_ENTITIES,
    CONF_STALE_TIMEOUT,
    DEFAULT_CONNECTIVITY_TIMEOUT,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_MAX_ENTITIES,
    DEFAULT_NAME,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
    MAX_MAX_ENTITIES,
    MAX_TIMEOUT,
    MIN_MAX_ENTITIES,
    options_with_defaults,
)

# Canonical Telegraf block (spec §8); README and spec must match it (tested).
TELEGRAF_SNIPPET = """[[outputs.http]]
  url = "{url}"
  method = "POST"
  data_format = "json"
  content_encoding = "gzip"
  ## 400/413 will never succeed; retry only 503 (Telegraf 1.29+)
  non_retryable_statuscodes = [400, 413]
  [outputs.http.headers]
    Content-Type = "application/json\""""


@callback
def async_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Return the LAN URL of the webhook (local_only, spec §7.1)."""
    try:
        return webhook.async_generate_url(
            hass, webhook_id, allow_external=False, allow_ip=True
        )
    except NoURLAvailableError:
        return (
            f"http://<home-assistant-ip>:{SERVER_PORT}"
            f"{webhook.async_generate_path(webhook_id)}"
        )


@callback
def _async_placeholders(hass: HomeAssistant, webhook_id: str) -> dict[str, str]:
    url = async_webhook_url(hass, webhook_id)
    return {"webhook_url": url, "telegraf_config": TELEGRAF_SNIPPET.format(url=url)}


class TelegrafConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a Telegraf webhook receiver."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a name, then generate the webhook."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if name := user_input[CONF_NAME].strip():
                webhook_id = webhook.async_generate_id()
                return self.async_create_entry(
                    title=name,
                    data={CONF_WEBHOOK_ID: webhook_id},
                    description_placeholders=_async_placeholders(self.hass, webhook_id),
                )
            errors[CONF_NAME] = "name_required"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector()}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TelegrafOptionsFlow:
        """Return the options flow."""
        return TelegrafOptionsFlow()


_SECONDS = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=MAX_TIMEOUT,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_INCLUDE_MEASUREMENTS, default=[]): TextSelector(
            TextSelectorConfig(multiple=True)
        ),
        vol.Optional(CONF_EXCLUDE_MEASUREMENTS, default=[]): TextSelector(
            TextSelectorConfig(multiple=True)
        ),
        vol.Optional(CONF_STALE_TIMEOUT, default=DEFAULT_STALE_TIMEOUT): _SECONDS,
        vol.Optional(
            CONF_CONNECTIVITY_TIMEOUT, default=DEFAULT_CONNECTIVITY_TIMEOUT
        ): _SECONDS,
        vol.Optional(CONF_MAX_ENTITIES, default=DEFAULT_MAX_ENTITIES): NumberSelector(
            NumberSelectorConfig(
                min=MIN_MAX_ENTITIES,
                max=MAX_MAX_ENTITIES,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(CONF_LOCAL_ONLY, default=DEFAULT_LOCAL_ONLY): BooleanSelector(),
    }
)


class TelegrafOptionsFlow(OptionsFlowWithReload):
    """Filters, staleness, entity cap and local-only check (spec §7.2)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options; saving reloads the entry."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    # The schema has filled in defaults for omitted fields.
                    CONF_INCLUDE_MEASUREMENTS: _clean_globs(
                        user_input[CONF_INCLUDE_MEASUREMENTS]
                    ),
                    CONF_EXCLUDE_MEASUREMENTS: _clean_globs(
                        user_input[CONF_EXCLUDE_MEASUREMENTS]
                    ),
                    CONF_STALE_TIMEOUT: int(user_input[CONF_STALE_TIMEOUT]),
                    CONF_CONNECTIVITY_TIMEOUT: int(
                        user_input[CONF_CONNECTIVITY_TIMEOUT]
                    ),
                    CONF_MAX_ENTITIES: int(user_input[CONF_MAX_ENTITIES]),
                    CONF_LOCAL_ONLY: user_input[CONF_LOCAL_ONLY],
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, options_with_defaults(self.config_entry.options)
            ),
            description_placeholders={
                "webhook_url": async_webhook_url(
                    self.hass, self.config_entry.data[CONF_WEBHOOK_ID]
                )
            },
        )


def _clean_globs(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value and value.strip()]
