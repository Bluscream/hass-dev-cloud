"""Release collection for the GitHub provider.

Releases are the most expensive thing this integration fetches: three nested GraphQL
connections — repositories, releases per repository, assets per release — each paginated to
completion, because every release, asset and download total the sensors report is derived by
measuring these lists rather than reading a count stored beside them.

Split from the provider because the cursor bookkeeping is self-contained; the functions take
the provider's GraphQL caller rather than the provider itself, so there is nothing to mock
but one callable.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ...const import RUNNING_JOBS_CONCURRENCY
from ...models import OrgData
from ..base import MAX_PAGES, async_map_limited
from .queries import (
    ALERTS_QUERY,
    ASSETS_QUERY,
    GRAPHQL_NESTED_PAGE_SIZE,
    GRAPHQL_PAGE_SIZE,
    ORG_RELEASES_QUERY,
    REFS_QUERY,
    REPO_RELEASES_QUERY,
    USER_RELEASES_QUERY,
)

Item = dict[str, Any]

_LOGGER = logging.getLogger(__name__)

#: Fetches one REST page of a repository's releases. The fallback needs nothing else.
RestPager = Callable[[str, dict[str, Any] | None], Awaitable[list[Any]]]

#: Runs a GraphQL document with variables and returns the unwrapped `data` envelope.
type GraphQLCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _release_assets(
    graphql: GraphQLCaller, release_id: str, after: str | None
) -> list[dict[str, Any]]:
    """Page through a single release's assets beyond the first page."""
    assets: list[dict[str, Any]] = []
    cursor: str | None = after

    for _ in range(MAX_PAGES):
        data = await graphql(
            ASSETS_QUERY,
            {"id": release_id, "cursor": cursor, "size": GRAPHQL_NESTED_PAGE_SIZE},
        )
        conn = ((data.get("node") or {}).get("releaseAssets")) or {}
        assets.extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    return assets


async def _repo_releases(
    graphql: GraphQLCaller, name_with_owner: str, after: str | None
) -> list[dict[str, Any]]:
    """Page through one repository's releases beyond the first page."""
    owner, _, name = name_with_owner.partition("/")
    releases: list[dict[str, Any]] = []
    cursor: str | None = after

    for _ in range(MAX_PAGES):
        data = await graphql(
            REPO_RELEASES_QUERY,
            {
                "owner": owner,
                "name": name,
                "cursor": cursor,
                "size": GRAPHQL_NESTED_PAGE_SIZE,
                "nested": GRAPHQL_NESTED_PAGE_SIZE,
            },
        )
        conn = ((data.get("repository") or {}).get("releases")) or {}
        releases.extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    return releases


async def _build_release(
    graphql: GraphQLCaller, name_with_owner: str, release: dict[str, Any]
) -> dict[str, Any]:
    """Flatten one GraphQL release node, completing its asset list if truncated."""
    assets_conn = release.get("releaseAssets") or {}
    assets = list(assets_conn.get("nodes") or [])
    page = assets_conn.get("pageInfo") or {}
    if page.get("hasNextPage") and release.get("id"):
        assets.extend(await _release_assets(graphql, release["id"], page.get("endCursor")))

    return {
        "name": release.get("name") or release.get("tagName"),
        "tag": release.get("tagName"),
        "published_at": release.get("publishedAt"),
        "url": release.get("url"),
        # Asset and download totals are derived from this list, never stored beside it.
        "assets": [{"name": a.get("name"), "downloads": a.get("downloadCount", 0)} for a in assets],
    }


#: Per-repository detail keyed by "owner/name": releases, branches, tags and the watcher
#: count. Values are heterogeneous because the watcher count is a number, not a list.
type RepoDetail = dict[str, dict[str, Any]]


async def async_fetch_all_releases(graphql: GraphQLCaller, login: str) -> RepoDetail:
    """Releases, branches and tags for every repository owned by the account."""
    return await _releases_for(graphql, USER_RELEASES_QUERY, login, "user")


async def async_fetch_org_releases(graphql: GraphQLCaller, orgs: list[OrgData]) -> RepoDetail:
    """The same detail across the given organisations' repositories."""

    async def _fetch(org: OrgData) -> RepoDetail:
        return await _releases_for(graphql, ORG_RELEASES_QUERY, org.name, "organization")

    results = await async_map_limited(orgs, _fetch, RUNNING_JOBS_CONCURRENCY)

    detail: RepoDetail = {}
    for result in results:
        if isinstance(result, BaseException):
            _LOGGER.debug("Org release listing failed: %s", result)
            continue
        detail.update(result)
    return detail


