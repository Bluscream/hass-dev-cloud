"""Regression tests for defects fixed in this integration.

Each test names the behaviour that broke, so a reintroduction fails here rather than in
Home Assistant.
"""

from __future__ import annotations

import inspect

from dev_cloud import sensor, storage
from dev_cloud.providers import PROVIDER_REGISTRY
from dev_cloud.providers.base import BaseDevCloudProvider
from dev_cloud.providers.github import (
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
