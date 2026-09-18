"""NuGet provider implementation."""

from __future__ import annotations

import logging

from ..const import PLATFORM_NUGET
from ..models import DevCloudData, PackageData, ProfileData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class NuGetProvider(BaseDevCloudProvider):
    """Provider for NuGet packages."""

    platform_id = PLATFORM_NUGET
    default_base_url = "https://azuresearch-usnc.nuget.org"
    supports_custom_url = False

    async def async_validate(self) -> bool:
        url = f"{self.base_url}/query?q=owner:{self.account_name}&take=1"
        data, _ = await self.async_get_json(url, use_etag=False)
        return isinstance(data, dict) and "data" in data

    async def async_fetch(self) -> DevCloudData:
        packages: list[PackageData] = []
        url = f"{self.base_url}/query?q=owner:{self.account_name}&prerelease=true&take=100"
        total_downloads = 0
        try:
            data, _ = await self.async_get_json(url)
            items = data.get("data", []) if isinstance(data, dict) else []
            for item in items:
                name = item.get("id", "")
                version = item.get("version")
                desc = item.get("description")
                dl = item.get("totalDownloads", 0)
                total_downloads += dl

                packages.append(
                    PackageData(
                        name=name,
                        version=version,
                        url=f"https://www.nuget.org/packages/{name}",
                        description=desc,
                        downloads_total=dl,
                        extra={
                            "authors": item.get("authors"),
                            "tags": item.get("tags"),
                        },
                    )
                )
        except Exception as err:
            _LOGGER.warning("Error fetching NuGet packages for %s: %s", self.account_name, err)

        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://www.nuget.org/profiles/{self.account_name}",
            public_repos=len(packages),
            extra={"total_downloads": total_downloads},
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
        )
