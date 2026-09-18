"""Base entity for Developer Cloud Services."""

from __future__ import annotations

import urllib.parse

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_INSTANCE_URL,
    CONF_PLATFORM,
    DOMAIN,
    PLATFORM_GITEA,
    SUPPORTED_PLATFORMS,
)
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
        instance_url = entry.data.get(CONF_INSTANCE_URL)

        if platform_id == PLATFORM_GITEA and instance_url:
            netloc = urllib.parse.urlparse(instance_url).netloc.split(":")[0]
            domain_slug = netloc.replace(".", "_").replace("-", "_").strip("_").lower()
            device_name = f"{netloc} ({account_name})"
            manufacturer = "Gitea"
            self._attr_suggested_object_id = f"{domain_slug}_{account_name.lower()}_{entity_key}"
        else:
            platform_name = SUPPORTED_PLATFORMS.get(platform_id, platform_id.title())
            device_name = f"{platform_name} ({account_name})"
            manufacturer = platform_name

        self._attr_unique_id = f"{entry.entry_id}_{entity_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer=manufacturer,
            model=f"{SUPPORTED_PLATFORMS.get(platform_id, platform_id.title())} Account",
            configuration_url=coordinator.data.profile.profile_url if coordinator.data else None,
        )
