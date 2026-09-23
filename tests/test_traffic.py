"""Tests for the rotating GitHub traffic sweep and its accumulated cache.

The cache exists because GitHub forgets after fourteen days, so most of what is worth
testing is about *not* losing what it no longer serves, and about the sweep getting round
every repository inside that window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from dev_cloud.models import RepoData
from dev_cloud.providers.base import DevCloudForbiddenError, DevCloudNotFoundError
from dev_cloud.providers.github import traffic
from yarl import URL

BASE = URL("https://api.github.com")
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _repo(full_name: str) -> RepoData:
    return RepoData(name=full_name.split("/")[-1], full_name=full_name, url="u")


def _samples(*days: tuple[str, int, int]) -> list[dict[str, Any]]:
    return [
        {"timestamp": f"{date}T00:00:00Z", "count": count, "uniques": uniques}
        for date, count, uniques in days
    ]


class _Api:
    """Stands in for the four traffic endpoints, recording what was asked for."""

    def __init__(
        self,
        views: dict[str, list[dict[str, Any]]] | None = None,
        clones: dict[str, list[dict[str, Any]]] | None = None,
        referrers: dict[str, list[dict[str, Any]]] | None = None,
        paths: dict[str, list[dict[str, Any]]] | None = None,
        forbidden: set[str] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.views = views or {}
        self.clones = clones or {}
        self.referrers = referrers or {}
        self.paths = paths or {}
        self.forbidden = forbidden or set()
        self.missing = missing or set()
        self.visited: list[str] = []
        self.requests = 0

    async def __call__(self, url: URL) -> Any:
        self.requests += 1
        parts = url.parts  # ("/", "repos", owner, name, "traffic", ...)
        full_name = f"{parts[2]}/{parts[3]}"
        if full_name not in self.visited:
            self.visited.append(full_name)
        if full_name in self.forbidden:
            raise DevCloudForbiddenError(f"Forbidden: {url}")
        if full_name in self.missing:
            raise DevCloudNotFoundError(f"Not found: {url}")

        tail = parts[5:]
        if tail == ("views",):
            return {"views": self.views.get(full_name, [])}
        if tail == ("clones",):
            return {"clones": self.clones.get(full_name, [])}
        if tail == ("popular", "referrers"):
            return self.referrers.get(full_name, [])
        return self.paths.get(full_name, [])


async def _sweep(
    api: _Api,
    repos: list[RepoData],
    cache: traffic.TrafficCache,
    *,
    per_sweep: int = 10,
    now: datetime = NOW,
    history_days: int = 730,
) -> traffic.TrafficCache:
    return await traffic.async_sweep_traffic(
        api,
        BASE,
        repos,
        cache,
        per_sweep=per_sweep,
        concurrency=4,
        history_days=history_days,
        forbidden_retry_days=7,
        now=now,
    )


# --- merging --------------------------------------------------------------------------


async def test_a_first_sweep_stores_the_window_it_was_given() -> None:
    api = _Api(
        views={"o/a": _samples(("2026-09-21", 12, 3), ("2026-09-22", 5, 2))},
        clones={"o/a": _samples(("2026-09-22", 2, 1))},
    )
    cache = await _sweep(api, [_repo("o/a")], {})

    assert cache["o/a"]["views"] == {
        "2026-09-21": {"count": 12, "uniques": 3},
        "2026-09-22": {"count": 5, "uniques": 2},
    }
    assert cache["o/a"]["clones"] == {"2026-09-22": {"count": 2, "uniques": 1}}
    assert cache["o/a"]["fetched_at"] == NOW.isoformat()


async def test_a_day_still_in_progress_is_replaced_not_added_to() -> None:
    """Today's bucket keeps filling, so two fetches of it must not double the count."""
    repos = [_repo("o/a")]
    first = await _sweep(_Api(views={"o/a": _samples(("2026-09-23", 4, 1))}), repos, {})
    second = await _sweep(_Api(views={"o/a": _samples(("2026-09-23", 9, 2))}), repos, first)

    assert second["o/a"]["views"]["2026-09-23"] == {"count": 9, "uniques": 2}


async def test_days_github_has_forgotten_survive_in_the_cache() -> None:
    """The entire reason this cache exists."""
    repos = [_repo("o/a")]
    old = await _sweep(_Api(views={"o/a": _samples(("2026-08-01", 40, 10))}), repos, {})

    # A later window no longer mentions August at all.
    new = await _sweep(_Api(views={"o/a": _samples(("2026-09-23", 1, 1))}), repos, old)

    assert new["o/a"]["views"]["2026-08-01"] == {"count": 40, "uniques": 10}
    assert new["o/a"]["views"]["2026-09-23"] == {"count": 1, "uniques": 1}


async def test_days_with_no_traffic_are_not_stored() -> None:
    """Six hundred quiet repositories would otherwise be a quarter-million empty buckets."""
    api = _Api(views={"o/a": _samples(("2026-09-21", 0, 0), ("2026-09-22", 3, 1))})
    cache = await _sweep(api, [_repo("o/a")], {})

    assert list(cache["o/a"]["views"]) == ["2026-09-22"]


