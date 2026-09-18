"""Base class for Developer Cloud Services providers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from aiohttp import ClientSession

from ..models import DevCloudData

_LOGGER = logging.getLogger(__name__)


class DevCloudProviderError(Exception):
    """Base exception for provider errors."""


class DevCloudAuthError(DevCloudProviderError):
    """Raised when authentication fails."""


class DevCloudNotFoundError(DevCloudProviderError):
    """Raised when a user or resource is not found."""


class DevCloudRateLimitError(DevCloudProviderError):
    """Raised when rate limit is reached."""


class BaseDevCloudProvider(ABC):
    """Abstract base provider for developer cloud services."""

    platform_id: str = ""
    default_base_url: str = ""
    supports_custom_url: bool = False
    requires_auth: bool = False
    supports_sponsors: bool = False

    def __init__(
        self,
        session: ClientSession,
        account_name: str,
        base_url: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self.session = session
        self.account_name = account_name.strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_token = api_token.strip() if api_token else None
        # In-memory conditional request caching
        self._etags: dict[str, str] = {}
        self._cached_responses: dict[str, Any] = {}

    def get_headers(self) -> dict[str, str]:
        """Return default headers."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-DevCloud/1.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def async_get_json(
        self,
        endpoint_url: str,
        headers: dict[str, str] | None = None,
        use_etag: bool = True,
    ) -> tuple[Any, dict[str, str]]:
        """Perform a GET request with ETag support."""
        req_headers = self.get_headers()
        if headers:
            req_headers.update(headers)

        if use_etag and endpoint_url in self._etags:
            req_headers["If-None-Match"] = self._etags[endpoint_url]

        async with self.session.get(endpoint_url, headers=req_headers) as response:
            resp_headers = dict(response.headers)
            if response.status == 304:
                # Content not modified; reuse cached payload
                return self._cached_responses.get(endpoint_url), resp_headers

            if response.status in (401, 403):
                # Check for rate limiting
                remaining = resp_headers.get("X-RateLimit-Remaining") or resp_headers.get(
                    "x-ratelimit-remaining"
                )
                if remaining == "0":
                    raise DevCloudRateLimitError("Rate limit exceeded")
                text = await response.text()
                if "rate limit" in text.lower():
                    raise DevCloudRateLimitError("Rate limit exceeded")
                raise DevCloudAuthError(f"Auth error: {response.status}")

            if response.status == 404:
                raise DevCloudNotFoundError(f"Resource not found: {endpoint_url}")

            response.raise_for_status()
            data = await response.json()

            if use_etag and "ETag" in resp_headers:
                self._etags[endpoint_url] = resp_headers["ETag"]
                self._cached_responses[endpoint_url] = data

            return data, resp_headers

    @abstractmethod
    async def async_fetch(self) -> DevCloudData:
        """Fetch all account information and return a DevCloudData snapshot."""

    @abstractmethod
    async def async_validate(self) -> bool:
        """Validate account and credentials during config flow."""
