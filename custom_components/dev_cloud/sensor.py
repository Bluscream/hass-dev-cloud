"""Sensor entities for Developer Cloud Services.

Sensors deliberately carry *only* aggregated counts and scalar metrics in their state
attributes. The full detail (repository lists, releases, packages, notifications, running
jobs, ...) is exported to `/local/dev/<platform>/<account>.json` by `storage.py`. The
profile sensor carries the `json_url` pointing at it — just the one entity, since the link is
identical for every entity on the account. See that module for the why.

Entities are registered purely from what a provider actually returned — there are no
per-platform hardcoded lists. A sensor with no data behind it is omitted entirely rather
than reporting a misleading zero.
"""

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
from .const import PLATFORM_ICONS
from .coordinator import DevCloudCoordinator
from .entity import DevCloudBaseEntity
from .models import RepoData


def counted_repos(coordinator: DevCloudCoordinator) -> list[RepoData]:
    """The account's own repositories plus those of organisations that count toward totals.

    Which organisations count is the entry's `include_non_owned_orgs` option: with it off
    (the default) only orgs the account owns contribute, so the totals describe the user
    rather than every organisation they happen to belong to.
    """
    data = coordinator.data
    if data is None:
        return []

    repos = list(data.repos)
    for org in data.orgs:
        if coordinator.include_non_owned_orgs or org.is_owned:
            repos.extend(org.repos)
    return repos


def counted_releases(coordinator: DevCloudCoordinator) -> list[dict[str, Any]]:
    """Releases belonging to repositories that count toward the totals."""
    data = coordinator.data
    if data is None:
        return []

    allowed = {r.full_name for r in counted_repos(coordinator)}
    return [r for r in data.releases if r.get("repository") in allowed]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DevCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DevCloud sensor platform."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [DevCloudProfileSensor(coordinator)]

    data = coordinator.data
    if data is None:
        async_add_entities(entities)
        return

    repos = data.repos
    packages = data.packages

    if repos:
        entities.append(DevCloudRepositoriesSensor(coordinator))

    if data.orgs:
        entities.append(DevCloudOrganizationsSensor(coordinator))

    if data.pastes:
        entities.append(DevCloudPastesSensor(coordinator))

    if data.notifications:
        entities.append(DevCloudNotificationsSensor(coordinator))

    if data.open_issues or any(r.open_issues for r in repos):
        entities.append(DevCloudOpenIssuesSensor(coordinator))

    if data.open_prs:
        entities.append(DevCloudOpenPullRequestsSensor(coordinator))

    counted = counted_repos(coordinator)
    if any(r.stars for r in counted) or any(p.star_count for p in packages):
        entities.append(DevCloudStarsSensor(coordinator))

    if any(r.watchers for r in counted):
        entities.append(DevCloudWatchersSensor(coordinator))

    if any(r.forks for r in counted):
        entities.append(DevCloudForksSensor(coordinator))

    if data.releases:
        entities.extend(
            (
                DevCloudReleasesSensor(coordinator),
                DevCloudReleaseAssetsSensor(coordinator),
                DevCloudDownloadsSensor(coordinator),
            )
        )

    if any(p.pull_count for p in packages) or any(
        isinstance(r.extra, dict) and r.extra.get("pull_count") for r in repos
    ):
        entities.append(DevCloudPullsSensor(coordinator))

    if data.sponsors_count is not None or data.sponsoring_count is not None:
        entities.append(DevCloudSponsorsSensor(coordinator))

    if packages:
        entities.append(DevCloudPackagesSensor(coordinator))

    if data.running_jobs_count is not None:
        entities.append(DevCloudRunningJobsSensor(coordinator))

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
            # The account's full JSON snapshot. Lives only here: it is the same URL for
            # every entity on this account, so repeating it on each one is pure noise.
            "json_url": self.coordinator.json_url,
            # Effective refresh interval and measured request cost per resource, so the
            # adaptive scheduler's choices are visible without reading the JSON dump.
            "scheduling": data.scheduling or None,
        }
        attrs.update(prof.extra)
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudRepositoriesSensor(DevCloudBaseEntity, SensorEntity):
    """Repositories sensor with count as state and aggregate totals as attributes."""

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
        return len(self.coordinator.data.repos)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        repos = self.coordinator.data.repos
        counted = counted_repos(self.coordinator)
        attrs = {
            "total_repositories": self.native_value,
            "public_repositories": sum(1 for r in repos if not r.is_private),
            "private_repositories": sum(1 for r in repos if r.is_private),
            # Totals span the organisation repositories that count for this entry, so they
            # can exceed total_repositories, which is the account's own repos alone.
            "counted_repositories": len(counted),
            "total_stars": sum(r.stars for r in counted),
            "total_forks": sum(r.forks for r in counted),
            "total_watchers": sum(r.watchers for r in counted),
        }
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudOrganizationsSensor(DevCloudBaseEntity, SensorEntity):
    """Organizations sensor showing the membership count."""

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
        owned = [o for o in orgs if o.is_owned]
        return {
            "total_organizations": len(orgs),
            "owned_organizations": len(owned),
            "total_organization_repositories": sum(len(o.repos) for o in orgs),
            "counted_organization_repositories": sum(
                len(o.repos) for o in orgs if self.coordinator.include_non_owned_orgs or o.is_owned
            ),
        }


