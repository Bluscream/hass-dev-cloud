"""Tests for cancelling an in-flight scrape.

The coordinator's own update method cannot be cancelled from outside, so the provider fetch
runs as its own task and the Force Stop Scraping button cancels that. The delicate part is
telling a cancellation *this* code asked for apart from Home Assistant shutting down: one
becomes a failed update, the other has to propagate or the shutdown strands.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from dev_cloud.coordinator import DevCloudCoordinator
from dev_cloud.models import DevCloudData, ProfileData
from homeassistant.helpers.update_coordinator import UpdateFailed


class _StallingProvider:
    """A provider whose fetch never finishes unless it is cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.scheduler = MagicMock()

    async def async_fetch(self) -> DevCloudData:
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return DevCloudData(profile=ProfileData(username="x"))

    def restore(self, snapshot: Any) -> None:
        return None


def _coordinator(provider: Any) -> Any:
    """A coordinator with just enough wired up to run one update."""
    coordinator = DevCloudCoordinator.__new__(DevCloudCoordinator)
    coordinator.provider = provider
    coordinator.platform_id = "github"
    coordinator.account_name = "Bluscream"
    coordinator._restored = True
    coordinator._fetch_task = None
    coordinator._stop_requested = False
    return coordinator


async def test_nothing_to_stop_is_not_an_error() -> None:
    coordinator = _coordinator(_StallingProvider())
    assert coordinator.is_scraping is False
    assert await coordinator.async_stop_scraping() is False


async def test_a_running_scrape_can_be_cancelled() -> None:
    provider = _StallingProvider()
    coordinator = _coordinator(provider)

    update = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    assert coordinator.is_scraping is True

    assert await coordinator.async_stop_scraping() is True

    with pytest.raises(UpdateFailed, match="cancelled on request"):
        await asyncio.wait_for(update, timeout=2)

    assert provider.cancelled, "the cancellation must reach the provider, not just the task"
    assert coordinator.is_scraping is False


async def test_a_shutdown_cancellation_is_not_swallowed() -> None:
    """Cancellation is cooperative; turning Home Assistant's own into UpdateFailed would
    leave the shutdown waiting on a task that reported success at being interrupted."""
    provider = _StallingProvider()
    coordinator = _coordinator(provider)

    update = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    update.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(update, timeout=2)


async def test_the_stop_flag_does_not_leak_into_the_next_update() -> None:
    """A later genuine shutdown must not be mistaken for the stop button being pressed."""
    provider = _StallingProvider()
    coordinator = _coordinator(provider)

    update = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    await coordinator.async_stop_scraping()
    with pytest.raises(UpdateFailed):
        await asyncio.wait_for(update, timeout=2)

    assert coordinator._stop_requested is False

    second = _StallingProvider()
    coordinator.provider = second
    again = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(second.started.wait(), timeout=2)
    again.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(again, timeout=2)
