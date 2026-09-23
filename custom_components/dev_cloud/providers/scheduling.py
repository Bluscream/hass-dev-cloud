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

    def is_exhausted(self) -> bool:
        """Whether the allowance is known to be spent and the window has not turned over.

        Once the reset time passes this goes false again even though `remaining` is still
        the stale zero, because nothing updates `remaining` until a request succeeds — and
        refusing to make that request would leave the quota permanently spent.
        """
        return self.remaining == 0 and self.seconds_until_reset() > 0

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

        # Nothing will succeed before the window turns over, so asking only prolongs the
        # block. `effective_interval` already stretches out to the reset, but that is
        # measured from the last fetch: a resource whose timestamp is old enough — after a
        # forced refresh, or after Home Assistant has been down a while — clears any
        # interval however long, and would stampede straight into the wall.
        if policy is not None and self.budget(policy.quota).is_exhausted():
            return False

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

    def reset(self) -> None:
        """Make every resource due right now.

        Backs out the failure backoff as well as the interval: a manual refresh is a person
        saying "try this again", and holding them to a doubling they cannot see would be
        perverse. The rate-limit stretch in `effective_interval` is deliberately *not*
        cleared - with the allowance spent, nothing will succeed before it resets, and a
        button press is not an argument against that.
        """
        for state in self._states.values():
            state.last_fetched = 0.0
            state.consecutive_failures = 0

    def persisted_budgets(self) -> dict[str, dict[str, Any]]:
        """Per-quota allowances, written into the snapshot and read back by restore_budgets.

        Persisted for the same reason the schedule is. These only ever came from a response
        header, so a reload started with no idea what the allowance was: every interval fell
        back to its declared floor, and an exhausted quota was forgotten outright. A handful
        of redeploys in one afternoon is exactly how a budget gets exhausted in the first
        place, and forgetting the exhaustion is what keeps it that way.
        """
        saved: dict[str, dict[str, Any]] = {}
        for quota, budget in self.budgets.items():
            if budget.remaining is None and budget.reset_epoch is None:
                continue
            entry: dict[str, Any] = {"remaining": budget.remaining}
            if budget.reset_epoch is not None:
                # ISO, matching every other timestamp in the snapshot.
                entry["reset_at"] = datetime.fromtimestamp(budget.reset_epoch, tz=UTC).isoformat()
            saved[quota] = entry
        return saved

    def restore_budgets(self, saved: Mapping[str, Mapping[str, Any]]) -> None:
        """Re-seed the allowances from a snapshot, dropping windows that have turned over.

        A remaining count belongs to one reset window and means nothing outside it. Carrying
        a stale figure forward would either invent headroom that was already spent or
        invent a block that has long since lifted, so an expired window is discarded and the
        next real response re-establishes the truth.
        """
        now = time.time()
        for quota, entry in saved.items():
            reset_raw = entry.get("reset_at")
            reset: float | None = None
            if isinstance(reset_raw, str):
                with contextlib.suppress(ValueError):
                    reset = datetime.fromisoformat(reset_raw).timestamp()
            if reset is None or reset <= now:
                continue

            budget = self.budget(quota)
            budget.reset_epoch = reset
            remaining = entry.get("remaining")
            if isinstance(remaining, int):
                budget.remaining = remaining

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