async def test_the_series_is_trimmed_to_the_history_limit_oldest_first() -> None:
    api = _Api(
        views={"o/a": _samples(*[(f"2026-09-{d:02d}", 1, 1) for d in range(1, 11)])},
    )
    cache = await _sweep(api, [_repo("o/a")], {}, history_days=3)

    assert sorted(cache["o/a"]["views"]) == ["2026-09-08", "2026-09-09", "2026-09-10"]


async def test_referrer_counts_replace_rather_than_accumulate() -> None:
    """They are overlapping fourteen-day windows; summing them counts a visit many times."""
    repos = [_repo("o/a")]
    first = await _sweep(
        _Api(referrers={"o/a": [{"referrer": "google.com", "count": 10, "uniques": 4}]}), repos, {}
    )
    second = await _sweep(
        _Api(referrers={"o/a": [{"referrer": "google.com", "count": 12, "uniques": 5}]}),
        repos,
        first,
        now=NOW + timedelta(days=1),
    )

    assert second["o/a"]["referrers"]["google.com"]["count"] == 12


async def test_a_referrer_remembers_when_it_was_first_seen() -> None:
    """What makes a genuinely new referring site distinguishable from a returning one."""
    repos = [_repo("o/a")]
    first = await _sweep(
        _Api(referrers={"o/a": [{"referrer": "news.ycombinator.com", "count": 1, "uniques": 1}]}),
        repos,
        {},
    )
    later = await _sweep(
        _Api(referrers={"o/a": [{"referrer": "news.ycombinator.com", "count": 8, "uniques": 6}]}),
        repos,
        first,
        now=NOW + timedelta(days=5),
    )

    entry = later["o/a"]["referrers"]["news.ycombinator.com"]
    assert entry["first_seen"] == "2026-09-23"
    assert entry["last_seen"] == "2026-09-28"


async def test_a_popular_path_keeps_its_title() -> None:
    api = _Api(paths={"o/a": [{"path": "/o/a", "title": "o/a: a thing", "count": 3, "uniques": 2}]})
    cache = await _sweep(api, [_repo("o/a")], {})

    assert cache["o/a"]["paths"]["/o/a"]["title"] == "o/a: a thing"


# --- the rotating sweep ---------------------------------------------------------------


async def test_a_sweep_covers_only_its_share_of_the_repositories() -> None:
    """Four requests each, six hundred repositories: the whole point of sweeping."""
    repos = [_repo(f"o/r{i}") for i in range(50)]
    api = _Api()
    await _sweep(api, repos, {}, per_sweep=10)

    assert len(api.visited) == 10
    assert api.requests == 40


async def test_successive_sweeps_work_their_way_round_everything() -> None:
    repos = [_repo(f"o/r{i}") for i in range(10)]
    cache: traffic.TrafficCache = {}
    seen: list[str] = []

    for step in range(5):
        api = _Api()
        cache = await _sweep(api, repos, cache, per_sweep=2, now=NOW + timedelta(minutes=10 * step))
        seen += api.visited

    assert sorted(seen) == sorted(r.full_name for r in repos), "every repository, exactly once"


async def test_the_least_recently_fetched_repository_goes_first() -> None:
    repos = [_repo("o/old"), _repo("o/new")]
    cache: traffic.TrafficCache = {
        "o/old": {"fetched_at": "2026-09-01T00:00:00+00:00"},
        "o/new": {"fetched_at": "2026-09-22T00:00:00+00:00"},
    }
    api = _Api()
    await _sweep(api, repos, cache, per_sweep=1)

    assert api.visited == ["o/old"]


async def test_a_repository_never_fetched_outranks_every_fetched_one() -> None:
    repos = [_repo("o/fetched"), _repo("o/fresh")]
    cache: traffic.TrafficCache = {"o/fetched": {"fetched_at": "2020-01-01T00:00:00+00:00"}}
    api = _Api()
    await _sweep(api, repos, cache, per_sweep=1)

    assert api.visited == ["o/fresh"]


# --- repositories the token may not read ----------------------------------------------


async def test_a_repository_without_push_access_is_remembered_not_retried() -> None:
    """Otherwise every sweep spends its whole budget rediscovering the same refusals."""
    repos = [_repo("o/a"), _repo("o/b")]
    cache = await _sweep(_Api(forbidden={"o/a"}), repos, {}, per_sweep=2)

    assert cache["o/a"]["forbidden_at"] == NOW.isoformat()

    api = _Api(forbidden={"o/a"})
    await _sweep(api, repos, cache, per_sweep=2, now=NOW + timedelta(hours=1))
    assert api.visited == ["o/b"]


async def test_a_forbidden_repository_is_retried_once_the_cooldown_expires() -> None:
    """Access can be granted later, and nothing else would ever notice."""
    repos = [_repo("o/a")]
    cache = await _sweep(_Api(forbidden={"o/a"}), repos, {})

    api = _Api()
    cache = await _sweep(api, repos, cache, now=NOW + timedelta(days=8))

    assert api.visited == ["o/a"]
    assert "forbidden_at" not in cache["o/a"], "access was granted, so the mark is cleared"


