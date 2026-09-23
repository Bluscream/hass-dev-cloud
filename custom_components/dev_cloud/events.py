"""Change detection between two polls.

Sensors say how much of something there is; events say what just happened. "You have 312
stars" is a dashboard figure — "someone starred VRCOSC-Modules, it went 41 to 42" is worth a
push, and carries enough to write the message from.

Detection runs over the **serialised snapshots**, after a poll has finished, rather than over
the live objects while one is in flight. The provider mutates its cached objects in place, so
holding a reference to the previous poll would compare a thing against itself. The snapshot
is already built every poll for writing, so diffing it costs nothing extra. And it is the
same document reloaded at startup, so events survive a restart instead of starting from no
baseline.

A poll is routinely *partial*: each resource has its own refresh schedule, and one that was
not due keeps serving its previous value. Those compare equal and produce nothing, so a
partial scrape reports exactly the parts that moved.

Two event types leave this module, no matter how many kinds of change were found:

* ``dev_cloud_update`` — every change in the poll, batched into size-bounded chunks so a
  busy poll cannot exceed the recorder's per-event limit.
* ``dev_cloud_notification`` — one per newly arrived unread notification, which wants to be
  a notification in its own right rather than a line in a digest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final, TypedDict

_LOGGER = logging.getLogger(__name__)

Item = dict[str, Any]


class Change(TypedDict, total=False):
    """One thing that changed, rendered as a single line by a consumer.

    ``kind`` is the specific change ("stars_changed"); ``thing`` is the coarse category it
    belongs to ("star"), which is what a notification picks an emoji from. Keeping both
    means a consumer can group by category without having to know every kind.
    """

    kind: str
    thing: str
    #: What the change is about, already human-readable: a repository name, a tag, "#42".
    subject: str
    repository: str
    url: str
    #: Scalar before/after, present on anything that changed value rather than appeared.
    old: Any
    new: Any
    #: Signed difference, present only when both sides are numeric.
    delta: float
    #: True the first time this metric moved off nothing — the first star, the first fork,
    #: the first clone. Computed here because only the diff knows the previous value.
    first: bool
    #: Kind-specific extras, deliberately small. Anything bulky stays in the snapshot.
    detail: dict[str, Any]


#: Coarse categories, used by consumers to pick an emoji per kind of thing.
THING_REPO: Final = "repository"
THING_STAR: Final = "star"
THING_FORK: Final = "fork"
THING_WATCHER: Final = "watcher"
THING_RELEASE: Final = "release"
THING_DOWNLOAD: Final = "download"
THING_BRANCH: Final = "branch"
THING_TAG: Final = "tag"
THING_ISSUE: Final = "issue"
THING_PR: Final = "pull_request"
THING_PACKAGE: Final = "package"
THING_PULL: Final = "pull"
THING_ORG: Final = "organization"
THING_SECURITY: Final = "security"
THING_NOTIFICATION: Final = "notification"
THING_VIEW: Final = "view"
THING_CLONE: Final = "clone"
THING_REFERRER: Final = "referrer"

#: Repository fields worth a line when they change. Stars, forks and watchers are absent on
#: purpose: each gets its own numeric change below, and listing them here too would report
#: the same star twice. Download counts are absent because they move on their own.
_REPO_WATCHED: Final = ("description", "default_branch", "upstream")
_RELEASE_WATCHED: Final = ("name", "tag", "published_at")
_PACKAGE_WATCHED: Final = ("version",)

#: Repository counters that get a dedicated change with a signed delta.
_REPO_METRICS: Final = (
    ("stars", THING_STAR),
    ("forks", THING_FORK),
    ("watchers", THING_WATCHER),
)

#: Fields the repository-detail walk supplies. A repository carrying none of them was not
#: covered by that walk on this side of the diff, which is emphatically not the same as
#: having none of them: a field nobody measured falls back to its default, and a default
#: zero is indistinguishable from a measured zero. Reading one as the other is how a single
#: poll announced 266 repositories gaining their first watcher simultaneously.
_DETAIL_EVIDENCE: Final = ("releases", "branches", "tags", "security_alerts")

#: Counters that come from that walk rather than from the repository listing.
_DETAIL_METRICS: Final = frozenset({"watchers"})

#: Collections nested inside a repository. Carried in the snapshot, never in a payload —
#: one repository's releases and their assets are larger than the whole event budget.
_BULKY_REPO_KEYS: Final = frozenset(
    {"releases", "branches", "tags", "issues", "prs", "security_alerts"}
)

#: Home Assistant's recorder rejects event data past 32 KiB. Chunks are built well under it:
#: the figure below is the payload budget for the changes themselves, leaving the rest for
#: the envelope (platform, account, run id, chunk counters) and for JSON's own overhead.
MAX_EVENT_DATA_BYTES: Final = 32_768
CHUNK_BUDGET_BYTES: Final = 24_000

#: A single change larger than this is truncated rather than dropped: losing the detail is
#: recoverable from the snapshot, losing the whole event is not.
MAX_CHANGE_BYTES: Final = 8_000


@dataclass(slots=True)
class Diff:
    """Everything one poll turned up, split by how it wants to be delivered."""

    updates: list[Change] = field(default_factory=list)
    notifications: list[Change] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.updates or self.notifications)


def diff(previous: Item | None, current: Item) -> Diff:
    """Every change between two serialised snapshots.

    Returns nothing without a baseline: the first poll of a fresh account would otherwise
    announce every repository, package and organisation that already existed.
    """
    if not previous:
        return Diff()

    updates: list[Change] = []
    updates += _repo_changes(previous, current)
    updates += _org_changes(previous, current)
    updates += _package_changes(previous, current)

    if _comparable(previous, current, "packages", "packages"):
        updates += _pull_changes(previous, current)

    return Diff(updates=updates, notifications=_notification_changes(previous, current))


def chunked(changes: list[Change]) -> list[list[Change]]:
    """Split changes into batches that each fit inside one event.

    Measured by serialising, not estimated from item counts: a repository description and a
    security advisory differ by two orders of magnitude, so any per-item guess is wrong in
    one direction or the other. A single change too large to ever fit is truncated to its
    identifying fields rather than dropped.
    """
    batches: list[list[Change]] = []
    current: list[Change] = []
    size = 0

    for change in changes:
        measured = _sizeof(change)
        if measured > MAX_CHANGE_BYTES:
            change = _truncate(change)
            measured = _sizeof(change)

        if current and size + measured > CHUNK_BUDGET_BYTES:
            batches.append(current)
            current, size = [], 0

        current.append(change)
        size += measured

    if current:
        batches.append(current)
    return batches


def _sizeof(change: Change) -> int:
    """Serialised size of one change, including the comma that will follow it."""
    return len(json.dumps(change, default=str, separators=(",", ":"))) + 1


def _truncate(change: Change) -> Change:
    """Reduce an oversized change to what identifies it, keeping the event deliverable."""
    _LOGGER.debug(
        "Change %s for %s exceeded the per-change budget; dropping its detail",
        change.get("kind"),
        change.get("subject"),
    )
    # Drops the bulky fields rather than whitelisting the small ones: `old`, `new` and
    # `detail` are the only ones that can grow without bound, and everything that identifies
    # the change survives untouched.
    kept = change.copy()
    kept.pop("old", None)
    kept.pop("new", None)
    kept["detail"] = {"truncated": True}
    return kept


def _by(items: list[Item] | None, key: str) -> dict[str, Item]:
    """Index a list of serialised items by one of their fields."""
    return {str(i[key]): i for i in items or [] if isinstance(i, dict) and i.get(key) is not None}


def _changed_fields(old: Item, new: Item, watched: tuple[str, ...]) -> list[str]:
    return [f for f in watched if old.get(f) != new.get(f)]


def _prune_repo(repo: Item) -> Item:
    """A repository without its nested collections, small enough to carry in a payload."""
    return {k: v for k, v in repo.items() if k not in _BULKY_REPO_KEYS}


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


def _removals_trustworthy(old: Item, new: Item, field_name: str, repository: str) -> bool:
    """Whether a nested collection emptying means things were really deleted.

    The parent survived this poll, so a collection under it going from populated to empty is
    far more likely to be a rate-limited or failed sub-request than an account deleting
    every release it had. Additions are still reported either way — a spurious addition
    cannot happen, because an empty response adds nothing.

    Removals *are* reported when the parent itself disappeared: that path never reaches here,
    it emits one change for the parent carrying its last known state.
    """
    if old.get(field_name) and not new.get(field_name):
        _LOGGER.debug(
            "%s of %s came back empty while the repository survived; "
            "treating as an incomplete fetch, not a deletion",
            field_name,
            repository,
        )
        return False
    return True


def _has_detail(repo: Item) -> bool:
    """Whether the detail walk covered this repository in this snapshot."""
    return any(repo.get(field) for field in _DETAIL_EVIDENCE)


def _first(old: Any, new: Any) -> bool:
    """Whether a counter just moved off nothing for the first time."""
    return not old and bool(new)


def _all_repos(payload: Item) -> list[Item]:
    """Every repository in a snapshot, the account's own and its organisations'.

    Organisation repositories are compared too: a star on one of them, or a new advisory,
    is the same event as on any other repository. Whether they count towards the *totals*
    is a separate question, decided per entry by the organisation option.
    """
    repos = list(payload.get("repos") or [])
    repos += [r for org in payload.get("orgs") or [] for r in org.get("repos") or []]
    return repos


def _repo_changes(previous: Item, current: Item) -> list[Change]:
    if not _comparable(previous, current, "repos", "repos"):
        return []

    was, now = _by(_all_repos(previous), "full_name"), _by(_all_repos(current), "full_name")
    if was and not now:
        return []

    changes: list[Change] = [
        {
            "kind": "new_repo",
            "thing": THING_REPO,
            "subject": name,
            "repository": name,
            "url": str(now[name].get("url") or ""),
            "new": _prune_repo(now[name]),
        }
        for name in sorted(now.keys() - was.keys())
    ]
    changes += [
        {
            "kind": "repo_removed",
            "thing": THING_REPO,
            "subject": name,
            "repository": name,
            "url": str(was[name].get("url") or ""),
            "old": _prune_repo(was[name]),
        }
        for name in sorted(was.keys() - now.keys())
    ]

    for name in sorted(now.keys() & was.keys()):
        changes += _one_repo(name, was[name], now[name])

    return changes


def _one_repo(name: str, old: Item, new: Item) -> list[Change]:
    """Changes within a single repository, from the headline down to its refs."""
    url = str(new.get("url") or old.get("url") or "")
    changes: list[Change] = []

    # Whether the detail walk covered this repository on *both* sides. Everything gated on
    # this comes from that walk - watchers, releases, refs, advisories, download counts -
    # and is otherwise being compared against a default rather than a measurement. The
    # comparison is then meaningless in both directions: things appear when the walk
    # arrives and vanish when it is skipped. The values stay in the snapshot either way;
    # they simply are not announced as news.
    detail_both = _has_detail(old) and _has_detail(new)

    for metric, thing in _REPO_METRICS:
        # Both the walk having run and the figure actually being present. An absent key is
        # the writer having pruned a None, which is this repository never having been
        # measured - not a measurement of zero.
        if metric in _DETAIL_METRICS and not (detail_both and metric in old and metric in new):
            continue
        before, after = old.get(metric, 0), new.get(metric, 0)
        if before == after:
            continue
        changes.append(
            {
                "kind": f"{metric}_changed",
                "thing": thing,
                "subject": name,
                "repository": name,
                "url": url,
                "old": before,
                "new": after,
                "delta": after - before,
                "first": _first(before, after),
            }
        )

    if old.get("is_archived") != new.get("is_archived"):
        changes.append(
            {
                "kind": "repo_archived",
                "thing": THING_REPO,
                "subject": name,
                "repository": name,
                "url": url,
                "old": bool(old.get("is_archived")),
                "new": bool(new.get("is_archived")),
            }
        )
    if old.get("is_private") != new.get("is_private"):
        changes.append(
            {
                "kind": "repo_visibility_changed",
                "thing": THING_REPO,
                "subject": name,
                "repository": name,
                "url": url,
                "old": "private" if old.get("is_private") else "public",
                "new": "private" if new.get("is_private") else "public",
            }
        )
    if old.get("name") != new.get("name"):
        changes.append(
            {
                "kind": "repo_renamed",
                "thing": THING_REPO,
                "subject": name,
                "repository": name,
                "url": url,
                "old": old.get("name"),
                "new": new.get("name"),
            }
        )

    fields = _changed_fields(old, new, _REPO_WATCHED)
    if fields:
        changes.append(
            {
                "kind": "repo_changed",
                "thing": THING_REPO,
                "subject": name,
                "repository": name,
                "url": url,
                "detail": {f: {"old": old.get(f), "new": new.get(f)} for f in fields},
            }
        )

    if detail_both:
        changes += _release_changes(name, old, new)
        changes += _security_changes(name, old, new)
        changes += _download_changes(name, old, new)
        changes += _ref_changes(name, old, new, "branches", THING_BRANCH, "branch")
        changes += _ref_changes(name, old, new, "tags", THING_TAG, "tag")

    # Traffic is not part of that walk and carries its own baseline rule, so it is compared
    # whether or not the detail walk has been anywhere near this repository.
    changes += _traffic_changes(name, old, new)
    changes += _thread_changes(name, old, new, "issues", THING_ISSUE, "issue")
    changes += _thread_changes(name, old, new, "prs", THING_PR, "pull_request")
    return changes


def _release_changes(repository: str, old: Item, new: Item) -> list[Change]:
    was, now = _by(old.get("releases"), "tag"), _by(new.get("releases"), "tag")
    changes: list[Change] = []

    for tag in sorted(now.keys() - was.keys()):
        changes.append(
            {
                "kind": "new_release",
                "thing": THING_RELEASE,
                "subject": tag,
                "repository": repository,
                "url": str(now[tag].get("url") or ""),
                "new": now[tag],
                # The repository's first release ever, not merely its newest.
                "first": not was,
            }
        )

    if _removals_trustworthy(old, new, "releases", repository):
        for tag in sorted(was.keys() - now.keys()):
            changes.append(
                {
                    "kind": "release_removed",
                    "thing": THING_RELEASE,
                    "subject": tag,
                    "repository": repository,
                    "url": str(was[tag].get("url") or ""),
                    "old": was[tag],
                }
            )

    for tag in sorted(now.keys() & was.keys()):
        fields = _changed_fields(was[tag], now[tag], _RELEASE_WATCHED)
        if fields:
            changes.append(
                {
                    "kind": "release_changed",
                    "thing": THING_RELEASE,
                    "subject": tag,
                    "repository": repository,
                    "url": str(now[tag].get("url") or ""),
                    "detail": {f: {"old": was[tag].get(f), "new": now[tag].get(f)} for f in fields},
                }
            )
    return changes


def _security_changes(repository: str, old: Item, new: Item) -> list[Change]:
    """New vulnerability alerts one by one; resolutions batched.

    A new alert is something to act on, so each gets its own line with the advisory
    attached. Resolutions arrive in bulk — one dependency bump can clear dozens at once, and
    one repository here has 68 open — so they are summarised per repository instead.
    """
    was = _by(old.get("security_alerts"), "number")
    now = _by(new.get("security_alerts"), "number")
    if not was and not now:
        return []

    changes: list[Change] = [
        {
            "kind": "new_security_alert",
            "thing": THING_SECURITY,
            "subject": str(now[n].get("ghsa") or now[n].get("package") or n),
            "repository": repository,
            "url": str(now[n].get("url") or ""),
            "new": now[n],
        }
        for n in sorted(now.keys() - was.keys())
    ]

    if not _removals_trustworthy(old, new, "security_alerts", repository):
        return changes

    resolved = [was[n] for n in sorted(was.keys() - now.keys())]
    if resolved:
        changes.append(
            {
                "kind": "security_alerts_resolved",
                "thing": THING_SECURITY,
                "subject": repository,
                "repository": repository,
                "old": len(was),
                "new": len(now),
                "delta": -len(resolved),
                "detail": {"resolved": len(resolved), "remaining": len(now)},
            }
        )
    return changes


def _download_changes(repository: str, old: Item, new: Item) -> list[Change]:
    """One line per repository whose assets gained downloads.

    Per asset would be unusable — this account holds 3,581 of them — but per repository is
    exactly one line per thing that actually moved, with the per-asset detail attached.
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

    total, previous_total = sum(now.values()), sum(was.get(k, 0) for k in now)
    return [
        {
            "kind": "new_downloads",
            "thing": THING_DOWNLOAD,
            "subject": repository,
            "repository": repository,
            "url": str(new.get("url") or ""),
            "old": previous_total,
            "new": total,
            "delta": total - previous_total,
            # The first download this repository has ever served, not merely a new one.
            "first": _first(sum(was.values()), total),
            "detail": {
                "assets": len(gains),
                "breakdown": sorted(gains, key=_delta, reverse=True)[:10],
            },
        }
    ]


