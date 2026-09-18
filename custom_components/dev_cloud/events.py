"""Change detection between two polls.

Sensors say how much of something there is; events say what just happened. The difference
matters for notifications — "you have 312 stars" is a dashboard figure, "someone starred
VRCOSC-Modules" is worth a push.

Detection is a pure function of two snapshots, so it is testable without Home Assistant and
cannot accidentally depend on anything but the data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .const import (
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_ORG,
    EVENT_NEW_PACKAGE,
    EVENT_NEW_RELEASE,
    EVENT_NEW_REPO,
    EVENT_ORG_REMOVED,
    EVENT_PACKAGE_REMOVED,
    EVENT_REPO_REMOVED,
    EVENT_STARS_CHANGED,
)
from .models import DevCloudData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RepoState:
    """The parts of a repository worth reacting to a change in."""

    url: str
    stars: int
    forks: int
    release_tags: frozenset[str]


@dataclass(frozen=True, slots=True)
class AccountState:
    """Everything change detection compares, reduced to what it needs.

    Deliberately not the full snapshot: holding one of those per account purely to diff
    against would double the integration's memory for no gain.
    """

    repos: dict[str, RepoState] = field(default_factory=dict)
    packages: frozenset[str] = frozenset()
    orgs: frozenset[str] = frozenset()
    notifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Which collections were actually fetched, so an absent one is never read as emptied.
    collected: frozenset[str] = frozenset()


def snapshot(data: DevCloudData) -> AccountState:
    """Reduce a poll result to the parts change detection compares."""
    repos = {
        repo.full_name: RepoState(
            url=repo.url,
            stars=repo.stars,
            forks=repo.forks,
            release_tags=frozenset(
                str(r.get("tag")) for r in repo.releases if r.get("tag") is not None
            ),
        )
        for repo in data.repos
        if repo.full_name
    }
    return AccountState(
        repos=repos,
        packages=frozenset(p.name for p in data.packages),
        orgs=frozenset(o.name for o in data.orgs),
        notifications={
            n.notification_id: {
                "title": n.title,
                "repository": n.repository,
                "url": n.url,
                "reason": n.reason,
                "subject_type": n.subject_type,
            }
            for n in data.notifications
            if n.unread
        },
        collected=frozenset(data.collected),
    )


def changes(
    previous: AccountState | None, current: AccountState
) -> list[tuple[str, dict[str, Any]]]:
    """Events describing what changed between two polls.

    Returns nothing when there is no baseline: the first poll of a process would otherwise
    announce every repository, package and organisation that already existed.
    """
    if previous is None:
        return []

    events: list[tuple[str, dict[str, Any]]] = []
    events += _repo_changes(previous, current)
    events += _membership_changes(previous, current)
    events += _notification_changes(previous, current)
    return events


def _fetched(previous: AccountState, current: AccountState, resource: str) -> bool:
    """Whether a collection can be compared at all this poll.

    A collection that was not fetched, or that came back empty after being populated, is a
    failed fetch rather than a mass deletion — and announcing 580 removals because one
    request failed is the worst thing this module could do.
    """
    return resource in current.collected and resource in previous.collected


def _repo_changes(
    previous: AccountState, current: AccountState
) -> list[tuple[str, dict[str, Any]]]:
    if not _fetched(previous, current, "repos"):
        return []
    if previous.repos and not current.repos:
        _LOGGER.debug("Repository listing came back empty; treating as a failed fetch")
        return []

    events: list[tuple[str, dict[str, Any]]] = []

    for name in current.repos.keys() - previous.repos.keys():
        events.append((EVENT_NEW_REPO, {"repository": name, "url": current.repos[name].url}))
    for name in previous.repos.keys() - current.repos.keys():
        events.append((EVENT_REPO_REMOVED, {"repository": name}))

    for name in current.repos.keys() & previous.repos.keys():
        was, now = previous.repos[name], current.repos[name]

        if now.stars != was.stars:
            events.append(
                (
                    EVENT_STARS_CHANGED,
                    {
                        "repository": name,
                        "url": now.url,
                        "stars": now.stars,
                        "previous_stars": was.stars,
                        "delta": now.stars - was.stars,
                    },
                )
            )

        for tag in now.release_tags - was.release_tags:
            events.append((EVENT_NEW_RELEASE, {"repository": name, "tag": tag, "url": now.url}))

    return events


def _membership_changes(
    previous: AccountState, current: AccountState
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    for resource, added_event, removed_event, field_name, attr in (
        ("packages", EVENT_NEW_PACKAGE, EVENT_PACKAGE_REMOVED, "package", "packages"),
        ("orgs", EVENT_NEW_ORG, EVENT_ORG_REMOVED, "organization", "orgs"),
    ):
        if not _fetched(previous, current, resource):
            continue
        was: frozenset[str] = getattr(previous, attr)
        now: frozenset[str] = getattr(current, attr)
        if was and not now:
            continue
        events += [(added_event, {field_name: name}) for name in now - was]
        events += [(removed_event, {field_name: name}) for name in was - now]

    return events


def _notification_changes(
    previous: AccountState, current: AccountState
) -> list[tuple[str, dict[str, Any]]]:
    if not _fetched(previous, current, "notifications"):
        return []

    return [
        (EVENT_NEW_NOTIFICATION, {"id": key, **current.notifications[key]})
        for key in current.notifications.keys() - previous.notifications.keys()
    ]
