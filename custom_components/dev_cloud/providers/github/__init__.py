"""GitHub provider implementation using aiogithubapi."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any, ClassVar

from aiogithubapi import (
    GitHubAPI,
    GitHubAuthenticationException,
    GitHubRatelimitException,
)
from aiohttp import ClientSession

from ...const import PLATFORM_GITHUB, RUNNING_JOBS_CONCURRENCY, RUNNING_JOBS_REPO_LIMIT
from ...models import DevCloudData, NotificationData, OrgData, PasteData, ProfileData, RepoData
from ..base import (
    MAX_PAGES,
    BaseDevCloudProvider,
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudRateLimitError,
    async_collect_running_jobs,
    async_map_limited,
)
from ..scheduling import QUOTA_GRAPHQL, PageWalker, ResourcePolicy
from .queries import SPONSORS_QUERY
from .releases import (
    RepoDetail,
    async_fetch_all_releases,
    async_fetch_org_releases,
    async_fetch_releases_via_rest,
)

_LOGGER = logging.getLogger(__name__)

GITHUB_PAGE_SIZE = 100
# GitHub's search API refuses to page past 1000 results, whatever total_count says.
GITHUB_SEARCH_RESULT_CAP = 1000


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
        "issues": ResourcePolicy(
            authenticated=900, anonymous=3600, depends_on=("repos",), min_cache=600
        ),
        "prs": ResourcePolicy(
            authenticated=900, anonymous=3600, depends_on=("repos",), min_cache=600
        ),
        "sponsors": ResourcePolicy(authenticated=3600, anonymous=None, quota=QUOTA_GRAPHQL),
        # The GraphQL walk: releases, assets, branches, tags and the real watcher count for
        # every repository. Named for what it fetches rather than for releases alone, which
        # is only the largest part of it. By far the most expensive resource here.
        "repo_detail": ResourcePolicy(
            authenticated=3600,
            anonymous=None,
            quota=QUOTA_GRAPHQL,
            depends_on=("repos",),
            min_cache=1800,
        ),
        # Fan out over every organisation, so they keep the same slow cadence.
        "org_repos": ResourcePolicy(
            authenticated=3600, anonymous=7200, depends_on=("orgs",), min_cache=1800
        ),
        "org_repo_detail": ResourcePolicy(
            authenticated=3600,
            anonymous=None,
            quota=QUOTA_GRAPHQL,
            depends_on=("orgs", "org_repos"),
            min_cache=1800,
        ),
        # Must stay fresh to mean anything, but is capped to a handful of repos.
        "running_jobs": ResourcePolicy(
            authenticated=300, anonymous=600, depends_on=("repos",), min_cache=120
        ),
    }

    def __init__(
        self,
        session: ClientSession,
        account_name: str,
        base_url: str | None = None,
        api_token: str | None = None,
        detailed: bool = True,
        include_non_owned_orgs: bool = True,
    ) -> None:
        super().__init__(
            session,
            account_name,
            base_url,
            api_token,
            detailed,
            include_non_owned_orgs=include_non_owned_orgs,
        )
        # Filled by the profile fetch; surfaced only in summary mode.
        self._reported_totals: dict[str, int] = {}
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

        # getattr rather than attribute access: GraphQL and REST responses do not carry the
        # same header model, and a missing attribute must not take the whole resource down.
        raw_remaining = getattr(headers, "x_ratelimit_remaining", None)
        raw_reset = getattr(headers, "x_ratelimit_reset", None)

        remaining: int | None = None
        reset: float | None = None
        with contextlib.suppress(TypeError, ValueError):
            if raw_remaining is not None:
                remaining = int(raw_remaining)
        with contextlib.suppress(TypeError, ValueError):
            if raw_reset is not None:
                reset = float(raw_reset)

        self.scheduler.observe_rate_limit(remaining, reset)  # REST headers

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
        data: dict[str, Any] = payload.get("data") or payload or {}
        self._observe_graphql_budget(data)
        return data

    def _observe_graphql_budget(self, data: dict[str, Any]) -> None:
        """Feed GitHub's GraphQL budget to the scheduler.

        GraphQL is billed in points rather than requests, on a separate 5000/hour budget
        from REST. Queries that ask for a deep tree cost hundreds of points each, so the
        request counter the scheduler measures is the wrong currency entirely — without
        this, a poll can exhaust the budget while appearing to have made 16 requests.
        """
        limit = data.get("rateLimit")
        if not isinstance(limit, dict):
            return

        remaining = limit.get("remaining")
        reset_at = limit.get("resetAt")
        reset_epoch: float | None = None
        if isinstance(reset_at, str):
            with contextlib.suppress(ValueError):
                reset_epoch = datetime.fromisoformat(reset_at).timestamp()

        if isinstance(remaining, int):
            self.scheduler.observe_rate_limit(remaining, reset_epoch, QUOTA_GRAPHQL)

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

        # The same response carries the collection totals. They are only published when the
        # matching list is not enumerated, so they never duplicate a length a client can take.
        self._reported_totals = {
            key: value
            for key, value in (
                ("repos", user.public_repos),
                ("pastes", user.public_gists),
            )
            if isinstance(value, int)
        }

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
            # watchers_count is a deprecated alias for stargazers_count, so it is not read
            # here at all. The real figure comes from the GraphQL walk.
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

    async def _async_fetch_search(self, kind: str) -> dict[str, list[dict[str, Any]]]:
        """Open issues (`kind="issue"`) or pull requests (`kind="pr"`), keyed by repository.

        Grouped rather than returned flat, because each one is attached to the repository it
        belongs to. The account-wide totals are then summed from those, so there is no
        second copy of the same items at the top level.
        """
        items = await self._async_search_all(f"user:{self.account_name} type:{kind} state:open")

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            # repository_url is .../repos/{owner}/{name}; the pair is the key repos use.
            parts = str(item.get("repository_url", "")).rstrip("/").split("/")
            full_name = "/".join(parts[-2:]) if len(parts) >= 2 else ""
            grouped.setdefault(full_name, []).append(
                {
                    "id": item.get("id"),
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "url": item.get("html_url"),
                    "author": item.get("user", {}).get("login"),
                    "comments": item.get("comments", 0),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        return grouped

    @staticmethod
    def _attach_detail(repos: list[RepoData], detail: RepoDetail) -> None:
        """Hang each repository's releases, branches and tags off the repository itself."""
        for repo in repos:
            found = detail.get(repo.full_name)
            if not found:
                continue
            repo.releases = found.get("releases", repo.releases)
            repo.branches = found.get("branches", repo.branches)
            repo.tags = found.get("tags", repo.tags)
            repo.security_alerts = found.get("security_alerts", repo.security_alerts)
            watchers = found.get("watchers")
            if isinstance(watchers, int):
                repo.watchers = watchers

    @staticmethod
    def _attach_issues(
        repos: list[RepoData],
        issues: dict[str, list[dict[str, Any]]],
        prs: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Hang each repository's open issues and pull requests off the repository itself."""
        for repo in repos:
            repo.issues = issues.get(repo.full_name, [])
            repo.prs = prs.get(repo.full_name, [])

        known = {repo.full_name for repo in repos}
        for full_name in (issues.keys() | prs.keys()) - known:
            # The search is scoped to this account, so a repository it returns should always
            # be in the listing. Worth a trace if that ever stops holding.
            _LOGGER.debug("Search returned %s, which is not in the repository listing", full_name)

    async def _async_fetch_sponsors(self) -> tuple[int | None, int | None]:
        """Sponsor totals. The API exposes only counts here, so there is no list to derive."""
        data = await self._async_graphql(SPONSORS_QUERY, {"login": self.account_name})
        user = data.get("user") or {}
        if not user:
            return None, None
        return (
            (user.get("sponsorshipsAsMaintainer") or {}).get("totalCount"),
            (user.get("sponsorshipsAsSponsor") or {}).get("totalCount"),
        )

    async def _async_releases(self) -> RepoDetail:
        """Releases for the account's own repositories, over GraphQL where possible.

        GraphQL is tried first because it returns every release and asset in a handful of
        queries. When it is unavailable — its points budget is spent, or the token cannot
        use it — the same data is rebuilt over REST, which bills a separate allowance.
        """
        try:
            return await async_fetch_all_releases(self._async_graphql, self.account_name)
        except Exception as err:
            _LOGGER.info(
                "GitHub GraphQL unavailable for %s (%s); falling back to the REST releases "
                "endpoint, which costs one request per repository",
                self.account_name,
                err,
            )

        repos: list[RepoData] = self._resource_values.get("repos", [])
        return await async_fetch_releases_via_rest(
            self._async_all_pages,
            [r.full_name for r in repos if r.full_name and not r.is_archived],
            RUNNING_JOBS_CONCURRENCY,
        )

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due.

        Each resource is gated by `async_resource`, so an expensive collection such as
        releases is re-fetched on its own schedule while cheap ones stay current. Skipped
        resources reuse their previous value, so the snapshot is always complete.
        """
        profile: ProfileData = await self.async_resource(
            "profile", self._async_fetch_profile, ProfileData(username=self.account_name)
        )

        if not self.detailed:
            # Summary mode: the profile response already carried the totals, so stop here
            # rather than spending a request per page of every collection.
            return DevCloudData(
                profile=profile,
                totals=dict(self._reported_totals),
                rate_limit_remaining=self.scheduler.budget().remaining,
                resources=self.scheduler.persisted_state(),
                collected=self.collected_resources(),
            )

        repos: list[RepoData] = await self.async_resource("repos", self._async_fetch_repos, [])
        orgs: list[OrgData] = await self.async_resource("orgs", self._async_fetch_orgs, [])

        # Attach each organisation's repositories. When include_non_owned_orgs is False,
        # non-owned organisations are skipped from repository and release walks to save API quota.
        target_orgs = orgs if self.include_non_owned_orgs else [o for o in orgs if o.is_owned]
        org_repos: dict[str, list[RepoData]] = await self.async_resource(
            "org_repos", lambda: self._async_fetch_org_repos(target_orgs), {}
        )
        for org in orgs:
            if not (self.include_non_owned_orgs or org.is_owned):
                org.repos = []
            else:
                org.repos = org_repos.get(org.name, org.repos)
        pastes: list[PasteData] = await self.async_resource("pastes", self._async_fetch_pastes, [])
        notifications: list[NotificationData] = await self.async_resource(
            "notifications", self._async_fetch_notifications, []
        )
        issues_by_repo: dict[str, list[dict[str, Any]]] = await self.async_resource(
            "issues", lambda: self._async_fetch_search("issue"), {}
        )
        prs_by_repo: dict[str, list[dict[str, Any]]] = await self.async_resource(
            "prs", lambda: self._async_fetch_search("pr"), {}
        )
        self._attach_issues(repos, issues_by_repo, prs_by_repo)
        sponsors: tuple[int | None, int | None] = await self.async_resource(
            "sponsors", self._async_fetch_sponsors, (None, None)
        )
        sponsors_count, sponsoring_count = sponsors
        repo_detail: RepoDetail = await self.async_resource("repo_detail", self._async_releases, {})
        org_detail: RepoDetail = await self.async_resource(
            "org_repo_detail",
            lambda: async_fetch_org_releases(self._async_graphql, target_orgs),
            {},
        )
        self._attach_detail(repos, repo_detail)
        for org in target_orgs:
            self._attach_detail(org.repos, org_detail)
        jobs: tuple[int | None, list[dict[str, Any]]] = await self.async_resource(
            "running_jobs", lambda: self._async_fetch_running_jobs(repos), (None, [])
        )
        running_jobs_count, running_jobs = jobs

        # Bound once: the REST allowance is what these two fields have always reported.
        rest_budget = self.scheduler.budget()
        reset_epoch = rest_budget.reset_epoch

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            pastes=pastes,
            notifications=notifications,
            sponsors_count=sponsors_count,
            sponsoring_count=sponsoring_count,
            running_jobs_count=running_jobs_count,
            running_jobs=running_jobs,
            rate_limit_remaining=rest_budget.remaining,
            rate_limit_reset=int(reset_epoch) if reset_epoch is not None else None,
            resources=self.scheduler.persisted_state(),
            collected=self.collected_resources(),
        )
