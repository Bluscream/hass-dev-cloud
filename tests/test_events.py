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
    EVENT_NEW_DOWNLOADS,
    EVENT_NEW_ISSUE,
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_ORG,
    EVENT_NEW_PR,
    EVENT_NEW_RELEASE,
    EVENT_NEW_REPO,
    EVENT_NEW_SECURITY_ALERT,
    EVENT_NEW_TAG,
    EVENT_ORG_REMOVED,
    EVENT_TAG_REMOVED,
    EVENT_REPO_ARCHIVED,
    EVENT_REPO_CHANGED,
    EVENT_REPO_REMOVED,
    EVENT_SECURITY_ALERTS_RESOLVED,
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


def test_a_download_does_not_also_count_as_the_release_changing() -> None:
    """Downloads get one batched event; they must not additionally mark every release as
    edited, which would fire per release on every poll."""
    before = _repo("o/a", releases=("v1",))
    before.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 10}]}]
    after = _repo("o/a", releases=("v1",))
    after.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 99}]}]

    result = events.changes(_snap(repos=[before]), _snap(repos=[after]))
    assert _types(result) == [EVENT_NEW_DOWNLOADS]
    assert _first(result, EVENT_NEW_DOWNLOADS)["delta"] == 89


def _repo_with_downloads(full_name: str, tag: str, downloads: int) -> RepoData:
    repo = _repo(full_name)
    repo.releases = [{"tag": tag, "name": tag, "assets": [{"name": "app.zip", "downloads": downloads}]}]
    return repo


def test_downloads_are_reported_as_one_batched_event() -> None:
    """Per-asset events would be unusable — this account has 3,581 release assets."""
    before = _snap(repos=[_repo_with_downloads("o/a", "v1", 100), _repo_with_downloads("o/b", "v1", 50)])
    after = _snap(repos=[_repo_with_downloads("o/a", "v1", 1100), _repo_with_downloads("o/b", "v1", 75)])

    result = events.changes(before, after)
    assert _types(result).count(EVENT_NEW_DOWNLOADS) == 1

    payload = _first(result, EVENT_NEW_DOWNLOADS)
    assert payload["delta"] == 1025
    assert payload["total"] == 1175
    assert payload["assets"] == 2
    assert payload["repositories"] == 2
    assert payload["top_repository"] == "o/a"
    assert payload["top_repository_delta"] == 1000
    assert payload["breakdown"][0] == {"repository": "o/a", "delta": 1000}


def test_no_download_event_when_nothing_moved() -> None:
    same = [_repo_with_downloads("o/a", "v1", 100)]
    assert events.changes(_snap(repos=same), _snap(repos=same)) == []


def test_downloads_only_report_increases() -> None:
    """A falling count means an asset or release was removed, which release_changed covers."""
    before = _snap(repos=[_repo_with_downloads("o/a", "v1", 500)])
    after = _snap(repos=[_repo_with_downloads("o/a", "v1", 400)])

    assert EVENT_NEW_DOWNLOADS not in _types(events.changes(before, after))


def test_organisation_downloads_count_towards_the_batch() -> None:
    """They are in the snapshot, so a push about "your downloads" should include them."""
    org_before = OrgData(name="Org", is_owned=True, repos=[_repo_with_downloads("Org/x", "v1", 10)])
    org_after = OrgData(name="Org", is_owned=True, repos=[_repo_with_downloads("Org/x", "v1", 60)])

    result = events.changes(_snap(orgs=[org_before]), _snap(orgs=[org_after]))
    assert _first(result, EVENT_NEW_DOWNLOADS)["delta"] == 50


def _repo_with_alerts(full_name: str, numbers: tuple[int, ...]) -> RepoData:
    repo = _repo(full_name)
    repo.security_alerts = [
        {
            "number": n,
            "severity": "HIGH",
            "package": "jsonwebtoken",
            "ecosystem": "NPM",
            "ghsa": f"GHSA-{n}",
            "cve": f"CVE-2022-{n}",
            "cvss": 8.1,
            "summary": "unrestricted key type",
            "url": f"https://github.com/advisories/GHSA-{n}",
        }
        for n in numbers
    ]
    return repo


def test_each_new_security_alert_gets_its_own_event() -> None:
    """A new advisory is something to act on, so it arrives whole rather than as a count."""
    result = events.changes(
        _snap(repos=[_repo_with_alerts("o/a", (1,))]),
        _snap(repos=[_repo_with_alerts("o/a", (1, 2))]),
    )
    payload = _first(result, EVENT_NEW_SECURITY_ALERT)
    assert payload["repository"] == "o/a"
    assert payload["ghsa"] == "GHSA-2"
    assert payload["cve"] == "CVE-2022-2"
    assert payload["cvss"] == 8.1
    assert payload["alert"]["package"] == "jsonwebtoken"


def test_resolved_alerts_are_batched_per_repository() -> None:
    """One dependency bump clears dozens at once — 68 on one repository here."""
    before = _snap(repos=[_repo_with_alerts("o/a", tuple(range(1, 69)))])
    after = _snap(repos=[_repo_with_alerts("o/a", (1,))])

    result = events.changes(before, after)
    assert _types(result).count(EVENT_SECURITY_ALERTS_RESOLVED) == 1

    payload = _first(result, EVENT_SECURITY_ALERTS_RESOLVED)
    assert payload["resolved"] == 67
    assert payload["remaining"] == 1
    assert len(payload["alerts"]) == 67


def test_a_repository_with_no_alerts_either_side_is_silent() -> None:
    assert events.changes(_snap(repos=[_repo("o/a")]), _snap(repos=[_repo("o/a")])) == []
