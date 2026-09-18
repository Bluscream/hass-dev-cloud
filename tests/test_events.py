"""Tests for change detection between polls.

Detection runs over serialised snapshots, so the fixtures build real ones through the same
path the coordinator uses rather than hand-written dicts.
"""

from __future__ import annotations

from typing import Any

from dev_cloud import events, storage
from dev_cloud.const import (
    EVENT_BRANCH_REMOVED,
    EVENT_ISSUE_CLOSED,
    EVENT_NEW_BRANCH,
    EVENT_NEW_ISSUE,
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_ORG,
    EVENT_NEW_PR,
    EVENT_NEW_RELEASE,
    EVENT_NEW_REPO,
    EVENT_NEW_TAG,
    EVENT_ORG_REMOVED,
    EVENT_TAG_REMOVED,
    EVENT_REPO_ARCHIVED,
    EVENT_REPO_CHANGED,
    EVENT_REPO_REMOVED,
    EVENT_REPO_VISIBILITY_CHANGED,
    EVENT_STARS_CHANGED,
)
from dev_cloud.models import DevCloudData, NotificationData, OrgData, ProfileData, RepoData

ALL = {"repos", "packages", "orgs", "notifications"}


def _repo(full_name: str, **kw: Any) -> RepoData:
    releases = kw.pop("releases", ())
    branches = kw.pop("branches", ())
    tags = kw.pop("tags", ())
    issues = kw.pop("issues", ())
    prs = kw.pop("prs", ())
    repo = RepoData(name=full_name.split("/")[-1], full_name=full_name, url="u", **kw)
    repo.releases = [{"tag": t, "name": t, "assets": []} for t in releases]
    repo.branches = [{"name": b, "sha": "s"} for b in branches]
    repo.tags = [{"name": t, "sha": "s"} for t in tags]
    repo.issues = [{"number": n, "title": f"issue {n}"} for n in issues]
    repo.prs = [{"number": n, "title": f"pr {n}"} for n in prs]
    return repo


def _snap(
    repos: list[RepoData] | None = None,
    orgs: list[OrgData] | None = None,
    notifications: list[NotificationData] | None = None,
    collected: set[str] | None = None,
) -> dict[str, Any]:
    return storage.build_snapshot(
        "github",
        "x",
        DevCloudData(
            profile=ProfileData(username="x"),
            repos=repos or [],
            orgs=orgs or [],
            notifications=notifications or [],
            collected=collected if collected is not None else set(ALL),
        ),
    )


def _types(result: list[tuple[str, dict]]) -> list[str]:
    return [e for e, _ in result]


def _first(result: list[tuple[str, dict]], event: str) -> dict:
    return next(payload for name, payload in result if name == event)


def test_no_events_without_a_baseline() -> None:
    assert events.changes(None, _snap(repos=[_repo("o/a")])) == []


def test_a_new_repository_carries_the_whole_repository() -> None:
    """Not just its name: an automation should never have to go looking anything up."""
    new = _repo("o/b", stars=7, releases=("v1",), branches=("main",), tags=("v1",))
    result = events.changes(_snap(repos=[_repo("o/a")]), _snap(repos=[_repo("o/a"), new]))

    payload = _first(result, EVENT_NEW_REPO)
    assert payload["repository"] == "o/b"
    assert payload["repo"]["stars"] == 7
    assert payload["repo"]["releases"][0]["tag"] == "v1"
    assert payload["repo"]["branches"][0]["name"] == "main"


def test_a_removed_repository_carries_its_last_known_state() -> None:
    result = events.changes(_snap(repos=[_repo("o/a", stars=3)]), _snap(repos=[]))
    # An emptied collection is a failed fetch, so use a surviving repo to prove the path.
    result = events.changes(
        _snap(repos=[_repo("o/a", stars=3), _repo("o/b")]), _snap(repos=[_repo("o/b")])
    )
    payload = _first(result, EVENT_REPO_REMOVED)
    assert payload["repository"] == "o/a"
    assert payload["repo"]["stars"] == 3


def test_a_star_change_carries_old_new_and_a_signed_delta() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", stars=41)]), _snap(repos=[_repo("o/a", stars=42)])
    )
    payload = _first(result, EVENT_STARS_CHANGED)
    assert (payload["previous_stars"], payload["stars"], payload["delta"]) == (41, 42, 1)
    assert payload["old"]["stars"] == 41 and payload["new"]["stars"] == 42


