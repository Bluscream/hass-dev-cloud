"""Adaptive per-resource polling and pagination control.

Two problems solved here, both consequences of the JSON dump needing *complete* lists:

1. **Complete lists cost requests.** Paginating everything on every coordinator update would
   burn the API quota. So each provider declares, per resource, how often that resource is
   allowed to be refreshed — separately for authenticated and anonymous use, since the
   quotas differ by orders of magnitude. `ResourceScheduler` then stretches those declared
   floors further whenever the observed rate-limit budget says the resource's *measured*
   cost would not comfortably fit before the quota resets. Resources that are skipped keep
   serving their last known value, so the snapshot stays complete either way.

2. **Pagination has to know when to stop.** A page count is not a reliable terminator: some
   endpoints ignore the page parameter entirely and happily return page 1 forever.
   `PageWalker` watches the identity of each page and stops when the API repeats itself.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Never stretch a resource beyond this, however tight the budget looks — a resource that
# refreshes once a day is indistinguishable from broken.
MAX_INTERVAL_SECONDS = 21_600  # 6 hours

# Spend at most this share of the remaining quota per reset window, leaving headroom for the
# config flow, other integrations, and the same token used elsewhere.
BUDGET_SAFETY_FACTOR = 0.25

# Assumed cost of a resource that has never been measured. Deliberately pessimistic so the
# first few polls back off rather than stampede.
DEFAULT_ASSUMED_COST = 5

# Consecutive failures double the interval up to this many times, then hold.
MAX_BACKOFF_DOUBLINGS = 4


#: Quota a resource draws on. A platform can meter several independently — GitHub bills
#: REST per request and GraphQL in points, against separate allowances that drain and reset
#: on their own schedules.
QUOTA_REST = "rest"
QUOTA_GRAPHQL = "graphql"


@dataclass(frozen=True)
class ResourcePolicy:
    """Declared refresh floor for one fetchable resource.

    `authenticated` and `anonymous` are minimum seconds between refreshes. `None` means the
    resource is unavailable in that mode and is never fetched (e.g. notifications without a
    token). The scheduler only ever makes these intervals *longer*, never shorter.

    `quota` names which allowance the resource spends, so a healthy REST budget cannot mask
    an exhausted GraphQL one.
    """

    authenticated: float | None
    anonymous: float | None
    quota: str = QUOTA_REST
    #: Resources this one is derived from. When a parent is refreshed the child's data was
    #: built from inputs that have since moved, so it is due again as soon as its floor
    #: allows — which is how the releases-of-repos-of-orgs chain reuses what is still valid
    #: instead of rebuilding the whole thing.
    depends_on: tuple[str, ...] = ()
    #: Hard floor. Never refetch inside this window, whatever else changed. Without it a
    #: parent refresh would cascade into its children on the very next poll.
    min_cache: float = 300.0

    def base_interval(self, has_token: bool) -> float | None:
        return self.authenticated if has_token else self.anonymous


@dataclass
class _ResourceState:
    last_fetched: float = 0.0
    measured_cost: int | None = None
    consecutive_failures: int = 0


@dataclass
class RateLimitBudget:
    """Most recent rate-limit headers observed from the platform."""

    remaining: int | None = None
    reset_epoch: float | None = None

    def seconds_until_reset(self) -> float:
        """Time left in the current window, never negative."""
        if self.reset_epoch is None:
            return 0.0
        return max(self.reset_epoch - time.time(), 0.0)

    def requests_per_second(self) -> float | None:
        """Safe sustained request rate until the quota resets, or None if unknown.

        Returns 0.0 — not None — when the allowance is spent. The two are different states
        and callers must not conflate them: unknown means "carry on", spent means "stop".
        """
        if self.remaining is None or self.reset_epoch is None:
            return None
        window = max(self.reset_epoch - time.time(), 1.0)
        return (self.remaining * BUDGET_SAFETY_FACTOR) / window


@dataclass
class ResourceScheduler:
    """Decides which resources may be refreshed on this poll."""

    policies: dict[str, ResourcePolicy]
    has_token: bool
    budgets: dict[str, RateLimitBudget] = field(default_factory=dict)
    _states: dict[str, _ResourceState] = field(default_factory=dict)

    def _state(self, key: str) -> _ResourceState:
        return self._states.setdefault(key, _ResourceState())

    def budget(self, quota: str = QUOTA_REST) -> RateLimitBudget:
        """The tracked allowance for one quota."""
        return self.budgets.setdefault(quota, RateLimitBudget())

    def observe_rate_limit(
        self,
        remaining: int | None,
        reset_epoch: float | None,
        quota: str = QUOTA_REST,
    ) -> None:
        """Record rate-limit headers for one quota.

        Keyed by quota because a platform meters several independently: writing a healthy
        REST figure over an exhausted GraphQL one hid the very resource that needed to back
        off the hardest.
        """
        budget = self.budget(quota)
        if remaining is not None:
            budget.remaining = remaining
        if reset_epoch is not None:
            budget.reset_epoch = reset_epoch

    def effective_interval(self, key: str) -> float | None:
        """Refresh floor for `key`, stretched to fit the remaining quota.

        Returns None when the resource is unavailable in the current auth mode.
        """
        policy = self.policies.get(key)
        if policy is None:
            return 0.0  # undeclared resources are refreshed every poll
        base = policy.base_interval(self.has_token)
        if base is None:
            return None

        budget = self.budget(policy.quota)
        rate = budget.requests_per_second()
        if rate is None:
            return base
        if rate <= 0:
            # The allowance is spent: nothing will succeed before it resets, so wait for
            # the reset rather than retrying into a wall. `not rate` here would have
            # treated exhaustion as "no information" and carried on at the base interval.
            return min(max(base, budget.seconds_until_reset()), MAX_INTERVAL_SECONDS)

        cost = self._state(key).measured_cost or DEFAULT_ASSUMED_COST
        # Seconds this resource must wait so that repeating it at that cadence consumes no
        # more than its share of the safe request rate.
        required = cost / rate
        return min(max(base, required), MAX_INTERVAL_SECONDS)

    def _failure_backoff(self, key: str, interval: float) -> float:
        """Widen the interval while a resource keeps failing.

        Without this a resource that errors is retried on every poll, because only a
        successful fetch updates its timestamp. Against a rate-limited endpoint that is the
        worst possible behaviour: the retries are what prolong the block.
        """
        failures = self._state(key).consecutive_failures
        if not failures:
            return interval
        widened = interval * float(2 ** min(failures, MAX_BACKOFF_DOUBLINGS))
        return min(widened, float(MAX_INTERVAL_SECONDS))

    def _parent_refreshed_since(self, key: str) -> bool:
        """Whether anything this resource is derived from has moved under it."""
        policy = self.policies.get(key)
        if policy is None:
            return False
        own = self._state(key).last_fetched
        return any(self._state(parent).last_fetched > own for parent in policy.depends_on)

    def should_fetch(self, key: str) -> bool:
        """Whether `key` is due for a refresh right now."""
        interval = self.effective_interval(key)
        if interval is None:
            return False

        policy = self.policies.get(key)
        age = time.time() - self._state(key).last_fetched

        # The hard floor wins over everything, including a parent having changed.
        if policy is not None and age < policy.min_cache:
            return False

        # Derived from inputs that have since been refreshed, so it is rebuilt from them.
        if self._parent_refreshed_since(key):
            return True

        return age >= self._failure_backoff(key, interval)

    def next_due(self, key: str) -> float:
        """Seconds until this resource may be refreshed again; 0 when it is due now."""
        interval = self.effective_interval(key)
        if interval is None:
            return 0.0
        policy = self.policies.get(key)
        floor = max(interval, policy.min_cache if policy else 0.0)
        waited = time.time() - self._state(key).last_fetched
        return max(self._failure_backoff(key, floor) - waited, 0.0)

    def restore(self, states: Mapping[str, Mapping[str, Any]]) -> None:
        """Seed timestamps and costs from a previously persisted snapshot.

        Without this every reload starts at zero and refetches everything, which is how a
        handful of redeploys in one afternoon exhausted an API budget. Only resources still
        declared are restored, so one that has been removed does not linger.
        """
        for key, saved in states.items():
            if key not in self.policies:
                continue
            state = self._state(key)
            fetched = saved.get("fetched_at")
            if isinstance(fetched, str):
                with contextlib.suppress(ValueError):
                    state.last_fetched = datetime.fromisoformat(fetched).timestamp()
            cost = saved.get("cost")
            if isinstance(cost, int):
                state.measured_cost = cost

    def record_fetch(self, key: str, cost: int) -> None:
        """Note that `key` was just refreshed, and what it actually cost in requests.

        The most recent measurement wins rather than the highest seen. A high-water mark
        never recovers, so one abnormally expensive poll would inflate the interval for the
        lifetime of the provider even after the cause passed.
        """
        state = self._state(key)
        state.last_fetched = time.time()
        state.measured_cost = cost
        state.consecutive_failures = 0

    def record_failure(self, key: str) -> None:
        """Note a failed attempt, so the retry is paced instead of immediate."""
        state = self._state(key)
        state.last_fetched = time.time()
        state.consecutive_failures += 1

    def persisted_state(self) -> dict[str, dict[str, Any]]:
        """Per-resource schedule, written into the snapshot and read back by `restore`.

        One block rather than two: this previously sat beside a `scheduling` diagnostics map
        that repeated `cost` and carried an `age` which was only ever now minus
        `fetched_at`. Timestamps are ISO, matching the snapshot's own `fetched_at`, so the
        file reads the same way throughout.
        """
        state: dict[str, dict[str, Any]] = {}
        for key in self.policies:
            last = self._state(key).last_fetched
            if not last:
                continue
            state[key] = {
                "fetched_at": datetime.fromtimestamp(last, tz=UTC).isoformat(),
                "next_due_in": round(self.next_due(key), 1),
                "interval": round(self.effective_interval(key) or 0, 1),
                "min_cache": self.policies[key].min_cache,
                "cost": self._state(key).measured_cost,
                "quota": self.policies[key].quota,
                "failures": self._state(key).consecutive_failures,
            }
        return state


class PageWalker:
    """Stops a pagination loop when the endpoint stops making progress.

    A page counter alone is not enough: an endpoint that ignores `page` returns the same
    first page indefinitely, and a naive loop would append it until the safety limit. This
    compares the identity of each page against the ones already seen and bails out instead.
    """

    def __init__(self, label: str, page_size: int) -> None:
        self._label = label
        self._page_size = page_size
        self._seen: set[str] = set()

    @staticmethod
    def _fingerprint(batch: list[Any]) -> str:
        """Identity of a page, derived from its first and last item."""

        def ident(item: Any) -> str:
            if isinstance(item, dict):
                for key in ("id", "node_id", "full_name", "path_with_namespace", "name"):
                    value = item.get(key)
                    if value is not None:
                        return f"{key}:{value}"
            return repr(item)[:120]

        return f"{len(batch)}|{ident(batch[0])}|{ident(batch[-1])}"

    def accept(self, batch: list[Any]) -> bool:
        """Whether `batch` is new data worth keeping. False means stop paginating."""
        if not batch:
            return False

        fingerprint = self._fingerprint(batch)
        if fingerprint in self._seen:
            _LOGGER.debug(
                "Pagination for %s repeated a page; treating the collection as complete",
                self._label,
            )
            return False

        self._seen.add(fingerprint)
        return True

    def is_last(self, batch: list[Any]) -> bool:
        """Whether a short page means the collection is exhausted."""
        return len(batch) < self._page_size
