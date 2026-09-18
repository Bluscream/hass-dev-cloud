"""GitHub provider implementation."""

from __future__ import annotations

import contextlib
import logging

from ..const import PLATFORM_GITHUB
from ..models import DevCloudData, OrgData, PasteData, ProfileData, RepoData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class GitHubProvider(BaseDevCloudProvider):
    """Provider for GitHub."""

    platform_id = PLATFORM_GITHUB
    default_base_url = "https://api.github.com"
    supports_custom_url = False

    def get_headers(self) -> dict[str, str]:
        headers = super().get_headers()
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if self.api_token:
            headers["Authorization"] = f"token {self.api_token}"
        return headers

    async def async_validate(self) -> bool:
        url = f"{self.base_url}/users/{self.account_name}"
        data, _ = await self.async_get_json(url, use_etag=False)
        return bool(data and data.get("login"))

    async def async_fetch(self) -> DevCloudData:
        # 1. Fetch user profile
        user_url = f"{self.base_url}/users/{self.account_name}"
        user_json, headers = await self.async_get_json(user_url)

        rate_limit_remaining = None
        rate_limit_reset = None
        if "X-RateLimit-Remaining" in headers:
            with contextlib.suppress(ValueError):
                rate_limit_remaining = int(headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in headers:
            with contextlib.suppress(ValueError):
                rate_limit_reset = int(headers["X-RateLimit-Reset"])

        profile = ProfileData(
            username=user_json.get("login", self.account_name),
            display_name=user_json.get("name"),
            user_id=user_json.get("id"),
            avatar_url=user_json.get("avatar_url"),
            profile_url=user_json.get("html_url"),
            bio=user_json.get("bio"),
            location=user_json.get("location"),
            company=user_json.get("company"),
            blog=user_json.get("blog"),
            email=user_json.get("email"),
            created_at=user_json.get("created_at"),
            followers=user_json.get("followers"),
            following=user_json.get("following"),
            public_repos=user_json.get("public_repos"),
            public_gists=user_json.get("public_gists"),
        )

        # 2. Fetch repos
        repos: list[RepoData] = []
        try:
            repos_url = f"{self.base_url}/users/{self.account_name}/repos?per_page=100&sort=updated"
            repos_json, _ = await self.async_get_json(repos_url)
            if isinstance(repos_json, list):
                for r in repos_json:
                    repos.append(
                        RepoData(
                            name=r.get("name", ""),
                            full_name=r.get("full_name", ""),
                            url=r.get("html_url", ""),
                            description=r.get("description"),
                            is_fork=bool(r.get("fork")),
                            is_private=bool(r.get("private")),
                            is_archived=bool(r.get("archived")),
                            stars=r.get("stargazers_count", 0),
                            forks=r.get("forks_count", 0),
                            watchers=r.get("watchers_count", 0),
                            open_issues=r.get("open_issues_count", 0),
                            primary_language=r.get("language"),
                            default_branch=r.get("default_branch"),
                            created_at=r.get("created_at"),
                            updated_at=r.get("updated_at"),
                            pushed_at=r.get("pushed_at"),
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub repos for %s: %s", self.account_name, err)

        # 3. Fetch orgs
        orgs: list[OrgData] = []
        try:
            orgs_url = f"{self.base_url}/users/{self.account_name}/orgs?per_page=100"
            orgs_json, _ = await self.async_get_json(orgs_url)
            if isinstance(orgs_json, list):
                for o in orgs_json:
                    orgs.append(
                        OrgData(
                            name=o.get("login", ""),
                            org_id=o.get("id"),
                            avatar_url=o.get("avatar_url"),
                            url=f"https://github.com/{o.get('login')}",
                            description=o.get("description"),
                        )
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub orgs for %s: %s", self.account_name, err)

        # 4. Fetch gists
        pastes: list[PasteData] = []
        try:
            gists_url = f"{self.base_url}/users/{self.account_name}/gists?per_page=100"
            gists_json, _ = await self.async_get_json(gists_url)
            if isinstance(gists_json, list):
                for g in gists_json:
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
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub gists for %s: %s", self.account_name, err)

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            pastes=pastes,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset=rate_limit_reset,
        )
