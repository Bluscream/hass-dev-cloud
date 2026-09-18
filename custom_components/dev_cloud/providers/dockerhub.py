"""Docker Hub provider implementation."""

from __future__ import annotations

import contextlib
import logging
from typing import ClassVar

from aiohttp import ClientSession
from yarl import URL

from ..const import PLATFORM_DOCKERHUB
from ..models import DevCloudData, PackageData, ProfileData
from .base import (
    BaseDevCloudProvider,
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudProviderError,
)
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class DockerHubProvider(BaseDevCloudProvider):
    """Provider for Docker Hub."""

    platform_id = PLATFORM_DOCKERHUB
    default_base_url = "https://hub.docker.com"
    supports_custom_url = False

    # Docker Hub applies no documented request quota to these endpoints, but pull counts
    # move slowly, so there is nothing to gain from polling them hard.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "profile": ResourcePolicy(authenticated=1800, anonymous=1800),
        "packages": ResourcePolicy(authenticated=900, anonymous=1800),
    }

    def __init__(
        self,
        session: ClientSession,
        account_name: str,
        base_url: str | None = None,
        api_token: str | None = None,
        detailed: bool = True,
    ) -> None:
        super().__init__(session, account_name, base_url, api_token, detailed)
        self._jwt_token: str | None = None

    async def _async_ensure_jwt_token(self) -> str | None:
        """Authenticate with username + PAT/password to get a JWT bearer token."""
        if not self.api_token:
            return None
        if self._jwt_token:
            return self._jwt_token

        login_url = self.base_url / "v2/users/login"
        payload = {"username": self.account_name, "password": self.api_token}
        try:
            async with self.session.post(login_url, json=payload) as resp:
                if resp.status == 401:
                    raise DevCloudAuthError("Invalid Docker Hub credentials")
                resp.raise_for_status()
                data = await resp.json()
                self._jwt_token = data.get("token")
                return self._jwt_token
        except DevCloudAuthError:
            raise
        except Exception as err:
            _LOGGER.warning("Error exchanging Docker Hub token for %s: %s", self.account_name, err)
            raise DevCloudProviderError(f"Docker Hub login error: {err}") from err

    def get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-DevCloud/1.0",
        }
        if self._jwt_token:
            headers["Authorization"] = f"Bearer {self._jwt_token}"
        return headers

    async def async_validate(self) -> bool:
        # If PAT was provided, exchange it first to validate credentials
        if self.api_token:
            await self._async_ensure_jwt_token()

        # Validate that the user or org exists
        url = self.base_url / "v2/orgs" / self.account_name
        try:
            data, _ = await self.async_get_json(url, use_etag=False)
            return bool(data)
        except DevCloudNotFoundError:
            user_url = self.base_url / "v2/users" / self.account_name
            data, _ = await self.async_get_json(user_url, use_etag=False)
            return bool(data)

    async def _async_fetch_profile(self) -> ProfileData:
        user_url = self.base_url / "v2/users" / self.account_name
        display_name = self.account_name
        avatar_url = None
        created_at = None
        bio = None

        with contextlib.suppress(Exception):
            u_json, _ = await self.async_get_json(user_url)
            if isinstance(u_json, dict):
                display_name = u_json.get("full_name") or self.account_name
                avatar_url = u_json.get("gravatar_url")
                created_at = u_json.get("date_joined")
                bio = u_json.get("profile_url")

        return ProfileData(
            username=self.account_name,
            display_name=display_name,
            avatar_url=avatar_url,
            profile_url=f"https://hub.docker.com/u/{self.account_name}",
            bio=bio,
            created_at=created_at,
        )

    async def _async_fetch_packages(self) -> list[PackageData]:
        """Every image in the namespace.

        Docker Hub calls these "repositories", but they are container images — the same
        thing this integration models as a package everywhere else. They were previously
        emitted as both a package and a repository, which duplicated every field.
        """
        url = self.base_url / "v2/namespaces" / self.account_name / "repositories"
        packages: list[PackageData] = []

        items = await self.async_get_all_pages(
            url,
            size_param="page_size",
            extract=lambda p: p.get("results", []) if isinstance(p, dict) else [],
        )

        for item in items:
            name = item.get("name", "")
            packages.append(
                PackageData(
                    name=name,
                    url=str(URL("https://hub.docker.com/r") / self.account_name / name),
                    description=item.get("description"),
                    pull_count=item.get("pull_count", 0),
                    star_count=item.get("star_count", 0),
                    updated_at=item.get("last_updated"),
                )
            )

        return packages

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        # The PAT is exchanged for a JWT before anything else; both resources need it.
        if self.api_token:
            await self._async_ensure_jwt_token()

        profile: ProfileData = await self.async_resource(
            "profile", self._async_fetch_profile, ProfileData(username=self.account_name)
        )
        packages: list[PackageData] = await self.async_resource(
            "packages", self._async_fetch_packages, []
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
            scheduling=self.scheduler.diagnostics(),
        )