class DevCloudPastesSensor(DevCloudBaseEntity, SensorEntity):
    """Pastes/gists sensor with the paste count as state."""

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
        return len(self.coordinator.data.pastes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        pastes = self.coordinator.data.pastes
        attrs = {
            "total_pastes": self.native_value,
            "public_pastes": sum(1 for p in pastes if p.is_public),
            "private_pastes": sum(1 for p in pastes if not p.is_public),
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

        attrs: dict[str, Any] = {"total_packages": len(packages)}
        if total_downloads > 0:
            attrs["total_downloads"] = total_downloads
        if total_pulls > 0:
            attrs["total_pulls"] = total_pulls

        return attrs


class DevCloudNotificationsSensor(DevCloudBaseEntity, SensorEntity):
    """Notifications sensor with the unread count as state."""

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
        return {
            "total_notifications": len(notifications),
            "unread_notifications": sum(1 for n in notifications if n.unread),
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
        data = self.coordinator.data
        if data.open_issues:
            return len(data.open_issues)
        # Platforms without an issue search still report per-repository counts.
        return sum(r.open_issues for r in data.repos)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_open_issues": self.native_value}


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
        return len(self.coordinator.data.open_prs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_open_prs": self.native_value}


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
        return sum(r.stars for r in counted_repos(self.coordinator))


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
        return sum(r.watchers for r in counted_repos(self.coordinator))


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
        return sum(r.forks for r in counted_repos(self.coordinator))


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


class DevCloudReleasesSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total releases count."""

    _attr_icon = "mdi:tag-multiple"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "releases"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "releases")
        self._attr_name = "Releases"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return len(counted_releases(self.coordinator))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_releases": self.native_value}


class DevCloudReleaseAssetsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total release assets count."""

    _attr_icon = "mdi:attachment"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "assets"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "release_assets")
        self._attr_name = "Assets"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(len(r.get("assets", ())) for r in counted_releases(self.coordinator))


class DevCloudDownloadsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total downloads across all release assets."""

    _attr_icon = "mdi:download"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "downloads"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "downloads")
        self._attr_name = "Downloads"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return sum(
            asset.get("downloads", 0)
            for release in counted_releases(self.coordinator)
            for asset in release.get("assets", ())
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_downloads": self.native_value}


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


class DevCloudRunningJobsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for currently running CI workflows / pipelines."""

    _attr_icon = "mdi:play-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "jobs"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "running_jobs")
        self._attr_name = "Running Jobs"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.running_jobs_count or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "platform": self.coordinator.platform_id,
            "account": self.coordinator.account_name,
        }
