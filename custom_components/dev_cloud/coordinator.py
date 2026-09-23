"""DataUpdateCoordinator for Developer Cloud Services."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from yarl import URL

from .aggregation import Totals, compute_totals
from .const import (
    CONF_ACCOUNT_NAME,
    CONF_API_TOKEN,
    CONF_DETAILED_RESULTS,
    CONF_ENABLE_EVENTS,
    CONF_INCLUDE_NON_OWNED_ORGS,
    CONF_INSTANCE_URL,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    DEFAULT_DETAILED_RESULTS,
    DEFAULT_ENABLE_EVENTS,
    DEFAULT_INCLUDE_NON_OWNED_ORGS,
    DEFAULT_SCAN_INTERVAL_ANONYMOUS,
    DEFAULT_SCAN_INTERVAL_AUTHENTICATED,
    DOMAIN,
    EVENT_DEV_CLOUD_NOTIFICATION,
    EVENT_DEV_CLOUD_UPDATE,
)
from .events import Diff, chunked, diff
from .models import DevCloudData
from .providers import DevCloudProviderError, get_provider
from .storage import (
    async_load_dev_cloud_json,
    async_write_page,
    async_write_snapshot,
    build_json_url,
    build_snapshot,
)

_LOGGER = logging.getLogger(__name__)


class DevCloudCoordinator(DataUpdateCoordinator[DevCloudData]):
    """Coordinates polling for a single platform:account entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.platform_id: str = entry.data[CONF_PLATFORM]
        self.account_name: str = entry.data[CONF_ACCOUNT_NAME]
        self.instance_url: str | None = entry.data.get(CONF_INSTANCE_URL)
        self.api_token: str | None = entry.data.get(CONF_API_TOKEN)

        default_interval = (
            DEFAULT_SCAN_INTERVAL_AUTHENTICATED
            if self.api_token
            else DEFAULT_SCAN_INTERVAL_ANONYMOUS
        )
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, default_interval)

        self.enable_events: bool = entry.options.get(CONF_ENABLE_EVENTS, DEFAULT_ENABLE_EVENTS)
        # When False, only organisations this account owns contribute to the totals.
        self.include_non_owned_orgs: bool = entry.options.get(
            CONF_INCLUDE_NON_OWNED_ORGS, DEFAULT_INCLUDE_NON_OWNED_ORGS
        )

        # Passed to the provider: it decides how much to enumerate, since only it knows
        # which of its endpoints are cheap.
        self.detailed_results: bool = entry.options.get(
            CONF_DETAILED_RESULTS, DEFAULT_DETAILED_RESULTS
        )

        session = async_get_clientsession(hass)
        self.provider = get_provider(
            platform=self.platform_id,
            session=session,
            account_name=self.account_name,
            instance_url=self.instance_url,
            api_token=self.api_token,
            detailed=self.detailed_results,
            include_non_owned_orgs=self.include_non_owned_orgs,
        )

        # Public URL of this account's full JSON snapshot, exposed on every sensor so the
        # bulky lists stripped from state attributes stay reachable.
        self.json_url: URL = build_json_url(self.platform_id, self.account_name)

        # Restored from the published snapshot on the first poll, so a reload resumes the
        # previous schedule rather than treating every resource as due.
        self._restored = False

        # Recomputed once per successful update; sensors read it instead of walking every
        # repository on each property access.
        self.totals: Totals | None = None

        # When the provider last returned a full result, surfaced by the Last Updated
        # diagnostic sensor. Seeded from the restored snapshot so a reload reports when the
        # data was actually gathered rather than blank until the next poll.
        self.last_updated: datetime | None = None

        # The in-flight provider fetch, held so it can be cancelled. The coordinator's own
        # update method is not cancellable from outside, so the fetch runs as its own task
        # and this is the handle on it.
        self._fetch_task: asyncio.Task[DevCloudData] | None = None
        # Set only by async_stop_scraping, so a cancellation this class asked for can be
        # told apart from Home Assistant shutting the coordinator down - one becomes a
        # failed update, the other has to propagate.
        self._stop_requested = False

        # The previous poll's serialised snapshot, diffed against the next one. Serialised
        # rather than live, because the provider mutates its cached objects in place — a
        # reference to the previous result would end up comparing objects against themselves.
        self._previous: dict[str, Any] | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({self.platform_id}:{self.account_name})",
            update_interval=timedelta(seconds=scan_interval),
        )

    def _fire(self, result: Diff, run_id: str) -> None:
        """Put one poll's changes on the bus, tagged with the account they belong to.

        Updates go out in size-bounded chunks carrying their position in the run, so a
        consumer can tell a three-part digest from three unrelated ones and knows when it
        has seen the whole poll. Notifications go out one at a time.
        """
        envelope = {
            "platform": self.platform_id,
            "account": self.account_name,
            "run_id": run_id,
        }

        for change in result.notifications:
            self.hass.bus.async_fire(EVENT_DEV_CLOUD_NOTIFICATION, {**envelope, **change})

        batches = chunked(result.updates)
        for index, batch in enumerate(batches, start=1):
            self.hass.bus.async_fire(
                EVENT_DEV_CLOUD_UPDATE,
                {
                    **envelope,
                    "chunk": index,
                    "chunks": len(batches),
                    "count": len(batch),
                    "total": len(result.updates),
                    "changes": batch,
                },
            )

    @property
    def is_scraping(self) -> bool:
        """Whether a fetch is in flight right now."""
        return self._fetch_task is not None and not self._fetch_task.done()

    async def async_stop_scraping(self) -> bool:
        """Cancel the in-flight scrape. Returns whether there was one to cancel.

        Cancelling the outer task propagates into whatever it is awaiting, so every
        concurrent sub-request underneath it - the organisation walks, the traffic sweep,
        the running-jobs fan-out - goes down with it rather than being left to finish
        against an API nobody is waiting on any more.

        Work already completed is not thrown away: each resource stores its value the
        moment it succeeds, so a cancelled scrape keeps whatever it had got through and the
        next poll resumes from there.
        """
        task = self._fetch_task
        if task is None or task.done():
            _LOGGER.debug("No scrape in flight for %s:%s", self.platform_id, self.account_name)
            return False

        _LOGGER.info(
            "Cancelling the in-flight scrape of %s:%s on request",
            self.platform_id,
            self.account_name,
        )
        self._stop_requested = True
        task.cancel()
        return True

    async def async_force_refresh(self) -> None:
        """Clear every resource's schedule and poll now.

        Resources are normally paced against the API budget, which is what keeps the
        integration inside it but also means a change can take an hour to show. This is the
        override, exposed as the Force Refresh button.
        """
        _LOGGER.debug("Forcing a full refresh of %s:%s", self.platform_id, self.account_name)
        self.provider.scheduler.reset()
        await self.async_request_refresh()

    async def _async_restore(self) -> None:
        """Seed the provider from the last published snapshot, once per process."""
        self._restored = True
        snapshot = await async_load_dev_cloud_json(self.hass, self.platform_id, self.account_name)
        if not snapshot:
            return

        try:
            self.provider.restore(snapshot)
        except Exception as err:
            # A snapshot from an older schema must never stop the integration loading.
            _LOGGER.debug("Ignoring unusable snapshot for %s: %s", self.account_name, err)
            return

        # The restored file is also the baseline for change detection, so a restart does
        # not lose the events that happened while it was down.
        self._previous = snapshot

        fetched_at = snapshot.get("fetched_at")
        if isinstance(fetched_at, str):
            with contextlib.suppress(ValueError):
                self.last_updated = datetime.fromisoformat(fetched_at)

        _LOGGER.debug(
            "Restored %s:%s from its published snapshot", self.platform_id, self.account_name
        )

    async def _async_update_data(self) -> DevCloudData:
        """Fetch updated data from the provider."""
        if not self._restored:
            await self._async_restore()

        # Run as a task rather than awaiting the coroutine directly, so the Force Stop
        # button has something to cancel. The reference is held for the duration; a bare
        # create_task can be collected mid-flight.
        self._fetch_task = asyncio.create_task(self.provider.async_fetch())
        try:
            data = await self._fetch_task
        except asyncio.CancelledError:
            if not self._stop_requested:
                # Home Assistant is cancelling us, not the other way round. Cancellation is
                # cooperative and swallowing this would strand the shutdown.
                raise
            self._stop_requested = False
            raise UpdateFailed(
                f"Scrape of {self.platform_id}:{self.account_name} was cancelled on request"
            ) from None
        except DevCloudProviderError as err:
            raise UpdateFailed(f"Error communicating with {self.platform_id}: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching {self.platform_id} data: {err}") from err
        finally:
            self._fetch_task = None

        # Set before anything reads them, so a sensor refresh triggered by this update sees
        # totals matching the data it is reading.
        self.data = data
        self.totals = compute_totals(self)
        self.last_updated = datetime.now(UTC)

        # Serialised once and used twice: diffed against the previous poll, then written.
        # The diff runs here, on the finished document, rather than anywhere inside the
        # fetch: a poll is routinely partial, and a resource that was not due reuses its
        # previous value, which compares equal and reports nothing.
        payload = build_snapshot(self.platform_id, self.account_name, data)
        if self.enable_events:
            # The snapshot's own timestamp identifies the run: unique per poll, already in
            # the payload, and meaningful to a human reading an event in the log.
            self._fire(diff(self._previous, payload), str(payload["fetched_at"]))
        self._previous = payload

        await async_write_snapshot(self.hass, self.platform_id, self.account_name, payload)
        # After the snapshot, so a first-ever poll writes a page that already lists it among
        # its siblings.
        await async_write_page(self.hass, self.platform_id, self.account_name)

        return data
