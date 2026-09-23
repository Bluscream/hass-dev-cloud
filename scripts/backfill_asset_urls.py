#!/usr/bin/env python3
"""Fill in release-asset download URLs that a published snapshot predates.

The walk now carries an asset's real download URL (GraphQL ``downloadUrl``, REST
``browser_download_url``), but snapshots written before that field existed have none, and
the page falls back to linking the release page until the walk next runs — up to an hour.

Every part of the URL is already in the snapshot, so this assembles it in the meantime:

    {repository url}/releases/download/{tag}/{asset name}

Confirmed against the live API rather than assumed, including the awkward case: GitHub
leaves a slash inside a tag unencoded, so ``06/29/2024`` really does produce
``…/releases/download/06/29/2024/MoreChatNotifications.dll``.

Only ever fills a *missing* URL, so it is safe to re-run and cannot overwrite a real one.
The next successful walk replaces everything here with what the API actually returned.

    backfill_asset_urls.py <www/dev>          # dry run, prints what it would do
    backfill_asset_urls.py <www/dev> --apply  # write

Reload the integration afterwards. The snapshot is both the published file and the state
restored on reload, and a poll rewrites it from whatever the provider holds in memory — so
without a reload the next poll overwrites this within minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: Forges whose release downloads live at /releases/download/<tag>/<name>. Gitea and
#: Forgejo copy GitHub's layout exactly. GitLab does not — its releases hang assets off
#: /-/releases/<tag>/downloads/ or off the generic package registry — so it is absent
#: rather than guessed at.
KNOWN_LAYOUT = {"github", "gitea"}

#: Characters that must not travel raw in a path segment. The slash is deliberately absent:
#: a tag containing one keeps it, which is what GitHub itself does.
_UNSAFE = "%?# "


def _segment(value: str) -> str:
    """Encode only what would actually break the path, leaving everything else readable."""
    return quote(value, safe="".join(c for c in map(chr, range(33, 127)) if c not in _UNSAFE))


def _repos(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Every repository in a snapshot, the account's own and its organisations'."""
    own = list(snapshot.get("repos") or [])
    org = [r for o in snapshot.get("orgs") or [] for r in (o.get("repos") or [])]
    return own + org


def backfill(snapshot: dict[str, Any]) -> tuple[int, int]:
    """Fill missing asset URLs in place. Returns (filled, already had one)."""
    filled = kept = 0
    for repo in _repos(snapshot):
        base = str(repo.get("url") or "").rstrip("/")
        if not base:
            continue
        for release in repo.get("releases") or []:
            tag = str(release.get("tag") or "")
            if not tag:
                continue
            for asset in release.get("assets") or []:
                name = str(asset.get("name") or "")
                if not name:
                    continue
                if asset.get("url"):
                    kept += 1
                    continue
                asset["url"] = f"{base}/releases/download/{_segment(tag)}/{_segment(name)}"
                filled += 1
    return filled, kept


def _write_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Same staging the integration uses, so a reader never sees a half-written document."""
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out, separators=(",", ":"), default=str)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="the www/dev directory holding <platform>/")
    parser.add_argument("--apply", action="store_true", help="write; otherwise only report")
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 1

    total_filled = 0
    for path in sorted(args.root.glob("*/*.json")):
        platform = path.parent.name
        if platform not in KNOWN_LAYOUT:
            continue

        snapshot = json.loads(path.read_text(encoding="utf-8"))
        filled, kept = backfill(snapshot)
        if not filled:
            print(f"  {platform}/{path.name}: nothing to fill ({kept} already had a URL)")
            continue

        total_filled += filled
        print(f"  {platform}/{path.name}: {filled} filled, {kept} left alone")
        if args.apply:
            _write_atomically(path, snapshot)

    verb = "filled" if args.apply else "would fill"
    print(f"\n{verb} {total_filled} asset URLs")
    if total_filled and not args.apply:
        print("re-run with --apply to write")
    elif total_filled:
        print("now reload the integration, or the next poll will overwrite this")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
