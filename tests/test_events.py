"""Tests for change detection between polls."""

from __future__ import annotations

from dev_cloud import events
from dev_cloud.const import (
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_ORG,
    EVENT_NEW_RELEASE,
    EVENT_NEW_REPO,
    EVENT_ORG_REMOVED,
    EVENT_REPO_REMOVED,
    EVENT_STARS_CHANGED,
)
from dev_cloud.models import DevCloudData, NotificationData, OrgData, ProfileData, RepoData

ALL = {"repos", "packages", "orgs", "notifications"}


def _repo(full_name: str, stars: int = 0, tags: tuple[str, ...] = ()) -> RepoData:
    repo = RepoData(name=full_name.split("/")[-1], full_name=full_name, url="u", stars=stars)
    repo.releases = [{"tag": t} for t in tags]
    return repo


def _state(
    repos: list[RepoData] | None = None,
    orgs: list[OrgData] | None = None,
    notifications: list[NotificationData] | None = None,
    collected: set[str] | None = None,
) -> events.AccountState:
    return events.snapshot(
        DevCloudData(
            profile=ProfileData(username="x"),
            repos=repos or [],
            orgs=orgs or [],
            notifications=notifications or [],
            collected=collected if collected is not None else set(ALL),
        )
    )


def _types(result: list[tuple[str, dict]]) -> list[str]:
    return [e for e, _ in result]


def test_no_events_without_a_baseline() -> None:
    """The first poll of a process must not announce everything that already existed."""
    assert events.changes(None, _state(repos=[_repo("o/a")])) == []


def test_a_new_repository_is_reported_with_its_url() -> None:
    before = _state(repos=[_repo("o/a")])
    after = _state(repos=[_repo("o/a"), _repo("o/b")])

    result = events.changes(before, after)
    assert _types(result) == [EVENT_NEW_REPO]
    assert result[0][1]["repository"] == "o/b"
    assert result[0][1]["url"] == "u"


def test_a_deleted_repository_is_reported() -> None:
    result = events.changes(_state(repos=[_repo("o/a"), _repo("o/b")]), _state(repos=[_repo("o/a")]))
    assert _types(result) == [EVENT_REPO_REMOVED]
    assert result[0][1]["repository"] == "o/b"


def test_a_star_change_carries_both_values_and_the_delta() -> None:
    result = events.changes(
        _state(repos=[_repo("o/a", stars=41)]), _state(repos=[_repo("o/a", stars=42)])
    )
    assert _types(result) == [EVENT_STARS_CHANGED]
    payload = result[0][1]
    assert (payload["previous_stars"], payload["stars"], payload["delta"]) == (41, 42, 1)


def test_losing_a_star_is_reported_too() -> None:
    result = events.changes(
        _state(repos=[_repo("o/a", stars=42)]), _state(repos=[_repo("o/a", stars=41)])
    )
    assert result[0][1]["delta"] == -1


def test_a_new_release_is_reported_per_tag() -> None:
    result = events.changes(
        _state(repos=[_repo("o/a", tags=("v1",))]),
        _state(repos=[_repo("o/a", tags=("v1", "v2", "v3"))]),
    )
    assert _types(result) == [EVENT_NEW_RELEASE, EVENT_NEW_RELEASE]
    assert {e[1]["tag"] for e in result} == {"v2", "v3"}


def test_organisation_membership_changes_are_reported() -> None:
    before = _state(orgs=[OrgData(name="one")])
    after = _state(orgs=[OrgData(name="two")])

    result = events.changes(before, after)
    assert set(_types(result)) == {EVENT_NEW_ORG, EVENT_ORG_REMOVED}


def test_a_new_unread_notification_carries_what_a_push_needs() -> None:
    after = _state(
        notifications=[
            NotificationData(
                notification_id="7",
                title="test workflow run failed for main branch",
                repository="Bluscream/ptero-cli",
                url="https://github.com/…",
                reason="ci_activity",
                subject_type="CheckSuite",
            )
        ]
    )
    result = events.changes(_state(), after)

    assert _types(result) == [EVENT_NEW_NOTIFICATION]
    payload = result[0][1]
    assert payload["title"].startswith("test workflow")
    assert payload["repository"] == "Bluscream/ptero-cli"
    assert payload["reason"] == "ci_activity"


def test_a_notification_that_was_read_is_not_re_reported() -> None:
    unread = NotificationData(notification_id="7", title="t", unread=True)
    read = NotificationData(notification_id="7", title="t", unread=False)

    assert events.changes(_state(notifications=[unread]), _state(notifications=[read])) == []


def test_an_uncollected_resource_is_never_read_as_emptied() -> None:
    """Regression guard: a failed fetch must not look like 580 deletions."""
    before = _state(repos=[_repo("o/a"), _repo("o/b")])
    after = _state(repos=[], collected={"packages", "orgs", "notifications"})

    assert events.changes(before, after) == []


def test_a_collection_emptying_entirely_is_treated_as_a_failed_fetch() -> None:
    """Even when the resource reports as collected, everything vanishing at once is far
    likelier to be a bad response than a real mass deletion."""
    before = _state(repos=[_repo("o/a"), _repo("o/b")])
    after = _state(repos=[])

    assert events.changes(before, after) == []
