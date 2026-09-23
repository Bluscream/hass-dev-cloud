"""Tests for change detection between polls.

Detection runs over serialised snapshots, so the fixtures build real ones through the same
path the coordinator uses rather than hand-written dicts.
"""

from __future__ import annotations

import json
from typing import Any

from dev_cloud import events, storage
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


def _kinds(result: events.Diff) -> list[str]:
    return [c["kind"] for c in result.updates]


def _one(result: events.Diff, kind: str) -> events.Change:
    return next(c for c in result.updates if c["kind"] == kind)


def _all_of(result: events.Diff, kind: str) -> list[events.Change]:
    return [c for c in result.updates if c["kind"] == kind]


def test_no_changes_without_a_baseline() -> None:
    assert not events.diff(None, _snap(repos=[_repo("o/a")]))


def test_a_new_repository_carries_the_repository_without_its_collections() -> None:
    """Enough to write the message from, small enough to survive the event size limit.

    The nested lists stay in the snapshot: one repository's releases and their assets are
    larger on their own than the whole per-event budget.
    """
    new = _repo("o/b", stars=7, releases=("v1",), branches=("main",), tags=("v1",))
    result = events.diff(_snap(repos=[_repo("o/a")]), _snap(repos=[_repo("o/a"), new]))

    change = _one(result, "new_repo")
    assert change["repository"] == "o/b"
    assert change["subject"] == "o/b"
    assert change["thing"] == events.THING_REPO
    assert change["new"]["stars"] == 7
    assert "releases" not in change["new"]
    assert "branches" not in change["new"]


def test_a_removed_repository_carries_its_last_known_state() -> None:
    # An emptied collection is a failed fetch, so use a surviving repo to prove the path.
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=3), _repo("o/b")]), _snap(repos=[_repo("o/b")])
    )
    change = _one(result, "repo_removed")
    assert change["repository"] == "o/a"
    assert change["old"]["stars"] == 3


def test_a_star_change_carries_old_new_and_a_signed_delta() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=41, releases=("v1",), branches=("b",))]),
        _snap(repos=[_repo("o/a", stars=42, releases=("v1",), branches=("b",))]),
    )
    change = _one(result, "stars_changed")
    assert (change["old"], change["new"], change["delta"]) == (41, 42, 1)
    assert change["thing"] == events.THING_STAR
    assert change["repository"] == "o/a"


def test_losing_a_star_reports_a_negative_delta() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=42)]), _snap(repos=[_repo("o/a", stars=41)])
    )
    assert _one(result, "stars_changed")["delta"] == -1


def test_forks_and_watchers_get_their_own_change_not_just_a_field_list() -> None:
    """The first fork and the first watcher are worth announcing on their own.

    Both sides carry a branch because watchers come from the detail walk, and a repository
    that walk has not covered has no watcher figure to compare - see the guard tests below.
    """
    result = events.diff(
        _snap(repos=[_repo("o/a", forks=0, watchers=0, branches=("main",))]),
        _snap(repos=[_repo("o/a", forks=1, watchers=2, branches=("main",))]),
    )
    assert _one(result, "forks_changed")["delta"] == 1
    assert _one(result, "watchers_changed")["delta"] == 2
    assert _one(result, "watchers_changed")["thing"] == events.THING_WATCHER


def test_a_counter_leaving_zero_is_marked_as_a_first() -> None:
    """The first star, the first fork, the first watcher — each worth its own announcement."""
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=0, forks=0)]),
        _snap(repos=[_repo("o/a", stars=1, forks=1)]),
    )
    assert _one(result, "stars_changed")["first"] is True
    assert _one(result, "forks_changed")["first"] is True


def test_a_counter_that_was_already_moving_is_not_a_first() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=41)]), _snap(repos=[_repo("o/a", stars=42)])
    )
    assert _one(result, "stars_changed")["first"] is False


def test_a_repositorys_very_first_release_is_marked_as_a_first() -> None:
    first = events.diff(
        _snap(repos=[_repo("o/a", branches=("main",))]),
        _snap(repos=[_repo("o/a", branches=("main",), releases=("v1",))]),
    )
    assert _one(first, "new_release")["first"] is True

    later = events.diff(
        _snap(repos=[_repo("o/a", releases=("v1",))]),
        _snap(repos=[_repo("o/a", releases=("v1", "v2"))]),
    )
    assert _one(later, "new_release")["first"] is False


