"""Tests for the GitHub GraphQL walk.

These drive `_releases_for` against a stub caller rather than asserting on helpers around
it. Watchers and security alerts were added to the query, read by the provider, documented
and shipped — while the walk in between never put them in its output, because a
search-and-replace silently matched nothing. Nothing tested the walk, so nothing caught it.
"""

from __future__ import annotations

from typing import Any

from dev_cloud.providers.github import releases as walk
from dev_cloud.providers.github.queries import USER_RELEASES_QUERY


def _repo_node(name: str = "o/a") -> dict[str, Any]:
    return {
        "nameWithOwner": name,
        "watchers": {"totalCount": 7},
        "vulnerabilityAlerts": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "number": 14,
                    "createdAt": "2022-12-22T23:33:47Z",
                    "securityVulnerability": {
                        "severity": "HIGH",
                        "package": {"name": "jsonwebtoken", "ecosystem": "NPM"},
                        "advisory": {
                            "ghsaId": "GHSA-8cf7-32gw-wr33",
                            "summary": "unrestricted key type ",
                            "permalink": "https://github.com/advisories/GHSA-8cf7-32gw-wr33",
                            "cvss": {"score": 8.1},
                            "identifiers": [
                                {"type": "GHSA", "value": "GHSA-8cf7-32gw-wr33"},
                                {"type": "CVE", "value": "CVE-2022-23539"},
                            ],
                        },
                    },
                }
            ],
        },
        "branches": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "main", "target": {"oid": "abc"}}]},
        "tags": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "v1", "target": {"oid": "abc"}}]},
        "releases": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "R_1",
                    "name": "v1",
                    "tagName": "v1",
                    "publishedAt": "2026-01-01T00:00:00Z",
                    "url": "https://github.com/o/a/releases/tag/v1",
                    "releaseAssets": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"name": "app.zip", "downloadCount": 508}],
                    },
                }
            ],
        },
    }


def _caller(nodes: list[dict[str, Any]]):
    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "user": {
                "repositories": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}
            }
        }

    return graphql


async def test_the_walk_returns_every_field_the_provider_reads() -> None:
    """The regression this file exists for: a key missing here is silently absent
    everywhere downstream, because the provider only reads keys that are present."""
    detail = await walk._releases_for(_caller([_repo_node()]), USER_RELEASES_QUERY, "x", "user")

    assert set(detail) == {"o/a"}
    assert set(detail["o/a"]) == {"releases", "branches", "tags", "watchers", "security_alerts"}


async def test_the_walk_carries_the_real_watcher_count() -> None:
    detail = await walk._releases_for(_caller([_repo_node()]), USER_RELEASES_QUERY, "x", "user")
    assert detail["o/a"]["watchers"] == 7


async def test_the_walk_flattens_alerts_into_usable_fields() -> None:
    detail = await walk._releases_for(_caller([_repo_node()]), USER_RELEASES_QUERY, "x", "user")
    alert = detail["o/a"]["security_alerts"][0]

    assert alert["number"] == 14
    assert alert["severity"] == "HIGH"
    assert alert["package"] == "jsonwebtoken"
    assert alert["ecosystem"] == "NPM"
    assert alert["ghsa"] == "GHSA-8cf7-32gw-wr33"
    assert alert["cve"] == "CVE-2022-23539"
    assert alert["cvss"] == 8.1
    assert alert["summary"] == "unrestricted key type"
    assert alert["url"].endswith("GHSA-8cf7-32gw-wr33")


async def test_the_walk_flattens_releases_assets_and_refs() -> None:
    detail = await walk._releases_for(_caller([_repo_node()]), USER_RELEASES_QUERY, "x", "user")
    repo = detail["o/a"]

    assert repo["releases"][0]["tag"] == "v1"
    assert repo["releases"][0]["assets"] == [{"name": "app.zip", "downloads": 508}]
    assert "repository" not in repo["releases"][0]
    assert repo["branches"] == [{"name": "main", "sha": "abc"}]
    assert repo["tags"] == [{"name": "v1", "sha": "abc"}]


async def test_a_repository_with_nothing_still_reports_every_key() -> None:
    bare = {"nameWithOwner": "o/b"}
    detail = await walk._releases_for(_caller([bare]), USER_RELEASES_QUERY, "x", "user")

    assert set(detail["o/b"]) == {"releases", "branches", "tags", "watchers", "security_alerts"}
    assert detail["o/b"]["security_alerts"] == []
    assert detail["o/b"]["watchers"] is None
