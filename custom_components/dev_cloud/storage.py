"""JSON cache export for Developer Cloud Services.

Every coordinator update dumps the complete provider snapshot to
``<config>/www/dev_cloud/<platform>/<account>.json``, which Home Assistant serves
statically at ``/local/dev_cloud/<platform>/<account>.json``.

This exists so sensors can keep *only* aggregated counts in their state
attributes: bulky lists (repositories, releases, packages, notifications, ...)
blow past HA's 16 KiB attribute limit and get written to the recorder on every
single state change. The full detail lives in the JSON file instead, and each
sensor carries a ``json_url`` attribute pointing at it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .models import DevCloudData

_LOGGER = logging.getLogger(__name__)

WWW_SUBDIR = "dev_cloud"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-z0-9._-]+")

# Counts that a consumer can derive by simply measuring a list already in this payload are
# dropped — they are redundant and can only drift. Every other `*_count` field is kept
# *because* its companion list is truncated by the upstream API (open_issues/open_prs cap at
# 50 search hits, releases at 20 per repo, repos at 100) or has no list at all, so the count
# carries information the list cannot. Re-check this set if a list ever becomes complete.
_REDUNDANT_COUNT_FIELDS = frozenset({"running_jobs_count"})


def slugify_account(account: str) -> str:
    """Return a filesystem- and URL-safe form of an account name."""
    slug = _UNSAFE_FILENAME_CHARS.sub("_", account.strip().lower()).strip("._-")
    return slug or "account"


def build_json_url(platform: str, account: str) -> str:
    """Return the public ``/local`` URL of an account's JSON cache file."""
    return f"/local/{WWW_SUBDIR}/{platform}/{slugify_account(account)}.json"


def _build_json_path(hass: HomeAssistant, platform: str, account: str) -> str:
    """Return the absolute path of an account's JSON cache file."""
    return hass.config.path("www", WWW_SUBDIR, platform, f"{slugify_account(account)}.json")


def _serialize(platform: str, account: str, data: DevCloudData) -> dict[str, Any]:
    """Build the full JSON payload for one account snapshot."""
    snapshot = {k: v for k, v in asdict(data).items() if k not in _REDUNDANT_COUNT_FIELDS}
    return {
        "platform": platform,
        "account": account,
        "fetched_at": datetime.now(UTC).isoformat(),
        "json_url": build_json_url(platform, account),
        **snapshot,
    }


def _write_json(path: str, payload: dict[str, Any]) -> None:
    """Write the payload atomically. Runs in an executor — never call from the event loop."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            # default=str keeps unexpected provider types (datetimes, enums) serializable
            # rather than failing the whole dump.
            json.dump(payload, handle, indent=2, default=str)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def async_dump_dev_cloud_json(
    hass: HomeAssistant,
    platform: str,
    account: str,
    data: DevCloudData,
) -> bool:
    """Dump a snapshot to the www cache. Returns whether the write succeeded.

    Never raises: a failed dump must not fail the coordinator update.
    """
    path = _build_json_path(hass, platform, account)
    try:
        payload = _serialize(platform, account, data)
        await hass.async_add_executor_job(_write_json, path, payload)
    except Exception as err:
        # Broad by design: a cache write must never break polling.
        _LOGGER.warning(
            "Could not write JSON cache for %s:%s to %s: %s", platform, account, path, err
        )
        return False
    return True