def test_a_new_release_carries_the_release_itself() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", releases=("v1",))]),
        _snap(repos=[_repo("o/a", releases=("v1", "v2"))]),
    )
    change = _one(result, "new_release")
    assert change["subject"] == "v2"
    assert change["new"]["name"] == "v2"


def test_branches_and_tags_are_reported_individually() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", branches=("main",), tags=("v1",))]),
        _snap(repos=[_repo("o/a", branches=("main", "dev"), tags=())]),
    )
    assert _one(result, "new_branch")["subject"] == "dev"
    assert _one(result, "new_branch")["new"]["sha"] == "s"
    # "main" survived, so no branch was removed. The tags list emptied entirely, which is
    # read as an incomplete fetch rather than a deletion.
    assert "branch_removed" not in _kinds(result)
    assert "tag_removed" not in _kinds(result)
    assert "new_tag" not in _kinds(result)


def test_a_tag_removal_is_reported_when_others_survive() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", tags=("v1", "v2"))]),
        _snap(repos=[_repo("o/a", tags=("v2",))]),
    )
    assert _one(result, "tag_removed")["subject"] == "v1"


def test_issues_and_pull_requests_opening_and_closing() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", issues=(1, 3), prs=(8,))]),
        _snap(repos=[_repo("o/a", issues=(2, 3), prs=(8, 9))]),
    )
    assert _one(result, "new_issue")["new"]["number"] == 2
    assert _one(result, "issue_closed")["old"]["number"] == 1
    assert _one(result, "new_pull_request")["new"]["number"] == 9
    assert _one(result, "new_issue")["subject"] == "#2"


def test_archiving_and_visibility_are_their_own_changes() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a")]),
        _snap(repos=[_repo("o/a", is_archived=True, is_private=True)]),
    )
    assert _one(result, "repo_archived")["new"] is True
    assert _one(result, "repo_visibility_changed")["new"] == "private"
    assert _one(result, "repo_visibility_changed")["old"] == "public"


def test_a_generic_change_carries_the_old_and_new_of_each_field() -> None:
    result = events.diff(
        _snap(repos=[_repo("o/a", description="old", stars=1)]),
        _snap(repos=[_repo("o/a", description="new", stars=2)]),
    )
    change = _one(result, "repo_changed")
    assert change["detail"] == {"description": {"old": "old", "new": "new"}}


def test_a_star_is_not_also_reported_as_a_generic_field_change() -> None:
    """Counters have their own change with a delta; listing them twice is noise."""
    result = events.diff(
        _snap(repos=[_repo("o/a", stars=1)]), _snap(repos=[_repo("o/a", stars=2)])
    )
    assert _kinds(result) == ["stars_changed"]


def test_organisation_membership_changes() -> None:
    result = events.diff(
        _snap(orgs=[OrgData(name="one")]), _snap(orgs=[OrgData(name="one"), OrgData(name="two")])
    )
    assert _one(result, "new_org")["subject"] == "two"

    result = events.diff(
        _snap(orgs=[OrgData(name="one"), OrgData(name="two")]), _snap(orgs=[OrgData(name="one")])
    )
    assert _one(result, "org_removed")["subject"] == "two"


def test_an_organisation_change_does_not_carry_its_repositories() -> None:
    """An organisation with 300 repositories would not fit in an event."""
    org = OrgData(name="two", repos=[_repo("two/a"), _repo("two/b")])
    result = events.diff(_snap(orgs=[OrgData(name="one")]), _snap(orgs=[OrgData(name="one"), org]))
    assert "repos" not in _one(result, "new_org")["new"]


# --- notifications --------------------------------------------------------------------


def test_a_new_notification_is_separated_from_the_update_digest() -> None:
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
    result = events.diff(_snap(), after)

    assert result.updates == []
    assert len(result.notifications) == 1
    change = result.notifications[0]
    assert change["subject"].startswith("test workflow")
    assert change["repository"] == "Bluscream/ptero-cli"
    assert change["new"]["reason"] == "ci_activity"
    assert change["new"]["subject_type"] == "CheckSuite"


def test_an_already_read_notification_is_not_announced() -> None:
    after = _snap(
        notifications=[NotificationData(notification_id="7", title="seen", unread=False)]
    )
    assert events.diff(_snap(), after).notifications == []


# --- guards against reading a failed fetch as a deletion -------------------------------


