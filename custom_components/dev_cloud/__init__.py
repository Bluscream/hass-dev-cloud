"""The Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import DevCloudCoordinator

_LOGGER = logging.getLogger(__name__)

type DevCloudConfigEntry = ConfigEntry[DevCloudCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DevCloudConfigEntry) -> bool:
    """Set up Developer Cloud Services from a config entry."""
    coordinator = DevCloudCoordinator(hass, entry)
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
