"""Tests for adaptive resource scheduling and pagination termination."""

from __future__ import annotations

import time

import pytest

from dev_cloud.providers.scheduling import (
    MAX_INTERVAL_SECONDS,
    PageWalker,
    ResourcePolicy,
    ResourceScheduler,
)

POLICIES = {
    "repos": ResourcePolicy(authenticated=900, anonymous=3600),
    "notifications": ResourcePolicy(authenticated=300, anonymous=None),
}


def _scheduler(*, has_token: bool = True) -> ResourceScheduler:
    return ResourceScheduler(policies=dict(POLICIES), has_token=has_token)


def test_first_poll_fetches_then_backs_off() -> None:
    sched = _scheduler()
    assert sched.should_fetch("repos")

    sched.record_fetch("repos", cost=6)
    assert not sched.should_fetch("repos")


def test_undeclared_resource_always_fetches() -> None:
    """Resources without a policy keep the previous every-poll behaviour."""
    assert _scheduler().should_fetch("something_new")


def test_anonymous_uses_the_anonymous_floor() -> None:
    assert _scheduler(has_token=False).effective_interval("repos") == 3600


def test_resource_unavailable_without_a_token_is_never_fetched() -> None:
    sched = _scheduler(has_token=False)
    assert sched.effective_interval("notifications") is None
    assert not sched.should_fetch("notifications")


def test_healthy_budget_leaves_the_declared_floor_alone() -> None:
    sched = _scheduler()
    sched.record_fetch("repos", cost=6)
    sched.observe_rate_limit(4800, time.time() + 3600)

    assert sched.effective_interval("repos") == 900


def test_starved_budget_stretches_the_interval() -> None:
    """20 requests left in the hour cannot sustain a 6-request resource every 900s."""
    sched = _scheduler()
    sched.record_fetch("repos", cost=6)
    sched.observe_rate_limit(20, time.time() + 3600)

    interval = sched.effective_interval("repos")
    assert interval is not None
    assert interval > 900


def test_stretching_is_clamped_so_a_resource_never_looks_dead() -> None:
    sched = _scheduler()
    sched.record_fetch("repos", cost=50)
    sched.observe_rate_limit(1, time.time() + 3600)

    assert sched.effective_interval("repos") == MAX_INTERVAL_SECONDS


def test_measured_cost_is_reported_in_diagnostics() -> None:
    sched = _scheduler()
    sched.record_fetch("repos", cost=7)

    assert sched.persisted_state()["repos"]["cost"] == 7


def _page(start: int, size: int = 100) -> list[dict[str, int]]:
    return [{"id": i} for i in range(start, start + size)]


def test_page_walker_accepts_distinct_pages() -> None:
    walker = PageWalker("/repos", 100)
    assert walker.accept(_page(0))
    assert walker.accept(_page(100))


def test_page_walker_stops_when_the_endpoint_repeats_itself() -> None:
    """Regression: an endpoint ignoring `page` used to be paged until the safety limit."""
    walker = PageWalker("/broken", 100)
    first = _page(0)

    assert walker.accept(first)
    assert not walker.accept(list(first))


def test_page_walker_treats_a_short_page_as_the_last() -> None:
    walker = PageWalker("/repos", 100)
    assert walker.is_last(_page(0, size=17))
    assert not walker.is_last(_page(0))


def test_page_walker_rejects_an_empty_page() -> None:
    assert not PageWalker("/repos", 100).accept([])


def test_measured_cost_tracks_the_latest_measurement() -> None:
    """A high-water mark never recovers: one expensive poll would inflate the interval for
    the lifetime of the provider."""
    sched = _scheduler()
    sched.record_fetch("repos", cost=200)
    sched.record_fetch("repos", cost=6)

    assert sched.persisted_state()["repos"]["cost"] == 6


