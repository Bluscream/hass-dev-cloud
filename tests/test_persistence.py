"""Tests for reloading state from the published snapshot."""

from __future__ import annotations

import time
from typing import Any

import aiohttp
import pytest
from dev_cloud import storage
from dev_cloud.models import DevCloudData, OrgData, ProfileData, RepoData
from dev_cloud.providers import get_provider
from dev_cloud.providers.scheduling import ResourcePolicy, ResourceScheduler


@pytest.fixture
async def provider() -> Any:
    async with aiohttp.ClientSession() as session:
        yield get_provider("github", session=session, account_name="Bluscream")


def _repo_with_releases() -> RepoData:
    repo = RepoData(name="r", full_name="Bluscream/r", url="u", stars=5)
    repo.releases = [{"tag": "v1", "assets": [{"downloads": 3}]}]
    repo.branches = [{"name": "main", "sha": "abc"}]
    repo.tags = [{"name": "v1", "sha": "abc"}]
    return repo


def _snapshot() -> dict[str, Any]:
    data = DevCloudData(
        profile=ProfileData(username="Bluscream", followers=296),
        repos=[_repo_with_releases()],
        orgs=[
            OrgData(
                name="Org", is_owned=True, repos=[RepoData(name="x", full_name="Org/x", url="u")]
            )
        ],
    )
    now = time.time()
    data.resources = {
        "profile": {"fetched_at": now, "cost": 1},
        "repos": {"fetched_at": now, "cost": 6},
        "orgs": {"fetched_at": now, "cost": 2},
        "releases": {"fetched_at": now, "cost": 16},
        "retired_resource": {"fetched_at": now, "cost": 1},
    }
    return storage._serialize("github", "Bluscream", data)


async def test_restore_rebuilds_typed_values(provider: Any) -> None:
    provider.restore(_snapshot())

    assert provider._resource_values["repos"][0].full_name == "Bluscream/r"
    assert isinstance(provider._resource_values["repos"][0], RepoData)
    assert provider._resource_values["orgs"][0].is_owned is True
    # Organisation repositories are reconstructed into the derived resource.
    assert provider._resource_values["org_repos"]["Org"][0].full_name == "Org/x"


async def test_restore_means_a_reload_does_not_refetch(provider: Any) -> None:
    """The point of the whole mechanism: redeploying must not re-hammer the API."""
    assert provider.scheduler.should_fetch("repos"), "a fresh provider starts due"

    provider.restore(_snapshot())
    assert not provider.scheduler.should_fetch("repos")
    assert not provider.scheduler.should_fetch("releases")


async def test_restore_ignores_resources_that_no_longer_exist(provider: Any) -> None:
    provider.restore(_snapshot())
    assert "retired_resource" not in provider.scheduler._states


async def test_restore_survives_a_snapshot_from_another_schema(provider: Any) -> None:
    provider.restore({"profile": {"username": "x"}, "unknown": [1, 2], "resources": {}})
    assert provider._resource_values["profile"].username == "x"


def test_a_child_is_due_again_once_its_parent_moves() -> None:
    """Releases are derived from repos: refreshing repos invalidates them."""
    sched = ResourceScheduler(
        policies={
            "repos": ResourcePolicy(authenticated=900, anonymous=900, min_cache=0),
            "releases": ResourcePolicy(
                authenticated=3600, anonymous=3600, depends_on=("repos",), min_cache=0
            ),
        },
        has_token=True,
    )
    sched.record_fetch("repos", cost=6)
    sched.record_fetch("releases", cost=16)
    assert not sched.should_fetch("releases")

    time.sleep(0.01)
    sched.record_fetch("repos", cost=6)
    assert sched.should_fetch("releases"), "parent moved, so the derived data is stale"


def test_the_minimum_cache_time_outranks_a_parent_change() -> None:
    """Otherwise every parent refresh cascades into its children on the next poll."""
    sched = ResourceScheduler(
        policies={
            "repos": ResourcePolicy(authenticated=900, anonymous=900, min_cache=0),
            "releases": ResourcePolicy(
                authenticated=3600, anonymous=3600, depends_on=("repos",), min_cache=1800
            ),
        },
        has_token=True,
    )
    sched.record_fetch("releases", cost=16)
    time.sleep(0.01)
    sched.record_fetch("repos", cost=6)

    assert not sched.should_fetch("releases")


def test_persisted_state_round_trips_through_the_scheduler() -> None:
    policies = {"repos": ResourcePolicy(authenticated=900, anonymous=900)}
    first = ResourceScheduler(policies=dict(policies), has_token=True)
    first.record_fetch("repos", cost=6)

    second = ResourceScheduler(policies=dict(policies), has_token=True)
    second.restore(first.persisted_state())

    assert second.diagnostics()["repos"]["cost"] == 6
    assert not second.should_fetch("repos")


async def test_restore_rebuilds_the_release_and_ref_detail(provider: Any) -> None:
    """Releases, branches and tags live inside their repository, so the grouped resources
    are reconstructed from there rather than stored a second time."""
    provider.restore(_snapshot())

    detail = provider._resource_values["releases"]["Bluscream/r"]
    assert detail["releases"][0]["assets"][0]["downloads"] == 3
    assert detail["branches"][0]["name"] == "main"
    assert detail["tags"][0]["name"] == "v1"
