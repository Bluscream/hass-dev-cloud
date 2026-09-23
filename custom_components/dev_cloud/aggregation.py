"""Which repositories and releases count towards an account's totals.

Separated from the sensor entities because this is policy, not presentation: an account's
own repositories always count, and organisation repositories count only when the entry's
`include_non_owned_orgs` option says they should. Every aggregate sensor reads from here so
the rule is stated once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import RepoData

if TYPE_CHECKING:
    from .coordinator import DevCloudCoordinator


def counted_repos(coordinator: DevCloudCoordinator) -> list[RepoData]:
    """The account's own repositories plus those of organisations that count toward totals.

    Which organisations count is the entry's `include_non_owned_orgs` option: with it off
    (the default) only orgs the account owns contribute, so the totals describe the user
    rather than every organisation they happen to belong to.
    """
    data = coordinator.data
    repos = list(data.repos)
    for org in data.orgs:
        if coordinator.include_non_owned_orgs or org.is_owned:
            repos.extend(org.repos)
    return repos


def counted_releases(coordinator: DevCloudCoordinator) -> list[dict[str, Any]]:
    """Releases of every repository that counts towards the totals.

    Flattened from the repositories rather than read from a list beside them, so the
    organisation rule applies here exactly as it does to stars and forks.
    """
    return [release for repo in counted_repos(coordinator) for release in repo.releases]


def assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    """Asset list of a release, normalised so callers never handle a missing key."""
    assets = release.get("assets")
    return assets if isinstance(assets, list) else []


def collection_total(coordinator: DevCloudCoordinator, field: str) -> int | None:
    """Size of a collection, whether or not it was enumerated.

    In detailed mode the list is complete and its length is authoritative. In summary mode
    the list is absent and the provider publishes the total the API reported instead — which
    is why that total exists only when there is no list to measure.
    """
    data = coordinator.data
    if field in data.collected:
        return len(getattr(data, field, ()))
    return data.totals.get(field)


def counted_issues(coordinator: DevCloudCoordinator) -> int | None:
    """Open issues across every repository that counts towards this entry's totals.

    Summed from each repository's own list. Platforms that never enumerate issues report a
    per-repository count instead, which is used when no lists were fetched at all.
    """
    return _counted_sub_items(coordinator, "issues", "open_issues")


def counted_prs(coordinator: DevCloudCoordinator) -> int | None:
    """Open pull requests across every repository that counts towards this entry's totals."""
    return _counted_sub_items(coordinator, "prs", None)


def _counted_sub_items(
    coordinator: DevCloudCoordinator, field: str, count_attr: str | None
) -> int | None:
    repos = counted_repos(coordinator)
    if field in coordinator.data.collected:
        return sum(len(getattr(repo, field, ())) for repo in repos)
    # Platforms that never enumerate them report a per-repository count instead.
    if count_attr is not None and any(getattr(repo, count_attr, 0) for repo in repos):
        return sum(getattr(repo, count_attr, 0) for repo in repos)
    return None


@dataclass(frozen=True, slots=True)
class Totals:
    """Every aggregate the sensors report, computed once per coordinator update.

    The underlying walks are individually cheap — a few hundred microseconds across 930
    repositories — but sensor properties are read far more often than the data changes: on
    every state write, every template render and every dashboard subscription. Computing
    them once per update turns fifteen repeated walks into one, and each property read into
    an attribute lookup.
    """

    repositories: int | None
    counted_repositories: int
    public_repositories: int
    private_repositories: int
    stars: int
    forks: int
    watchers: int
    releases: int
    assets: int
    downloads: int
    issues: int | None
    prs: int | None
    pastes: int | None
    packages: int
    package_pulls: int
    package_stars: int
    security_alerts: int
    security_alerts_by_severity: dict[str, int]
    repositories_with_alerts: int
    highest_cvss: float | None


