"""Change detection between two polls.

Sensors say how much of something there is; events say what just happened. "You have 312
stars" is a dashboard figure — "someone starred VRCOSC-Modules, it went 41 to 42" is worth a
push, and carries enough to write the message from.

Detection runs over the **serialised snapshots** rather than the live objects, for three
reasons. The provider mutates its cached objects in place, so holding a reference to the
previous poll would compare a thing against itself. The snapshot is already built every poll
for writing, so diffing it costs nothing extra. And it is the same document reloaded at
startup, so events survive a restart instead of starting from no baseline.

Every payload carries the whole item, old and new where both exist, so an automation never
has to go looking anything up.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    EVENT_BRANCH_REMOVED,
    EVENT_FORKS_CHANGED,
    EVENT_ISSUE_CLOSED,
    EVENT_NEW_BRANCH,
    EVENT_NEW_DOWNLOADS,
    EVENT_NEW_ISSUE,
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_ORG,
    EVENT_NEW_PACKAGE,
    EVENT_NEW_PR,
    EVENT_NEW_PULLS,
    EVENT_NEW_RELEASE,
    EVENT_NEW_REPO,
    EVENT_NEW_SECURITY_ALERT,
    EVENT_NEW_TAG,
    EVENT_ORG_REMOVED,
    EVENT_PACKAGE_CHANGED,
    EVENT_PACKAGE_REMOVED,
    EVENT_PR_CLOSED,
    EVENT_RELEASE_CHANGED,
    EVENT_RELEASE_REMOVED,
    EVENT_REPO_ARCHIVED,
    EVENT_REPO_CHANGED,
    EVENT_REPO_REMOVED,
    EVENT_REPO_RENAMED,
    EVENT_REPO_VISIBILITY_CHANGED,
    EVENT_SECURITY_ALERTS_RESOLVED,
    EVENT_STARS_CHANGED,
    EVENT_TAG_REMOVED,
)

_LOGGER = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]
Item = dict[str, Any]

#: Repository fields worth an event when they change. Deliberately excludes anything that
#: moves on its own — download counts tick upward constantly and would fire every poll.
_REPO_WATCHED = ("stars", "forks", "watchers", "description", "default_branch", "upstream")
_RELEASE_WATCHED = ("name", "tag", "published_at")
_PACKAGE_WATCHED = ("version",)


def _by(items: list[Item] | None, key: str) -> dict[str, Item]:
    """Index a list of serialised items by one of their fields."""
    return {str(i[key]): i for i in items or [] if isinstance(i, dict) and i.get(key) is not None}


def _changed_fields(old: Item, new: Item, watched: tuple[str, ...]) -> list[str]:
    return [f for f in watched if old.get(f) != new.get(f)]


def _comparable(previous: Item, current: Item, resource: str, collection: str) -> bool:
    """Whether a collection can be diffed at all this poll.

    Not collected means the previous value was reused, so there is nothing to compare. A
    collection that emptied entirely is treated as a failed response rather than a mass
    deletion — announcing 580 removals because one request failed is the worst thing this
    module could do.
    """
    if resource not in set(previous.get("collected") or ()) | {"__always__"}:
        return False
    if resource not in set(current.get("collected") or ()):
        return False
    if previous.get(collection) and not current.get(collection):
        _LOGGER.debug("%s came back empty; treating as a failed fetch, not a deletion", collection)
        return False
    return True


def changes(previous: Item | None, current: Item) -> list[Event]:
    """Every change between two serialised snapshots.

    Returns nothing without a baseline: the first poll of a fresh account would otherwise
    announce every repository, package and organisation that already existed.

    Download counters are reported per repository rather than per asset: an account can
    hold thousands of assets, but only a handful of repositories move between polls.
    """
    if not previous:
        return []

    events: list[Event] = []
    events += _repo_changes(previous, current)
    events += _simple_collection(
        previous, current, "orgs", "name", EVENT_NEW_ORG, EVENT_ORG_REMOVED, "organization"
    )
    events += _package_changes(previous, current)
    events += _notification_changes(previous, current)

    if _comparable(previous, current, "packages", "packages"):
        events += _pull_changes(previous, current)

    return events


def _all_repos(payload: Item) -> list[Item]:
    """Every repository in a snapshot, the account's own and its organisations'.

    Organisation repositories are compared too: a star on one of them, or a new advisory,
    is the same event as on any other repository. Whether they count towards the *totals*
    is a separate question, decided per entry by the organisation option.
    """
    repos = list(payload.get("repos") or [])
    repos += [r for org in payload.get("orgs") or [] for r in org.get("repos") or []]
    return repos


def _repo_changes(previous: Item, current: Item) -> list[Event]:
    if not _comparable(previous, current, "repos", "repos"):
        return []

    was, now = _by(_all_repos(previous), "full_name"), _by(_all_repos(current), "full_name")
    if was and not now:
        return []
    events: list[Event] = []

    for name in now.keys() - was.keys():
        events.append((EVENT_NEW_REPO, {"repository": name, "repo": now[name]}))
    for name in was.keys() - now.keys():
        events.append((EVENT_REPO_REMOVED, {"repository": name, "repo": was[name]}))

    for name in now.keys() & was.keys():
        events += _one_repo(name, was[name], now[name])

    return events


def _one_repo(name: str, old: Item, new: Item) -> list[Event]:
    """Changes within a single repository, from the headline down to its refs."""
    events: list[Event] = []
    common = {"repository": name, "old": old, "new": new}

    if old.get("stars") != new.get("stars"):
        events.append(
            (
                EVENT_STARS_CHANGED,
                {
                    **common,
                    "stars": new.get("stars", 0),
                    "previous_stars": old.get("stars", 0),
                    "delta": new.get("stars", 0) - old.get("stars", 0),
                },
            )
        )
    if old.get("forks") != new.get("forks"):
        events.append(
            (
                EVENT_FORKS_CHANGED,
                {
                    **common,
                    "forks": new.get("forks", 0),
                    "previous_forks": old.get("forks", 0),
                    "delta": new.get("forks", 0) - old.get("forks", 0),
                },
            )
        )
    if old.get("is_archived") != new.get("is_archived"):
        events.append((EVENT_REPO_ARCHIVED, {**common, "archived": bool(new.get("is_archived"))}))
    if old.get("is_private") != new.get("is_private"):
        events.append(
            (EVENT_REPO_VISIBILITY_CHANGED, {**common, "private": bool(new.get("is_private"))})
        )
    if old.get("name") != new.get("name"):
        events.append(
            (
                EVENT_REPO_RENAMED,
                {**common, "previous_name": old.get("name"), "name": new.get("name")},
            )
        )

    fields = _changed_fields(old, new, _REPO_WATCHED)
    if fields:
        events.append((EVENT_REPO_CHANGED, {**common, "changed": fields}))

    events += _release_changes(name, old, new)
    events += _security_changes(name, old, new)
    events += _download_changes(name, old, new)
    events += _ref_changes(
        name, old, new, "branches", EVENT_NEW_BRANCH, EVENT_BRANCH_REMOVED, "branch"
    )
    events += _ref_changes(name, old, new, "tags", EVENT_NEW_TAG, EVENT_TAG_REMOVED, "tag")
    events += _thread_changes(
        name, old, new, "issues", EVENT_NEW_ISSUE, EVENT_ISSUE_CLOSED, "issue"
    )
    events += _thread_changes(name, old, new, "prs", EVENT_NEW_PR, EVENT_PR_CLOSED, "pull_request")
    return events


def _release_changes(repository: str, old: Item, new: Item) -> list[Event]:
    was, now = _by(old.get("releases"), "tag"), _by(new.get("releases"), "tag")
    events: list[Event] = []

    for tag in now.keys() - was.keys():
        events.append(
            (EVENT_NEW_RELEASE, {"repository": repository, "tag": tag, "release": now[tag]})
        )
    for tag in was.keys() - now.keys():
        events.append(
            (EVENT_RELEASE_REMOVED, {"repository": repository, "tag": tag, "release": was[tag]})
        )
    for tag in now.keys() & was.keys():
        fields = _changed_fields(was[tag], now[tag], _RELEASE_WATCHED)
        if fields:
            events.append(
                (
                    EVENT_RELEASE_CHANGED,
                    {
                        "repository": repository,
                        "tag": tag,
                        "old": was[tag],
                        "new": now[tag],
                        "changed": fields,
                    },
                )
            )
    return events


def _security_changes(repository: str, old: Item, new: Item) -> list[Event]:
    """New vulnerability alerts one by one; resolutions batched.

    A new alert is something to act on, so each gets its own event with the advisory
    attached. Resolutions arrive in bulk — one dependency bump can clear dozens at once, and
    one repository here has 68 open — so they are summarised per repository instead.
    """
    was = _by(old.get("security_alerts"), "number")
    now = _by(new.get("security_alerts"), "number")
    if not was and not now:
        return []

    events: list[Event] = [
        (
            EVENT_NEW_SECURITY_ALERT,
            {"repository": repository, "alert": now[n], **now[n]},
        )
        for n in sorted(now.keys() - was.keys())
    ]

    resolved = [was[n] for n in sorted(was.keys() - now.keys())]
    if resolved:
        events.append(
            (
                EVENT_SECURITY_ALERTS_RESOLVED,
                {
                    "repository": repository,
                    "resolved": len(resolved),
                    "remaining": len(now),
                    "alerts": resolved,
                },
            )
        )
    return events


def _download_changes(repository: str, old: Item, new: Item) -> list[Event]:
    """One event per repository whose assets gained downloads.

    Per asset would be unusable — this account holds 3,581 of them — but per repository is
    exactly one notification per thing that actually moved, with the per-asset detail
    attached for the message.
    """
    was = _asset_downloads(old)
    now = _asset_downloads(new)
    gains = [
        {"tag": tag, "asset": asset, "delta": count - was.get((tag, asset), 0)}
        for (tag, asset), count in now.items()
        if count > was.get((tag, asset), 0)
    ]
    if not gains:
        return []

    return [
        (
            EVENT_NEW_DOWNLOADS,
            {
                "repository": repository,
                "delta": sum(g["delta"] for g in gains),
                "total": sum(now.values()),
                "previous_total": sum(was.get(k, 0) for k in now),
                "assets": len(gains),
                "breakdown": sorted(gains, key=_delta, reverse=True)[:10],
            },
        )
    ]


def _delta(gain: dict[str, Any]) -> int:
    """Sort key for the per-asset breakdown."""
    value = gain.get("delta", 0)
    return value if isinstance(value, int) else 0


def _asset_downloads(repo: Item) -> dict[tuple[str, str], int]:
    """Download count for every asset of one repository, keyed by tag and asset name."""
    return {
        (str(release.get("tag")), str(asset.get("name"))): int(asset.get("downloads", 0) or 0)
        for release in repo.get("releases") or []
        for asset in release.get("assets") or []
    }


def _pull_changes(previous: Item, current: Item) -> list[Event]:
    """One event per image that gained pulls."""
    was = {
        str(p.get("name")): int(p.get("pull_count", 0) or 0) for p in previous.get("packages") or []
    }
    events: list[Event] = []

    for package in current.get("packages") or []:
        name = str(package.get("name"))
        after = int(package.get("pull_count", 0) or 0)
        before = was.get(name, 0)
        if after > before:
            events.append(
                (
                    EVENT_NEW_PULLS,
                    {
                        "name": name,
                        "package": package,
                        "pulls": after,
                        "previous_pulls": before,
                        "delta": after - before,
                    },
                )
            )
    return events


def _ref_changes(
    repository: str, old: Item, new: Item, field: str, added: str, removed: str, label: str
) -> list[Event]:
    was, now = _by(old.get(field), "name"), _by(new.get(field), "name")
    return [
        *(
            (added, {"repository": repository, label: now[n], "name": n})
            for n in now.keys() - was.keys()
        ),
        *(
            (removed, {"repository": repository, label: was[n], "name": n})
            for n in was.keys() - now.keys()
        ),
    ]


def _thread_changes(
    repository: str, old: Item, new: Item, field: str, opened: str, closed: str, label: str
) -> list[Event]:
    """Issues and pull requests. The lists hold only open ones, so a disappearance means it
    was closed or merged rather than deleted."""
    was, now = _by(old.get(field), "number"), _by(new.get(field), "number")
    return [
        *((opened, {"repository": repository, label: now[n]}) for n in now.keys() - was.keys()),
        *((closed, {"repository": repository, label: was[n]}) for n in was.keys() - now.keys()),
    ]


def _simple_collection(
    previous: Item, current: Item, collection: str, key: str, added: str, removed: str, label: str
) -> list[Event]:
    if not _comparable(previous, current, collection, collection):
        return []
    was, now = _by(previous.get(collection), key), _by(current.get(collection), key)
    return [
        *((added, {label: now[n], "name": n}) for n in now.keys() - was.keys()),
        *((removed, {label: was[n], "name": n}) for n in was.keys() - now.keys()),
    ]


def _package_changes(previous: Item, current: Item) -> list[Event]:
    if not _comparable(previous, current, "packages", "packages"):
        return []
    was, now = _by(previous.get("packages"), "name"), _by(current.get("packages"), "name")
    events: list[Event] = [
        *((EVENT_NEW_PACKAGE, {"package": now[n], "name": n}) for n in now.keys() - was.keys()),
        *((EVENT_PACKAGE_REMOVED, {"package": was[n], "name": n}) for n in was.keys() - now.keys()),
    ]
    for name in now.keys() & was.keys():
        fields = _changed_fields(was[name], now[name], _PACKAGE_WATCHED)
        if fields:
            events.append(
                (
                    EVENT_PACKAGE_CHANGED,
                    {"name": name, "old": was[name], "new": now[name], "changed": fields},
                )
            )
    return events


def _notification_changes(previous: Item, current: Item) -> list[Event]:
    if not _comparable(previous, current, "notifications", "notifications"):
        return []
    was = _by(previous.get("notifications"), "notification_id")
    now = _by(current.get("notifications"), "notification_id")
    return [
        (EVENT_NEW_NOTIFICATION, {"notification": now[n], **now[n]})
        for n in now.keys() - was.keys()
        if now[n].get("unread", True)
    ]
