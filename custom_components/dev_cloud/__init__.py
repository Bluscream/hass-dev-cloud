"""The Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
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

    # Invalidate HA internal integration platform cache
    with contextlib.suppress(Exception):
        from homeassistant.loader import DATA_INTEGRATIONS

        integration = hass.data.get(DATA_INTEGRATIONS, {}).get(DOMAIN)
        if hasattr(integration, "_cache"):
            for platform in PLATFORMS:
                integration._cache.pop(f"{DOMAIN}.{platform}", None)

    # Re-resolve after the reload above: this module is not itself reloaded, so the
    # module-level import below still points at the pre-reload class object.
    from .coordinator import DevCloudCoordinator as CoordinatorClass

    coordinator = CoordinatorClass(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DevCloudConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: DevCloudConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