def test_an_uncollected_resource_is_never_read_as_emptied() -> None:
    """Regression guard: a failed fetch must not look like 580 deletions."""
    before = _snap(repos=[_repo("o/a"), _repo("o/b")])
    after = _snap(repos=[], collected={"packages", "orgs", "notifications"})

    assert not events.diff(before, after)


def test_a_collection_emptying_entirely_is_treated_as_a_failed_fetch() -> None:
    before = _snap(repos=[_repo("o/a"), _repo("o/b")])
    assert not events.diff(before, _snap(repos=[]))


def test_a_repositorys_releases_emptying_is_not_a_mass_deletion() -> None:
    """A rate-limited detail walk empties the nested lists while the repository survives.

    Announcing every release of a repository as deleted because one GraphQL page came back
    short is the same failure as the account-wide guard above, one level down.
    """
    before = _snap(repos=[_repo("o/a", releases=("v1", "v2", "v3"))])
    after = _snap(repos=[_repo("o/a")])

    assert "release_removed" not in _kinds(events.diff(before, after))


def test_a_genuine_release_deletion_is_still_reported() -> None:
    """The guard must not silence a real deletion, only a collection that vanished whole."""
    before = _snap(repos=[_repo("o/a", releases=("v1", "v2"))])
    after = _snap(repos=[_repo("o/a", releases=("v2",))])

    assert _one(events.diff(before, after), "release_removed")["subject"] == "v1"


def test_alerts_emptying_while_the_repository_survives_is_not_a_resolution() -> None:
    before = _snap(repos=[_repo_with_alerts("o/a", tuple(range(1, 69)))])
    after = _snap(repos=[_repo("o/a")])

    assert "security_alerts_resolved" not in _kinds(events.diff(before, after))


def test_a_deleted_repository_reports_itself_not_each_of_its_releases() -> None:
    """One line for the repository, carrying its last known state - not 40 for its refs."""
    gone = _repo("o/a", releases=("v1", "v2"), branches=("main",), tags=("v1",))
    result = events.diff(_snap(repos=[gone, _repo("o/b")]), _snap(repos=[_repo("o/b")]))

    assert _kinds(result) == ["repo_removed"]


# --- downloads ------------------------------------------------------------------------


def test_a_download_does_not_also_count_as_the_release_changing() -> None:
    """Downloads get one batched change; they must not additionally mark every release as
    edited, which would fire per release on every poll."""
    before = _repo("o/a", releases=("v1",))
    before.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 10}]}]
    after = _repo("o/a", releases=("v1",))
    after.releases = [{"tag": "v1", "name": "v1", "assets": [{"name": "x", "downloads": 99}]}]

    result = events.diff(_snap(repos=[before]), _snap(repos=[after]))
    assert _kinds(result) == ["new_downloads"]
    assert _one(result, "new_downloads")["delta"] == 89


def _repo_with_downloads(full_name: str, tag: str, downloads: int) -> RepoData:
    repo = _repo(full_name)
    repo.releases = [
        {"tag": tag, "name": tag, "assets": [{"name": "app.zip", "downloads": downloads}]}
    ]
    return repo


def test_downloads_are_reported_once_per_repository() -> None:
    """Per asset would be unusable — 3,581 of them here — but per repository is exactly one
    notification per thing that moved."""
    before = _snap(
        repos=[_repo_with_downloads("o/a", "v1", 100), _repo_with_downloads("o/b", "v1", 50)]
    )
    after = _snap(
        repos=[_repo_with_downloads("o/a", "v1", 1100), _repo_with_downloads("o/b", "v1", 75)]
    )

    result = _all_of(events.diff(before, after), "new_downloads")
    assert len(result) == 2, "one change per repository that gained downloads"

    by_repo = {c["repository"]: c for c in result}
    assert by_repo["o/a"]["delta"] == 1000
    assert by_repo["o/a"]["new"] == 1100
    assert by_repo["o/a"]["old"] == 100
    assert by_repo["o/a"]["detail"]["assets"] == 1
    assert by_repo["o/a"]["detail"]["breakdown"][0] == {
        "tag": "v1",
        "asset": "app.zip",
        "delta": 1000,
    }
    assert by_repo["o/b"]["delta"] == 25


