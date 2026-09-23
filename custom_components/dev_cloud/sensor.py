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

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import DevCloudConfigEntry
from .aggregation import (
    collection_total,
    counted_issues,
    top_referrers,
    traffic_totals,
)
from .const import (
    NOTIFICATION_ATTRIBUTE_LIMIT,
    PLATFORM_ICONS,
    TRAFFIC_REFERRER_ATTRIBUTE_LIMIT,
)
from .coordinator import DevCloudCoordinator
from .entity import DevCloudBaseEntity
from .models import NotificationData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DevCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DevCloud sensor platform.

    The profile sensor always exists; every other sensor is registered only when the
    provider actually returned something behind it, per _OPTIONAL_SENSORS below.
    """
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        DevCloudProfileSensor(coordinator),
        DevCloudLastUpdatedSensor(coordinator),
    ]
    entities.extend(
        build(coordinator) for build, has_data in _OPTIONAL_SENSORS if has_data(coordinator)
    )

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
            # Stringified deliberately: it is an identifier, not a quantity, and the
            # frontend renders a numeric attribute as "3,318,223".
            "user_id": str(prof.user_id) if prof.user_id is not None else None,
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
            "json_url": str(self.coordinator.json_url),
        }
        attrs.update(prof.extra)
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudRepositoriesSensor(DevCloudBaseEntity, SensorEntity):
    """Repositories sensor with count as state and aggregate totals as attributes."""

    _attr_icon = "mdi:source-repository-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "repos"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "repositories")
        self._attr_name = "Repositories"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "repositories")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        totals = self.coordinator.totals
        if totals is None:
            return {}
        attrs = {
            "total_repositories": totals.repositories,
            "public_repositories": totals.public_repositories,
            "private_repositories": totals.private_repositories,
            # Totals span the organisation repositories that count for this entry, so they
            # can exceed total_repositories, which is the account's own repos alone.
            "counted_repositories": totals.counted_repositories,
            "total_stars": totals.stars,
            "total_forks": totals.forks,
            "total_watchers": totals.watchers,
        }
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudOrganizationsSensor(DevCloudBaseEntity, SensorEntity):
    """Organizations sensor showing the membership count."""

    _attr_icon = "mdi:domain"
    _attr_state_class = SensorStateClass.MEASUREMENT
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
    _attr_state_class = SensorStateClass.MEASUREMENT
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
        return _total(self.coordinator, "pastes")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        pastes = self.coordinator.data.pastes
        starred = [p for p in pastes if p.stars is not None]
        forked = [p for p in pastes if p.forks is not None]
        attrs: dict[str, Any] = {
            "total_pastes": self.native_value,
            "public_pastes": sum(1 for p in pastes if p.is_public),
            "private_pastes": sum(1 for p in pastes if not p.is_public),
            "forks_of_others": sum(1 for p in pastes if p.is_fork),
            # None until the GraphQL walk has been round: REST exposes neither figure, so
            # summing what it returned would be a confident zero for something never asked.
            "total_stars": sum(p.stars or 0 for p in starred) if starred else None,
            "total_forks": sum(p.forks or 0 for p in forked) if forked else None,
        }
        return {k: v for k, v in attrs.items() if v is not None}


class DevCloudPackagesSensor(DevCloudBaseEntity, SensorEntity):
    """Packages sensor for registry platforms."""

    _attr_icon = "mdi:package-variant-closed"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "packages"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "packages")
        self._attr_name = "Packages"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "packages")

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
    _attr_state_class = SensorStateClass.MEASUREMENT
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
        """Totals the state does not already give, plus the unread list itself.

        `unread_notifications` is deliberately absent: it is exactly the state, and an
        attribute repeating the state is a second answer to the same question that can
        disagree with the first.
        """
        if not self.coordinator.data:
            return {}
        notifications = self.coordinator.data.notifications
        unread = [n for n in notifications if n.unread]
        return {
            "total_notifications": len(notifications),
            "read_notifications": len(notifications) - len(unread),
            # Newest first, so a truncated list keeps the ones worth seeing. Capped because
            # attributes are written to the recorder on every state change; the complete
            # list is in the JSON snapshot the profile sensor links to.
            "unread": [
                {
                    k: v
                    for k, v in (
                        ("id", n.notification_id),
                        ("title", n.title),
                        ("repository", n.repository),
                        ("reason", n.reason),
                        ("type", n.subject_type),
                        ("url", n.url),
                        ("updated_at", n.updated_at),
                    )
                    if v is not None
                }
                for n in sorted(unread, key=_notification_age, reverse=True)[
                    :NOTIFICATION_ATTRIBUTE_LIMIT
                ]
            ],
            "unread_truncated": max(len(unread) - NOTIFICATION_ATTRIBUTE_LIMIT, 0),
        }


class DevCloudOpenIssuesSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total open issues across repositories."""

    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "issues"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "open_issues")
        self._attr_name = "Open Issues"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "issues")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_open_issues": self.native_value}


class DevCloudOpenPullRequestsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total open pull requests across repositories."""

    _attr_icon = "mdi:source-pull"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "PRs"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "open_prs")
        self._attr_name = "Open Pull Requests"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "prs")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_open_prs": self.native_value}


class DevCloudStarsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for stars across all repositories."""

    _attr_icon = "mdi:star"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "stars"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "stars")
        self._attr_name = "Stars"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "stars")


class DevCloudWatchersSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for watchers across all repositories."""

    _attr_icon = "mdi:eye"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "watchers"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "watchers")
        self._attr_name = "Watchers"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "watchers")


class DevCloudForksSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for forks across all repositories."""

    _attr_icon = "mdi:source-fork"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "forks"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "forks")
        self._attr_name = "Forks"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "forks")


class DevCloudPullsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total pull/download count across all repositories or packages."""

    _attr_icon = "mdi:download"
    # TOTAL for the same reason as downloads: image pulls only ever accumulate.
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
        return _total(self.coordinator, "package_pulls")


class DevCloudReleasesSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total releases count."""

    _attr_icon = "mdi:tag-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "releases"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "releases")
        self._attr_name = "Releases"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "releases")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_releases": self.native_value}


class DevCloudReleaseAssetsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total release assets count."""

    _attr_icon = "mdi:attachment"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "assets"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "release_assets")
        self._attr_name = "Assets"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "assets")


class DevCloudDownloadsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for total downloads across all release assets."""

    _attr_icon = "mdi:download"
    # TOTAL, not MEASUREMENT: a download once served is never un-served, so the
    # period-over-period change is the meaningful figure. Everything that counts things
    # which can be deleted is a gauge instead.
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
        return _total(self.coordinator, "downloads")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"total_downloads": self.native_value}


class DevCloudSponsorsSensor(DevCloudBaseEntity, SensorEntity):
    """Sensor for active sponsors count."""

    _attr_icon = "mdi:heart"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "sponsors"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "sponsors")
        self._attr_name = "Sponsors"

    @property
    def native_value(self) -> StateType:
        """None rather than 0 while the count is genuinely unknown.

        A restored snapshot can say the resource was collected without carrying the figure,
        and reporting a confident zero there would be a lie that lasts until the next
        GraphQL refresh. Unknown is the honest state, and the entity stays registered.
        """
        if not self.coordinator.data:
            return None
        return self.coordinator.data.sponsors_count

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


class DevCloudSecurityAlertsSensor(DevCloudBaseEntity, SensorEntity):
    """Open dependency vulnerability alerts across the counted repositories."""

    _attr_icon = "mdi:shield-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "alerts"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "security_alerts")
        self._attr_name = "Security Alerts"

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return _total(self.coordinator, "security_alerts")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        totals = self.coordinator.totals
        if totals is None:
            return {}

        return {
            "total_alerts": totals.security_alerts,
            # Severity counts rather than the alerts themselves; the full list is in the
            # snapshot, where it does not cost a recorder write on every update.
            **{
                f"{name}_alerts": count
                for name, count in totals.security_alerts_by_severity.items()
            },
            "affected_repositories": totals.repositories_with_alerts,
            "highest_cvss": totals.highest_cvss,
        }


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
        """None rather than 0 while the count is genuinely unknown.

        Same reasoning as Sponsors: a restored snapshot can say the resource was collected
        without carrying the figure, and "no jobs running" is a claim this has no business
        making until something has actually looked.
        """
        if not self.coordinator.data:
            return None
        return self.coordinator.data.running_jobs_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "platform": self.coordinator.platform_id,
            "account": self.coordinator.account_name,
        }


class _DevCloudTrafficSensor(DevCloudBaseEntity, SensorEntity):
    """Shared base for the two accumulated traffic series.

    TOTAL rather than TOTAL_INCREASING, for the same reason as downloads: the history is
    trimmed once it passes the retention limit, and TOTAL_INCREASING would read that fall as
    a counter reset and add the whole figure again as if it were new traffic.
    """

    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 0

    #: Which accumulated series this sensor reports, set by each subclass.
    _series: str = ""

    @property
    def native_value(self) -> StateType:
        if not self.coordinator.data:
            return None
        return int(getattr(traffic_totals(self.coordinator), self._series))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        totals = traffic_totals(self.coordinator)
        return {
            f"unique_{self._series}": getattr(totals, f"unique_{self._series}"),
            # How far the rotating sweep has got, which is the honest caveat on the figure
            # above: repositories it has not reached yet contribute nothing.
            "repositories_with_traffic": totals.repositories,
            "days_recorded": totals.days,
            "top_referrers": top_referrers(self.coordinator, TRAFFIC_REFERRER_ATTRIBUTE_LIMIT),
        }


