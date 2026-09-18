"""Config flow for Developer Cloud Services."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_API_TOKEN,
    CONF_ENABLE_EVENTS,
    CONF_INSTANCE_PRESET,
    CONF_INSTANCE_URL,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    DEFAULT_ENABLE_EVENTS,
    DEFAULT_SCAN_INTERVAL_ANONYMOUS,
    DEFAULT_SCAN_INTERVAL_AUTHENTICATED,
    DEFAULT_URLS,
    DOMAIN,
    GITEA_PRESETS,
    INSTANCE_CUSTOM,
    MIN_SCAN_INTERVAL,
    PLATFORM_GITEA,
    PLATFORM_GITHUB,
    PLATFORM_GITLAB,
    SUPPORTED_PLATFORMS,
)
from .providers import (
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudProviderError,
    DevCloudRateLimitError,
    get_provider,
)

_LOGGER = logging.getLogger(__name__)


class DevCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Developer Cloud Services."""

    VERSION = 1

    def __init__(self) -> None:
        self._selected_platform: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Choose platform."""
        if user_input is not None:
            self._selected_platform = user_input[CONF_PLATFORM]
            return await self.async_step_account()

        options = [SelectOptionDict(value=k, label=v) for k, v in SUPPORTED_PLATFORMS.items()]

        schema = vol.Schema(
            {
                vol.Required(CONF_PLATFORM, default=PLATFORM_GITHUB): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Enter account details & optional credentials/URL."""
        errors: dict[str, str] = {}
        platform = self._selected_platform or PLATFORM_GITHUB
        is_gitea = platform == PLATFORM_GITEA
        supports_custom_url = platform in (PLATFORM_GITLAB, PLATFORM_GITEA)

        if user_input is not None:
            account = user_input[CONF_ACCOUNT_NAME].strip()

            # Determine instance URL
            instance_url = ""
            if is_gitea:
                preset = user_input.get(CONF_INSTANCE_PRESET, "codeberg")
                if preset == INSTANCE_CUSTOM:
                    instance_url = (user_input.get(CONF_INSTANCE_URL) or "").strip()
                    if not instance_url:
                        errors[CONF_INSTANCE_URL] = "invalid_url"
                else:
                    instance_url = GITEA_PRESETS.get(preset, {}).get("url", "")
            else:
                instance_url = user_input.get(CONF_INSTANCE_URL) or DEFAULT_URLS.get(platform, "")

            instance_url = instance_url.rstrip("/")

            api_token = user_input.get(CONF_API_TOKEN)
            if api_token:
                api_token = api_token.strip()

            if not errors:
                # Generate unique ID: platform_host_account
                host = urllib.parse.urlparse(instance_url).netloc or "cloud"
                unique_id = f"{platform}_{host}_{account}".lower()

                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                session = async_get_clientsession(self.hass)
                provider = get_provider(
                    platform=platform,
                    session=session,
                    account_name=account,
                    instance_url=instance_url,
                    api_token=api_token,
                )

                try:
                    valid = await provider.async_validate()
                    if not valid:
                        errors["base"] = "user_not_found"
                except DevCloudAuthError:
                    errors["base"] = "invalid_auth"
                except DevCloudNotFoundError:
                    errors["base"] = "user_not_found"
                except DevCloudRateLimitError:
                    errors["base"] = "rate_limited"
                except DevCloudProviderError:
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception(
                        "Unexpected error validating %s account %s: %s", platform, account, err
                    )
                    errors["base"] = "unknown"

                if not errors:
                    if platform == PLATFORM_GITEA and instance_url:
                        netloc = urllib.parse.urlparse(instance_url).netloc.split(":")[0]
                        domain_slug = netloc.replace(".", "_").replace("-", "_").strip("_").lower()
                        title = f"{domain_slug} {account}"
                    else:
                        title = f"{SUPPORTED_PLATFORMS.get(platform, platform)} ({account})"

                    data = {
                        CONF_PLATFORM: platform,
                        CONF_ACCOUNT_NAME: account,
                        CONF_INSTANCE_URL: instance_url,
                    }
                    if api_token:
                        data[CONF_API_TOKEN] = api_token
                    return self.async_create_entry(title=title, data=data)

        fields: dict[Any, Any] = {
            vol.Required(CONF_ACCOUNT_NAME): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
        }

        if is_gitea:
            preset_options = [
                SelectOptionDict(value=k, label=v["name"]) for k, v in GITEA_PRESETS.items()
            ]
            fields[vol.Required(CONF_INSTANCE_PRESET, default="codeberg")] = SelectSelector(
                SelectSelectorConfig(
                    options=preset_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            # Optional custom URL if "Custom Instance..." is selected
            fields[vol.Optional(CONF_INSTANCE_URL, default="")] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            )
        elif supports_custom_url:
            default_url = DEFAULT_URLS.get(platform, "")
            fields[vol.Optional(CONF_INSTANCE_URL, default=default_url)] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            )

        fields[vol.Optional(CONF_API_TOKEN)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"platform": SUPPORTED_PLATFORMS.get(platform, platform)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return DevCloudOptionsFlow()


class DevCloudOptionsFlow(OptionsFlow):
    """Handle options for an existing DevCloud entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        has_token = bool(self.config_entry.data.get(CONF_API_TOKEN))
        default_interval = (
            DEFAULT_SCAN_INTERVAL_AUTHENTICATED if has_token else DEFAULT_SCAN_INTERVAL_ANONYMOUS
        )
        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, default_interval)
        current_events = self.config_entry.options.get(CONF_ENABLE_EVENTS, DEFAULT_ENABLE_EVENTS)

        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current_interval): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=86400,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_ENABLE_EVENTS, default=current_events): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
