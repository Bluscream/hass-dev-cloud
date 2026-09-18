"""GitHub provider implementation using aiogithubapi."""

from __future__ import annotations

import logging
from typing import Any

from aiogithubapi import (
    GitHubAPI,
    GitHubAuthenticationException,
    GitHubRatelimitException,
)
from aiohttp import ClientSession

from ..const import PLATFORM_GITHUB
from ..models import DevCloudData, NotificationData, OrgData, PasteData, ProfileData, RepoData
from .base import (
    BaseDevCloudProvider,
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudRateLimitError,
)

_LOGGER = logging.getLogger(__name__)


class GitHubProvider(BaseDevCloudProvider):
    """Provider for GitHub using aiogithubapi."""

    platform_id = PLATFORM_GITHUB
    default_base_url = "https://api.github.com"
    supports_custom_url = False

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

    async def async_fetch(self) -> DevCloudData:
        # 1. Fetch user profile
        rate_limit_remaining: int | None = None
        rate_limit_reset: int | None = None

        user_resp = await self._api.users.get(self.account_name)
        user = user_resp.data
        if user_resp.headers.x_ratelimit_remaining is not None:
            with contextlib_suppress():
                rate_limit_remaining = int(user_resp.headers.x_ratelimit_remaining)
        if user_resp.headers.x_ratelimit_reset is not None:
            with contextlib_suppress():
                rate_limit_reset = int(user_resp.headers.x_ratelimit_reset)

        private_gists: int | None = None
        private_repos: int | None = None
        if self.api_token:
            try:
                auth_user_resp = await self._api.generic("/user")
                if isinstance(auth_user_resp.data, dict):
                    private_gists = auth_user_resp.data.get("private_gists")
                    private_repos = auth_user_resp.data.get("total_private_repos")
            except Exception as err:
                _LOGGER.debug("Could not fetch authenticated user details: %s", err)

        total_gists: int | None = None
        if user.public_gists is not None:
            total_gists = user.public_gists + (private_gists or 0)

        profile = ProfileData(
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
            public_repos=user.public_repos,
            public_gists=user.public_gists,
            private_repos=private_repos,
            private_gists=private_gists,
            total_gists=total_gists,
        )

        # 2. Fetch repos
        repos: list[RepoData] = []
        try:
            repos_resp = (
                await self._api.user.repos(params={"per_page": 100, "sort": "updated"})
                if self.api_token
                else await self._api.users.repos(
                    self.account_name, params={"per_page": 100, "sort": "updated"}
                )
            )
            for r in repos_resp.data:
                repos.append(
                    RepoData(
                        name=r.name or "",
                        full_name=r.full_name or "",
                        url=r.html_url or "",
                        description=r.description,
                        is_fork=bool(r.fork),
                        is_private=bool(r.private),
                        is_archived=bool(r.archived),
                        stars=r.stargazers_count or 0,
                        forks=r.forks_count or 0,
                        watchers=r.watchers_count or 0,
                        open_issues=r.open_issues_count or 0,
                        primary_language=r.language,
                        default_branch=r.default_branch,
                        created_at=r.created_at,
                        updated_at=r.updated_at,
                        pushed_at=r.pushed_at,
                    )
                )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub repos for %s: %s", self.account_name, err)

        # 3. Fetch orgs
        # If authenticated, use api.user.orgs() to fetch all memberships (including private/hidden)
        # If unauthenticated, use api.users.orgs(username) for public org memberships
        orgs: list[OrgData] = []
        try:
            if self.api_token:
                orgs_resp = await self._api.user.orgs(params={"per_page": 100})
            else:
                orgs_resp = await self._api.users.orgs(self.account_name, params={"per_page": 100})
            for o in orgs_resp.data:
                orgs.append(
                    OrgData(
                        name=o.login or "",
                        org_id=o.id,
                        avatar_url=o.avatar_url,
                        url=f"https://github.com/{o.login}",
                        description=o.description,
                    )
                )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub orgs for %s: %s", self.account_name, err)

        # 4. Fetch gists (query /gists when authenticated to include private/secret gists)
        pastes: list[PasteData] = []
        try:
            gists_endpoint = "/gists" if self.api_token else f"/users/{self.account_name}/gists"
            gists_resp = await self._api.generic(gists_endpoint, params={"per_page": 100})
            if isinstance(gists_resp.data, list):
                for g in gists_resp.data:
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

        # 5. Fetch notifications (if authenticated)
        notifications: list[NotificationData] = []
        if self.api_token:
            try:
                notifs_resp = await self._api.notifications.list(per_page=100)
                for n in notifs_resp.data:
                    subject = getattr(n, "subject", {}) or {}
                    repo = getattr(n, "repository", {}) or {}
                    subj_dict = subject if isinstance(subject, dict) else subject.as_dict
                    repo_dict = repo if isinstance(repo, dict) else repo.as_dict
                    notifications.append(
                        NotificationData(
                            notification_id=str(n.id),
                            title=subj_dict.get("title", "Notification"),
                            reason=n.reason,
                            repository=repo_dict.get("full_name"),
                            url=subj_dict.get("html_url") or repo_dict.get("html_url"),
                            unread=bool(n.unread),
                            updated_at=n.updated_at,
                            subject_type=subj_dict.get("type"),
                        )
                    )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching GitHub notifications for %s: %s", self.account_name, err
                )

        # 6. Fetch Open Issues & PRs across user repos and orgs
        open_issues_count: int | None = None
        open_prs_count: int | None = None
        open_issues: list[dict[str, Any]] = []
        open_prs: list[dict[str, Any]] = []

        try:
            issues_resp = await self._api.generic(
                "/search/issues",
                params={
                    "q": f"user:{self.account_name} type:issue state:open",
                    "per_page": 50,
                    "sort": "updated",
                },
            )
            if isinstance(issues_resp.data, dict) and "total_count" in issues_resp.data:
                open_issues_count = issues_resp.data.get("total_count", 0)
                for item in issues_resp.data.get("items", []):
                    open_issues.append(
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
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub open issues for %s: %s", self.account_name, err)

        try:
            prs_resp = await self._api.generic(
                "/search/issues",
                params={
                    "q": f"user:{self.account_name} type:pr state:open",
                    "per_page": 50,
                    "sort": "updated",
                },
            )
            if isinstance(prs_resp.data, dict) and "total_count" in prs_resp.data:
                open_prs_count = prs_resp.data.get("total_count", 0)
                for item in prs_resp.data.get("items", []):
                    open_prs.append(
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
                    )
        except Exception as err:
            _LOGGER.warning("Error fetching GitHub open PRs for %s: %s", self.account_name, err)

        # 7. Fetch Sponsors data via GraphQL
        sponsors_count: int | None = None
        sponsoring_count: int | None = None
        if self.api_token:
            try:
                query = f"""
                query {{
                  user(login: "{self.account_name}") {{
                    sponsorshipsAsMaintainer(activeOnly: true) {{
                      totalCount
                    }}
                    sponsorshipsAsSponsor(activeOnly: true) {{
                      totalCount
                    }}
                  }}
                }}
                """
                gql_resp = await self._api.graphql(query=query)
                user_gql = (gql_resp.data or {}).get("data", {}).get("user", {})
                if user_gql:
                    sponsors_data = user_gql.get("sponsorshipsAsMaintainer", {})
                    sponsoring_data = user_gql.get("sponsorshipsAsSponsor", {})
                    sponsors_count = sponsors_data.get("totalCount")
                    sponsoring_count = sponsoring_data.get("totalCount")
            except Exception as err:
                _LOGGER.debug(
                    "Could not fetch GitHub sponsorships for %s: %s", self.account_name, err
                )

        return DevCloudData(
            profile=profile,
            orgs=orgs,
            repos=repos,
            pastes=pastes,
            notifications=notifications,
            open_issues_count=open_issues_count,
            open_prs_count=open_prs_count,
            open_issues=open_issues,
            open_prs=open_prs,
            sponsors_count=sponsors_count,
            sponsoring_count=sponsoring_count,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset=rate_limit_reset,
        )


def contextlib_suppress():
    """Helper for suppressing ValueError."""
    import contextlib

    return contextlib.suppress(ValueError)
