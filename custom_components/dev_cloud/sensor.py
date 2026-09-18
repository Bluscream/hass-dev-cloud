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
from .const import PLATFORM_DOCKERHUB, PLATFORM_ICONS
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

    # Add Open Issues, PRs, Stars, Watchers, Forks sensors for forge platforms
    if coordinator.platform_id in FORGE_PLATFORMS:
        entities.append(DevCloudOpenIssuesSensor(coordinator))
        entities.append(DevCloudOpenPullRequestsSensor(coordinator))
        entities.append(DevCloudStarsSensor(coordinator))
        entities.append(DevCloudWatchersSensor(coordinator))
        entities.append(DevCloudForksSensor(coordinator))
    elif coordinator.platform_id == PLATFORM_DOCKERHUB:
        # Docker Hub provides total stars and total pulls across repositories/images
        entities.append(DevCloudStarsSensor(coordinator))
        entities.append(DevCloudPullsSensor(coordinator))

    # Add Sponsors sensor only if platform supports sponsors or has sponsors data
    has_sponsors_data = bool(
        coordinator.data
        and (
            coordinator.data.sponsors_count is not None
            or coordinator.data.sponsoring_count is not None
        )
    )
    if getattr(coordinator.provider, "supports_sponsors", False) or has_sponsors_data:
        entities.append(DevCloudSponsorsSensor(coordinator))

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
        self._attr_icon = PLATFORM_ICONS.get(coordinator.platform_id, "mdi:account-circle")

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
        data = self.coordinator.data
        prof = data.profile

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
            "rate_limit_remaining": data.rate_limit_remaining,
            "rate_limit_reset": data.rate_limit_reset,
        }
        attrs.update(prof.extra)
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudRepositoriesSensor(DevCloudBaseEntity, SensorEntity):
    """Repositories sensor with count as state and details dictionary as attributes."""

    _attr_icon = "mdi:source-repository-multiple"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "repos"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "repositories")
        self._attr_name = "Repositories"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        prof = self.coordinator.data.profile
        if prof.public_repos is not None:
            return prof.public_repos + (prof.private_repos or 0)
        return len(self.coordinator.data.repos)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        repos = self.coordinator.data.repos
        total_stars = sum(r.stars for r in repos)
        total_forks = sum(r.forks for r in repos)
        total_watchers = sum(r.watchers for r in repos)

        repo_list = [
            {
                "name": r.name,
                "full_name": r.full_name,
                "url": r.url,
                "description": r.description,
                "stars": r.stars,
                "forks": r.forks,
                "watchers": r.watchers,
                "is_fork": r.is_fork,
                "is_private": r.is_private,
                "language": r.primary_language,
                "upstream": r.upstream,
                "updated_at": r.updated_at,
            }
            for r in repos[:25]
        ]

        prof = self.coordinator.data.profile
        attrs = {
            "total_repositories": self.native_value,
            "public_repositories": prof.public_repos,
            "private_repositories": prof.private_repos,
            "total_stars": total_stars,
            "total_forks": total_forks,
            "total_watchers": total_watchers,
            "repositories": repo_list,
        }
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudOrganizationsSensor(DevCloudBaseEntity, SensorEntity):
    """Organizations sensor showing count as state and org mapping in attributes."""

    _attr_icon = "mdi:domain"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "orgs"
    _attr_suggested_display_precision = 0

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
    _attr_native_unit_of_measurement = "pastes"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "pastes")
        if coordinator.platform_id == "github":
            self._attr_name = "Gists"
        elif coordinator.platform_id == "gitlab":
            self._attr_name = "Snippets"
        else:
            self._attr_name = "Pastes"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        prof = self.coordinator.data.profile
        if prof.total_gists is not None:
            return prof.total_gists
        if prof.public_gists is not None:
            return prof.public_gists
        return len(self.coordinator.data.pastes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        pastes = self.coordinator.data.pastes
        prof = self.coordinator.data.profile
        attrs = {
            "total_pastes": self.native_value,
            "public_pastes": prof.public_gists,
            "private_pastes": prof.private_gists,
            "pastes": [
                {
                    "id": p.paste_id,
                    "title": p.title,
                    "url": p.url,
                    "is_public": p.is_public,
                    "files_count": p.files_count,
                    "updated_at": p.updated_at,
                }
                for p in pastes[:25]
            ],
        }
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudPackagesSensor(DevCloudBaseEntity, SensorEntity):
    """Packages sensor for registry platforms."""

    _attr_icon = "mdi:package-variant-closed"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "packages"
    _attr_suggested_display_precision = 0

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
    _attr_native_unit_of_measurement = "notifications"
    _attr_suggested_display_precision = 0

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


class DevCloudOpenIssuesSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total open issues across repositories."""

    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "issues"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "open_issues")
        self._attr_name = "Open Issues"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        if self.coordinator.data.open_issues_count is not None:
            return self.coordinator.data.open_issues_count
        # Fallback to summing up open_issues in repos list
        return sum(r.open_issues for r in self.coordinator.data.repos)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "total_open_issues": self.native_value,
            "issues": self.coordinator.data.open_issues,
        }


class DevCloudOpenPullRequestsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total open pull requests across repositories."""

    _attr_icon = "mdi:source-pull"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "PRs"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "open_prs")
        self._attr_name = "Open Pull Requests"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.open_prs_count or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "total_open_prs": self.native_value,
            "pull_requests": self.coordinator.data.open_prs,
        }


class DevCloudStarsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for stars across all repositories."""

    _attr_icon = "mdi:star"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "stars"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "stars")
        self._attr_name = "Stars"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(r.stars for r in self.coordinator.data.repos)


class DevCloudWatchersSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for watchers across all repositories."""

    _attr_icon = "mdi:eye"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "watchers"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "watchers")
        self._attr_name = "Watchers"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(r.watchers for r in self.coordinator.data.repos)


class DevCloudForksSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for forks across all repositories."""

    _attr_icon = "mdi:source-fork"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "forks"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "forks")
        self._attr_name = "Forks"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(r.forks for r in self.coordinator.data.repos)


class DevCloudPullsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total pull/download count across all repositories or packages."""

    _attr_icon = "mdi:download"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "pulls"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "pulls")
        self._attr_name = "Pulls"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        # Sum from packages or repository extras
        packages_pulls = sum(p.pull_count or 0 for p in self.coordinator.data.packages)
        repos_pulls = sum(
            r.extra.get("pull_count", 0)
            for r in self.coordinator.data.repos
            if isinstance(r.extra, dict)
        )
        return max(packages_pulls, repos_pulls)


class DevCloudSponsorsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for active sponsors count."""

    _attr_icon = "mdi:heart"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "sponsors"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "sponsors")
        self._attr_name = "Sponsors"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.sponsors_count or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        data = self.coordinator.data
        attrs: dict[str, Any] = {
            "sponsors": data.sponsors_count,
            "sponsoring": data.sponsoring_count,
        }
        return {k: v for k, v in attrs.items() if v is not None}
