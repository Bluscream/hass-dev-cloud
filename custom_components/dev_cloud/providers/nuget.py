"""NuGet provider implementation."""

from __future__ import annotations

import logging

from ..const import PLATFORM_NUGET
from ..models import DevCloudData, PackageData, ProfileData
from .base import BaseDevCloudProvider
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class NuGetProvider(BaseDevCloudProvider):
    """Provider for NuGet packages."""

    platform_id = PLATFORM_NUGET
    default_base_url = "https://azuresearch-usnc.nuget.org"
    supports_custom_url = False

    SEARCH_URL = "https://azuresearch-usnc.nuget.org"

    # The NuGet search index is a public CDN; download counts update on its own schedule,
    # so polling faster than this only re-reads the same numbers.
    resource_policies = {
        "packages": ResourcePolicy(authenticated=1800, anonymous=1800),
    }

    SEARCH_PAGE_SIZE = 100

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

    async def _async_fetch_packages(self) -> list[PackageData]:
        """Every package owned by the account, following NuGet's `skip` offset paging."""
        search_base = self.base_url if "azuresearch" in self.base_url else self.SEARCH_URL
        url = f"{search_base}/query?q=owner:{self.account_name}&prerelease=true"

        items = await self.async_get_all_offset(
            url,
            extract=lambda p: p.get("data", []) if isinstance(p, dict) else [],
            page_size=self.SEARCH_PAGE_SIZE,
            offset_param="skip",
            size_param="take",
        )

        return [
            PackageData(
                name=item.get("id", ""),
                version=item.get("version"),
                url=f"https://www.nuget.org/packages/{item.get('id', '')}",
                description=item.get("description"),
                downloads_total=item.get("totalDownloads", 0),
                extra={"authors": item.get("authors"), "tags": item.get("tags")},
            )
            for item in items
        ]

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        packages = await self.async_resource("packages", self._async_fetch_packages, [])

        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://www.nuget.org/profiles/{self.account_name}",
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
            scheduling=self.scheduler.diagnostics(),
        )
