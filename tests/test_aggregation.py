"""Tests for how organisation repositories feed the account totals."""

from __future__ import annotations

from typing import Any

from dev_cloud import sensor
from dev_cloud.models import DevCloudData, OrgData, ProfileData, RepoData


class _FakeCoordinator:
    """Minimal stand-in: the aggregation helpers only read data and the org option."""

    def __init__(self, data: DevCloudData, *, include_non_owned_orgs: bool) -> None:
        self.data = data
        self.include_non_owned_orgs = include_non_owned_orgs


def _repo(full_name: str, stars: int = 0, forks: int = 0) -> RepoData:
    return RepoData(name=full_name.split("/")[-1], full_name=full_name, url="", stars=stars,
                    forks=forks)


def _data() -> DevCloudData:
    return DevCloudData(
        profile=ProfileData(username="Bluscream"),
        repos=[_repo("Bluscream/own", stars=679, forks=159)],
        orgs=[
            OrgData(name="EpicGames", is_owned=False, repos=[_repo("EpicGames/ue", stars=50000)]),
            OrgData(name="Mine", is_owned=True, repos=[_repo("Mine/mods", stars=12)]),
            OrgData(name="Unknown", is_owned=None, repos=[_repo("Unknown/x", stars=999)]),
        ],
        releases=[
            {"repository": "Bluscream/own", "assets": [{"downloads": 100}]},
            {"repository": "EpicGames/ue", "assets": [{"downloads": 999999}]},
            {"repository": "Mine/mods", "assets": [{"downloads": 50}]},
        ],
    )


def _coordinator(*, include: bool) -> Any:
    return _FakeCoordinator(_data(), include_non_owned_orgs=include)


def test_owned_orgs_always_count() -> None:
    repos = sensor.counted_repos(_coordinator(include=False))
    assert {r.full_name for r in repos} == {"Bluscream/own", "Mine/mods"}


def test_non_owned_orgs_are_excluded_by_default() -> None:
    """Default off, or EpicGames' 50k stars would be reported as the user's."""
    assert sum(r.stars for r in sensor.counted_repos(_coordinator(include=False))) == 691


def test_unknown_ownership_is_treated_as_not_owned() -> None:
    """GitLab and Gitea expose no role, so the conservative reading applies."""
    repos = sensor.counted_repos(_coordinator(include=False))
    assert "Unknown/x" not in {r.full_name for r in repos}


def test_enabling_the_option_includes_every_org() -> None:
    repos = sensor.counted_repos(_coordinator(include=True))
    assert len(repos) == 4
    assert sum(r.stars for r in repos) == 51690


def test_releases_follow_the_same_rule_as_repos() -> None:
    assert len(sensor.counted_releases(_coordinator(include=False))) == 2
    assert len(sensor.counted_releases(_coordinator(include=True))) == 3


def test_releases_of_uncounted_orgs_do_not_reach_the_download_total() -> None:
    releases = sensor.counted_releases(_coordinator(include=False))
    downloads = sum(a["downloads"] for r in releases for a in r["assets"])
    assert downloads == 150


def test_assets_helper_tolerates_a_missing_or_malformed_key() -> None:
    assert sensor._assets({}) == []
    assert sensor._assets({"assets": None}) == []
    assert sensor._assets({"assets": [{"downloads": 1}]}) == [{"downloads": 1}]
