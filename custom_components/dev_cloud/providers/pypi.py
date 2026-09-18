"""PyPI (pip) provider implementation."""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from ..const import PLATFORM_PYPI
from ..models import DevCloudData, PackageData, ProfileData
from .base import BaseDevCloudProvider
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class PyPIProvider(BaseDevCloudProvider):
    """Provider for PyPI."""

    platform_id = PLATFORM_PYPI
    default_base_url = "https://pypi.org"
    supports_custom_url = False

    # PyPI publishes no per-user JSON API, so this scrapes the profile page. That makes it
    # the politest of all the providers to poll rarely.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "packages": ResourcePolicy(authenticated=3600, anonymous=3600),
    }

    # The user profile page lists every project in one document with no pagination, so a
    # single request already yields the complete list.
    _PROJECT_LINK = re.compile(r'href="/project/([a-zA-Z0-9_\-\.]+)/"')

    async def async_validate(self) -> bool:
        # PyPI user page check
        url = self.base_url / "user" / self.account_name / ""
        async with self.session.get(url, headers=self.get_headers()) as resp:
            return resp.status == 200

    async def _async_fetch_packages(self) -> list[PackageData]:
        """Scrape the profile page for project links."""
        user_url = self.base_url / "user" / self.account_name / ""

        self.request_count += 1
        async with self.session.get(user_url, headers=self.get_headers()) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()

        return [
            PackageData(name=name, url=f"https://pypi.org/project/{name}/")
            for name in sorted(set(self._PROJECT_LINK.findall(text)))
        ]

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        packages: list[PackageData] = await self.async_resource(
            "packages", self._async_fetch_packages, []
        )

        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://pypi.org/user/{self.account_name}/",
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
            scheduling=self.scheduler.diagnostics(),
            resources=self.scheduler.persisted_state(),
            collected=self.collected_resources(),
        )