def _traffic_series_total(traffic: Item, series: str) -> int:
    """Lifetime total of one accumulated daily series."""
    return sum(int(day.get("count", 0) or 0) for day in (traffic.get(series) or {}).values())


def _traffic_changes(repository: str, old: Item, new: Item) -> list[Change]:
    """Views, clones and referring sites, from the accumulated traffic cache.

    Only fires for a repository the sweep has visited at least twice. The first visit brings
    back fourteen days at once, and reporting that as a delta would announce a fortnight of
    history as if it had just happened — the same rule the module already follows for an
    account it has never seen before.
    """
    was, now = old.get("traffic") or {}, new.get("traffic") or {}
    if not was or not now:
        return []

    url = str(new.get("url") or old.get("url") or "")
    changes: list[Change] = []

    for series, thing in (("views", THING_VIEW), ("clones", THING_CLONE)):
        before, after = _traffic_series_total(was, series), _traffic_series_total(now, series)
        if after <= before:
            continue
        changes.append(
            {
                "kind": f"new_{series}",
                "thing": thing,
                "subject": repository,
                "repository": repository,
                "url": url,
                "old": before,
                "new": after,
                "delta": after - before,
                "first": _first(before, after),
            }
        )

    # A referring site that has never appeared before. GitHub only ever shows the top ten of
    # a rolling window, so "not in the cache" is the only way to tell a new one from a
    # familiar one that had dropped out and come back — which is what first_seen is for.
    was_referrers = was.get("referrers") or {}
    now_referrers = now.get("referrers") or {}
    for name in sorted(now_referrers.keys() - was_referrers.keys()):
        entry = now_referrers[name]
        changes.append(
            {
                "kind": "new_referrer",
                "thing": THING_REFERRER,
                "subject": name,
                "repository": repository,
                "url": url,
                "new": entry,
                "delta": int(entry.get("count", 0) or 0),
                "first": True,
            }
        )

    return changes


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


