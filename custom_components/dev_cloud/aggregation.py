"""Which repositories and releases count towards an account's totals.

Separated from the sensor entities because this is policy, not presentation: an account's
own repositories always count, and organisation repositories count only when the entry's
`include_non_owned_orgs` option says they should. Every aggregate sensor reads from here so
the rule is stated once.
"""

from __future__ import annotations

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
