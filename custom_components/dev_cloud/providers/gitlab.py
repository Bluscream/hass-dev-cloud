"""GitLab provider implementation."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, ClassVar

from ..const import PLATFORM_GITLAB, RUNNING_JOBS_CONCURRENCY, RUNNING_JOBS_REPO_LIMIT
from ..models import DevCloudData, NotificationData, OrgData, PasteData, ProfileData, RepoData
from .base import BaseDevCloudProvider, async_collect_running_jobs, async_map_limited
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class GitLabProvider(BaseDevCloudProvider):
    """Provider for GitLab (cloud or self-hosted)."""

    platform_id = PLATFORM_GITLAB
    default_base_url = "https://gitlab.com"
    supports_custom_url = True

    # GitLab.com allows ~2000 authenticated requests/minute but far less unauthenticated,
    # and self-hosted instances vary wildly — so the anonymous column is conservative.
    # Groups and todos require a token.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "profile": ResourcePolicy(authenticated=600, anonymous=1800),
        "repos": ResourcePolicy(authenticated=900, anonymous=3600),
        "pastes": ResourcePolicy(authenticated=1800, anonymous=3600),
        "orgs": ResourcePolicy(authenticated=3600, anonymous=None),
        # Fans out over every group, so it shares the slow org cadence.
        "org_repos": ResourcePolicy(authenticated=3600, anonymous=None),
        "notifications": ResourcePolicy(authenticated=300, anonymous=None),
        "running_jobs": ResourcePolicy(authenticated=300, anonymous=900),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Resolved from the username by the profile fetch; every other endpoint is keyed
        # by numeric id, so the profile resource must run before them.
        self._user_id: int | None = None

    def get_headers(self) -> dict[str, str]:
        headers = super().get_headers()
        if self.api_token:
            headers["PRIVATE-TOKEN"] = self.api_token
            headers.pop("Authorization", None)
        return headers

    async def async_validate(self) -> bool:
        url = f"{self.base_url}/api/v4/users?username={self.account_name}"
        data, _ = await self.async_get_json(url, use_etag=False)
        return bool(isinstance(data, list) and len(data) > 0)

    async def _async_fetch_running_jobs(
        self, repos: list[RepoData]
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """Count running pipelines across the user's most recently active projects.

        GitLab only exposes pipelines per project, so the query fans out over the
        most recently active projects that have CI enabled.
        """
        candidates = [
            repo
            for repo in repos
            if repo.extra.get("project_id") is not None
            and repo.extra.get("builds_access_level") != "disabled"
            and repo.extra.get("jobs_enabled") is not False
        ][:RUNNING_JOBS_REPO_LIMIT]

        async def _fetch(repo: RepoData) -> list[dict[str, Any]]:
            project_id = repo.extra["project_id"]
            url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines?scope=running"
            pipelines, _ = await self.async_get_json(url, use_etag=False)
            if not isinstance(pipelines, list):
                return []
            return [
                {
                    "id": pipeline.get("id"),
                    "repository": repo.full_name,
                    "name": pipeline.get("name") or f"Pipeline #{pipeline.get('iid')}",
                    "ref": pipeline.get("ref"),
                    "source": pipeline.get("source"),
                    "status": pipeline.get("status"),
                    "url": pipeline.get("web_url"),
                    "created_at": pipeline.get("created_at"),
                    "updated_at": pipeline.get("updated_at"),
                }
                for pipeline in pipelines
            ]

        return await async_collect_running_jobs(candidates, _fetch, RUNNING_JOBS_CONCURRENCY)

    async def _async_fetch_user_id(self) -> tuple[int | None, dict[str, Any]]:
        """Resolve the username to a numeric id, and return the raw user payload."""
        users_url = f"{self.base_url}/api/v4/users?username={self.account_name}"
        users_json, headers = await self.async_get_json(users_url)

        if not isinstance(users_json, list) or not users_json:
            raise ValueError(f"GitLab user '{self.account_name}' not found")

        if "RateLimit-Remaining" in headers:
            with contextlib.suppress(ValueError):
                self.scheduler.observe_rate_limit(int(headers["RateLimit-Remaining"]), None)

        user = users_json[0]
        return user.get("id"), user

    async def _async_fetch_profile(self) -> ProfileData:
        user_id, u = await self._async_fetch_user_id()
        self._user_id = user_id

        # The detail endpoint adds bio/location/organization, but is not always permitted.
        with contextlib.suppress(Exception):
            detail_json, _ = await self.async_get_json(f"{self.base_url}/api/v4/users/{user_id}")
            if isinstance(detail_json, dict):
                u.update(detail_json)

        return ProfileData(
            username=u.get("username", self.account_name),
            display_name=u.get("name"),
            user_id=user_id,
            avatar_url=u.get("avatar_url"),
            profile_url=u.get("web_url"),
            bio=u.get("bio"),
            location=u.get("location"),
            company=u.get("organization"),
            blog=u.get("website_url"),
            created_at=u.get("created_at"),
            followers=u.get("followers"),
            following=u.get("following"),
        )

    async def _async_fetch_repos(self) -> list[RepoData]:
        """Every project owned by the user."""
        url = f"{self.base_url}/api/v4/users/{self._user_id}/projects?order_by=updated_at"
        return [self._to_repo(p) for p in await self.async_get_all_pages(url)]

    @staticmethod
    def _to_repo(p: dict[str, Any]) -> RepoData:
        """Map a GitLab project payload. Shared by the user and group listings."""
        fork_source = p.get("forked_from_project", {})
        return RepoData(
            name=p.get("name", ""),
            full_name=p.get("path_with_namespace", p.get("name", "")),
            url=p.get("web_url", ""),
            description=p.get("description"),
            is_fork=bool(fork_source),
            is_private=p.get("visibility") == "private",
            is_archived=bool(p.get("archived")),
            stars=p.get("star_count", 0),
            forks=p.get("forks_count", 0),
            open_issues=p.get("open_issues_count", 0),
            default_branch=p.get("default_branch"),
            created_at=p.get("created_at"),
            updated_at=p.get("last_activity_at"),
            upstream=fork_source.get("web_url") if fork_source else None,
            extra={
                "project_id": p.get("id"),
                # Needed to skip projects without CI when polling pipelines.
                "builds_access_level": p.get("builds_access_level"),
                "jobs_enabled": p.get("jobs_enabled"),
            },
        )

    async def _async_fetch_pastes(self) -> list[PasteData]:
        url = f"{self.base_url}/api/v4/users/{self._user_id}/snippets"
        return [
            PasteData(
                paste_id=str(s.get("id", "")),
                title=s.get("title") or s.get("file_name"),
                url=s.get("web_url"),
                is_public=s.get("visibility") == "public",
                files_count=len(s.get("files") or []) or 1,
                created_at=s.get("created_at"),
                updated_at=s.get("updated_at"),
            )
            for s in await self.async_get_all_pages(url)
        ]

    async def _async_fetch_orgs(self) -> list[OrgData]:
        """Groups the token can see. Requires authentication."""
        return [
            OrgData(
                name=g.get("full_path", g.get("name", "")),
                org_id=g.get("id"),
                display_name=g.get("name"),
                avatar_url=g.get("avatar_url"),
                url=g.get("web_url"),
                description=g.get("description"),
            )
            for g in await self.async_get_all_pages(f"{self.base_url}/api/v4/groups")
        ]

    async def _async_fetch_org_repos(self, orgs: list[OrgData]) -> dict[str, list[RepoData]]:
        """Every project belonging to each group, keyed by group name."""

        async def _fetch(org: OrgData) -> tuple[str, list[RepoData]]:
            url = f"{self.base_url}/api/v4/groups/{org.org_id}/projects"
            return org.name, [self._to_repo(p) for p in await self.async_get_all_pages(url)]

        results = await async_map_limited(orgs, _fetch, RUNNING_JOBS_CONCURRENCY)

        by_org: dict[str, list[RepoData]] = {}
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.debug("GitLab group project listing failed: %s", result)
                continue
            name, repos = result
            by_org[name] = repos
        return by_org

    async def _async_fetch_notifications(self) -> list[NotificationData]:
        """Pending todos, GitLab's equivalent of notifications."""
        url = f"{self.base_url}/api/v4/todos?state=pending"
        notifications: list[NotificationData] = []

        for t in await self.async_get_all_pages(url):
            project = t.get("project", {})
            target = t.get("target", {})
            notifications.append(
                NotificationData(
                    notification_id=str(t.get("id", "")),
                    title=target.get("title") or t.get("action_name", "Todo"),
                    reason=t.get("action_name"),
                    repository=project.get("path_with_namespace"),
                    url=t.get("target_url"),
                    unread=t.get("state") == "pending",
                    updated_at=t.get("updated_at") or t.get("created_at"),
                    subject_type=t.get("target_type"),
                )
            )
        return notifications

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        profile = await self.async_resource(
            "profile", self._async_fetch_profile, ProfileData(username=self.account_name)
        )
        if self._user_id is None:
            raise ValueError(f"GitLab user '{self.account_name}' could not be resolved")

        repos = await self.async_resource("repos", self._async_fetch_repos, [])
        pastes = await self.async_resource("pastes", self._async_fetch_pastes, [])
        orgs = await self.async_resource("orgs", self._async_fetch_orgs, [])
        org_repos = await self.async_resource(
            "org_repos", lambda: self._async_fetch_org_repos(orgs), {}
        )
        for org in orgs:
            org.repos = org_repos.get(org.name, org.repos)
        notifications = await self.async_resource(
            "notifications", self._async_fetch_notifications, []
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
            running_jobs_count=running_jobs_count,
            running_jobs=running_jobs,
            rate_limit_remaining=self.scheduler.budget.remaining,
            scheduling=self.scheduler.diagnostics(),
        )
