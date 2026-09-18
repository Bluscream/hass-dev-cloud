"""Tests for adaptive resource scheduling and pagination termination."""

from __future__ import annotations

import time

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

    assert sched.diagnostics()["repos"]["cost"] == 7


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
