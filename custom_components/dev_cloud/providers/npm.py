"""NPM provider implementation."""

from __future__ import annotations

import logging
from typing import ClassVar

from ..const import PLATFORM_NPM
from ..models import DevCloudData, OrgData, PackageData, ProfileData
from .base import BaseDevCloudProvider
from .scheduling import ResourcePolicy

_LOGGER = logging.getLogger(__name__)


class NPMProvider(BaseDevCloudProvider):
    """Provider for NPM packages."""

    platform_id = PLATFORM_NPM
    default_base_url = "https://registry.npmjs.org"
    supports_custom_url = False

    # The npm registry is unauthenticated and unmetered here, but package metadata is
    # near-static, so there is no reason to poll it often.
    # No `profile` resource: npm's user document (/-/user/org.couchdb.user:<name>)
    # returns {"ok": false} even with a valid token, so there is nothing to fetch and
    # the profile is synthesised from the account name at no request cost.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {
        "packages": ResourcePolicy(authenticated=1800, anonymous=1800),
        "orgs": ResourcePolicy(authenticated=3600, anonymous=3600),
    }

    # npm's search endpoint refuses a page larger than this.
    SEARCH_PAGE_SIZE = 250

    async def async_validate(self) -> bool:
        url = (self.base_url / "-/v1/search").with_query(
            {"text": f"maintainer:{self.account_name}", "size": 1}
        )
        data, _ = await self.async_get_json(url, use_etag=False)
        return isinstance(data, dict) and "objects" in data

    async def _async_fetch_packages(self) -> list[PackageData]:
        """Every package the account maintains, following npm's `from` offset paging."""
        url = (self.base_url / "-/v1/search").with_query(
            {"text": f"maintainer:{self.account_name}"}
        )
        objects = await self.async_get_all_offset(
            url,
            extract=lambda p: p.get("objects", []) if isinstance(p, dict) else [],
            page_size=self.SEARCH_PAGE_SIZE,
            offset_param="from",
            size_param="size",
        )

        packages: list[PackageData] = []
        for obj in objects:
            info = obj.get("package", {})
            name = info.get("name", "")
            links = info.get("links", {})
            packages.append(
                PackageData(
                    name=name,
                    version=info.get("version"),
                    url=links.get("npm") or f"https://www.npmjs.com/package/{name}",
                    description=info.get("description"),
                    updated_at=info.get("date"),
                    extra={
                        "publisher": info.get("publisher", {}).get("username"),
                        "repository": links.get("repository"),
                    },
                )
            )
        return packages

    async def _async_fetch_orgs(self) -> list[OrgData]:
        """Org memberships. Returns a single {org: role} mapping, so there is no paging."""
        orgs_url = self.base_url / "-/org" / self.account_name / "user"
        try:
            org_json, _ = await self.async_get_json(orgs_url)
        except Exception as err:
            _LOGGER.debug(
                "Authenticated npm org lookup failed for %s, retrying anonymously: %s",
                self.account_name,
                err,
            )
            # Fall back to an anonymous query when the token lacks the org scope (403).
            anon_headers = {
                "User-Agent": "HomeAssistant-DevCloud/1.0",
                "Accept": "application/json",
                "Authorization": "",
            }
            org_json, _ = await self.async_get_json(orgs_url, headers=anon_headers)

        if not isinstance(org_json, dict):
            return []
        return [
            OrgData(
                name=org_name,
                display_name=org_name,
                url=f"https://www.npmjs.com/org/{org_name}",
                description=f"Role: {role}",
            )
            for org_name, role in org_json.items()
        ]

    async def async_fetch(self) -> DevCloudData:
        """Assemble a snapshot, refreshing only the resources that are due."""
        packages: list[PackageData] = await self.async_resource(
            "packages", self._async_fetch_packages, []
        )
        orgs: list[OrgData] = await self.async_resource("orgs", self._async_fetch_orgs, [])

        # No avatar: this was guessed as avatars.githubusercontent.com/<account>, which
        # assumes the npm name belongs to the same person on GitHub. It does not —
        # github.com/bluscream1 does not exist, yet that URL still serves an image, so the
        # sensor showed a face belonging to nobody. npm exposes no avatar of its own.
        profile = ProfileData(
            username=self.account_name,
            display_name=self.account_name,
            profile_url=f"https://www.npmjs.com/~{self.account_name}",
        )

        return DevCloudData(
            profile=profile,
            packages=packages,
            orgs=orgs,
            resources=self.scheduler.persisted_state(),
            collected=self.collected_resources(),
        )
