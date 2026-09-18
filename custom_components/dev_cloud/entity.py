"""Base entity for Developer Cloud Services."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NAME, CONF_PLATFORM, DOMAIN, SUPPORTED_PLATFORMS
from .coordinator import DevCloudCoordinator


class DevCloudBaseEntity(CoordinatorEntity[DevCloudCoordinator]):
    """Base class for DevCloud entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DevCloudCoordinator, entity_key: str) -> None:
        super().__init__(coordinator)
        self._entity_key = entity_key
        entry = coordinator.entry

        platform_id = entry.data[CONF_PLATFORM]
        account_name = entry.data[CONF_ACCOUNT_NAME]
        platform_name = SUPPORTED_PLATFORMS.get(platform_id, platform_id.title())

        self._attr_unique_id = f"{entry.entry_id}_{entity_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{platform_name} ({account_name})",
            manufacturer=platform_name,
            model=f"{platform_name} Account",
            configuration_url=coordinator.data.profile.profile_url if coordinator.data else None,
        )
