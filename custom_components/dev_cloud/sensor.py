"""Sensor entities for Developer Cloud Services."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import DevCloudConfigEntry
from .coordinator import DevCloudCoordinator
from .entity import DevCloudBaseEntity

FORGE_PLATFORMS = ("github", "gitlab", "gitea")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DevCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DevCloud sensor platform."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        DevCloudProfileSensor(coordinator),
        DevCloudRepositoriesSensor(coordinator),
    ]

    # Add Org sensor if platform supports or has orgs
    has_orgs = bool(coordinator.data and coordinator.data.orgs)
    if has_orgs or coordinator.platform_id in FORGE_PLATFORMS:
        entities.append(DevCloudOrganizationsSensor(coordinator))

    # Add Pastes/Gists sensor if platform supports or has pastes
    has_pastes = bool(coordinator.data and coordinator.data.pastes)
    if has_pastes or coordinator.platform_id in ("github", "gitlab"):
        entities.append(DevCloudPastesSensor(coordinator))

    # Add Notifications sensor if platform supports or has notifications
    if coordinator.platform_id in FORGE_PLATFORMS:
        entities.append(DevCloudNotificationsSensor(coordinator))

    # Add Packages sensor if platform has packages
    if coordinator.data and coordinator.data.packages:
        entities.append(DevCloudPackagesSensor(coordinator))

    async_add_entities(entities)


class DevCloudProfileSensor(DevCloudBaseEntity, SensorEntity):
    """Profile sensor holding user avatar in entity_picture and profile metadata in attributes."""

    _attr_icon = "mdi:account-circle"

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "profile")
        self._attr_name = "Profile"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        prof = self.coordinator.data.profile
        return prof.display_name or prof.username

    @property
    def entity_picture(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.profile.avatar_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        prof = self.coordinator.data.profile
        attrs: dict[str, Any] = {
            "username": prof.username,
            "display_name": prof.display_name,
            "user_id": prof.user_id,
            "profile_url": prof.profile_url,
            "bio": prof.bio,
            "location": prof.location,
            "company": prof.company,
            "blog": prof.blog,
            "email": prof.email,
            "created_at": prof.created_at,
            "followers": prof.followers,
            "following": prof.following,
            "rate_limit_remaining": self.coordinator.data.rate_limit_remaining,
            "rate_limit_reset": self.coordinator.data.rate_limit_reset,
        }
        attrs.update(prof.extra)
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudRepositoriesSensor(DevCloudBaseEntity, SensorEntity):
    """Repositories sensor with count as state and details dictionary as attributes."""

    _attr_icon = "mdi:source-repository-multiple"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "repositories")
        self._attr_name = "Repositories"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        if self.coordinator.data.profile.public_repos is not None:
            return self.coordinator.data.profile.public_repos
        return len(self.coordinator.data.repos)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        repos = self.coordinator.data.repos
        total_stars = sum(r.stars for r in repos)
        total_forks = sum(r.forks for r in repos)

        repo_list = [
            {
                "name": r.name,
                "full_name": r.full_name,
                "url": r.url,
                "description": r.description,
                "stars": r.stars,
                "forks": r.forks,
                "is_fork": r.is_fork,
                "is_private": r.is_private,
                "language": r.primary_language,
                "upstream": r.upstream,
                "updated_at": r.updated_at,
            }
            for r in repos
        ]

        return {
            "total_repositories": len(repos),
            "total_stars": total_stars,
            "total_forks": total_forks,
            "repositories": repo_list,
        }


class DevCloudOrganizationsSensor(DevCloudBaseEntity, SensorEntity):
    """Organizations sensor showing count as state and org mapping in attributes."""

    _attr_icon = "mdi:domain"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "organizations")
        self._attr_name = "Organizations"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return len(self.coordinator.data.orgs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        orgs = self.coordinator.data.orgs
        return {
            "total_organizations": len(orgs),
            "organizations": [
                {
                    "name": o.name,
                    "display_name": o.display_name,
                    "url": o.url,
                    "avatar_url": o.avatar_url,
                    "description": o.description,
                }
                for o in orgs
            ],
        }


class DevCloudPastesSensor(DevCloudBaseEntity, SensorEntity):
    """Pastes/gists sensor with count as state and paste items in attributes."""

    _attr_icon = "mdi:code-braces"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "pastes")
        self._attr_name = "Pastes and Gists"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        if self.coordinator.data.profile.public_gists is not None:
            return self.coordinator.data.profile.public_gists
        return len(self.coordinator.data.pastes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        pastes = self.coordinator.data.pastes
        return {
            "total_pastes": len(pastes),
            "pastes": [
                {
                    "id": p.paste_id,
                    "title": p.title,
                    "url": p.url,
                    "is_public": p.is_public,
                    "files_count": p.files_count,
                    "updated_at": p.updated_at,
                }
                for p in pastes
            ],
        }


class DevCloudPackagesSensor(DevCloudBaseEntity, SensorEntity):
    """Packages sensor for registry platforms."""

    _attr_icon = "mdi:package-variant-closed"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "packages")
        self._attr_name = "Packages"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return len(self.coordinator.data.packages)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        packages = self.coordinator.data.packages
        total_downloads = sum(p.downloads_total or 0 for p in packages)
        total_pulls = sum(p.pull_count or 0 for p in packages)

        attrs: dict[str, Any] = {
            "total_packages": len(packages),
            "packages": [
                {
                    "name": p.name,
                    "version": p.version,
                    "url": p.url,
                    "description": p.description,
                    "downloads": p.downloads_total,
                    "pulls": p.pull_count,
                    "updated_at": p.updated_at,
                }
                for p in packages
            ],
        }
        if total_downloads > 0:
            attrs["total_downloads"] = total_downloads
        if total_pulls > 0:
            attrs["total_pulls"] = total_pulls

        return attrs


class DevCloudNotificationsSensor(DevCloudBaseEntity, SensorEntity):
    """Notifications sensor with unread count as state and notifications map in attributes."""

    _attr_icon = "mdi:bell"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "notifications")
        self._attr_name = "Notifications"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(1 for n in self.coordinator.data.notifications if n.unread)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        notifications = self.coordinator.data.notifications
        unread_count = sum(1 for n in notifications if n.unread)
        return {
            "total_notifications": len(notifications),
            "unread_notifications": unread_count,
            "notifications": [
                {
                    "id": n.notification_id,
                    "title": n.title,
                    "reason": n.reason,
                    "repository": n.repository,
                    "url": n.url,
                    "unread": n.unread,
                    "subject_type": n.subject_type,
                    "updated_at": n.updated_at,
                }
                for n in notifications
            ],
        }