def _pull_changes(previous: Item, current: Item) -> list[Change]:
    """One line per image that gained pulls."""
    was = {
        str(p.get("name")): int(p.get("pull_count", 0) or 0) for p in previous.get("packages") or []
    }
    changes: list[Change] = []

    for package in current.get("packages") or []:
        name = str(package.get("name"))
        after = int(package.get("pull_count", 0) or 0)
        before = was.get(name, 0)
        if after > before:
            changes.append(
                {
                    "kind": "new_pulls",
                    "thing": THING_PULL,
                    "subject": name,
                    "url": str(package.get("url") or ""),
                    "old": before,
                    "new": after,
                    "delta": after - before,
                    "first": _first(before, after),
                }
            )
    return changes


def _ref_changes(
    repository: str, old: Item, new: Item, field_name: str, thing: str, label: str
) -> list[Change]:
    was, now = _by(old.get(field_name), "name"), _by(new.get(field_name), "name")
    changes: list[Change] = [
        {
            "kind": f"new_{label}",
            "thing": thing,
            "subject": name,
            "repository": repository,
            "new": now[name],
        }
        for name in sorted(now.keys() - was.keys())
    ]
    if _removals_trustworthy(old, new, field_name, repository):
        changes += [
            {
                "kind": f"{label}_removed",
                "thing": thing,
                "subject": name,
                "repository": repository,
                "old": was[name],
            }
            for name in sorted(was.keys() - now.keys())
        ]
    return changes


