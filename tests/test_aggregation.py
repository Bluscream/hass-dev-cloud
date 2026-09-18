"""Tests for how organisation repositories feed the account totals."""

from __future__ import annotations

from typing import Any

from dev_cloud import aggregation
from dev_cloud.models import DevCloudData, OrgData, ProfileData, RepoData


class _FakeCoordinator:
    """Minimal stand-in: the aggregation helpers only read data and the org option."""

    def __init__(self, data: DevCloudData, *, include_non_owned_orgs: bool) -> None:
        self.data = data
        self.include_non_owned_orgs = include_non_owned_orgs


def _repo(full_name: str, stars: int = 0, forks: int = 0, downloads: int = 0) -> RepoData:
    repo = RepoData(
        name=full_name.split("/")[-1], full_name=full_name, url="", stars=stars, forks=forks
    )
    if downloads:
        repo.releases = [{"tag": "v1", "assets": [{"downloads": downloads}]}]
    return repo


def _data() -> DevCloudData:
    return DevCloudData(
        profile=ProfileData(username="Bluscream"),
        repos=[_repo("Bluscream/own", stars=679, forks=159, downloads=100)],
        orgs=[
            OrgData(
                name="EpicGames",
                is_owned=False,
                repos=[_repo("EpicGames/ue", stars=50000, downloads=999999)],
            ),
            OrgData(
                name="Mine", is_owned=True, repos=[_repo("Mine/mods", stars=12, downloads=50)]
            ),
            OrgData(name="Unknown", is_owned=None, repos=[_repo("Unknown/x", stars=999)]),
        ],
        collected={"repos", "orgs", "repo_detail"},
    )


def _coordinator(*, include: bool) -> Any:
    return _FakeCoordinator(_data(), include_non_owned_orgs=include)


def test_owned_orgs_always_count() -> None:
    repos = aggregation.counted_repos(_coordinator(include=False))
    assert {r.full_name for r in repos} == {"Bluscream/own", "Mine/mods"}


def test_non_owned_orgs_are_excluded_by_default() -> None:
    """Default off, or EpicGames' 50k stars would be reported as the user's."""
    assert sum(r.stars for r in aggregation.counted_repos(_coordinator(include=False))) == 691


def test_unknown_ownership_is_treated_as_not_owned() -> None:
    """GitLab and Gitea expose no role, so the conservative reading applies."""
    repos = aggregation.counted_repos(_coordinator(include=False))
    assert "Unknown/x" not in {r.full_name for r in repos}


def test_enabling_the_option_includes_every_org() -> None:
    repos = aggregation.counted_repos(_coordinator(include=True))
    assert len(repos) == 4
    assert sum(r.stars for r in repos) == 51690


def test_releases_follow_the_same_rule_as_repos() -> None:
    assert len(aggregation.counted_releases(_coordinator(include=False))) == 2
    assert len(aggregation.counted_releases(_coordinator(include=True))) == 3


def test_releases_of_uncounted_orgs_do_not_reach_the_download_total() -> None:
    releases = aggregation.counted_releases(_coordinator(include=False))
    downloads = sum(a["downloads"] for r in releases for a in r["assets"])
    assert downloads == 150


def test_assets_helper_tolerates_a_missing_or_malformed_key() -> None:
    assert aggregation.assets({}) == []
    assert aggregation.assets({"assets": None}) == []
    assert aggregation.assets({"assets": [{"downloads": 1}]}) == [{"downloads": 1}]


def test_collection_total_prefers_the_list_when_it_was_enumerated() -> None:
    coordinator = _coordinator(include=False)
    assert aggregation.collection_total(coordinator, "repos") == 1


def test_collection_total_falls_back_to_the_reported_total() -> None:
    """Summary mode does not enumerate, so the API's own figure stands in."""
    data = DevCloudData(profile=ProfileData(username="x"), totals={"repos": 580, "pastes": 191})
    coordinator = _FakeCoordinator(data, include_non_owned_orgs=False)

    assert aggregation.collection_total(coordinator, "repos") == 580
    assert aggregation.collection_total(coordinator, "pastes") == 191


