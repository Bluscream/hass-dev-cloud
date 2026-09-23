"""Repository traffic, kept past the fourteen days GitHub is willing to remember.

GitHub's traffic graphs answer "who looked at this repository" — views, clones, referring
sites and popular paths — and then forget: every endpoint returns a rolling fourteen-day
window and nothing older. Anything wanting a yearly view has to keep its own copy, which is
what this module is for. Each sweep merges the fresh window into an accumulated record, so
the history grows for as long as the integration keeps running.

Two properties of the API shape everything here:

**It costs four requests per repository and needs push access.** Sweeping six hundred
repositories in one go is roughly half an hourly quota, so a sweep covers a fixed handful
and moves on, oldest-first, until it has been round everything. Fourteen days of retention
is the real deadline: as long as every repository is revisited inside that window nothing is
lost, and a sweep of ten repositories every ten minutes gets round six hundred in ten hours.
The cost per sweep is deliberately constant, which is what lets `ResourceScheduler` measure
it once and pace the resource against the remaining budget like any other.

**Views and clones are exact per day; referrers and paths are not.** A daily bucket can be
accumulated honestly — the count for the 20th is the count for the 20th, whoever asks and
whenever. Referrers and paths come back as totals over the rolling window, so adding
successive windows together would count the same visit up to fourteen times. Those are
stored as the latest window plus when each was first and last seen, which is both truthful
and enough to answer "is this a referring site we have never had before".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from yarl import URL

from ..base import (
    DevCloudForbiddenError,
    DevCloudNotFoundError,
    DevCloudRateLimitError,
    async_map_limited,
)

_LOGGER = logging.getLogger(__name__)

#: full_name -> that repository's accumulated traffic record.
TrafficCache = dict[str, dict[str, Any]]

#: Fetches one endpoint and returns its decoded JSON.
JsonFetcher = Callable[[URL], Awaitable[Any]]

#: The two per-day series, mapped to the key GitHub returns their samples under.
_DAILY_SERIES: Final[tuple[tuple[str, str], ...]] = (("views", "views"), ("clones", "clones"))


class _SweepHaltedError(Exception):
    """A repository skipped because the sweep had already run out of quota.

    Carried through `async_map_limited`'s per-item exception list rather than cancelling the
    gather, so the repositories that did succeed still get merged.
    """


def _day(timestamp: str) -> str:
    """The date part of one of GitHub's sample timestamps.

    Samples are midnight UTC, so the date alone identifies the bucket and keying by it means
    re-fetching a day that is still in progress replaces the partial figure rather than
    adding to it.
    """
    return timestamp[:10]


def _merge_daily(kept: dict[str, Any], fresh: list[dict[str, Any]], history_days: int) -> None:
    """Fold a fresh fourteen-day window into the accumulated series, in place.

    Today's bucket is still filling, so a later fetch of the same date must *replace* what
    is stored rather than add to it. Days that fell out of GitHub's window are untouched,
    which is the whole point of keeping this.
    """
    for sample in fresh:
        timestamp = sample.get("timestamp")
        if not isinstance(timestamp, str):
            continue
        kept[_day(timestamp)] = {
            "count": int(sample.get("count", 0) or 0),
            "uniques": int(sample.get("uniques", 0) or 0),
        }

    # Zero-traffic days carry no information and would otherwise dominate the snapshot: six
    # hundred repositories that nobody visited is a quarter of a million empty buckets.
    for date in [d for d, value in kept.items() if not value.get("count")]:
        del kept[date]

    if len(kept) > history_days:
        for date in sorted(kept)[: len(kept) - history_days]:
            del kept[date]


def _merge_window(kept: dict[str, Any], fresh: list[dict[str, Any]], key: str, today: str) -> None:
    """Fold a rolling-window top-ten (referrers, paths) into what is already known.

    Counts are *replaced*, never summed: they describe overlapping fourteen-day windows, and
    adding them would count one visit once per sweep. `first_seen` is what makes a genuinely
    new referring site distinguishable from one that merely re-entered the top ten.
    """
    for entry in fresh:
        name = entry.get(key)
        if not isinstance(name, str) or not name:
            continue
        existing = kept.get(name) or {}
        record = {
            "count": int(entry.get("count", 0) or 0),
            "uniques": int(entry.get("uniques", 0) or 0),
            "first_seen": existing.get("first_seen", today),
            "last_seen": today,
        }
        title = entry.get("title")
        if isinstance(title, str) and title:
            record["title"] = title
        kept[name] = record


def _eligible(
    repos: Iterable[Any], cache: TrafficCache, now: datetime, forbidden_retry_days: int
) -> list[str]:
    """Repositories due a traffic fetch, least recently fetched first.

    A repository the token cannot push to answers 403 for all four endpoints, which is a
    property of the repository rather than a failure, so it is remembered and retried only
    occasionally — otherwise every sweep would spend its whole budget rediscovering the same
    hundred repositories it is not allowed to read.
    """
    retry_before = (now - timedelta(days=forbidden_retry_days)).isoformat()
    candidates: list[tuple[str, str]] = []

    for repo in repos:
        name = getattr(repo, "full_name", "")
        if not name:
            continue
        record = cache.get(name) or {}
        forbidden_at = record.get("forbidden_at")
        if isinstance(forbidden_at, str) and forbidden_at > retry_before:
            continue
        # Never fetched sorts first: "" is below every ISO timestamp.
        candidates.append((str(record.get("fetched_at") or ""), name))

    candidates.sort()
    return [name for _, name in candidates]


async def _fetch_one(get_json: JsonFetcher, base_url: URL, full_name: str) -> dict[str, Any] | None:
    """All four traffic endpoints for one repository.

    Returns None when the token may not read this repository's traffic, which the caller
    records so the repository drops out of the rotation.
    """
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return None

    root = base_url / "repos" / owner / name / "traffic"
    try:
        views, clones, referrers, paths = (
            await get_json((root / "views").with_query(per="day")),
            await get_json((root / "clones").with_query(per="day")),
            await get_json(root / "popular" / "referrers"),
            await get_json(root / "popular" / "paths"),
        )
    except DevCloudForbiddenError:
        _LOGGER.debug("No push access to %s, so its traffic is not readable", full_name)
        return None
    except DevCloudNotFoundError:
        # Renamed or deleted between the repository listing and this sweep.
        _LOGGER.debug("Traffic endpoint missing for %s", full_name)
        return None

    return {
        "views": views if isinstance(views, dict) else {},
        "clones": clones if isinstance(clones, dict) else {},
        "referrers": referrers if isinstance(referrers, list) else [],
        "paths": paths if isinstance(paths, list) else [],
    }


async def async_sweep_traffic(
    get_json: JsonFetcher,
    base_url: URL,
    repos: Iterable[Any],
    cache: TrafficCache,
    *,
    per_sweep: int,
    concurrency: int,
    history_days: int,
    forbidden_retry_days: int,
    now: datetime | None = None,
) -> TrafficCache:
    """Advance the rotating sweep by one step and return the updated cache.

    The cache is copied rather than mutated: `async_resource` stores whatever is returned,
    and a function that edited its input would leave the stored value changed even on the
    path where the fetch failed.
    """
    moment = now or datetime.now(UTC)
    stamp, today = moment.isoformat(), moment.date().isoformat()

    updated: TrafficCache = {name: dict(record) for name, record in cache.items()}
    due = _eligible(repos, updated, moment, forbidden_retry_days)[:per_sweep]
    if not due:
        return updated

    # A mutable flag rather than a cancellation: the workers still queued behind the
    # semaphore have not spent anything yet, and there is no point spending it on requests
    # that can only come back refused.
    halted: list[str] = []

    async def _worker(full_name: str) -> tuple[str, dict[str, Any] | None]:
        if halted:
            raise _SweepHaltedError(full_name)
        try:
            return full_name, await _fetch_one(get_json, base_url, full_name)
        except DevCloudRateLimitError:
            # Swallowed rather than propagated: the repositories already fetched keep their
            # progress, the rest keep their place at the front of the rotation, and the
            # scheduler - which heard about the exhausted quota from the refusal itself -
            # holds every resource off until the window resets.
            halted.append(full_name)
            raise

    results = await async_map_limited(due, _worker, concurrency)

    for result in results:
        if isinstance(result, BaseException):
            # One repository failing must not lose the rest of the sweep; the scheduler's
            # own failure handling covers the case where the whole resource is broken.
            if not isinstance(result, _SweepHaltedError):
                _LOGGER.debug("Traffic fetch failed: %s", result)
            continue

        full_name, fetched = result
        record = updated.setdefault(full_name, {})

        if fetched is None:
            record["forbidden_at"] = stamp
            continue

        record.pop("forbidden_at", None)
        record["fetched_at"] = stamp

        for series, sample_key in _DAILY_SERIES:
            samples = fetched[series].get(sample_key)
            _merge_daily(
                record.setdefault(series, {}),
                samples if isinstance(samples, list) else [],
                history_days,
            )

        _merge_window(record.setdefault("referrers", {}), fetched["referrers"], "referrer", today)
        _merge_window(record.setdefault("paths", {}), fetched["paths"], "path", today)

    if halted:
        _LOGGER.warning(
            "Traffic sweep stopped early: the API quota ran out partway through %d "
            "repositories. What was fetched is kept, and the rest stay first in line.",
            len(due),
        )

    return updated


def attach_traffic(repos: Iterable[Any], cache: TrafficCache) -> None:
    """Hang each repository's accumulated traffic off the repository itself.

    Same shape as releases and refs: the snapshot nests everything under the repository it
    describes, so a restore rebuilds the cache from there rather than storing it twice.
    """
    for repo in repos:
        found = cache.get(getattr(repo, "full_name", ""))
        if found:
            repo.traffic = found
