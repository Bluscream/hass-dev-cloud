"""JSON cache export for Developer Cloud Services.

Every coordinator update dumps the complete provider snapshot to
``<config>/www/dev/<platform>/<account>.json``, which Home Assistant serves
statically at ``/local/dev/<platform>/<account>.json``.

This exists so sensors can keep *only* aggregated counts in their state
attributes: bulky lists (repositories, releases, packages, notifications, ...)
blow past HA's 16 KiB attribute limit and get written to the recorder on every
single state change. The full detail lives in the JSON file instead, which the profile
sensor links to via its ``json_url`` attribute.
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
from pathlib import Path
from typing import Any, Final

from homeassistant.core import HomeAssistant
from yarl import URL

from .models import DevCloudData

_LOGGER = logging.getLogger(__name__)

# Directory under <config>/www. Deliberately not the domain name: it appears in every
# published URL, so it stays short.
WWW_SUBDIR = "dev"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-z0-9._-]+")

# Every list in this payload is fetched to completion, so any count measuring one would be a
# second copy of len(). Those counts were removed from the model outright; the only survivor
# is running_jobs_count, which the model still needs because None ("no CI, or no token")
# carries meaning that len(running_jobs) cannot express. It is dropped here instead.
# sponsors_count/sponsoring_count stay in the payload: the API exposes no list for them.
_REDUNDANT_COUNT_FIELDS = frozenset({"running_jobs_count"})

# `/local` is served without authentication, so this file is readable by anyone who can
# reach Home Assistant. No provider puts a credential in the snapshot today, but `extra`
# dicts pass provider payloads through verbatim — one future field named `token` would be
# enough. Any key matching this is replaced before the file is written.
_SECRET_KEY_PATTERN = re.compile(
    r"token|secret|password|passwd|api[_-]?key|credential|authorization|bearer|private[_-]?key",
    re.IGNORECASE,
)
_REDACTED = "***redacted***"


# Values carrying no information. `0` and `False` are deliberately absent: a zero download
# count or `is_fork: false` is an answer, not a missing one.
_EMPTY: Final[tuple[object, ...]] = (None, "", [], {})


def _prune(value: Any) -> Any:
    """Drop keys whose value says nothing, recursively.

    Roughly 5% of a snapshot was nulls and empty lists repeated across every item. Consumers
    read a missing key exactly as they read an empty one, so use `.get(key, default)`.
    """
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in _EMPTY}
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def _redact(value: Any) -> Any:
    """Recursively replace values whose key looks like a credential."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_PATTERN.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def slugify_account(account: str) -> str:
    """Return a filesystem- and URL-safe form of an account name."""
    slug = _UNSAFE_FILENAME_CHARS.sub("_", account.strip().lower()).strip("._-")
    return slug or "account"


def build_json_url(platform: str, account: str) -> URL:
    """Return the public ``/local`` URL of an account's JSON cache file."""
    return URL("/local") / WWW_SUBDIR / platform / f"{slugify_account(account)}.json"


def _build_json_path(hass: HomeAssistant, platform: str, account: str) -> Path:
    """Return the absolute path of an account's JSON cache file."""
    return Path(hass.config.path("www", WWW_SUBDIR, platform, f"{slugify_account(account)}.json"))


def _drop_totals_with_a_list(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove any reported total whose collection is present in the same payload.

    `totals` carries a figure only for collections that were not enumerated. Publishing one
    beside its list would hand consumers two answers to the same question, and the list is
    always the authoritative one.
    """
    totals = snapshot.get("totals")
    if not isinstance(totals, dict):
        return snapshot

    snapshot["totals"] = {
        field: value for field, value in totals.items() if not snapshot.get(field)
    }
    return snapshot


def _serialize(platform: str, account: str, data: DevCloudData) -> dict[str, Any]:
    """Build the full JSON payload for one account snapshot."""
    snapshot = _prune(
        _drop_totals_with_a_list(
            _redact({k: v for k, v in asdict(data).items() if k not in _REDUNDANT_COUNT_FIELDS})
        )
    )
    return {
        "platform": platform,
        "account": account,
        "fetched_at": datetime.now(UTC).isoformat(),
        # Serialized here because JSON is a wire format; the URL type is kept in-process.
        "json_url": str(build_json_url(platform, account)),
        **snapshot,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write the payload atomically. Runs in an executor — never call from the event loop."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # Written to a sibling temp file and renamed, so a reader never sees a half-written
    # document and a crash mid-write cannot truncate the previous snapshot.
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    tmp_path = Path(tmp_name)
    try:
        # fdopen adopts the descriptor mkstemp already opened, so closing the wrapper closes
        # it exactly once. Reopening tmp_path by name would leak that original descriptor.
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            # Minified: these files are fetched by programs, not read by hand, and the
            # GitHub snapshot runs past a megabyte. default=str keeps unexpected provider
            # types (datetimes, enums) serializable rather than failing the whole dump.
            json.dump(payload, handle, separators=(",", ":"), default=str)
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
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
