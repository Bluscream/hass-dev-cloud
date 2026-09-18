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
    CONF_INCLUDE_NON_OWNED_ORGS,
    CONF_INSTANCE_PRESET,
    CONF_INSTANCE_URL,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    DEFAULT_ENABLE_EVENTS,
    DEFAULT_INCLUDE_NON_OWNED_ORGS,
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
        self._selected_instance_url: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Choose platform."""
        if user_input is not None:
            self._selected_platform = user_input[CONF_PLATFORM]
            if self._selected_platform == PLATFORM_GITEA:
                return await self.async_step_gitea_instance()
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

    async def async_step_gitea_instance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 (Gitea only): Choose preset, existing configured instance, or custom instance."""
        if user_input is not None:
            preset = user_input.get(CONF_INSTANCE_PRESET, "codeberg")
            if preset == INSTANCE_CUSTOM:
                self._selected_instance_url = None
            elif preset.startswith("existing_"):
                # Value stored in custom option
                url = preset[len("existing_") :]
                self._selected_instance_url = url
            else:
                self._selected_instance_url = GITEA_PRESETS.get(preset, {}).get("url", "")
            return await self.async_step_account()

        preset_options: list[SelectOptionDict] = []

        # 1. Existing configured Gitea / Forgejo instances
        configured_urls: set[str] = set()
        for entry in self._async_current_entries():
            if entry.data.get(CONF_PLATFORM) == PLATFORM_GITEA:
                url = (entry.data.get(CONF_INSTANCE_URL) or "").rstrip("/")
                if url and url not in configured_urls:
                    configured_urls.add(url)
                    netloc = urllib.parse.urlparse(url).netloc
                    preset_options.append(
                        SelectOptionDict(
                            value=f"existing_{url}",
                            label=f"{netloc} (Configured)",
                        )
                    )

        # 2. Predefined public instances
        for k, v in GITEA_PRESETS.items():
            if k == INSTANCE_CUSTOM:
                continue
            preset_options.append(SelectOptionDict(value=k, label=v["name"]))

        # 3. Custom Instance option
        preset_options.append(
            SelectOptionDict(value=INSTANCE_CUSTOM, label=GITEA_PRESETS[INSTANCE_CUSTOM]["name"])
        )

        default_preset = preset_options[0]["value"]
        schema = vol.Schema(
            {
                vol.Required(CONF_INSTANCE_PRESET, default=default_preset): SelectSelector(
                    SelectSelectorConfig(
                        options=preset_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="gitea_instance", data_schema=schema)

    def _resolve_instance_url(
        self, user_input: dict[str, Any], platform: str, is_custom_gitea: bool
    ) -> tuple[str, dict[str, str]]:
        """Work out the base URL for the entry, and any error it produced."""
        if platform == PLATFORM_GITEA:
            if not is_custom_gitea:
                return (self._selected_instance_url or "").rstrip("/"), {}
            instance_url = (user_input.get(CONF_INSTANCE_URL) or "").strip()
            if not instance_url:
                return "", {CONF_INSTANCE_URL: "invalid_url"}
        else:
            instance_url = user_input.get(CONF_INSTANCE_URL) or DEFAULT_URLS.get(platform, "")
        return instance_url.rstrip("/"), {}

    async def _async_validate_account(
        self, platform: str, account: str, instance_url: str, api_token: str | None
    ) -> str | None:
        """Validate credentials, returning an error key or None when the account is good."""
        provider = get_provider(
            platform=platform,
            session=async_get_clientsession(self.hass),
            account_name=account,
            instance_url=instance_url,
            api_token=api_token,
        )

        try:
            return None if await provider.async_validate() else "user_not_found"
        except DevCloudAuthError:
            return "invalid_auth"
        except DevCloudNotFoundError:
            return "user_not_found"
        except DevCloudRateLimitError:
            return "rate_limited"
        except DevCloudProviderError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error validating %s account %s", platform, account)
            return "unknown"

    @staticmethod
    def _entry_title(platform: str, account: str, instance_url: str) -> str:
        """Title for the entry. Gitea entries carry the host, since there can be several."""
        if platform == PLATFORM_GITEA and instance_url:
            netloc = urllib.parse.urlparse(instance_url).netloc.split(":")[0]
            domain_slug = netloc.replace(".", "_").replace("-", "_").strip("_").lower()
            return f"{domain_slug} {account}"
        return f"{SUPPORTED_PLATFORMS.get(platform, platform)} ({account})"

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2/3: Enter account details & optional credentials/URL."""
        errors: dict[str, str] = {}
        platform = self._selected_platform or PLATFORM_GITHUB
        is_gitea = platform == PLATFORM_GITEA
        is_custom_gitea = is_gitea and not self._selected_instance_url
        supports_custom_url = (platform == PLATFORM_GITLAB) or is_custom_gitea

        if user_input is not None:
            account = user_input[CONF_ACCOUNT_NAME].strip()
            instance_url, errors = self._resolve_instance_url(user_input, platform, is_custom_gitea)
            api_token = (user_input.get(CONF_API_TOKEN) or "").strip() or None

            if not account:
                errors[CONF_ACCOUNT_NAME] = "user_not_found"

            if not errors:
                host = urllib.parse.urlparse(instance_url).netloc or "cloud"
                await self.async_set_unique_id(f"{platform}_{host}_{account}".lower())
                self._abort_if_unique_id_configured()

                error = await self._async_validate_account(
                    platform, account, instance_url, api_token
                )
                if error:
                    errors["base"] = error
                else:
                    data = {
                        CONF_PLATFORM: platform,
                        CONF_ACCOUNT_NAME: account,
                        CONF_INSTANCE_URL: instance_url,
                    }
                    if api_token:
                        data[CONF_API_TOKEN] = api_token
                    # Stored as an option, not entry data, so the options flow edits the
                    # same key rather than a second copy.
                    options = {
                        CONF_INCLUDE_NON_OWNED_ORGS: user_input.get(
                            CONF_INCLUDE_NON_OWNED_ORGS, DEFAULT_INCLUDE_NON_OWNED_ORGS
                        )
                    }
                    return self.async_create_entry(
                        title=self._entry_title(platform, account, instance_url),
                        data=data,
                        options=options,
                    )

        fields: dict[Any, Any] = {
            vol.Required(CONF_ACCOUNT_NAME): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Optional(
                CONF_INCLUDE_NON_OWNED_ORGS, default=DEFAULT_INCLUDE_NON_OWNED_ORGS
            ): BooleanSelector(),
        }

        if supports_custom_url:
            default_url = DEFAULT_URLS.get(platform, "") if not is_gitea else ""
            url_selector = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
            if is_custom_gitea:
                fields[vol.Required(CONF_INSTANCE_URL)] = url_selector
            else:
                fields[vol.Optional(CONF_INSTANCE_URL, default=default_url)] = url_selector

        fields[vol.Optional(CONF_API_TOKEN)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )

        placeholders: dict[str, str] = {"platform": SUPPORTED_PLATFORMS.get(platform, platform)}
        if is_gitea and self._selected_instance_url:
            placeholders["instance"] = urllib.parse.urlparse(self._selected_instance_url).netloc

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders=placeholders,
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
        current_include_orgs = self.config_entry.options.get(
            CONF_INCLUDE_NON_OWNED_ORGS, DEFAULT_INCLUDE_NON_OWNED_ORGS
        )

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
                vol.Required(
                    CONF_INCLUDE_NON_OWNED_ORGS, default=current_include_orgs
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