async def test_a_repository_that_disappeared_does_not_break_the_sweep() -> None:
    repos = [_repo("o/gone"), _repo("o/here")]
    api = _Api(missing={"o/gone"}, views={"o/here": _samples(("2026-09-22", 3, 1))})
    cache = await _sweep(api, repos, {}, per_sweep=2)

    assert cache["o/here"]["views"]["2026-09-22"]["count"] == 3


async def test_the_sweep_does_not_mutate_the_cache_it_was_given() -> None:
    """async_resource stores whatever is returned; editing the input would corrupt the
    stored value even on the path where the fetch failed."""
    original: traffic.TrafficCache = {"o/a": {"fetched_at": "2026-09-01T00:00:00+00:00"}}
    before = {name: dict(record) for name, record in original.items()}

    await _sweep(_Api(views={"o/a": _samples(("2026-09-22", 9, 3))}), [_repo("o/a")], original)

    assert original == before


# --- attaching and totalling ----------------------------------------------------------


def test_traffic_is_hung_off_the_repository_it_describes() -> None:
    repo = _repo("o/a")
    traffic.attach_traffic([repo], {"o/a": {"views": {"2026-09-22": {"count": 4, "uniques": 1}}}})

    assert repo.traffic["views"]["2026-09-22"]["count"] == 4


def test_a_repository_with_no_cached_traffic_is_left_alone() -> None:
    repo = _repo("o/a")
    traffic.attach_traffic([repo], {})
    assert repo.traffic == {}


# --- running out of quota partway through ----------------------------------------------


class _RateLimitedApi(_Api):
    """Refuses with a rate-limit error once it has served `budget` repositories."""

    def __init__(self, budget: int, **kw: Any) -> None:
        super().__init__(**kw)
        self.budget = budget
        self._served: set[str] = set()

    async def __call__(self, url: URL) -> Any:
        parts = url.parts
        full_name = f"{parts[2]}/{parts[3]}"
        if full_name not in self._served and len(self._served) >= self.budget:
            raise DevCloudRateLimitError("Rate limit exceeded")
        self._served.add(full_name)
        return await super().__call__(url)


async def test_a_sweep_that_runs_out_of_quota_stops_instead_of_pressing_on() -> None:
    """Continuing would only collect more refusals and prolong the block."""
    repos = [_repo(f"o/r{i}") for i in range(10)]
    api = _RateLimitedApi(budget=3)
    await traffic.async_sweep_traffic(
        api,
        BASE,
        repos,
        {},
        per_sweep=10,
        # Serial, so "stop after the third" is deterministic rather than a race between
        # however many workers were already past the semaphore.
        concurrency=1,
        history_days=730,
        forbidden_retry_days=7,
        now=NOW,
    )

    assert len(api.visited) < len(repos), "the sweep must not walk the whole batch"


async def test_what_was_fetched_before_the_limit_is_kept() -> None:
    repos = [_repo(f"o/r{i}") for i in range(10)]
    api = _RateLimitedApi(
        budget=2, views={f"o/r{i}": _samples(("2026-09-22", 5, 1)) for i in range(10)}
    )
    cache = await traffic.async_sweep_traffic(
        api, BASE, repos, {}, per_sweep=10, concurrency=1,
        history_days=730, forbidden_retry_days=7, now=NOW,
    )

    fetched = [name for name, record in cache.items() if record.get("fetched_at")]
    assert fetched, "progress made before the limit must survive"
    assert all(cache[name]["views"]["2026-09-22"]["count"] == 5 for name in fetched)


async def test_repositories_skipped_by_a_limit_keep_their_place_at_the_front() -> None:
    """They were never fetched, so the next sweep must reach them before anything else."""
    repos = [_repo(f"o/r{i}") for i in range(6)]
    limited = _RateLimitedApi(budget=2)
    cache = await traffic.async_sweep_traffic(
        repos=repos, cache={}, get_json=limited, base_url=BASE, per_sweep=6, concurrency=1,
        history_days=730, forbidden_retry_days=7, now=NOW,
    )
    reached = {name for name, record in cache.items() if record.get("fetched_at")}

    after = _Api()
    await _sweep(after, repos, cache, per_sweep=6, now=NOW + timedelta(minutes=10))

    assert reached.isdisjoint(after.visited[: len(repos) - len(reached)]), (
        "the untouched repositories come first"
    )


async def test_a_rate_limit_is_not_mistaken_for_a_missing_permission() -> None:
    """Marking it forbidden would drop the repository for a week over a transient limit."""
    repos = [_repo("o/a")]
    api = _RateLimitedApi(budget=0)
    cache = await traffic.async_sweep_traffic(
        api, BASE, repos, {}, per_sweep=1, concurrency=1,
        history_days=730, forbidden_retry_days=7, now=NOW,
    )

    assert "forbidden_at" not in cache.get("o/a", {})
