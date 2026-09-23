"""The Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import DevCloudCoordinator

_LOGGER = logging.getLogger(__name__)

type DevCloudConfigEntry = ConfigEntry[DevCloudCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DevCloudConfigEntry) -> bool:
    """Set up Developer Cloud Services from a config entry."""
    # Reload submodules so code changes take effect on reload without full HA restart
    import contextlib
    import importlib
    import sys

    # Two passes: the first re-executes every submodule against the new sources, the second
    # re-binds the cross-module `from .x import Y` names that the first pass may have
    # resolved before their own module was refreshed.
    for _ in range(2):
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("custom_components.dev_cloud."):
                mod = sys.modules.get(mod_name)
                if mod:
                    with contextlib.suppress(Exception):
                        importlib.reload(mod)

    # Re-resolved for the same reason as the coordinator below: this module is not itself
    # reloaded, so the module-level PLATFORMS imported at first load is whatever const.py
    # said back then. A platform added since - the button platform was the first - would
    # never be forwarded, and the new entities simply would not appear until Home Assistant
    # was restarted outright.
    from .const import PLATFORMS as CURRENT_PLATFORMS

    # Invalidate HA internal integration platform cache
    with contextlib.suppress(Exception):
        from homeassistant.loader import DATA_INTEGRATIONS

        # The registry may hold an Integration, an in-flight Future, or nothing at all;
        # only a resolved Integration carries the platform cache we need to invalidate.
        integration = hass.data.get(DATA_INTEGRATIONS, {}).get(DOMAIN)
        cache = getattr(integration, "_cache", None)
        if cache is not None:
            for platform in CURRENT_PLATFORMS:
                cache.pop(f"{DOMAIN}.{platform}", None)

    # Invalidate translation cache so newly added translation strings show up immediately
    with contextlib.suppress(Exception):
        from homeassistant.helpers.translation import TRANSLATION_FLATTEN_CACHE

        trans_cache = hass.data.get(TRANSLATION_FLATTEN_CACHE)
        if trans_cache is not None:
            for lang_loaded in trans_cache.cache_data.loaded.values():
                lang_loaded.discard(DOMAIN)
            for lang_cache in trans_cache.cache_data.cache.values():
                for cat_cache in lang_cache.values():
                    cat_cache.pop(DOMAIN, None)

    # Re-resolve after the reload above: this module is not itself reloaded, so the
    # module-level import below still points at the pre-reload class object.
    from .coordinator import DevCloudCoordinator as CoordinatorClass

    coordinator = CoordinatorClass(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, CURRENT_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DevCloudConfigEntry) -> bool:
    """Unload a config entry."""
    # Re-resolved rather than using the module-level import, for the same staleness reason
    # as the setup above. Unloading a platform that was never set up is a no-op, so the
    # worst case of the two lists disagreeing is harmless.
    from .const import PLATFORMS as CURRENT_PLATFORMS

    return await hass.config_entries.async_unload_platforms(entry, CURRENT_PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: DevCloudConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
