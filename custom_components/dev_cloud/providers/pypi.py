"""PyPI (pip) provider implementation."""

from __future__ import annotations

import logging

from ..const import PLATFORM_PYPI
from ..models import DevCloudData, PackageData, ProfileData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class PyPIProvider(BaseDevCloudProvider):
    """Provider for PyPI."""

    platform_id = PLATFORM_PYPI
    default_base_url = "https://pypi.org"
    supports_custom_url = False

    async def async_validate(self) -> bool:
        # PyPI user page check
        url = f"{self.base_url}/user/{self.account_name}/"
        async with self.session.get(url, headers=self.get_headers()) as resp:
            return resp.status == 200

    async def async_fetch(self) -> DevCloudData:
        packages: list[PackageData] = []
        # Attempt to scrape packages list from user page or simple index
        user_url = f"{self.base_url}/user/{self.account_name}/"
        try:
            async with self.session.get(user_url, headers=self.get_headers()) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    import re

                    # Look for /project/<name>/ links on user profile
                    matches = set(re.findall(r'href="/project/([a-zA-Z0-9_\-\.]+)/"', text))
                    for pkg_name in matches:
                        packages.append(
                            PackageData(
                                name=pkg_name,
                                url=f"https://pypi.org/project/{pkg_name}/",
                            )
                        )
        except Exception as err:
            _LOGGER.warning("Error fetching PyPI user page for %s: %s", self.account_name, err)

        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://pypi.org/user/{self.account_name}/",
            public_repos=len(packages),
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
        )