def _thread_changes(
    repository: str, old: Item, new: Item, field_name: str, thing: str, label: str
) -> list[Change]:
    """Issues and pull requests. The lists hold only open ones, so a disappearance means it
    was closed or merged rather than deleted."""
    was, now = _by(old.get(field_name), "number"), _by(new.get(field_name), "number")
    changes: list[Change] = [
        {
            "kind": f"new_{label}",
            "thing": thing,
            "subject": f"#{n}",
            "repository": repository,
            "url": str(now[n].get("url") or ""),
            "new": now[n],
        }
        for n in sorted(now.keys() - was.keys())
    ]
    if _removals_trustworthy(old, new, field_name, repository):
        changes += [
            {
                "kind": f"{label}_closed",
                "thing": thing,
                "subject": f"#{n}",
                "repository": repository,
                "url": str(was[n].get("url") or ""),
                "old": was[n],
            }
            for n in sorted(was.keys() - now.keys())
        ]
    return changes


def _org_changes(previous: Item, current: Item) -> list[Change]:
    if not _comparable(previous, current, "orgs", "orgs"):
        return []
    was, now = _by(previous.get("orgs"), "name"), _by(current.get("orgs"), "name")
    changes: list[Change] = [
        {
            "kind": "new_org",
            "thing": THING_ORG,
            "subject": name,
            "url": str(now[name].get("url") or ""),
            "new": {k: v for k, v in now[name].items() if k != "repos"},
        }
        for name in sorted(now.keys() - was.keys())
    ]
    changes += [
        {
            "kind": "org_removed",
            "thing": THING_ORG,
            "subject": name,
            "url": str(was[name].get("url") or ""),
            "old": {k: v for k, v in was[name].items() if k != "repos"},
        }
        for name in sorted(was.keys() - now.keys())
    ]
    return changes


