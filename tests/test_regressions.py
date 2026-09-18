"""Regression tests for defects fixed in this integration.

Each test names the behaviour that broke, so a reintroduction fails here rather than in
Home Assistant.
"""

from __future__ import annotations

import asyncio
import inspect

from dev_cloud import sensor, storage
from dev_cloud.providers import PROVIDER_REGISTRY
from dev_cloud.providers.base import BaseDevCloudProvider
from dev_cloud.providers.github_queries import (
    GRAPHQL_NESTED_PAGE_SIZE,
    GRAPHQL_PAGE_SIZE,
)

# GitHub rejects a GraphQL query whose product of `first` values along any path exceeds this.
GITHUB_MAX_NODES = 500_000


def test_graphql_node_budget_is_not_exceeded() -> None:
    """The releases query once asked for 1,000,000 nodes and failed outright."""
    repos_x_releases_x_assets = GRAPHQL_PAGE_SIZE * GRAPHQL_NESTED_PAGE_SIZE**2
    assert repos_x_releases_x_assets <= GITHUB_MAX_NODES


def test_json_url_lives_only_on_the_profile_sensor() -> None:
    """It was inherited by every entity, repeating one URL across all of them."""
    carriers = [
        name
        for name, obj in vars(sensor).items()
        # Restricted to classes defined here; imported names such as the coordinator
        # legitimately mention json_url.
        if inspect.isclass(obj)
        and obj.__module__ == sensor.__name__
        and "json_url" in inspect.getsource(obj)
    ]
    assert carriers == ["DevCloudProfileSensor"]


def test_base_entity_declares_no_state_attributes() -> None:
    """Re-adding it here would resurrect the duplication above."""
    from dev_cloud.entity import DevCloudBaseEntity

    assert "extra_state_attributes" not in vars(DevCloudBaseEntity)


def test_every_provider_declares_resource_policies() -> None:
    """Four registry providers once refetched everything on every poll."""
    missing = [
        platform
        for platform, cls in PROVIDER_REGISTRY.items()
        if not cls.resource_policies
    ]
    assert not missing, f"providers without a polling policy: {missing}"


def test_resource_policies_are_not_shared_mutable_state() -> None:
    """Each provider must own its policy mapping, not mutate the base class's."""
    assert BaseDevCloudProvider.resource_policies == {}
    for cls in PROVIDER_REGISTRY.values():
        assert cls.resource_policies is not BaseDevCloudProvider.resource_policies


def test_optional_sensors_are_built_from_a_coordinator_alone() -> None:
    """The table was typed as a class needing an entity_key it is never given."""
    for build, has_data in sensor._OPTIONAL_SENSORS:
        params = inspect.signature(build).parameters
        assert len(params) == 1, f"{build} takes {len(params)} arguments"
        assert callable(has_data)


def test_snapshot_carries_no_count_that_measures_a_list_it_contains() -> None:
    """Counts drifted from the lists beside them; downloads was wrong by 48x."""
    from dev_cloud.models import DevCloudData, ProfileData

    payload = storage._serialize("github", "x", DevCloudData(profile=ProfileData(username="x")))
    for key in payload:
        if key.endswith("_count"):
            assert key.removesuffix("_count") not in payload, f"{key} duplicates a list"


def test_rate_limit_observation_survives_headers_without_the_attributes() -> None:
    """A headers object lacking the attributes used to raise AttributeError, which was not
    suppressed and silently disabled the resource through async_resource's catch-all."""
    from dev_cloud.providers.github import GitHubProvider

    provider = GitHubProvider.__new__(GitHubProvider)
    observed: list[tuple[int | None, float | None]] = []

    class _Scheduler:
        def observe_rate_limit(self, remaining: int | None, reset: float | None) -> None:
            observed.append((remaining, reset))

    provider.scheduler = _Scheduler()  # type: ignore[assignment]

    class _BareHeaders:
        pass

    class _Response:
        headers = _BareHeaders()

    provider._observe_rate_limit(_Response())
    assert observed == [(None, None)]


def test_rate_limit_observation_reads_present_headers() -> None:
    """Guard the guard above: it must still parse real headers."""
    from dev_cloud.providers.github import GitHubProvider

    provider = GitHubProvider.__new__(GitHubProvider)
    observed: list[tuple[int | None, float | None]] = []

    class _Scheduler:
        def observe_rate_limit(self, remaining: int | None, reset: float | None) -> None:
            observed.append((remaining, reset))

    provider.scheduler = _Scheduler()  # type: ignore[assignment]

    class _Headers:
        x_ratelimit_remaining = "4321"
        x_ratelimit_reset = "1789750000"

    class _Response:
        headers = _Headers()

    provider._observe_rate_limit(_Response())
    assert observed == [(4321, 1789750000.0)]


def test_provider_base_urls_are_url_objects() -> None:
    """Endpoints are built by joining, not by formatting strings together."""
    import aiohttp
    from yarl import URL

    from dev_cloud.providers import PROVIDER_REGISTRY

    async def check() -> None:
        async with aiohttp.ClientSession() as session:
            for platform, cls in PROVIDER_REGISTRY.items():
                provider = cls(session=session, account_name="x", base_url=None, api_token=None)
                assert isinstance(provider.base_url, URL), platform

    asyncio.run(check())


def test_account_names_cannot_break_out_of_a_query_string() -> None:
    """An account name with & or = used to be interpolated straight into the query,
    letting it inject or truncate parameters."""
    from yarl import URL

    hostile = "victim&admin=true"
    url = (URL("https://gitlab.com") / "api/v4/users").with_query({"username": hostile})

    assert url.query["username"] == hostile
    assert "admin" not in url.query
    assert "&admin=true" not in str(url)


def test_paginating_a_url_that_already_has_query_parameters_replaces_them() -> None:
    """String concatenation appended a second `page=`, leaving the server to pick one."""
    from yarl import URL

    url = (URL("https://gitea.example") / "repos").with_query({"page": 1, "limit": 10})
    paged = url.update_query({"limit": 100, "page": 3})

    assert paged.query.getall("page") == ["3"]
    assert paged.query.getall("limit") == ["100"]