class DevCloudTrafficViewsSensor(_DevCloudTrafficSensor):
    """Accumulated repository views, past the fourteen days GitHub keeps."""

    _attr_icon = "mdi:chart-line"
    _attr_native_unit_of_measurement = "views"
    _series = "views"

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "traffic_views")
        self._attr_name = "Views"


class DevCloudTrafficClonesSensor(_DevCloudTrafficSensor):
    """Accumulated repository clones, past the fourteen days GitHub keeps."""

    _attr_icon = "mdi:content-copy"
    _attr_native_unit_of_measurement = "clones"
    _series = "clones"

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "traffic_clones")
        self._attr_name = "Clones"


class DevCloudLastUpdatedSensor(DevCloudBaseEntity, SensorEntity):
    """When the provider last returned a complete result.

    Diagnostic rather than a headline reading, and a timestamp rather than an age: Home
    Assistant renders a timestamp as a live "3 minutes ago" by itself, whereas a number of
    seconds would have to be re-recorded on every poll to stay true.
    """

    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "last_updated")
        self._attr_name = "Last Updated"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_updated

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-resource schedule, so a stalled collection is visible without the JSON."""
        if not self.coordinator.data:
            return {}
        resources = self.coordinator.data.resources
        return {
            "resources": sorted(resources),
            "next_due_in": {
                key: state.get("next_due_in")
                for key, state in sorted(resources.items())
                if state.get("next_due_in") is not None
            },
            "failing": sorted(key for key, state in resources.items() if state.get("failures")),
        }


def _notification_age(notification: NotificationData) -> str:
    """Sort key putting the most recently updated notification first.

    ISO-8601 timestamps sort correctly as strings, and an empty string sorts a
    notification with no timestamp to the end rather than raising.
    """
    return notification.updated_at or ""


def _total(coordinator: DevCloudCoordinator, field: str) -> StateType:
    """Read one precomputed aggregate.

    None before the first update completes, which Home Assistant renders as unknown.
    """
    totals = coordinator.totals
    return getattr(totals, field) if totals is not None else None


def _collected(coordinator: DevCloudCoordinator, *resources: str) -> bool:
    """Whether the data behind a sensor was gathered at all.

    The test is "did we collect this", never "is it non-empty". Zero unread notifications,
    zero open pull requests and zero stars are all readings a dashboard wants; a platform
    that has no notion of them at all is what should produce no sensor.
    """
    return any(resource in coordinator.data.collected for resource in resources)


#: Sensor classes paired with the test for whether this account has the data behind them.
#: Evaluated once at platform setup, so a sensor whose resource starts being collected
#: appears on the next reload.
_OPTIONAL_SENSORS: tuple[
    tuple[
        Callable[[DevCloudCoordinator], SensorEntity],
        Callable[[DevCloudCoordinator], bool],
    ],
    ...,
] = (
    (DevCloudRepositoriesSensor, lambda c: collection_total(c, "repos") is not None),
    (DevCloudOrganizationsSensor, lambda c: _collected(c, "orgs")),
    (DevCloudPastesSensor, lambda c: collection_total(c, "pastes") is not None),
    (DevCloudNotificationsSensor, lambda c: _collected(c, "notifications")),
    (DevCloudOpenIssuesSensor, lambda c: counted_issues(c) is not None),
    (DevCloudOpenPullRequestsSensor, lambda c: _collected(c, "prs")),
    # Stars, forks and watchers come off the repositories, so collecting those is what
    # makes them answerable — a repository with no stars still has an answer.
    (DevCloudStarsSensor, lambda c: _collected(c, "repos", "packages")),
    # Watchers come from the GraphQL walk, not the repository listing: REST only exposes a
    # deprecated alias for stars. Without that walk there is no watcher figure to report.
    (DevCloudWatchersSensor, lambda c: _collected(c, "repo_detail")),
    (DevCloudForksSensor, lambda c: _collected(c, "repos")),
    (DevCloudReleasesSensor, lambda c: _collected(c, "repo_detail")),
    (DevCloudReleaseAssetsSensor, lambda c: _collected(c, "repo_detail")),
    (DevCloudDownloadsSensor, lambda c: _collected(c, "repo_detail")),
    (DevCloudPullsSensor, lambda c: any(p.pull_count for p in c.data.packages)),
    (DevCloudSponsorsSensor, lambda c: _collected(c, "sponsors")),
    (DevCloudPackagesSensor, lambda c: _collected(c, "packages")),
    (DevCloudRunningJobsSensor, lambda c: _collected(c, "running_jobs")),
    # Traffic needs push access and a token, so most platforms never collect it.
    (DevCloudTrafficViewsSensor, lambda c: _collected(c, "traffic")),
    (DevCloudTrafficClonesSensor, lambda c: _collected(c, "traffic")),
    # Only GitHub reports these, so elsewhere the resource is never collected.
    (DevCloudSecurityAlertsSensor, lambda c: _collected(c, "repo_detail")),
)