def _package_changes(previous: Item, current: Item) -> list[Change]:
    if not _comparable(previous, current, "packages", "packages"):
        return []
    was, now = _by(previous.get("packages"), "name"), _by(current.get("packages"), "name")
    changes: list[Change] = [
        {
            "kind": "new_package",
            "thing": THING_PACKAGE,
            "subject": name,
            "url": str(now[name].get("url") or ""),
            "new": now[name],
        }
        for name in sorted(now.keys() - was.keys())
    ]
    changes += [
        {
            "kind": "package_removed",
            "thing": THING_PACKAGE,
            "subject": name,
            "url": str(was[name].get("url") or ""),
            "old": was[name],
        }
        for name in sorted(was.keys() - now.keys())
    ]
    for name in sorted(now.keys() & was.keys()):
        fields = _changed_fields(was[name], now[name], _PACKAGE_WATCHED)
        if fields:
            changes.append(
                {
                    "kind": "package_changed",
                    "thing": THING_PACKAGE,
                    "subject": name,
                    "url": str(now[name].get("url") or ""),
                    "detail": {
                        f: {"old": was[name].get(f), "new": now[name].get(f)} for f in fields
                    },
                }
            )
    return changes


def _notification_changes(previous: Item, current: Item) -> list[Change]:
    """Newly arrived unread notifications, one change each.

    These leave as their own event type rather than as lines in a digest: a notification is
    already the unit a person acts on, and batching them would bury the one that mattered.
    """
    if not _comparable(previous, current, "notifications", "notifications"):
        return []
    was = _by(previous.get("notifications"), "notification_id")
    now = _by(current.get("notifications"), "notification_id")
    return [
        {
            "kind": "new_notification",
            "thing": THING_NOTIFICATION,
            "subject": str(now[n].get("title") or n),
            "repository": str(now[n].get("repository") or ""),
            "url": str(now[n].get("url") or ""),
            "new": now[n],
        }
        for n in sorted(now.keys() - was.keys())
        if now[n].get("unread", True)
    ]