def test_collection_total_is_none_when_neither_exists() -> None:
    """Which is what keeps the sensor unregistered rather than reporting a false zero."""
    data = DevCloudData(profile=ProfileData(username="x"))
    coordinator = _FakeCoordinator(data, include_non_owned_orgs=False)

    assert aggregation.collection_total(coordinator, "repos") is None


def test_a_reported_total_never_coexists_with_the_list_it_describes() -> None:
    """The totals map exists precisely because there is no list to measure; carrying both
    would reintroduce the duplication the snapshot format forbids."""
    from dev_cloud import storage

    data = DevCloudData(
        profile=ProfileData(username="x"),
        repos=[_repo("o/r")],
        totals={"repos": 580},
    )
    payload = storage.build_snapshot("github", "x", data)

    assert not (payload.get("repos") and payload.get("totals", {}).get("repos")), (
        "a total and its list must not both be published"
    )


def _repo_with_issues(full_name: str, issues: int, prs: int) -> RepoData:
    repo = _repo(full_name)
    repo.issues = [{"number": i} for i in range(issues)]
    repo.prs = [{"number": i} for i in range(prs)]
    return repo


def test_issue_and_pr_totals_are_summed_from_the_repositories() -> None:
    data = DevCloudData(
        profile=ProfileData(username="x"),
        repos=[_repo_with_issues("o/a", 3, 2), _repo_with_issues("o/b", 1, 0)],
        collected={"repos", "issues", "prs"},
    )
    coordinator = _FakeCoordinator(data, include_non_owned_orgs=False)

    assert aggregation.counted_issues(coordinator) == 4
    assert aggregation.counted_prs(coordinator) == 2


def test_issues_of_uncounted_organisations_are_excluded() -> None:
    """Same rule as every other total: non-owned orgs only count when the option says so."""
    org = OrgData(name="Other", is_owned=False, repos=[_repo_with_issues("Other/x", 99, 99)])
    data = DevCloudData(
        profile=ProfileData(username="x"),
        repos=[_repo_with_issues("o/a", 3, 2)],
        orgs=[org],
        collected={"repos", "orgs", "issues", "prs"},
    )

    assert aggregation.counted_issues(_FakeCoordinator(data, include_non_owned_orgs=False)) == 3
    assert aggregation.counted_issues(_FakeCoordinator(data, include_non_owned_orgs=True)) == 102


def test_platforms_without_issue_lists_fall_back_to_the_reported_count() -> None:
    repo = _repo("o/a")
    repo.open_issues = 7
    # "repos" collected but not "issues": the platform never enumerates them.
    data = DevCloudData(profile=ProfileData(username="x"), repos=[repo], collected={"repos"})

    assert aggregation.counted_issues(_FakeCoordinator(data, include_non_owned_orgs=False)) == 7
    assert aggregation.counted_prs(_FakeCoordinator(data, include_non_owned_orgs=False)) is None


def test_repo_issue_count_is_dropped_once_both_lists_are_present() -> None:
    """open_issues_count counts issues and PRs, so with both listed it is derivable."""
    from dev_cloud import storage

    repo = _repo_with_issues("o/a", 3, 2)
    repo.open_issues = 5
    payload = storage.build_snapshot(
        "github", "x", DevCloudData(profile=ProfileData(username="x"), repos=[repo])
    )

    assert "open_issues" not in payload["repos"][0]
    assert len(payload["repos"][0]["issues"]) == 3
    assert len(payload["repos"][0]["prs"]) == 2


def test_releases_are_flattened_from_the_repositories_that_count() -> None:
    assert len(aggregation.counted_releases(_coordinator(include=False))) == 2
    assert len(aggregation.counted_releases(_coordinator(include=True))) == 3


def test_a_nested_release_does_not_name_its_own_repository() -> None:
    """It hangs off the repository, so repeating the name inside it is a duplicate."""
    from dev_cloud import storage

    repo = _repo("o/a", downloads=5)
    payload = storage.build_snapshot(
        "github", "x", DevCloudData(profile=ProfileData(username="x"), repos=[repo])
    )
    assert "repository" not in payload["repos"][0]["releases"][0]


