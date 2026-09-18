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

    SEARCH_URL = "https://azuresearch-usnc.nuget.org"

    def get_headers(self) -> dict[str, str]:
        """Return headers with X-NuGet-ApiKey if token is provided."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-DevCloud/1.0",
        }
        if self.api_token:
            headers["X-NuGet-ApiKey"] = self.api_token
        return headers

    async def async_validate(self) -> bool:
        # Check either NuGet search API for packages or public profile page
        search_url = f"{self.SEARCH_URL}/query?q=owner:{self.account_name}&take=1"
        try:
            data, _ = await self.async_get_json(search_url, use_etag=False)
            if isinstance(data, dict) and "data" in data and data.get("totalHits", 0) > 0:
                return True
        except Exception:  # noqa: BLE001, S110
            pass

        # Fallback to checking public profile page on nuget.org
        profile_url = f"https://www.nuget.org/profiles/{self.account_name}"
        async with self.session.get(
            profile_url, headers={"User-Agent": "HomeAssistant-DevCloud/1.0"}
        ) as resp:
            return resp.status == 200

    async def async_fetch(self) -> DevCloudData:
        packages: list[PackageData] = []
        search_base = self.base_url if "azuresearch" in self.base_url else self.SEARCH_URL
        url = f"{search_base}/query?q=owner:{self.account_name}&prerelease=true&take=100"
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
            extra={"total_downloads": total_downloads},
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
        )