def test_losing_a_star_reports_a_negative_delta() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", stars=42)]), _snap(repos=[_repo("o/a", stars=41)])
    )
    assert _first(result, EVENT_STARS_CHANGED)["delta"] == -1


def test_a_new_release_carries_the_release_itself() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", releases=("v1",))]),
        _snap(repos=[_repo("o/a", releases=("v1", "v2"))]),
    )
    payload = _first(result, EVENT_NEW_RELEASE)
    assert payload["tag"] == "v2"
    assert payload["release"]["name"] == "v2"


def test_branches_and_tags_are_reported_individually() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", branches=("main",), tags=("v1",))]),
        _snap(repos=[_repo("o/a", branches=("main", "dev"), tags=())]),
    )
    assert _first(result, EVENT_NEW_BRANCH)["name"] == "dev"
    assert _first(result, EVENT_NEW_BRANCH)["branch"]["sha"] == "s"
    # "main" survived, so no branch was removed; the tag is what disappeared.
    assert EVENT_BRANCH_REMOVED not in _types(result)
    assert _first(result, EVENT_TAG_REMOVED)["name"] == "v1"
    assert EVENT_NEW_TAG not in _types(result)


def test_issues_and_pull_requests_opening_and_closing() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", issues=(1,), prs=())]),
        _snap(repos=[_repo("o/a", issues=(2,), prs=(9,))]),
    )
    assert _first(result, EVENT_NEW_ISSUE)["issue"]["number"] == 2
    assert _first(result, EVENT_ISSUE_CLOSED)["issue"]["number"] == 1
    assert _first(result, EVENT_NEW_PR)["pull_request"]["number"] == 9


def test_archiving_and_visibility_are_their_own_events() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a")]),
        _snap(repos=[_repo("o/a", is_archived=True, is_private=True)]),
    )
    assert _first(result, EVENT_REPO_ARCHIVED)["archived"] is True
    assert _first(result, EVENT_REPO_VISIBILITY_CHANGED)["private"] is True


def test_a_generic_change_names_the_fields_that_moved() -> None:
    result = events.changes(
        _snap(repos=[_repo("o/a", description="old", stars=1)]),
        _snap(repos=[_repo("o/a", description="new", stars=2)]),
    )
    assert set(_first(result, EVENT_REPO_CHANGED)["changed"]) == {"description", "stars"}


def test_organisation_membership_changes() -> None:
    result = events.changes(
        _snap(orgs=[OrgData(name="one")]), _snap(orgs=[OrgData(name="one"), OrgData(name="two")])
    )
    assert _first(result, EVENT_NEW_ORG)["name"] == "two"

    result = events.changes(
        _snap(orgs=[OrgData(name="one"), OrgData(name="two")]), _snap(orgs=[OrgData(name="one")])
    )
    assert _first(result, EVENT_ORG_REMOVED)["name"] == "two"


def test_a_new_notification_carries_everything_a_push_needs() -> None:
    after = _snap(
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
    payload = _first(events.changes(_snap(), after), EVENT_NEW_NOTIFICATION)
    assert payload["title"].startswith("test workflow")
    assert payload["repository"] == "Bluscream/ptero-cli"
    assert payload["reason"] == "ci_activity"
    assert payload["notification"]["subject_type"] == "CheckSuite"


def test_an_uncollected_resource_is_never_read_as_emptied() -> None:
    """Regression guard: a failed fetch must not look like 580 deletions."""
    before = _snap(repos=[_repo("o/a"), _repo("o/b")])
    after = _snap(repos=[], collected={"packages", "orgs", "notifications"})

    assert events.changes(before, after) == []


def test_a_collection_emptying_entirely_is_treated_as_a_failed_fetch() -> None:
    before = _snap(repos=[_repo("o/a"), _repo("o/b")])
    assert events.changes(before, _snap(repos=[])) == []


def test_download_counts_ticking_upward_do_not_fire_events() -> None:
    """Downloads only ever increase, so treating them as a change would fire every poll."""
    before = _repo("o/a", releases=("v1",))
    before.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 10}]}]
    after = _repo("o/a", releases=("v1",))
    after.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 99}]}]

    assert events.changes(_snap(repos=[before]), _snap(repos=[after])) == []
