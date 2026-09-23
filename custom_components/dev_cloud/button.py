"""Diagnostic buttons for Developer Cloud Services.

One button per account: make every resource due and poll now. Each resource normally
refreshes on its own schedule — repositories every fifteen minutes, the GraphQL detail walk
hourly, organisations less often than that — which is what keeps the integration inside its
API budget, but it also means a change made a moment ago may not show for an hour. This is
the override for that, for when you know something moved and want it now.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DevCloudConfigEntry
from .coordinator import DevCloudCoordinator
from .entity import DevCloudBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DevCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the DevCloud button platform."""
    coordinator = entry.runtime_data
    async_add_entities(
        [DevCloudForceRefreshButton(coordinator), DevCloudStopScrapingButton(coordinator)]
    )


class DevCloudForceRefreshButton(DevCloudBaseEntity, ButtonEntity):
    """Clear every resource's schedule and poll immediately."""

    _attr_icon = "mdi:cloud-refresh-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "force_refresh")
        self._attr_name = "Force Refresh"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What pressing this will actually refetch, and the one thing it will not."""
        if not self.coordinator.data:
            return {}
        return {
            "resources": sorted(self.coordinator.data.resources),
            # Traffic is a rotating sweep by necessity, not by schedule: four requests per
            # repository against six hundred repositories is half an hourly quota, so
            # forcing it still advances one batch rather than all of them.
            "traffic_is_swept_in_batches": "traffic" in self.coordinator.data.resources,
        }

    async def async_press(self) -> None:
        """Make everything due and refresh.

        The rate-limit stretch still applies on top: with the allowance spent, the scheduler
        holds resources off until the window resets, and this button cannot talk it out of
        that. Forcing a refresh is meant to skip the pacing, not the ceiling.
        """
        await self.coordinator.async_force_refresh()


class DevCloudStopScrapingButton(DevCloudBaseEntity, ButtonEntity):
    """Cancel the scrape that is running right now."""

    _attr_icon = "mdi:cloud-cancel-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DevCloudCoordinator) -> None:
        super().__init__(coordinator, "stop_scraping")
        self._attr_name = "Force Stop Scraping"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Whether there is currently anything to stop."""
        return {"scraping": self.coordinator.is_scraping}

    async def async_press(self) -> None:
        """Cancel the in-flight scrape, and everything running underneath it.

        Harmless when nothing is running. Nothing already fetched is discarded: each
        resource is stored the moment it succeeds, so the next poll picks up where this one
        was interrupted rather than starting over.
        """
        await self.coordinator.async_stop_scraping()