def test_budgets_are_tracked_per_quota() -> None:
    """Regression: REST and GraphQL shared one field, so a healthy REST reading overwrote an
    exhausted GraphQL one — masking the resource that most needed to back off."""
    from dev_cloud.providers.scheduling import QUOTA_GRAPHQL, QUOTA_REST

    sched = ResourceScheduler(
        policies={
            "repos": ResourcePolicy(authenticated=900, anonymous=3600),
            # Deliberately a short floor, so an exhausted quota visibly stretches it.
            "repo_detail": ResourcePolicy(authenticated=300, anonymous=None, quota=QUOTA_GRAPHQL),
        },
        has_token=True,
    )
    sched.observe_rate_limit(0, time.time() + 3600, QUOTA_GRAPHQL)
    sched.observe_rate_limit(4900, time.time() + 3600, QUOTA_REST)

    assert sched.budget(QUOTA_REST).remaining == 4900
    assert sched.budget(QUOTA_GRAPHQL).remaining == 0

    sched.record_fetch("repo_detail", cost=20)
    sched.record_fetch("repos", cost=6)

    # REST is healthy, so its resource keeps its floor.
    assert sched.effective_interval("repos") == 900
    # GraphQL is spent, so its resource waits for the reset rather than retrying into it.
    releases_interval = sched.effective_interval("repo_detail")
    assert releases_interval is not None
    assert releases_interval > 300
    assert releases_interval == pytest.approx(3600, abs=5)


def test_an_exhausted_quota_is_not_mistaken_for_an_unknown_one() -> None:
    """`if not rate` treated 0 remaining as "no information" and carried on at the base
    interval — retrying straight into a wall."""
    from dev_cloud.providers.scheduling import QUOTA_REST, RateLimitBudget

    spent = RateLimitBudget(remaining=0, reset_epoch=time.time() + 1800)
    unknown = RateLimitBudget()

    assert spent.requests_per_second() == 0.0
    assert unknown.requests_per_second() is None

    sched = ResourceScheduler(
        policies={"repos": ResourcePolicy(authenticated=60, anonymous=60)}, has_token=True
    )
    sched.observe_rate_limit(0, time.time() + 1800, QUOTA_REST)
    interval = sched.effective_interval("repos")
    assert interval is not None and interval == pytest.approx(1800, abs=5)


def test_a_failing_resource_backs_off_instead_of_retrying_every_poll() -> None:
    """Regression: only a successful fetch updated the timestamp, so a rate-limited
    resource was retried on every single poll — the retries prolong the block."""
    sched = _scheduler()
    assert sched.should_fetch("notifications")

    sched.record_failure("notifications")
    assert not sched.should_fetch("notifications")

    base = sched.effective_interval("notifications")
    assert base is not None
    assert sched._failure_backoff("notifications", base) > base

    # Repeated failures widen it further, then hold at the ceiling.
    for _ in range(10):
        sched.record_failure("notifications")
    assert sched._failure_backoff("notifications", base) <= MAX_INTERVAL_SECONDS


def test_a_success_clears_the_failure_backoff() -> None:
    sched = _scheduler()
    sched.record_failure("repos")
    sched.record_fetch("repos", cost=6)

    base = sched.effective_interval("repos")
    assert base is not None
    assert sched._failure_backoff("repos", base) == base


def test_a_forced_refresh_makes_everything_due_again() -> None:
    """The Force Refresh button: skip the pacing, for when you know something moved."""
    scheduler = ResourceScheduler(
        policies={
            "repos": ResourcePolicy(authenticated=900, anonymous=3600),
            "repo_detail": ResourcePolicy(authenticated=3600, anonymous=None, min_cache=1800),
        },
        has_token=True,
    )
    scheduler.record_fetch("repos", 6)
    scheduler.record_fetch("repo_detail", 16)
    assert not scheduler.should_fetch("repos")
    assert not scheduler.should_fetch("repo_detail")

    scheduler.reset()

    assert scheduler.should_fetch("repos")
    assert scheduler.should_fetch("repo_detail"), "even the half-hour hard floor"


def test_a_forced_refresh_clears_the_failure_backoff() -> None:
    """Holding someone to a doubling they cannot see would be perverse."""
    scheduler = ResourceScheduler(
        policies={"repos": ResourcePolicy(authenticated=900, anonymous=3600)}, has_token=True
    )
    for _ in range(4):
        scheduler.record_failure("repos")

    scheduler.reset()

    assert scheduler.should_fetch("repos")


