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
from dev_cloud.providers.github.queries import (
    GRAPHQL_NESTED_PAGE_SIZE,
    GRAPHQL_PAGE_SIZE,
)

# GitHub rejects a GraphQL query whose product of `first` values along any path exceeds this.
GITHUB_MAX_NODES = 500_000


def test_graphql_node_budget_is_not_exceeded() -> None:
    """The releases query once asked for 1,000,000 nodes and failed outright."""
    repos_x_releases_x_assets = GRAPHQL_PAGE_SIZE * GRAPHQL_NESTED_PAGE_SIZE**2
    assert repos_x_releases_x_assets <= GITHUB_MAX_NODES


def test_graphql_query_is_affordable_in_points() -> None:
    """Validity is not the binding constraint: GitHub bills ~1 point per 100 requested
    nodes against a 5000/hour budget. At 100x50x50 a single query cost ~2551 points, so two
    exhausted the hour and the releases fetch died with "API rate limit exceeded"."""
    repos, nested = GRAPHQL_PAGE_SIZE, GRAPHQL_NESTED_PAGE_SIZE
    nodes = repos + repos * nested + repos * nested * nested
    points = nodes / 100

    # Must stay affordable enough to walk the account plus ~45 organisations in one hour.
    assert points <= 50, f"query costs ~{points:.0f} points; 46 walks would need {points*46:.0f}"


def test_release_queries_ask_for_the_rate_limit_budget() -> None:
    """Without this the scheduler measures requests, which is the wrong currency for
    GraphQL and cannot see the budget draining."""
    from dev_cloud.providers.github.queries import ORG_RELEASES_QUERY, USER_RELEASES_QUERY

    for query in (USER_RELEASES_QUERY, ORG_RELEASES_QUERY):
        assert "rateLimit" in query
        assert "remaining" in query and "resetAt" in query


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


def test_no_top_level_count_duplicates_a_list_length() -> None:
    """Every list is fetched to completion, so any count beside one is a second copy."""
    from dev_cloud.models import DevCloudData, ProfileData

    data = DevCloudData(profile=ProfileData(username="x"), sponsors_count=3)
    payload = storage._serialize("github", "x", data)

    lists = {k: v for k, v in payload.items() if isinstance(v, list)}
    for key, value in payload.items():
        if not isinstance(value, int):
            continue
        for list_key, items in lists.items():
            assert not (items and len(items) == value and key.startswith(list_key)), (
                f"{key} duplicates len({list_key})"
            )


def test_docker_hub_does_not_emit_images_as_both_packages_and_repos() -> None:
    """Docker Hub images were emitted twice, as a package and an identical repository."""
    import inspect

    from dev_cloud.providers.dockerhub import DockerHubProvider

    source = inspect.getsource(DockerHubProvider)
    assert "RepoData" not in source, "Docker Hub images are packages, not repositories"


def test_collected_is_derived_from_what_was_fetched_not_declared() -> None:
    """Providers do not announce capabilities: the set is whatever actually returned."""
    import asyncio

    import aiohttp

    from dev_cloud.providers import get_provider

    async def check() -> None:
        async with aiohttp.ClientSession() as session:
            provider = get_provider("github", session=session, account_name="x")
            assert provider.collected_resources() == set()

            provider._resource_values["notifications"] = []
            assert provider.collected_resources() == {"notifications"}

    asyncio.run(check())


def test_an_empty_collection_still_counts_as_collected() -> None:
    """Zero unread notifications must keep its sensor; the old rule dropped it."""
    import asyncio

    import aiohttp

    from dev_cloud.providers import get_provider

    async def check() -> None:
        async with aiohttp.ClientSession() as session:
            # A token, because notifications are unavailable anonymously and would then be
            # correctly skipped rather than collected.
            provider = get_provider(
                "github", session=session, account_name="x", api_token="t"
            )

            async def _empty() -> list[str]:
                return []

            await provider.async_resource("notifications", _empty, [])
            assert "notifications" in provider.collected_resources()

    asyncio.run(check())


def test_a_resource_unavailable_without_a_token_is_not_marked_collected() -> None:
    """The counterpart: never fetched means no sensor, which is the correct absence."""
    import asyncio

    import aiohttp

    from dev_cloud.providers import get_provider

    async def check() -> None:
        async with aiohttp.ClientSession() as session:
            provider = get_provider("github", session=session, account_name="x")

            async def _unreachable() -> list[str]:
                raise AssertionError("must not be called without a token")

            await provider.async_resource("notifications", _unreachable, [])
            assert "notifications" not in provider.collected_resources()

    asyncio.run(check())


def test_user_id_is_published_as_a_string() -> None:
    """A numeric attribute is rendered as a quantity — "3,318,223" — but an id is a label."""
    import inspect

    from dev_cloud import sensor

    source = inspect.getsource(sensor.DevCloudProfileSensor)
    assert '"user_id": str(' in source


def test_scheduling_is_not_a_sensor_attribute() -> None:
    """Diagnostic detail belongs in the snapshot, not in the state machine and recorder."""
    import inspect

    from dev_cloud import sensor

    assert '"scheduling"' not in inspect.getsource(sensor.DevCloudProfileSensor)


def test_only_accumulating_counts_use_the_total_state_class() -> None:
    """TOTAL makes Home Assistant compute a sum, which is meaningless for a count that can
    fall when something is deleted. It once gave Open Pull Requests a `sum` of 3.0."""
    import ast
    from pathlib import Path

    accumulating = {"DevCloudDownloadsSensor", "DevCloudPullsSensor"}
    tree = ast.parse(Path(sensor_source()).read_text(encoding="utf-8"))

    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("DevCloud"):
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "_attr_state_class"
            ):
                value = ast.unparse(stmt.value).split(".")[-1]
                expected = "TOTAL" if node.name in accumulating else "MEASUREMENT"
                assert value == expected, f"{node.name} is {value}, expected {expected}"


def sensor_source() -> str:
    from dev_cloud import sensor

    return str(sensor.__file__)


def test_watchers_are_not_read_from_the_rest_alias() -> None:
    """GitHub's watchers_count is a deprecated alias for stargazers_count, so reading it
    made the Watchers sensor a second copy of Stars — both read 1894."""
    import inspect

    from dev_cloud.providers.github import GitHubProvider

    # The call, not the word: the source carries a comment explaining why it is not read.
    assert '.get("watchers_count")' not in inspect.getsource(GitHubProvider._to_repo)


def test_the_graphql_walk_asks_for_the_real_watcher_count() -> None:
    from dev_cloud.providers.github.queries import ORG_RELEASES_QUERY, USER_RELEASES_QUERY

    for query in (USER_RELEASES_QUERY, ORG_RELEASES_QUERY):
        assert "watchers { totalCount }" in query


def test_npm_does_not_fabricate_an_avatar_from_another_service() -> None:
    """The npm account name was used to build a GitHub avatar URL. They are different
    namespaces: github.com/bluscream1 does not exist, yet the URL still serves an image."""
    import inspect

    from dev_cloud.providers.npm import NPMProvider

    import ast

    # Parsed, not grepped: the source carries a comment naming the URL it no longer builds.
    tree = ast.parse(inspect.getsource(NPMProvider).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "avatar_url":
            raise AssertionError("npm has no avatar of its own to report")