async def _refs(
    graphql: GraphQLCaller, name_with_owner: str, prefix: str, connection: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flatten a refs connection, completing it if the first page was not the only one."""
    owner, _, name = name_with_owner.partition("/")
    refs = [
        {"name": node.get("name"), "sha": (node.get("target") or {}).get("oid")}
        for node in connection.get("nodes") or []
    ]

    page = connection.get("pageInfo") or {}
    cursor = page.get("endCursor") if page.get("hasNextPage") else None
    for _ in range(MAX_PAGES):
        if not cursor:
            break
        data = await graphql(
            REFS_QUERY, {"owner": owner, "name": name, "prefix": prefix, "cursor": cursor}
        )
        conn = ((data.get("repository") or {}).get("refs")) or {}
        refs.extend(
            {"name": node.get("name"), "sha": (node.get("target") or {}).get("oid")}
            for node in conn.get("nodes") or []
        )
        page = conn.get("pageInfo") or {}
        cursor = page.get("endCursor") if page.get("hasNextPage") else None

    return refs


def _alert(node: Item) -> dict[str, Any]:
    """Flatten one alert into the fields a notification or dashboard actually uses."""
    vuln = node.get("securityVulnerability") or {}
    advisory = vuln.get("advisory") or {}
    identifiers = {i.get("type"): i.get("value") for i in advisory.get("identifiers") or []}
    return {
        "number": node.get("number"),
        "severity": vuln.get("severity"),
        "package": (vuln.get("package") or {}).get("name"),
        "ecosystem": (vuln.get("package") or {}).get("ecosystem"),
        "ghsa": advisory.get("ghsaId"),
        "cve": identifiers.get("CVE"),
        "cvss": (advisory.get("cvss") or {}).get("score"),
        "summary": advisory.get("summary", "").strip() or None,
        "url": advisory.get("permalink"),
        "created_at": node.get("createdAt"),
    }


async def _alerts(
    graphql: GraphQLCaller, name_with_owner: str, connection: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flatten an alerts connection, completing it when one page was not enough."""
    owner, _, name = name_with_owner.partition("/")
    alerts = [_alert(n) for n in connection.get("nodes") or []]

    page = connection.get("pageInfo") or {}
    cursor = page.get("endCursor") if page.get("hasNextPage") else None
    for _ in range(MAX_PAGES):
        if not cursor:
            break
        data = await graphql(ALERTS_QUERY, {"owner": owner, "name": name, "cursor": cursor})
        conn = ((data.get("repository") or {}).get("vulnerabilityAlerts")) or {}
        alerts.extend(_alert(n) for n in conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        cursor = page.get("endCursor") if page.get("hasNextPage") else None

    return alerts


async def _releases_for(
    graphql: GraphQLCaller, query: str, login: str, root_key: str
) -> RepoDetail:
    """Every release of every repository under one user or organisation.

    Every connection is paginated to completion — repositories, releases per repository,
    assets per release, and the branch and tag refs — so each list is authoritative and the
    totals can be summed from them rather than stored beside them.
    """
    detail: RepoDetail = {}
    cursor: str | None = None

    for _ in range(MAX_PAGES):
        data = await graphql(
            query,
            {
                "login": login,
                "cursor": cursor,
                "size": GRAPHQL_PAGE_SIZE,
                "nested": GRAPHQL_NESTED_PAGE_SIZE,
            },
        )
        repos_conn = ((data.get(root_key) or {}).get("repositories")) or {}

        for repo in repos_conn.get("nodes") or []:
            name_with_owner = repo.get("nameWithOwner")
            rel_conn = repo.get("releases") or {}
            nodes = list(rel_conn.get("nodes") or [])

            rel_page = rel_conn.get("pageInfo") or {}
            if rel_page.get("hasNextPage"):
                nodes.extend(
                    await _repo_releases(graphql, name_with_owner, rel_page.get("endCursor"))
                )

            detail[name_with_owner] = {
                "releases": [
                    await _build_release(graphql, name_with_owner, node) for node in nodes
                ],
                "branches": await _refs(
                    graphql, name_with_owner, "refs/heads/", repo.get("branches") or {}
                ),
                "tags": await _refs(graphql, name_with_owner, "refs/tags/", repo.get("tags") or {}),
            }

        repo_page = repos_conn.get("pageInfo") or {}
        if not repo_page.get("hasNextPage"):
            break
        cursor = repo_page.get("endCursor")

    return detail


def _rest_release(name_with_owner: str, release: dict[str, Any]) -> dict[str, Any]:
    """Map a REST release payload into the same shape the GraphQL walk produces."""
    assets = release.get("assets") or []
    return {
        "name": release.get("name") or release.get("tag_name"),
        "tag": release.get("tag_name"),
        "published_at": release.get("published_at"),
        "url": release.get("html_url"),
        "assets": [
            {"name": a.get("name"), "downloads": a.get("download_count", 0)} for a in assets
        ],
    }


async def async_fetch_releases_via_rest(
    pager: RestPager, repos: Sequence[str], concurrency: int
) -> RepoDetail:
    """Collect releases over REST, one paginated call per repository.

    The fallback for when GraphQL is unavailable — its budget is spent, or the token cannot
    use it. REST bills per request rather than in points, so this trades a much larger
    request count for not touching the GraphQL allowance at all. That makes it markedly more
    expensive per release than the GraphQL walk, which is why it is a fallback and not the
    default path.
    """

    async def _fetch(name_with_owner: str) -> tuple[str, list[dict[str, Any]]]:
        items = await pager(f"/repos/{name_with_owner}/releases", None)
        return name_with_owner, [
            _rest_release(name_with_owner, r) for r in items if isinstance(r, dict)
        ]

    results = await async_map_limited(list(repos), _fetch, concurrency)

    detail: RepoDetail = {}
    for result in results:
        if isinstance(result, BaseException):
            _LOGGER.debug("REST release listing failed: %s", result)
            continue
        name, releases = result
        # Branches and tags are absent here: over REST each would be another request per
        # repository, which is the cost this fallback exists to avoid.
        detail[name] = {"releases": releases}
    return detail
