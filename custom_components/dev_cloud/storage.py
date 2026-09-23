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

# The browsable index written beside the snapshots in each platform directory. Shipped as a
# real .html asset rather than a Python string: it is a page, editable and viewable as one,
# and a 400-line heredoc in this module would be neither.
_INDEX_TEMPLATE = Path(__file__).parent / "www" / "index.html"
_INDEX_NAME = "index.html"
#: Replaced with the JSON array of account slugs found in the directory.
_ACCOUNTS_PLACEHOLDER = "__ACCOUNTS__"

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
    if isinstance(value, set | frozenset):
        # Sets are used in-process for membership; JSON has no such type, and sorting keeps
        # the file stable between writes instead of reordering on every dump.
        return sorted(str(item) for item in value)
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


def _drop_repo_counts_with_lists(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Drop a repository's `open_issues` count once its issues and PRs are both listed.

    GitHub's `open_issues_count` counts issues *and* pull requests, so with both lists
    present it is exactly len(issues) + len(prs). Platforms that never enumerate them keep
    the count, because there it is the only answer available.
    """
    for collection in ("repos", "orgs"):
        for item in snapshot.get(collection) or []:
            if not isinstance(item, dict):
                continue
            if collection == "orgs":
                _drop_repo_counts_with_lists(item)
                continue
            if item.get("issues") and item.get("prs"):
                item.pop("open_issues", None)
    return snapshot


def build_snapshot(platform: str, account: str, data: DevCloudData) -> dict[str, Any]:
    """Build the full JSON payload for one account snapshot.

    Public because the coordinator diffs this against the previous poll's payload — the
    same document that gets written, serialised once and used twice.
    """
    snapshot = _prune(
        _drop_repo_counts_with_lists(
            _drop_totals_with_a_list(
                # running_jobs_count is kept even though running_jobs is a list beside it.
                # None means "this platform has no CI, or there is no token" and an empty
                # list cannot say that -- dropping it was what left the Running Jobs sensor
                # unrestorable, and so marked "no longer provided" after every reload.
                _redact(asdict(data))
            )
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


async def async_write_snapshot(
    hass: HomeAssistant,
    platform: str,
    account: str,
    payload: dict[str, Any],
) -> bool:
    """Dump a snapshot to the www cache. Returns whether the write succeeded.

    Never raises: a failed dump must not fail the coordinator update.
    """
    path = _build_json_path(hass, platform, account)
    try:
        await hass.async_add_executor_job(_write_json, path, payload)
    except Exception as err:
        # Broad by design: a cache write must never break polling.
        _LOGGER.warning(
            "Could not write JSON cache for %s:%s to %s: %s", platform, account, path, err
        )
        return False
    return True


def _account_slugs(directory: Path) -> list[str]:
    """Account snapshots sitting in one platform directory.

    Leading-dot names are skipped: `_write_json` stages every write through a `.tmp-*.json`
    sibling, and pathlib's glob — unlike a shell's — happily matches those.
    """
    return sorted(
        path.stem
        for path in directory.glob("*.json")
        if not path.name.startswith(".") and path.stem != "accounts"
    )


def _write_index(directory: Path) -> None:
    """Refresh a platform's index page. Runs in an executor — never call from the loop."""
    page = _INDEX_TEMPLATE.read_text(encoding="utf-8").replace(
        _ACCOUNTS_PLACEHOLDER, json.dumps(_account_slugs(directory))
    )
    target = directory / _INDEX_NAME

    # Only rewritten when it would actually differ. The page is static apart from the
    # account list, so writing it on every poll would churn the disk and bust every
    # browser cache to produce a byte-identical file.
    with contextlib.suppress(OSError):
        if target.read_text(encoding="utf-8") == page:
            return

    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")


async def async_write_index(hass: HomeAssistant, platform: str) -> bool:
    """Write the browsable index for one platform. Returns whether it succeeded.

    Never raises: like the snapshot dump, a page that failed to render must not fail a poll.
    """
    directory = _build_json_path(hass, platform, "unused").parent
    try:
        await hass.async_add_executor_job(_write_index, directory)
    except Exception as err:
        # Broad by design: this is a convenience page, not part of the data path.
        _LOGGER.warning("Could not write the index page for %s in %s: %s", platform, directory, err)
        return False
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a snapshot back. Runs in an executor — never call from the event loop."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def async_load_dev_cloud_json(
    hass: HomeAssistant, platform: str, account: str
) -> dict[str, Any] | None:
    """Load the previous snapshot for an account, or None if there is not a usable one.

    This is the integration's persistence: the file it publishes for consumers doubles as
    the state it reloads from, so a restart does not begin by refetching the whole account.
    """
    path = _build_json_path(hass, platform, account)
    try:
        return await hass.async_add_executor_job(_read_json, path)
    except Exception as err:
        _LOGGER.debug("Could not read snapshot for %s:%s from %s: %s", platform, account, path, err)
        return None
