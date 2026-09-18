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

import logging
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ResourcePolicy:
    """Declared refresh floor for one fetchable resource.

    `authenticated` and `anonymous` are minimum seconds between refreshes. `None` means the
    resource is unavailable in that mode and is never fetched (e.g. notifications without a
    token). The scheduler only ever makes these intervals *longer*, never shorter.
    """

    authenticated: float | None
    anonymous: float | None

    def base_interval(self, has_token: bool) -> float | None:
        return self.authenticated if has_token else self.anonymous


@dataclass
class _ResourceState:
    last_fetched: float = 0.0
    measured_cost: int | None = None


@dataclass
class RateLimitBudget:
    """Most recent rate-limit headers observed from the platform."""

    remaining: int | None = None
    reset_epoch: float | None = None

    def requests_per_second(self) -> float | None:
        """Safe sustained request rate until the quota resets, or None if unknown."""
        if self.remaining is None or self.reset_epoch is None:
            return None
        window = max(self.reset_epoch - time.time(), 1.0)
        return (self.remaining * BUDGET_SAFETY_FACTOR) / window


@dataclass
class ResourceScheduler:
    """Decides which resources may be refreshed on this poll."""

    policies: dict[str, ResourcePolicy]
    has_token: bool
    budget: RateLimitBudget = field(default_factory=RateLimitBudget)
    _states: dict[str, _ResourceState] = field(default_factory=dict)

    def _state(self, key: str) -> _ResourceState:
        return self._states.setdefault(key, _ResourceState())

    def observe_rate_limit(self, remaining: int | None, reset_epoch: float | None) -> None:
        """Record rate-limit headers so intervals can adapt to the real budget."""
        if remaining is not None:
            self.budget.remaining = remaining
        if reset_epoch is not None:
            self.budget.reset_epoch = reset_epoch

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

        rate = self.budget.requests_per_second()
        if not rate:
            return base

        cost = self._state(key).measured_cost or DEFAULT_ASSUMED_COST
        # Seconds this resource must wait so that repeating it at that cadence consumes no
        # more than its share of the safe request rate.
        required = cost / rate
        return min(max(base, required), MAX_INTERVAL_SECONDS)

    def should_fetch(self, key: str) -> bool:
        """Whether `key` is due for a refresh right now."""
        interval = self.effective_interval(key)
        if interval is None:
            return False
        return (time.time() - self._state(key).last_fetched) >= interval

    def record_fetch(self, key: str, cost: int) -> None:
        """Note that `key` was just refreshed, and what it actually cost in requests."""
        state = self._state(key)
        state.last_fetched = time.time()
        # Smooth over spikes but converge quickly; costs grow as an account grows.
        previous = state.measured_cost
        state.measured_cost = cost if previous is None else max(cost, previous)

    def diagnostics(self) -> dict[str, Any]:
        """Per-resource scheduling state, surfaced on the profile sensor for debugging."""
        return {
            key: {
                "interval": round(self.effective_interval(key) or 0, 1),
                "cost": self._state(key).measured_cost,
                "age": round(time.time() - self._state(key).last_fetched, 1),
            }
            for key in self.policies
        }


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