def test_the_first_download_of_an_asset_is_marked_as_a_first() -> None:
    before = _snap(repos=[_repo_with_downloads("o/a", "v1", 0)])
    after = _snap(repos=[_repo_with_downloads("o/a", "v1", 1)])

    assert _one(events.diff(before, after), "new_downloads")["first"] is True


def test_a_repository_whose_downloads_did_not_move_fires_nothing() -> None:
    before = _snap(
        repos=[_repo_with_downloads("o/a", "v1", 100), _repo_with_downloads("o/b", "v1", 50)]
    )
    after = _snap(
        repos=[_repo_with_downloads("o/a", "v1", 100), _repo_with_downloads("o/b", "v1", 75)]
    )

    fired = [c["repository"] for c in _all_of(events.diff(before, after), "new_downloads")]
    assert fired == ["o/b"]


def test_no_download_change_when_nothing_moved() -> None:
    same = [_repo_with_downloads("o/a", "v1", 100)]
    assert not events.diff(_snap(repos=same), _snap(repos=same))


def test_downloads_only_report_increases() -> None:
    """A falling count means an asset or release was removed, which release_changed covers."""
    before = _snap(repos=[_repo_with_downloads("o/a", "v1", 500)])
    after = _snap(repos=[_repo_with_downloads("o/a", "v1", 400)])

    assert "new_downloads" not in _kinds(events.diff(before, after))


def test_organisation_repositories_report_their_own_downloads() -> None:
    """They are in the snapshot, so a push about downloads should cover them too."""
    org_before = OrgData(name="Org", is_owned=True, repos=[_repo_with_downloads("Org/x", "v1", 10)])
    org_after = OrgData(name="Org", is_owned=True, repos=[_repo_with_downloads("Org/x", "v1", 60)])

    result = _all_of(events.diff(_snap(orgs=[org_before]), _snap(orgs=[org_after])), "new_downloads")
    assert len(result) == 1
    assert result[0]["repository"] == "Org/x"
    assert result[0]["delta"] == 50


# --- security -------------------------------------------------------------------------


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


def test_each_new_security_alert_gets_its_own_change() -> None:
    """A new advisory is something to act on, so it arrives whole rather than as a count."""
    result = events.diff(
        _snap(repos=[_repo_with_alerts("o/a", (1,))]),
        _snap(repos=[_repo_with_alerts("o/a", (1, 2))]),
    )
    change = _one(result, "new_security_alert")
    assert change["repository"] == "o/a"
    assert change["subject"] == "GHSA-2"
    assert change["new"]["cve"] == "CVE-2022-2"
    assert change["new"]["cvss"] == 8.1
    assert change["new"]["package"] == "jsonwebtoken"


def test_resolved_alerts_are_batched_per_repository() -> None:
    """One dependency bump clears dozens at once — 68 on one repository here."""
    before = _snap(repos=[_repo_with_alerts("o/a", tuple(range(1, 69)))])
    after = _snap(repos=[_repo_with_alerts("o/a", (1,))])

    result = events.diff(before, after)
    assert _kinds(result).count("security_alerts_resolved") == 1

    change = _one(result, "security_alerts_resolved")
    assert change["detail"] == {"resolved": 67, "remaining": 1}
    assert change["delta"] == -67


def test_a_repository_with_no_alerts_either_side_is_silent() -> None:
    assert not events.diff(_snap(repos=[_repo("o/a")]), _snap(repos=[_repo("o/a")]))


def test_organisation_repositories_get_the_same_changes_as_your_own() -> None:
    """A star on an organisation repository is the same event as on any other. Whether it
    counts towards the totals is a separate question, decided by the organisation option."""
    before = OrgData(name="Org", is_owned=True, repos=[_repo("Org/x", stars=1)])
    after = OrgData(name="Org", is_owned=True, repos=[_repo("Org/x", stars=2)])

    change = _one(events.diff(_snap(orgs=[before]), _snap(orgs=[after])), "stars_changed")
    assert change["repository"] == "Org/x"
    assert change["delta"] == 1


def test_a_repository_moving_between_the_account_and_an_org_is_not_a_delete() -> None:
    """It is indexed by full_name across both, so a transfer that keeps the name is quiet."""
    repo = _repo("Bluscream/x", stars=3)
    before = _snap(repos=[repo])
    after = _snap(orgs=[OrgData(name="Bluscream", is_owned=True, repos=[repo])])

    fired = _kinds(events.diff(before, after))
    # Gaining the organisation is a real change; the repository moving inside it is not.
    assert "new_org" in fired
    assert "new_repo" not in fired
    assert "repo_removed" not in fired


