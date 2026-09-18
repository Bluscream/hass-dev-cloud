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
    """Releases belonging to repositories that count toward the totals."""
    allowed = {r.full_name for r in counted_repos(coordinator)}
    return [r for r in coordinator.data.releases if r.get("repository") in allowed]


def assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    """Asset list of a release, normalised so callers never handle a missing key."""
    assets = release.get("assets")
    return assets if isinstance(assets, list) else []
