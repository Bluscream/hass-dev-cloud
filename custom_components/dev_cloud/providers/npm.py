"""NPM provider implementation."""

from __future__ import annotations

import logging

from ..const import PLATFORM_NPM
from ..models import DevCloudData, OrgData, PackageData, ProfileData
from .base import BaseDevCloudProvider

_LOGGER = logging.getLogger(__name__)


class NPMProvider(BaseDevCloudProvider):
    """Provider for NPM packages."""

    platform_id = PLATFORM_NPM
    default_base_url = "https://registry.npmjs.org"
    supports_custom_url = False

    async def async_validate(self) -> bool:
        url = f"{self.base_url}/-/v1/search?text=maintainer:{self.account_name}&size=1"
        data, _ = await self.async_get_json(url, use_etag=False)
        return isinstance(data, dict) and "objects" in data

    async def async_fetch(self) -> DevCloudData:
        packages: list[PackageData] = []
        search_url = f"{self.base_url}/-/v1/search?text=maintainer:{self.account_name}&size=250"
        try:
            data, _ = await self.async_get_json(search_url)
            objects = data.get("objects", []) if isinstance(data, dict) else []
            for obj in objects:
                pkg_info = obj.get("package", {})
                name = pkg_info.get("name", "")
                version = pkg_info.get("version")
                desc = pkg_info.get("description")
                links = pkg_info.get("links", {})
                npm_url = links.get("npm") or f"https://www.npmjs.com/package/{name}"
                date = pkg_info.get("date")

                packages.append(
                    PackageData(
                        name=name,
                        version=version,
                        url=npm_url,
                        description=desc,
                        updated_at=date,
                        extra={
                            "publisher": pkg_info.get("publisher", {}).get("username"),
                            "repository": links.get("repository"),
                        },
                    )
                )
        except Exception as err:
            _LOGGER.warning("Error fetching NPM packages for %s: %s", self.account_name, err)

        # 2. Fetch Organizations (org memberships / teams)
        orgs: list[OrgData] = []
        orgs_url = f"{self.base_url}/-/org/{self.account_name}/user"
        try:
            # First try with default headers
            try:
                org_json, _ = await self.async_get_json(orgs_url)
            except Exception:
                # Fallback to anonymous query if token lacks org scope (403)
                anon_headers = {
                    "User-Agent": "HomeAssistant-DevCloud/1.0",
                    "Accept": "application/json",
                    "Authorization": "",
                }
                org_json, _ = await self.async_get_json(orgs_url, headers=anon_headers)

            if isinstance(org_json, dict):
                for org_name, role in org_json.items():
                    orgs.append(
                        OrgData(
                            name=org_name,
                            display_name=org_name,
                            url=f"https://www.npmjs.com/org/{org_name}",
                            description=f"Role: {role}",
                        )
                    )
        except Exception as err:
            _LOGGER.debug("Could not fetch NPM orgs for %s: %s", self.account_name, err)

        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://www.npmjs.com/~{self.account_name}",
            avatar_url=f"https://avatars.githubusercontent.com/{self.account_name}",
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
            orgs=orgs,
        )