# --- traffic --------------------------------------------------------------------------


def _repo_with_traffic(
    full_name: str,
    views: dict[str, int] | None = None,
    clones: dict[str, int] | None = None,
    referrers: tuple[str, ...] = (),
) -> RepoData:
    repo = _repo(full_name)
    repo.traffic = {
        "views": {d: {"count": c, "uniques": 1} for d, c in (views or {}).items()},
        "clones": {d: {"count": c, "uniques": 1} for d, c in (clones or {}).items()},
        "referrers": {r: {"count": 3, "uniques": 2, "first_seen": "2026-09-01"} for r in referrers},
        "fetched_at": "2026-09-23T00:00:00+00:00",
    }
    return repo


def test_views_and_clones_are_reported_as_signed_deltas() -> None:
    before = _snap(repos=[_repo_with_traffic("o/a", views={"2026-09-21": 10}, clones={})])
    after = _snap(
        repos=[
            _repo_with_traffic(
                "o/a", views={"2026-09-21": 10, "2026-09-22": 5}, clones={"2026-09-22": 2}
            )
        ]
    )
    result = events.diff(before, after)

    assert _one(result, "new_views")["delta"] == 5
    assert _one(result, "new_views")["thing"] == events.THING_VIEW
    assert _one(result, "new_clones")["delta"] == 2
    assert _one(result, "new_clones")["first"] is True, "the repository's first ever clone"


def test_the_first_sweep_of_a_repository_is_a_baseline_not_news() -> None:
    """One sweep brings back fourteen days; reporting that as a delta announces a fortnight
    of history as if it had just happened."""
    before = _snap(repos=[_repo("o/a")])
    after = _snap(repos=[_repo_with_traffic("o/a", views={"2026-09-10": 400})])

    assert "new_views" not in _kinds(events.diff(before, after))


def test_a_referring_site_never_seen_before_is_its_own_change() -> None:
    before = _snap(repos=[_repo_with_traffic("o/a", views={"2026-09-21": 1}, referrers=("google.com",))])
    after = _snap(
        repos=[
            _repo_with_traffic(
                "o/a", views={"2026-09-21": 1}, referrers=("google.com", "news.ycombinator.com")
            )
        ]
    )
    change = _one(events.diff(before, after), "new_referrer")

    assert change["subject"] == "news.ycombinator.com"
    assert change["thing"] == events.THING_REFERRER
    assert change["first"] is True
    assert change["repository"] == "o/a"


def test_a_familiar_referring_site_is_not_announced_again() -> None:
    repos = [_repo_with_traffic("o/a", views={"2026-09-21": 1}, referrers=("google.com",))]
    assert "new_referrer" not in _kinds(events.diff(_snap(repos=repos), _snap(repos=repos)))


def test_traffic_that_did_not_move_is_silent() -> None:
    repos = [_repo_with_traffic("o/a", views={"2026-09-21": 10}, clones={"2026-09-21": 2})]
    assert not events.diff(_snap(repos=repos), _snap(repos=repos))


# --- chunking -------------------------------------------------------------------------


def _payload_size(batch: list[events.Change]) -> int:
    """What the coordinator will actually put on the bus for one chunk."""
    return len(
        json.dumps(
            {
                "platform": "github",
                "account": "Bluscream",
                "run_id": "2026-09-23T00:00:00+00:00",
                "chunk": 1,
                "chunks": 9,
                "count": len(batch),
                "total": 999,
                "changes": batch,
            },
            default=str,
            separators=(",", ":"),
        )
    )


def test_a_small_poll_is_one_chunk() -> None:
    changes = events.diff(
        _snap(repos=[_repo("o/a", stars=1)]), _snap(repos=[_repo("o/a", stars=2)])
    ).updates
    assert len(events.chunked(changes)) == 1


def test_nothing_produces_no_chunks() -> None:
    assert events.chunked([]) == []


def test_every_chunk_fits_inside_the_recorder_limit() -> None:
    """The reason this module chunks at all."""
    before = _snap(repos=[_repo(f"o/r{i}", stars=1, description="x" * 400) for i in range(400)])
    after = _snap(repos=[_repo(f"o/r{i}", stars=2, description="y" * 400) for i in range(400)])

    changes = events.diff(before, after).updates
    batches = events.chunked(changes)

    assert len(batches) > 1, "this fixture is meant to need more than one chunk"
    assert sum(len(b) for b in batches) == len(changes), "no change may be dropped"
    for batch in batches:
        assert _payload_size(batch) < events.MAX_EVENT_DATA_BYTES