def compute_totals(coordinator: DevCloudCoordinator) -> Totals:
    """Walk the repositories once and derive everything from that single pass."""
    data = coordinator.data
    repos = data.repos
    counted = counted_repos(coordinator)
    releases = counted_releases(coordinator)
    all_assets = [asset for release in releases for asset in assets(release)]

    alerts = [alert for repo in counted for alert in repo.security_alerts]
    severities: dict[str, int] = {}
    for alert in alerts:
        key = str(alert.get("severity") or "UNKNOWN").lower()
        severities[key] = severities.get(key, 0) + 1

    return Totals(
        repositories=collection_total(coordinator, "repos"),
        counted_repositories=len(counted),
        public_repositories=sum(1 for r in repos if not r.is_private),
        private_repositories=sum(1 for r in repos if r.is_private),
        stars=sum(r.stars for r in counted) + sum(p.star_count or 0 for p in data.packages),
        forks=sum(r.forks for r in counted),
        watchers=sum(r.watchers for r in counted),
        releases=len(releases),
        assets=len(all_assets),
        downloads=sum(int(a.get("downloads", 0) or 0) for a in all_assets),
        issues=counted_issues(coordinator),
        prs=counted_prs(coordinator),
        pastes=collection_total(coordinator, "pastes"),
        packages=len(data.packages),
        package_pulls=sum(p.pull_count or 0 for p in data.packages),
        package_stars=sum(p.star_count or 0 for p in data.packages),
        security_alerts=len(alerts),
        security_alerts_by_severity=severities,
        repositories_with_alerts=sum(1 for repo in counted if repo.security_alerts),
        highest_cvss=max((float(a.get("cvss") or 0) for a in alerts), default=None) or None,
    )


@dataclass(frozen=True, slots=True)
class TrafficTotals:
    """Lifetime traffic across the repositories that count towards this entry.

    Summed from the accumulated daily buckets rather than from one API response, so these
    keep growing after GitHub's own graphs have forgotten — which is why the cache exists.
    """

    views: int = 0
    clones: int = 0
    unique_views: int = 0
    unique_clones: int = 0
    #: Repositories with any traffic history at all, which is how much of the rotating
    #: sweep has been round so far.
    repositories: int = 0
    #: Distinct days on record, counted per repository, so a figure that only ever grows.
    days: int = 0


def _series_total(repos: list[RepoData], series: str) -> tuple[int, int]:
    """Count and uniques summed over one accumulated daily series."""
    count = uniques = 0
    for repo in repos:
        for day in (repo.traffic.get(series) or {}).values():
            count += int(day.get("count", 0) or 0)
            uniques += int(day.get("uniques", 0) or 0)
    return count, uniques


def traffic_totals(coordinator: DevCloudCoordinator) -> TrafficTotals:
    """Add up every repository's accumulated view and clone history."""
    repos = [repo for repo in counted_repos(coordinator) if repo.traffic]
    views, unique_views = _series_total(repos, "views")
    clones, unique_clones = _series_total(repos, "clones")

    return TrafficTotals(
        views=views,
        clones=clones,
        unique_views=unique_views,
        unique_clones=unique_clones,
        repositories=len(repos),
        days=sum(
            len(repo.traffic.get(series) or {}) for repo in repos for series in ("views", "clones")
        ),
    )


def top_referrers(coordinator: DevCloudCoordinator, limit: int) -> list[dict[str, Any]]:
    """The busiest referring sites across every repository, most recent window first.

    Counts describe GitHub's rolling fourteen-day window, so they are summed across
    repositories but never across time — adding successive windows would count one visit
    once per sweep.
    """
    combined: dict[str, dict[str, Any]] = {}
    for repo in counted_repos(coordinator):
        for name, entry in (repo.traffic.get("referrers") or {}).items():
            into = combined.setdefault(
                name, {"referrer": name, "count": 0, "uniques": 0, "first_seen": ""}
            )
            into["count"] += int(entry.get("count", 0) or 0)
            into["uniques"] += int(entry.get("uniques", 0) or 0)
            seen = str(entry.get("first_seen") or "")
            if not into["first_seen"] or (seen and seen < into["first_seen"]):
                into["first_seen"] = seen

    return sorted(combined.values(), key=lambda r: int(r["count"]), reverse=True)[:limit]


def counted_security_alerts(coordinator: DevCloudCoordinator) -> list[dict[str, Any]]:
    """Open vulnerability alerts across the repositories that count for this entry."""
    return [alert for repo in counted_repos(coordinator) for alert in repo.security_alerts]
