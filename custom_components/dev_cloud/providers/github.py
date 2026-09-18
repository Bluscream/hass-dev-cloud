"""GitHub provider implementation using aiogithubapi."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, ClassVar

from aiogithubapi import (
    GitHubAPI,
    GitHubAuthenticationException,
    GitHubRatelimitException,
)
from aiohttp import ClientSession

from ..const import PLATFORM_GITHUB, RUNNING_JOBS_CONCURRENCY, RUNNING_JOBS_REPO_LIMIT
from ..models import DevCloudData, NotificationData, OrgData, PasteData, ProfileData, RepoData
from .base import (
    MAX_PAGES,
    BaseDevCloudProvider,
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudRateLimitError,
    async_collect_running_jobs,
    async_map_limited,
)
from .scheduling import PageWalker, ResourcePolicy

_LOGGER = logging.getLogger(__name__)

GITHUB_PAGE_SIZE = 100
# GitHub's search API refuses to page past 1000 results, whatever total_count says.
GITHUB_SEARCH_RESULT_CAP = 1000
# GraphQL connection page sizes. GitHub rejects a query whose *product* of `first` values
# along any path exceeds 500,000 nodes, so the nested connections have to be smaller than the
# outer one: 100 repos x 50 releases x 50 assets = 250,000. Anything past those nested page
# sizes is picked up by the per-repository and per-release follow-up queries.
GRAPHQL_PAGE_SIZE = 100
GRAPHQL_NESTED_PAGE_SIZE = 50

_RELEASE_FIELDS = """
  id
  name
  tagName
  publishedAt
  url
  releaseAssets(first: $nested) {
    pageInfo { hasNextPage endCursor }
    nodes { name downloadCount }
  }
"""

_USER_RELEASES_QUERY = f"""
query($login: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  user(login: $login) {{
    repositories(
      first: $size,
      after: $cursor,
      ownerAffiliations: [OWNER],
      orderBy: {{field: PUSHED_AT, direction: DESC}}
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        releases(first: $nested, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ {_RELEASE_FIELDS} }}
        }}
      }}
    }}
  }}
}}
"""

_REPO_RELEASES_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  repository(owner: $owner, name: $name) {{
    releases(first: $size, after: $cursor, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {_RELEASE_FIELDS} }}
    }}
  }}
}}
"""

_ORG_RELEASES_QUERY = f"""
query($login: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  organization(login: $login) {{
    repositories(first: $size, after: $cursor, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        releases(first: $nested, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ {_RELEASE_FIELDS} }}
        }}
      }}
    }}
  }}
}}
"""

_SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) {
    sponsorshipsAsMaintainer(activeOnly: true) { totalCount }
    sponsorshipsAsSponsor(activeOnly: true) { totalCount }
  }
}
"""

_ASSETS_QUERY = """
query($id: ID!, $cursor: String, $size: Int!) {
  node(id: $id) {
    ... on Release {
      releaseAssets(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { name downloadCount }
      }
    }
  }
}
"""


class GitHubProvider(BaseDevCloudProvider):
    """Provider for GitHub using aiogithubapi."""

    platform_id = PLATFORM_GITHUB
    default_base_url = "https://api.github.com"
    supports_custom_url = False

    # Authenticated REST gets 5000 requests/hour, anonymous only 60 — hence the order of
    # magnitude between the two columns. The scheduler stretches these further if the live
    # budget demands it; it never shortens them.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "profile": ResourcePolicy(authenticated=600, anonymous=1800),
        "repos": ResourcePolicy(authenticated=900, anonymous=3600),
        "orgs": ResourcePolicy(authenticated=3600, anonymous=7200),
        "pastes": ResourcePolicy(authenticated=1800, anonymous=3600),
        # Notifications are the one thing worth polling briskly, and they need a token.
        "notifications": ResourcePolicy(authenticated=300, anonymous=None),
        # Search has its own much tighter quota (30/min authenticated, 10/min anonymous).
        "issues": ResourcePolicy(authenticated=900, anonymous=3600),
        "prs": ResourcePolicy(authenticated=900, anonymous=3600),
        "sponsors": ResourcePolicy(authenticated=3600, anonymous=None),
        # By far the most expensive: three nested paginated GraphQL connections.
        "releases": ResourcePolicy(authenticated=3600, anonymous=None),
        # Fan out over every organisation, so they keep the same slow cadence as releases.
        "org_repos": ResourcePolicy(authenticated=3600, anonymous=7200),
        "org_releases": ResourcePolicy(authenticated=3600, anonymous=None),
        # Must stay fresh to mean anything, but is capped to a handful of repos.
        "running_jobs": ResourcePolicy(authenticated=300, anonymous=600),
    }

    def __init__(
        self,
        session: ClientSession,
        account_name: str,
        base_url: str | None = None,
        api_token: str | None = None,
    ) -> None:
        super().__init__(session, account_name, base_url, api_token)
        self._api = GitHubAPI(
            token=self.api_token,
            session=self.session,
            **{"client_name": "HomeAssistant-DevCloud/1.0"},
        )

    def get_headers(self) -> dict[str, str]:
        headers = super().get_headers()
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if self.api_token:
            headers["Authorization"] = f"token {self.api_token}"
        return headers

    async def async_validate(self) -> bool:
        try:
            user_resp = await self._api.users.get(self.account_name)
            return bool(user_resp.data and user_resp.data.login)
        except GitHubAuthenticationException as err:
            raise DevCloudAuthError("Invalid GitHub credentials") from err
        except GitHubRatelimitException as err:
            raise DevCloudRateLimitError("GitHub rate limit reached") from err
        except Exception as err:
            raise DevCloudNotFoundError(f"GitHub user {self.account_name} not found") from err

    def _observe_rate_limit(self, resp: Any) -> None:
        """Feed GitHub's rate-limit headers to the scheduler so intervals can adapt."""
        headers = getattr(resp, "headers", None)
        if headers is None:
            return

        remaining: int | None = None
        reset: float | None = None
        with contextlib.suppress(TypeError, ValueError):
            if headers.x_ratelimit_remaining is not None:
                remaining = int(headers.x_ratelimit_remaining)
        with contextlib.suppress(TypeError, ValueError):
            if headers.x_ratelimit_reset is not None:
                reset = float(headers.x_ratelimit_reset)

        self.scheduler.observe_rate_limit(remaining, reset)

    async def _async_all_pages(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Page through a REST endpoint until exhausted, via aiogithubapi's generic caller.

        Stops on a short or empty page. Lists in the JSON dump have to be complete because
        every count is derived from them, so nothing here caps the total item count.
        """
        items: list[Any] = []
        page_params = dict(params or {})
        page_params["per_page"] = GITHUB_PAGE_SIZE
        walker = PageWalker(endpoint, GITHUB_PAGE_SIZE)

        for page in range(1, MAX_PAGES + 1):
            page_params["page"] = page
            self.request_count += 1
            resp = await self._api.generic(endpoint, params=page_params)
            self._observe_rate_limit(resp)
            batch = resp.data
            if not isinstance(batch, list) or not walker.accept(batch):
                break
            items.extend(batch)
            if walker.is_last(batch):
                break
        else:
            _LOGGER.warning(
                "Pagination for %s stopped at the %d page safety limit; list may be incomplete",
                endpoint,
                MAX_PAGES,
            )

        return items

    async def _async_search_all(self, query: str) -> list[dict[str, Any]]:
        """Page through the issue/PR search API.

        GitHub caps *search* at 1000 returned results regardless of `total_count`; past that
        the API errors rather than paging, so the loop simply stops there.
        """
        items: list[dict[str, Any]] = []

        for page in range(1, (GITHUB_SEARCH_RESULT_CAP // GITHUB_PAGE_SIZE) + 1):
            self.request_count += 1
            resp = await self._api.generic(
                "/search/issues",
                params={
                    "q": query,
                    "per_page": GITHUB_PAGE_SIZE,
                    "page": page,
                    "sort": "updated",
                },
            )
            payload = resp.data if isinstance(resp.data, dict) else {}
            batch = payload.get("items", [])
            if not batch:
                break

            items.extend(batch)
            if len(batch) < GITHUB_PAGE_SIZE or len(items) >= payload.get("total_count", 0):
                break

        return items

    async def _async_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL query and unwrap the `data` envelope."""
        self.request_count += 1
        resp = await self._api.graphql(query=query, variables=variables)
        self._observe_rate_limit(resp)
        payload = resp.data or {}
        return payload.get("data") or payload or {}

    async def _async_release_assets(self, release_id: str, after: str) -> list[dict[str, Any]]:
        """Page through a single release's assets beyond the first page."""
        assets: list[dict[str, Any]] = []
        cursor: str | None = after

        for _ in range(MAX_PAGES):
            data = await self._async_graphql(
                _ASSETS_QUERY,
                {"id": release_id, "cursor": cursor, "size": GRAPHQL_NESTED_PAGE_SIZE},
            )
            conn = ((data.get("node") or {}).get("releaseAssets")) or {}
            assets.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        return assets

    async def _async_repo_releases(self, name_with_owner: str, after: str) -> list[dict[str, Any]]:
        """Page through one repository's releases beyond the first page."""
        owner, _, name = name_with_owner.partition("/")
        releases: list[dict[str, Any]] = []
        cursor: str | None = after

        for _ in range(MAX_PAGES):
            data = await self._async_graphql(
                _REPO_RELEASES_QUERY,
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

    async def _async_build_release(
        self, name_with_owner: str, release: dict[str, Any]
    ) -> dict[str, Any]:
        """Flatten one GraphQL release node, completing its asset list if truncated."""
        assets_conn = release.get("releaseAssets") or {}
        assets = list(assets_conn.get("nodes") or [])
        page = assets_conn.get("pageInfo") or {}
        if page.get("hasNextPage") and release.get("id"):
            assets.extend(await self._async_release_assets(release["id"], page.get("endCursor")))

        return {
            "repository": name_with_owner,
            "name": release.get("name") or release.get("tagName"),
            "tag": release.get("tagName"),
            "published_at": release.get("publishedAt"),
            "url": release.get("url"),
            # Asset and download totals are derived from this list, never stored beside it.
            "assets": [
                {"name": a.get("name"), "downloads": a.get("downloadCount", 0)} for a in assets
            ],
        }

    async def _async_fetch_all_releases(self) -> list[dict[str, Any]]:
        """Every release of every repository owned by the account."""
        return await self._async_releases_for(_USER_RELEASES_QUERY, self.account_name, "user")

    async def _async_fetch_org_releases(self, orgs: list[OrgData]) -> list[dict[str, Any]]:
        """Every release across the given organisations' repositories."""

        async def _fetch(org: OrgData) -> list[dict[str, Any]]:
            return await self._async_releases_for(_ORG_RELEASES_QUERY, org.name, "organization")

        results = await async_map_limited(orgs, _fetch, RUNNING_JOBS_CONCURRENCY)

        releases: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.debug("Org release listing failed: %s", result)
                continue
            releases.extend(result)
        return releases

    async def _async_releases_for(
        self, query: str, login: str, root_key: str
    ) -> list[dict[str, Any]]:
        """Every release of every repository under one user or organisation.

        Three nested GraphQL connections are paginated: repositories, releases per
        repository, and assets per release. Nothing is capped by item count, so
        `len(releases)` and the per-release asset lists are authoritative.
        """
        releases: list[dict[str, Any]] = []
        cursor: str | None = None

        for _ in range(MAX_PAGES):
            data = await self._async_graphql(
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
                        await self._async_repo_releases(name_with_owner, rel_page.get("endCursor"))
                    )

                for node in nodes:
                    releases.append(await self._async_build_release(name_with_owner, node))

            repo_page = repos_conn.get("pageInfo") or {}
            if not repo_page.get("hasNextPage"):
                break
            cursor = repo_page.get("endCursor")

        return releases

    async def _async_fetch_running_jobs(
        self, repos: list[RepoData]
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """Count in-progress GitHub Actions workflow runs.

        GitHub has no account-wide "running runs" query, so runs are fetched per repository
        over the most recently updated, non-archived repositories (``repos`` arrives sorted
        by ``updated``).
        """
        candidates = [r for r in repos if not r.is_archived and r.full_name][
            :RUNNING_JOBS_REPO_LIMIT
        ]

        async def _fetch(repo: RepoData) -> list[dict[str, Any]]:
            self.request_count += 1
            resp = await self._api.generic(
                f"/repos/{repo.full_name}/actions/runs",
                params={"status": "in_progress", "per_page": 50},
            )
            payload = resp.data if isinstance(resp.data, dict) else {}
            return [
                {
                    "id": run.get("id"),
                    "repository": repo.full_name,
                    "name": run.get("name") or run.get("display_title"),
                    "ref": run.get("head_branch"),
                    "event": run.get("event"),
                    "status": run.get("status"),
                    "url": run.get("html_url"),
                    "created_at": run.get("created_at"),
                    "updated_at": run.get("updated_at"),
                }
                for run in payload.get("workflow_runs", [])
            ]

        return await async_collect_running_jobs(candidates, _fetch, RUNNING_JOBS_CONCURRENCY)

    async def _async_fetch_profile(self) -> ProfileData:
        """Identity and follower counts. Followers have no list, so they stay as counts."""
        self.request_count += 1
        user_resp = await self._api.users.get(self.account_name)
        self._observe_rate_limit(user_resp)
        user = user_resp.data

        return ProfileData(
            username=user.login or self.account_name,
            display_name=user.name,
            user_id=user.id,
            avatar_url=user.avatar_url,
            profile_url=user.html_url,
            bio=user.bio,
            location=user.location,
            company=user.company,
            blog=user.blog,
            email=user.email,
            created_at=user.created_at,
            followers=user.followers,
            following=user.following,
        )

    async def _async_fetch_repos(self) -> list[RepoData]:
        """Every repository owned by the account.

        `affiliation=owner` keeps the authenticated listing to the account's own repos,
        matching the unauthenticated endpoint instead of pulling in every org repo the token
        can see — otherwise `len(repos)` would stop meaning "this account's repositories".
        """
        if self.api_token:
            items = await self._async_all_pages(
                "/user/repos", params={"sort": "updated", "affiliation": "owner"}
            )
        else:
            items = await self._async_all_pages(
                f"/users/{self.account_name}/repos", params={"sort": "updated"}
            )

        return [self._to_repo(r) for r in items]

    @staticmethod
    def _to_repo(r: dict[str, Any]) -> RepoData:
        """Map a REST repository payload. Shared by the account and organisation listings."""
        return RepoData(
            name=r.get("name", ""),
            full_name=r.get("full_name", ""),
            url=r.get("html_url", ""),
            description=r.get("description"),
            is_fork=bool(r.get("fork")),
            is_private=bool(r.get("private")),
            is_archived=bool(r.get("archived")),
            stars=r.get("stargazers_count") or 0,
            forks=r.get("forks_count") or 0,
            watchers=r.get("watchers_count") or 0,
            open_issues=r.get("open_issues_count") or 0,
            primary_language=r.get("language"),
            default_branch=r.get("default_branch"),
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
            pushed_at=r.get("pushed_at"),
        )

    async def _async_fetch_org_roles(self) -> dict[str, bool]:
        """Map org login -> whether this account administers it.

        One paginated call covers every membership, so ownership is known without a
        per-organisation request.
        """
        roles: dict[str, bool] = {}
        if not self.api_token:
            return roles

        for m in await self._async_all_pages("/user/memberships/orgs"):
            login = (m.get("organization") or {}).get("login")
            if login:
                roles[login] = m.get("role") == "admin"
        return roles

    async def _async_fetch_orgs(self) -> list[OrgData]:
        """Org memberships. Authenticated listing includes private/hidden memberships."""
        endpoint = "/user/orgs" if self.api_token else f"/users/{self.account_name}/orgs"

        roles: dict[str, bool] = {}
        with contextlib.suppress(Exception):
            roles = await self._async_fetch_org_roles()

        return [
            OrgData(
                name=o.get("login", ""),
                org_id=o.get("id"),
                avatar_url=o.get("avatar_url"),
                url=f"https://github.com/{o.get('login')}",
                description=o.get("description"),
                is_owned=roles.get(o.get("login", "")),
            )
            for o in await self._async_all_pages(endpoint)
        ]

    async def _async_fetch_org_repos(self, orgs: list[OrgData]) -> dict[str, list[RepoData]]:
        """Every repository belonging to each organisation, keyed by org name."""

        async def _fetch(org: OrgData) -> tuple[str, list[RepoData]]:
            items = await self._async_all_pages(
                f"/orgs/{org.name}/repos", params={"sort": "pushed"}
            )
            return org.name, [self._to_repo(r) for r in items]

        results = await async_map_limited(orgs, _fetch, RUNNING_JOBS_CONCURRENCY)

        by_org: dict[str, list[RepoData]] = {}
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.debug("Org repository listing failed: %s", result)
                continue
            name, repos = result
            by_org[name] = repos
        return by_org

    async def _async_fetch_pastes(self) -> list[PasteData]:
        """Gists. `/gists` includes private and secret gists when authenticated."""
        endpoint = "/gists" if self.api_token else f"/users/{self.account_name}/gists"
        pastes: list[PasteData] = []

        for g in await self._async_all_pages(endpoint):
            files = g.get("files", {})
            first_file = next(iter(files.keys())) if files else None
            pastes.append(
                PasteData(
                    paste_id=str(g.get("id", "")),
                    title=g.get("description") or first_file or "Gist",
                    url=g.get("html_url"),
                    is_public=bool(g.get("public", True)),
                    files_count=len(files),
                    comments_count=g.get("comments", 0),
                    created_at=g.get("created_at"),
                    updated_at=g.get("updated_at"),
                )
            )
        return pastes

    async def _async_fetch_notifications(self) -> list[NotificationData]:
        notifications: list[NotificationData] = []

        for n in await self._async_all_pages("/notifications"):
            subject = n.get("subject") or {}
            repo = n.get("repository") or {}
            notifications.append(
                NotificationData(
                    notification_id=str(n.get("id", "")),
                    title=subject.get("title", "Notification"),
                    reason=n.get("reason"),
                    repository=repo.get("full_name"),
                    url=subject.get("html_url") or repo.get("html_url"),
                    unread=bool(n.get("unread", True)),
                    updated_at=n.get("updated_at"),
                    subject_type=subject.get("type"),
                )
            )
        return notifications

    async def _async_fetch_search(self, kind: str) -> list[dict[str, Any]]:
        """Open issues (`kind="issue"`) or pull requests (`kind="pr"`) across the account."""
        items = await self._async_search_all(f"user:{self.account_name} type:{kind} state:open")
        return [
            {
                "id": item.get("id"),
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("html_url"),
                "repository": item.get("repository_url", "").split("/")[-1],
                "author": item.get("user", {}).get("login"),
                "comments": item.get("comments", 0),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in items
        ]

    async def _async_fetch_sponsors(self) -> tuple[int | None, int | None]:
        """Sponsor totals. The API exposes only counts here, so there is no list to derive."""
        data = await self._async_graphql(_SPONSORS_QUERY, {"login": self.account_name})
        user = data.get("user") or {}
        if not user:
            return None, None
        return (
            (user.get("sponsorshipsAsMaintainer") or {}).get("totalCount"),
            (user.get("sponsorshipsAsSponsor") or {}).get("totalCount"),
        )

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due.

        Each resource is gated by `async_resource`, so an expensive collection such as
        releases is re-fetched on its own schedule while cheap ones stay current. Skipped
        resources reuse their previous value, so the snapshot is always complete.
        """
        profile = await self.async_resource(
            "profile", self._async_fetch_profile, ProfileData(username=self.account_name)
        )
        repos = await self.async_resource("repos", self._async_fetch_repos, [])
        orgs = await self.async_resource("orgs", self._async_fetch_orgs, [])

        # Attach each organisation's repositories. Whether they feed the account totals is
        # decided per entry by CONF_INCLUDE_NON_OWNED_ORGS, applied in sensor.py — the
        # snapshot always carries the full picture.
        org_repos = await self.async_resource(
            "org_repos", lambda: self._async_fetch_org_repos(orgs), {}
        )
        for org in orgs:
            org.repos = org_repos.get(org.name, org.repos)
        pastes = await self.async_resource("pastes", self._async_fetch_pastes, [])
        notifications = await self.async_resource(
            "notifications", self._async_fetch_notifications, []
        )
        open_issues = await self.async_resource(
            "issues", lambda: self._async_fetch_search("issue"), []
        )
        open_prs = await self.async_resource("prs", lambda: self._async_fetch_search("pr"), [])
        sponsors_count, sponsoring_count = await self.async_resource(
            "sponsors", self._async_fetch_sponsors, (None, None)
        )
        releases = await self.async_resource("releases", self._async_fetch_all_releases, [])
        releases = releases + await self.async_resource(
            "org_releases", lambda: self._async_fetch_org_releases(orgs), []
        )
        running_jobs_count, running_jobs = await self.async_resource(
            "running_jobs", lambda: self._async_fetch_running_jobs(repos), (None, [])
        )

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            pastes=pastes,
            notifications=notifications,
            open_issues=open_issues,
            open_prs=open_prs,
            sponsors_count=sponsors_count,
            sponsoring_count=sponsoring_count,
            running_jobs_count=running_jobs_count,
            running_jobs=running_jobs,
            releases=releases,
            rate_limit_remaining=self.scheduler.budget.remaining,
            rate_limit_reset=(
                int(self.scheduler.budget.reset_epoch)
                if self.scheduler.budget.reset_epoch
                else None
            ),
            scheduling=self.scheduler.diagnostics(),
        )
