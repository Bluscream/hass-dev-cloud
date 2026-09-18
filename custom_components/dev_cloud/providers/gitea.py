"""Gitea and Forgejo provider implementation (also covers Codeberg)."""

from __future__ import annotations

import logging

from ..const import PLATFORM_GITEA
from ..models import DevCloudData, NotificationData, OrgData, ProfileData, RepoData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class GiteaProvider(BaseDevCloudProvider):
    """Provider for Gitea, Forgejo, or Codeberg."""

    platform_id = PLATFORM_GITEA
    default_base_url = "https://gitea.com"
    supports_custom_url = True

    def get_headers(self) -> dict[str, str]:
        headers = super().get_headers()
        if self.api_token:
            headers["Authorization"] = f"token {self.api_token}"
        return headers

    async def async_validate(self) -> bool:
        url = f"{self.base_url}/api/v1/users/{self.account_name}"
        data, _ = await self.async_get_json(url, use_etag=False)
        return bool(data and data.get("username"))

    async def async_fetch(self) -> DevCloudData:
        # 1. Fetch user profile
        user_url = f"{self.base_url}/api/v1/users/{self.account_name}"
        user_json, _ = await self.async_get_json(user_url)

        profile = ProfileData(
            username=user_json.get("username", self.account_name),
            display_name=user_json.get("full_name"),
            user_id=user_json.get("id"),
            avatar_url=user_json.get("avatar_url"),
            profile_url=f"{self.base_url}/{user_json.get('username', self.account_name)}",
            bio=user_json.get("description"),
            location=user_json.get("location"),
            blog=user_json.get("website"),
            email=user_json.get("email"),
            created_at=user_json.get("created"),
            followers=user_json.get("followers_count"),
            following=user_json.get("following_count"),
        )

        # 2. Fetch user repos
        repos: list[RepoData] = []
        try:
            repos_url = f"{self.base_url}/api/v1/users/{self.account_name}/repos?limit=100"
            repos_json, _ = await self.async_get_json(repos_url)
            if isinstance(repos_json, list):
                for r in repos_json:
                    upstream = None
                    if r.get("fork") and r.get("parent"):
                        upstream = r["parent"].get("html_url")
                    repos.append(
                        RepoData(
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
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching Gitea repos for %s: %s", self.account_name, err)

        # 3. Fetch orgs
        orgs: list[OrgData] = []
        try:
            orgs_url = f"{self.base_url}/api/v1/users/{self.account_name}/orgs?limit=100"
            orgs_json, _ = await self.async_get_json(orgs_url)
            if isinstance(orgs_json, list):
                for o in orgs_json:
                    orgs.append(
                        OrgData(
                            name=o.get("username", ""),
                            org_id=o.get("id"),
                            display_name=o.get("full_name"),
                            avatar_url=o.get("avatar_url"),
                            url=f"{self.base_url}/{o.get('username')}",
                            description=o.get("description"),
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching Gitea orgs for %s: %s", self.account_name, err)

        # 4. Fetch notifications (if authenticated)
        notifications: list[NotificationData] = []
        if self.api_token:
            try:
                notif_url = f"{self.base_url}/api/v1/notifications?limit=100"
                notif_json, _ = await self.async_get_json(notif_url)
                if isinstance(notif_json, list):
                    for n in notif_json:
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
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching Gitea notifications for %s: %s", self.account_name, err
                )

        profile.public_repos = len(repos)

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            notifications=notifications,
        )
