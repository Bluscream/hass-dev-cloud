"""DataUpdateCoordinator for Developer Cloud Services."""

from __future__ import annotations

import logging
from datetime import timedelta
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
)
from .events import changes
from .models import DevCloudData
from .providers import DevCloudProviderError, get_provider
from .storage import (
    async_load_dev_cloud_json,
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

    def _fire(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        """Put each detected change on the bus, tagged with the account it belongs to."""
        for event, payload in events:
            self.hass.bus.async_fire(
                event,
                {"platform": self.platform_id, "account": self.account_name, **payload},
            )

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

        _LOGGER.debug(
            "Restored %s:%s from its published snapshot", self.platform_id, self.account_name
        )

    async def _async_update_data(self) -> DevCloudData:
        """Fetch updated data from the provider."""
        if not self._restored:
            await self._async_restore()

        try:
            data = await self.provider.async_fetch()
        except DevCloudProviderError as err:
            raise UpdateFailed(f"Error communicating with {self.platform_id}: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching {self.platform_id} data: {err}") from err

        # Set before anything reads them, so a sensor refresh triggered by this update sees
        # totals matching the data it is reading.
        self.data = data
        self.totals = compute_totals(self)

        # Serialised once and used twice: diffed against the previous poll, then written.
        payload = build_snapshot(self.platform_id, self.account_name, data)
        if self.enable_events:
            self._fire(changes(self._previous, payload))
        self._previous = payload

        await async_write_snapshot(self.hass, self.platform_id, self.account_name, payload)

        return data
