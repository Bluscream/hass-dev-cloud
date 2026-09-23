"""Gitea and Forgejo provider implementation (also covers Codeberg)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from ..const import PLATFORM_GITEA, RUNNING_JOBS_CONCURRENCY, RUNNING_JOBS_REPO_LIMIT
from ..models import DevCloudData, NotificationData, OrgData, ProfileData, RepoData
from .base import BaseDevCloudProvider, async_collect_running_jobs, async_map_limited
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class GiteaProvider(BaseDevCloudProvider):
    """Provider for Gitea, Forgejo, or Codeberg."""

    platform_id = PLATFORM_GITEA
    default_base_url = "https://gitea.com"
    supports_custom_url = True

    # Self-hosted instances are usually small and unmetered, but they are also somebody's
    # Raspberry Pi — so these stay polite rather than maximal. Notifications and Actions
    # need a token.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "profile": ResourcePolicy(authenticated=600, anonymous=1800),
        "repos": ResourcePolicy(authenticated=900, anonymous=3600),
        "orgs": ResourcePolicy(authenticated=3600, anonymous=7200),
        # Fans out over every organisation, so it shares the slow org cadence.
        "org_repos": ResourcePolicy(
            authenticated=3600, anonymous=7200, depends_on=("orgs",), min_cache=1800
        ),
        "notifications": ResourcePolicy(authenticated=300, anonymous=None),
        "running_jobs": ResourcePolicy(
            authenticated=300, anonymous=None, depends_on=("repos",), min_cache=120
        ),
    }

    def get_headers(self) -> dict[str, str]:
        headers = super().get_headers()
        if self.api_token:
            headers["Authorization"] = f"token {self.api_token}"
        return headers

    async def async_validate(self) -> bool:
        url = self.base_url / "api/v1/users" / self.account_name
        data, _ = await self.async_get_json(url, use_etag=False)
        return bool(data and data.get("username"))

    async def _async_fetch_running_jobs(
        self, repos: list[RepoData]
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """Count running Gitea/Forgejo Actions runs across the most recently updated repos.

        The Actions API needs authentication, and instances older than Gitea 1.22 do not
        expose it at all — both cases fall through to ``(None, [])`` so the sensor is omitted.
        """
        if not self.api_token:
            return None, []

        candidates = sorted(
            (r for r in repos if not r.is_archived and r.full_name),
            key=lambda r: r.updated_at or "",
            reverse=True,
        )
        candidates = [r for r in candidates if r.extra.get("has_actions") is not False][
            :RUNNING_JOBS_REPO_LIMIT
        ]

        async def _fetch(repo: RepoData) -> list[dict[str, Any]]:
            url = (self.base_url / "api/v1/repos" / repo.full_name / "actions/runs").with_query(
                {"status": "running"}
            )
            payload, _ = await self.async_get_json(url, use_etag=False)
            if not isinstance(payload, dict):
                return []
            return [
                {
                    "id": run.get("id"),
                    "repository": repo.full_name,
                    "name": run.get("display_title") or run.get("name"),
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
        user_url = self.base_url / "api/v1/users" / self.account_name
        u, _ = await self.async_get_json(user_url)

        return ProfileData(
            username=u.get("username", self.account_name),
            display_name=u.get("full_name"),
            user_id=u.get("id"),
            avatar_url=u.get("avatar_url"),
            profile_url=str(self.base_url / str(u.get("username", self.account_name))),
            bio=u.get("description"),
            location=u.get("location"),
            blog=u.get("website"),
            email=u.get("email"),
            created_at=u.get("created"),
            followers=u.get("followers_count"),
            following=u.get("following_count"),
        )

    async def _async_fetch_repos(self) -> list[RepoData]:
        url = self.base_url / "api/v1/users" / self.account_name / "repos"
        repos: list[RepoData] = []

        for r in await self.async_get_all_pages(url, size_param="limit"):
            repos.append(self._to_repo(r))
        return repos

    @staticmethod
    def _to_repo(r: dict[str, Any]) -> RepoData:
        """Map a Gitea repository payload. Shared by the user and organisation listings."""
        upstream = None
        if r.get("fork") and r.get("parent"):
            upstream = r["parent"].get("html_url")
        return RepoData(
            name=r.get("name", ""),
            full_name=r.get("full_name", ""),
            url=r.get("html_url", ""),
            description=r.get("description"),
            is_fork=bool(r.get("fork")),
            is_private=bool(r.get("private")),
            is_archived=bool(r.get("archived")),
            stars=r.get("stars_count", 0),
            forks=r.get("forks_count", 0),
            watchers=r.get("watchers_count", 0),
            open_issues=r.get("open_issues_count", 0),
            primary_language=r.get("language"),
            default_branch=r.get("default_branch"),
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
            upstream=upstream,
            # has_actions exists from Gitea 1.21 / Forgejo 1.21 onwards.
            extra={"has_actions": r.get("has_actions")},
        )

    async def _async_fetch_orgs(self) -> list[OrgData]:
        url = self.base_url / "api/v1/users" / self.account_name / "orgs"
        return [
            OrgData(
                name=o.get("username", ""),
                org_id=o.get("id"),
                display_name=o.get("full_name"),
                avatar_url=o.get("avatar_url"),
                url=str(self.base_url / str(o.get("username", ""))),
                description=o.get("description"),
            )
            for o in await self.async_get_all_pages(url, size_param="limit")
        ]

    async def _async_fetch_org_repos(self, orgs: list[OrgData]) -> dict[str, list[RepoData]]:
        """Every repository belonging to each organisation, keyed by org name."""

        async def _fetch(org: OrgData) -> tuple[str, list[RepoData]]:
            url = self.base_url / "api/v1/orgs" / org.name / "repos"
            items = await self.async_get_all_pages(url, size_param="limit")
            return org.name, [self._to_repo(r) for r in items]

        results = await async_map_limited(orgs, _fetch, RUNNING_JOBS_CONCURRENCY)

        by_org: dict[str, list[RepoData]] = {}
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.debug("Gitea org repository listing failed: %s", result)
                continue
            name, repos = result
            by_org[name] = repos
        return by_org

    async def _async_fetch_notifications(self) -> list[NotificationData]:
        url = self.base_url / "api/v1/notifications"
        notifications: list[NotificationData] = []

        for n in await self.async_get_all_pages(url, size_param="limit"):
            subject = n.get("subject", {})
            repo = n.get("repository", {})
            notifications.append(
                NotificationData(
                    notification_id=str(n.get("id", "")),
                    title=subject.get("title", "Notification"),
                    repository=repo.get("full_name"),
                    url=subject.get("url") or repo.get("html_url"),
                    unread=bool(n.get("unread", True)),
                    updated_at=n.get("updated_at"),
                    subject_type=subject.get("type"),
                )
            )
        return notifications

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        profile: ProfileData = await self.async_resource(
            "profile", self._async_fetch_profile, ProfileData(username=self.account_name)
        )
        repos: list[RepoData] = await self.async_resource("repos", self._async_fetch_repos, [])
        orgs: list[OrgData] = await self.async_resource("orgs", self._async_fetch_orgs, [])
        org_repos: dict[str, list[RepoData]] = await self.async_resource(
            "org_repos", lambda: self._async_fetch_org_repos(orgs), {}
        )
        for org in orgs:
            org.repos = org_repos.get(org.name, org.repos)
        notifications: list[NotificationData] = await self.async_resource(
            "notifications", self._async_fetch_notifications, []
        )
        jobs: tuple[int | None, list[dict[str, Any]]] = await self.async_resource(
            "running_jobs", lambda: self._async_fetch_running_jobs(repos), (None, [])
        )
        running_jobs_count, running_jobs = jobs

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            notifications=notifications,
            running_jobs_count=running_jobs_count,
            running_jobs=running_jobs,
            resources=self.scheduler.persisted_state(),
            budgets=self.scheduler.persisted_budgets(),
            collected=self.collected_resources(),
        )