def test_chunks_preserve_order_and_every_change() -> None:
    before = _snap(repos=[_repo(f"o/r{i}", stars=1) for i in range(200)])
    after = _snap(repos=[_repo(f"o/r{i}", stars=2) for i in range(200)])

    changes = events.diff(before, after).updates
    flattened = [c for batch in events.chunked(changes) for c in batch]
    assert flattened == changes


def test_one_oversized_change_is_truncated_rather_than_dropped() -> None:
    """Losing the detail is recoverable from the snapshot; losing the event is not."""
    huge: events.Change = {
        "kind": "new_security_alert",
        "thing": events.THING_SECURITY,
        "subject": "GHSA-1",
        "repository": "o/a",
        "url": "https://github.com/advisories/GHSA-1",
        "new": {"summary": "x" * (events.MAX_CHANGE_BYTES * 2)},
    }
    batches = events.chunked([huge])

    assert len(batches) == 1
    kept = batches[0][0]
    assert kept["subject"] == "GHSA-1"
    assert kept["url"].endswith("GHSA-1")
    assert kept["detail"] == {"truncated": True}
    assert "new" not in kept
    assert _payload_size(batches[0]) < events.MAX_EVENT_DATA_BYTES


# --- a measurement nobody took is not a zero --------------------------------------------


def test_a_repository_the_detail_walk_missed_reports_no_watcher_change() -> None:
    """The bug this guard exists for, exactly as it happened.

    restore() rebuilt the detail resource without watchers, so the field fell back to its
    dataclass default of zero on every reload. The next successful walk then read 0 -> 1
    across 266 repositories at once and called every one of them a first watcher. Half the
    polls read it the other way, 1 -> 0, which is the tell: it was flapping, not changing.
    """
    unwalked = _repo("o/a", watchers=0)
    walked = _repo("o/a", watchers=1, branches=("main",), releases=("v1",))

    assert "watchers_changed" not in _kinds(events.diff(_snap(repos=[unwalked]), _snap(repos=[walked])))
    assert "watchers_changed" not in _kinds(events.diff(_snap(repos=[walked]), _snap(repos=[unwalked])))


def test_a_real_watcher_change_is_still_reported() -> None:
    """The guard must not silence the thing it is guarding."""
    before = _repo("o/a", watchers=3, branches=("main",))
    after = _repo("o/a", watchers=4, branches=("main",))

    assert _one(events.diff(_snap(repos=[before]), _snap(repos=[after])), "watchers_changed")["delta"] == 1


def test_advisories_arriving_with_the_walk_are_not_announced_as_new() -> None:
    """871 advisories appearing at once meant the walk had returned, not that they had."""
    unwalked = _repo("o/a")
    walked = _repo_with_alerts("o/a", tuple(range(1, 50)))
    walked.branches = [{"name": "main", "sha": "s"}]

    assert "new_security_alert" not in _kinds(events.diff(_snap(repos=[unwalked]), _snap(repos=[walked])))


def test_a_genuinely_new_advisory_is_still_announced() -> None:
    before = _repo_with_alerts("o/a", (1,))
    after = _repo_with_alerts("o/a", (1, 2))

    assert _one(events.diff(_snap(repos=[before]), _snap(repos=[after])), "new_security_alert")["subject"] == "GHSA-2"


def test_releases_arriving_with_the_walk_are_not_announced_as_new() -> None:
    unwalked = _repo("o/a")
    walked = _repo("o/a", releases=("v1", "v2"), branches=("main",))

    assert "new_release" not in _kinds(events.diff(_snap(repos=[unwalked]), _snap(repos=[walked])))


def test_traffic_is_compared_even_where_the_detail_walk_has_never_been() -> None:
    """Traffic is a separate resource with its own baseline rule; gating it on the detail
    walk would silence it for every repository that walk has not reached."""
    before = _repo_with_traffic("o/a", views={"2026-09-21": 10})
    after = _repo_with_traffic("o/a", views={"2026-09-21": 10, "2026-09-22": 4})

    assert _one(events.diff(_snap(repos=[before]), _snap(repos=[after])), "new_views")["delta"] == 4