def test_a_forced_refresh_does_not_override_an_exhausted_quota() -> None:
    """Nothing will succeed before the window resets, and a button is not an argument."""
    scheduler = ResourceScheduler(
        policies={"repos": ResourcePolicy(authenticated=900, anonymous=3600)}, has_token=True
    )
    scheduler.observe_rate_limit(0, time.time() + 1800)

    scheduler.reset()

    interval = scheduler.effective_interval("repos")
    assert interval is not None
    assert interval > 900, "still stretched past its base interval, out to the reset"
    assert not scheduler.should_fetch("repos")


def test_a_long_outage_does_not_stampede_into_a_spent_quota() -> None:
    """The same wall the forced refresh must not walk into, reached a different way.

    effective_interval stretches out to the reset, but that is measured from the last
    fetch - so a resource untouched for a day clears any interval however long.
    """
    scheduler = ResourceScheduler(
        policies={"repos": ResourcePolicy(authenticated=900, anonymous=3600)}, has_token=True
    )
    scheduler.record_fetch("repos", 6)
    scheduler._state("repos").last_fetched = time.time() - 86_400
    assert scheduler.should_fetch("repos"), "due on the strength of its age alone"

    scheduler.observe_rate_limit(0, time.time() + 1800)

    assert not scheduler.should_fetch("repos")


def test_the_quota_is_usable_again_once_the_window_turns_over() -> None:
    """`remaining` stays a stale zero until a request succeeds, so waiting on it deadlocks."""
    scheduler = ResourceScheduler(
        policies={"repos": ResourcePolicy(authenticated=900, anonymous=3600)}, has_token=True
    )
    scheduler.observe_rate_limit(0, time.time() - 1)

    assert scheduler.should_fetch("repos")


# --- persisting the rate-limit allowance ------------------------------------------------


def test_a_budget_round_trips_through_a_snapshot() -> None:
    """Otherwise every reload starts with no idea what the allowance is."""
    from dev_cloud.providers.scheduling import QUOTA_GRAPHQL, QUOTA_REST

    sched = _scheduler()
    reset = time.time() + 1800
    sched.observe_rate_limit(4128, reset, QUOTA_REST)
    sched.observe_rate_limit(3900, reset, QUOTA_GRAPHQL)

    restored = _scheduler()
    restored.restore_budgets(sched.persisted_budgets())

    assert restored.budget(QUOTA_REST).remaining == 4128
    assert restored.budget(QUOTA_GRAPHQL).remaining == 3900
    assert restored.budget(QUOTA_REST).seconds_until_reset() == pytest.approx(1800, abs=5)


def test_an_exhausted_quota_survives_a_reload() -> None:
    """The point of persisting it at all.

    A reload used to forget the block entirely and go straight back at the API, which is
    what keeps a spent allowance spent - and a handful of redeploys in one afternoon is how
    it gets spent in the first place.
    """
    sched = _scheduler()
    sched.observe_rate_limit(0, time.time() + 1800)
    assert not sched.should_fetch("repos")

    restored = _scheduler()
    restored.restore_budgets(sched.persisted_budgets())

    assert not restored.should_fetch("repos")


def test_a_window_that_has_turned_over_is_not_restored() -> None:
    """A remaining count means nothing outside the window it was measured in: carrying it
    forward would invent either headroom already spent or a block long since lifted."""
    sched = _scheduler()
    sched.observe_rate_limit(0, time.time() - 1)
    saved = sched.persisted_budgets()
    assert saved, "it is still written; restoring is where the staleness is judged"

    restored = _scheduler()
    restored.restore_budgets(saved)

    assert restored.budget().remaining is None
    assert restored.should_fetch("repos")


def test_an_unobserved_budget_is_not_written() -> None:
    """An absent figure and a figure of zero mean opposite things to every caller."""
    assert _scheduler().persisted_budgets() == {}


def test_restoring_junk_does_not_raise() -> None:
    """Snapshots move between schema versions; a restore must never stop the integration."""
    sched = _scheduler()
    sched.restore_budgets({"rest": {"remaining": "lots", "reset_at": "not a date"}})
    assert sched.budget().remaining is None
