"""Base class for Developer Cloud Services providers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, cast

from aiohttp import ClientSession
from yarl import URL

from ..models import DevCloudData, from_dict
from .scheduling import PageWalker, ResourcePolicy, ResourceScheduler

_LOGGER = logging.getLogger(__name__)

# Safety valve so a misbehaving endpoint that always returns a full page cannot loop forever.
# At 100 items per page this allows 10k items per collection.
MAX_PAGES = 100


async def async_map_limited[T, R](
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    limit: int,
) -> list[R | BaseException]:
    """Run ``worker`` over ``items`` with bounded concurrency, collecting exceptions."""
    semaphore = asyncio.Semaphore(limit)

    async def _run(item: T) -> R:
        async with semaphore:
            return await worker(item)

    return await asyncio.gather(*(_run(item) for item in items), return_exceptions=True)


async def async_collect_running_jobs[T](
    items: Sequence[T],
    worker: Callable[[T], Awaitable[list[dict[str, Any]]]],
    limit: int,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Aggregate per-repository running-job queries into a count and a flat list.

    Returns ``(None, [])`` when no query produced a usable answer — no candidate
    repositories, an unsupported endpoint, or missing credentials. Callers propagate that
    ``None`` so the Running Jobs sensor is omitted entirely instead of reporting a
    misleading zero.
    """
    if not items:
        return None, []

    results = await async_map_limited(items, worker, limit)

    jobs: list[dict[str, Any]] = []
    any_succeeded = False
    for result in results:
        if isinstance(result, BaseException):
            _LOGGER.debug("Running jobs query failed: %s", result)
            continue
        any_succeeded = True
        jobs.extend(result)

    if not any_succeeded:
        return None, []
    return len(jobs), jobs


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

    #: How often each resource may be refreshed, declared by the subclass. Resources absent
    #: from this mapping are refreshed on every poll. See `scheduling.ResourceScheduler`.
    resource_policies: ClassVar[dict[str, ResourcePolicy]] = {}

    def __init__(
        self,
        session: ClientSession,
        account_name: str,
        base_url: str | None = None,
        api_token: str | None = None,
        detailed: bool = True,
    ) -> None:
        self.session = session
        self.account_name = account_name.strip()
        # Parsed once at the boundary; every endpoint is built from it with yarl's
        # joining and query helpers rather than string formatting.
        self.base_url = URL(base_url or self.default_base_url)
        self.api_token = api_token.strip() if api_token else None
        # False asks the provider for summary totals instead of enumerating every item.
        # Which endpoints that spares is provider-specific, so each one decides.
        self.detailed = detailed
        # In-memory conditional request caching & TTL caching
        self._etags: dict[str, str] = {}
        self._cached_responses: dict[str, Any] = {}

        self.scheduler = ResourceScheduler(
            policies=dict(self.resource_policies), has_token=bool(self.api_token)
        )
        # Every HTTP call increments this, so a resource's real cost can be measured rather
        # than guessed.
        self.request_count = 0
        self._resource_values: dict[str, Any] = {}

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Re-seed cached values and schedule timestamps from a persisted snapshot.

        A reload otherwise begins with every resource due and every cache empty, so the
        integration refetches the entire account each time it is redeployed.
        """
        self.scheduler.restore(snapshot.get("resources") or {})

        data = from_dict(DevCloudData, snapshot)
        for key, value in (
            ("profile", data.profile),
            ("repos", data.repos),
            ("orgs", data.orgs),
            ("pastes", data.pastes),
            ("packages", data.packages),
            ("notifications", data.notifications),
            ("issues", data.open_issues),
            ("prs", data.open_prs),
            ("releases", data.releases),
        ):
            if value:
                self._resource_values[key] = value

        # Organisation repositories live inside their organisation, so the derived resource
        # is reconstructed from them rather than stored twice.
        org_repos = {org.name: org.repos for org in data.orgs if org.repos}
        if org_repos:
            self._resource_values["org_repos"] = org_repos

    async def async_resource[T](
        self,
        key: str,
        fetcher: Callable[[], Awaitable[T]],
        default: T,
    ) -> T:
        """Refresh `key` if its schedule allows, otherwise reuse the last known value.

        Failures are logged and fall back to the previous value too, so one flaky endpoint
        cannot empty a list that was previously complete.
        """
        # Values are stored heterogeneously by key, so the cast restores the caller's T.
        if not self.scheduler.should_fetch(key):
            return cast("T", self._resource_values.get(key, default))

        before = self.request_count
        try:
            value = await fetcher()
        except DevCloudNotFoundError as err:
            # A 404 here means the endpoint does not exist on this instance or the token
            # lacks the scope — a permanent condition, not an incident. Debug, not warning.
            _LOGGER.debug(
                "Resource %s unavailable for %s:%s: %s",
                key,
                self.platform_id,
                self.account_name,
                err,
            )
            self.scheduler.record_failure(key)
            return cast("T", self._resource_values.get(key, default))
        except Exception as err:
            _LOGGER.warning(
                "Error fetching %s for %s:%s: %s", key, self.platform_id, self.account_name, err
            )
            self.scheduler.record_failure(key)
            return cast("T", self._resource_values.get(key, default))

        self.scheduler.record_fetch(key, self.request_count - before)
        self._resource_values[key] = value
        return value

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
        endpoint_url: URL,
        headers: dict[str, str] | None = None,
        use_etag: bool = True,
    ) -> tuple[Any, dict[str, str]]:
        """Perform a GET request with ETag support."""
        req_headers = self.get_headers()
        if headers:
            req_headers.update(headers)

        cache_key = str(endpoint_url)
        if use_etag and cache_key in self._etags:
            req_headers["If-None-Match"] = self._etags[cache_key]

        self.request_count += 1
        async with self.session.get(endpoint_url, headers=req_headers) as response:
            resp_headers = dict(response.headers)
            if response.status == 304:
                # Content not modified; reuse cached payload
                return self._cached_responses.get(cache_key), resp_headers

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
                self._etags[cache_key] = resp_headers["ETag"]
                self._cached_responses[cache_key] = data

            return data, resp_headers

    async def async_get_all_pages(
        self,
        url: URL,
        page_size: int = 100,
        page_param: str = "page",
        size_param: str = "per_page",
        first_page: int = 1,
        extract: Callable[[Any], list[Any]] | None = None,
    ) -> list[Any]:
        """Follow classic ``page``/``per_page`` pagination until the API runs out of items.

        Stops on a short page, an empty page, or ``MAX_PAGES`` — never on a fixed item
        count, so the returned list is everything the endpoint will give us. Lists in the
        JSON dump must be complete, because every count is derived from them.

        ``extract`` pulls the item list out of an envelope response (Docker Hub's
        ``{"results": [...]}``); omit it for endpoints that return a bare array.
        """
        walker = PageWalker(str(url), page_size)
        items: list[Any] = []

        for page in range(first_page, first_page + MAX_PAGES):
            page_url = url.update_query({size_param: page_size, page_param: page})
            payload, _ = await self.async_get_json(page_url, use_etag=False)

            batch = extract(payload) if extract else payload
            if not isinstance(batch, list) or not walker.accept(batch):
                break

            items.extend(batch)
            if walker.is_last(batch):
                break
        else:
            _LOGGER.warning(
                "Pagination for %s stopped at the %d page safety limit; list may be incomplete",
                url,
                MAX_PAGES,
            )

        return items

    async def async_get_all_offset(
        self,
        url: URL,
        extract: Callable[[Any], list[Any]],
        page_size: int,
        offset_param: str = "from",
        size_param: str = "size",
    ) -> list[Any]:
        """Follow offset/limit pagination (npm's `from`, NuGet's `skip`) to completion.

        The counterpart to `async_get_all_pages` for search APIs that take a starting
        offset rather than a page number.
        """
        walker = PageWalker(str(url), page_size)
        items: list[Any] = []

        for page in range(MAX_PAGES):
            page_url = url.update_query({size_param: page_size, offset_param: page * page_size})
            payload, _ = await self.async_get_json(page_url, use_etag=False)

            batch = extract(payload)
            if not isinstance(batch, list) or not walker.accept(batch):
                break

            items.extend(batch)
            if walker.is_last(batch):
                break
        else:
            _LOGGER.warning(
                "Offset pagination for %s stopped at the %d page safety limit; "
                "list may be incomplete",
                url,
                MAX_PAGES,
            )

        return items

    @abstractmethod
    async def async_fetch(self) -> DevCloudData:
        """Fetch all account information and return a DevCloudData snapshot."""

    @abstractmethod
    async def async_validate(self) -> bool:
        """Validate account and credentials during config flow."""