def test_zero_is_a_reading_but_uncollected_is_an_absence() -> None:
    """The distinction the whole registration rule rests on: an empty collection that was
    fetched still answers the question, one that was never fetched does not."""
    fetched_empty = DevCloudData(
        profile=ProfileData(username="x"), repos=[], collected={"repos"}
    )
    never_fetched = DevCloudData(profile=ProfileData(username="x"), repos=[])

    assert aggregation.collection_total(
        _FakeCoordinator(fetched_empty, include_non_owned_orgs=False), "repos"
    ) == 0
    assert aggregation.collection_total(
        _FakeCoordinator(never_fetched, include_non_owned_orgs=False), "repos"
    ) is None


def test_issue_total_is_zero_when_collected_and_empty() -> None:
    """Regression: zero open issues used to remove the sensor rather than report zero."""
    data = DevCloudData(
        profile=ProfileData(username="x"),
        repos=[_repo("o/a")],
        collected={"repos", "issues", "prs"},
    )
    coordinator = _FakeCoordinator(data, include_non_owned_orgs=False)

    assert aggregation.counted_issues(coordinator) == 0
    assert aggregation.counted_prs(coordinator) == 0


def test_totals_are_computed_in_a_single_pass() -> None:
    """Sensor properties are read far more often than the data changes, so every aggregate
    comes from one walk per update rather than one walk per property access."""
    coordinator = _coordinator(include=False)
    totals = aggregation.compute_totals(coordinator)

    assert totals.repositories == 1
    assert totals.counted_repositories == 2  # own + the owned org
    assert totals.stars == 691
    assert totals.releases == 2
    assert totals.downloads == 150


def test_totals_honour_the_organisation_option() -> None:
    included = aggregation.compute_totals(_coordinator(include=True))
    excluded = aggregation.compute_totals(_coordinator(include=False))

    assert included.stars == 51690
    assert excluded.stars == 691
    assert included.releases > excluded.releases


def test_every_total_the_sensors_read_exists_on_the_dataclass() -> None:
    """A typo in a sensor's field name would otherwise surface as an AttributeError at
    runtime, on a property Home Assistant calls constantly."""
    import ast
    import dataclasses
    from pathlib import Path

    from dev_cloud import sensor

    names = {f.name for f in dataclasses.fields(aggregation.Totals)}
    tree = ast.parse(Path(sensor.__file__).read_text(encoding="utf-8"))

    requested = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_total"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert requested, "expected sensors to read totals"
    assert requested <= names, f"unknown totals: {requested - names}"


def test_security_alerts_are_counted_and_broken_down_by_severity() -> None:
    repo = _repo("o/a")
    repo.security_alerts = [
        {"number": 1, "severity": "HIGH", "cvss": 8.1},
        {"number": 2, "severity": "MODERATE", "cvss": 5.0},
        {"number": 3, "severity": "HIGH", "cvss": 9.8},
    ]
    data = DevCloudData(profile=ProfileData(username="x"), repos=[repo], collected={"repos"})
    totals = aggregation.compute_totals(_FakeCoordinator(data, include_non_owned_orgs=False))

    assert totals.security_alerts == 3
    assert totals.security_alerts_by_severity == {"high": 2, "moderate": 1}
    assert totals.repositories_with_alerts == 1
    assert totals.highest_cvss == 9.8


def test_alerts_in_uncounted_organisations_are_excluded() -> None:
    """Same rule as every other total."""
    other = _repo("Other/x")
    other.security_alerts = [{"number": 1, "severity": "CRITICAL", "cvss": 10.0}]
    org = OrgData(name="Other", is_owned=False, repos=[other])
    data = DevCloudData(
        profile=ProfileData(username="x"), repos=[], orgs=[org], collected={"repos", "orgs"}
    )

    assert aggregation.compute_totals(
        _FakeCoordinator(data, include_non_owned_orgs=False)
    ).security_alerts == 0
    assert aggregation.compute_totals(
        _FakeCoordinator(data, include_non_owned_orgs=True)
    ).security_alerts == 1
