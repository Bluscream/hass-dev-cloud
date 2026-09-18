"""GitLab provider implementation."""

from __future__ import annotations

import contextlib
import logging

from ..const import PLATFORM_GITLAB
from ..models import DevCloudData, OrgData, PasteData, ProfileData, RepoData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class GitLabProvider(BaseDevCloudProvider):
    """Provider for GitLab (cloud or self-hosted)."""

    platform_id = PLATFORM_GITLAB
    default_base_url = "https://gitlab.com"
    supports_custom_url = True

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

    async def async_fetch(self) -> DevCloudData:
        # 1. Fetch user by username
        users_url = f"{self.base_url}/api/v4/users?username={self.account_name}"
        users_json, headers = await self.async_get_json(users_url)

        if not isinstance(users_json, list) or not users_json:
            raise ValueError(f"GitLab user '{self.account_name}' not found")

        u = users_json[0]
        user_id = u.get("id")

        rate_limit_remaining = None
        if "RateLimit-Remaining" in headers:
            with contextlib.suppress(ValueError):
                rate_limit_remaining = int(headers["RateLimit-Remaining"])

        # 2. Fetch detailed profile if possible
        user_detail_url = f"{self.base_url}/api/v4/users/{user_id}"
        try:
            detail_json, _ = await self.async_get_json(user_detail_url)
            if isinstance(detail_json, dict):
                u.update(detail_json)
        except Exception:
            pass

        profile = ProfileData(
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

        # 3. Fetch user projects (repos)
        repos: list[RepoData] = []
        try:
            projects_url = (
                f"{self.base_url}/api/v4/users/{user_id}/projects?per_page=100&order_by=updated_at"
            )
            projects_json, _ = await self.async_get_json(projects_url)
            if isinstance(projects_json, list):
                for p in projects_json:
                    fork_source = p.get("forked_from_project", {})
                    upstream = fork_source.get("web_url") if fork_source else None
                    repos.append(
                        RepoData(
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
                            upstream=upstream,
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitLab projects for %s: %s", self.account_name, err)

        # 4. Fetch user snippets
        pastes: list[PasteData] = []
        try:
            snippets_url = f"{self.base_url}/api/v4/users/{user_id}/snippets?per_page=100"
            snippets_json, _ = await self.async_get_json(snippets_url)
            if isinstance(snippets_json, list):
                for s in snippets_json:
                    files = s.get("files", [])
                    pastes.append(
                        PasteData(
                            paste_id=str(s.get("id", "")),
                            title=s.get("title") or s.get("file_name"),
                            url=s.get("web_url"),
                            is_public=s.get("visibility") == "public",
                            files_count=len(files) if files else 1,
                            created_at=s.get("created_at"),
                            updated_at=s.get("updated_at"),
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitLab snippets for %s: %s", self.account_name, err)

        # 5. Fetch user memberships/groups if authenticated
        orgs: list[OrgData] = []
        if self.api_token:
            try:
                groups_url = f"{self.base_url}/api/v4/groups?per_page=100"
                groups_json, _ = await self.async_get_json(groups_url)
                if isinstance(groups_json, list):
                    for g in groups_json:
                        orgs.append(
                            OrgData(
                                name=g.get("full_path", g.get("name", "")),
                                org_id=g.get("id"),
                                display_name=g.get("name"),
                                avatar_url=g.get("avatar_url"),
                                url=g.get("web_url"),
                                description=g.get("description"),
                            )
                        )
            except Exception as err:
                _LOGGER.warning("Error fetching GitLab groups for %s: %s", self.account_name, err)

        profile.public_repos = len(repos)
        profile.public_gists = len(pastes)

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            pastes=pastes,
            rate_limit_remaining=rate_limit_remaining,
        )
