"""Tests for new-repository / new-package event dispatch."""

from __future__ import annotations

from typing import Any

from dev_cloud.const import EVENT_NEW_REPO
from dev_cloud.coordinator import DevCloudCoordinator


class _Bus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event: str, data: dict[str, Any]) -> None:
        self.fired.append((event, data))


class _Hass:
    def __init__(self) -> None:
        self.bus = _Bus()


def _coordinator() -> Any:
    coord = DevCloudCoordinator.__new__(DevCloudCoordinator)
    coord.hass = _Hass()  # type: ignore[assignment]
    coord.platform_id = "github"
    coord.account_name = "Bluscream"
    return coord


def test_no_events_without_a_baseline() -> None:
    """Regression: a failed first fetch left an empty baseline, so the next successful poll
    fired one event per repository — a push notification per repository downstream."""
    coord = _coordinator()
    coord._dispatch_additions(EVENT_NEW_REPO, "repository", set(), {f"o/r{i}" for i in range(580)})

    assert coord.hass.bus.fired == []


def test_additions_against_an_existing_baseline_are_reported() -> None:
    coord = _coordinator()
    coord._dispatch_additions(EVENT_NEW_REPO, "repository", {"o/a"}, {"o/a", "o/b"})

    assert len(coord.hass.bus.fired) == 1
    event, payload = coord.hass.bus.fired[0]
    assert event == EVENT_NEW_REPO
    assert payload == {"platform": "github", "account": "Bluscream", "repository": "o/b"}


def test_removals_fire_nothing() -> None:
    coord = _coordinator()
    coord._dispatch_additions(EVENT_NEW_REPO, "repository", {"o/a", "o/b"}, {"o/a"})

    assert coord.hass.bus.fired == []
