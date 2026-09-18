"""Tests for the JSON snapshot exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dev_cloud import storage
from dev_cloud.models import DevCloudData, OrgData, ProfileData, RepoData


def _snapshot(**kwargs: object) -> DevCloudData:
    return DevCloudData(profile=ProfileData(username="Bluscream"), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ("Bluscream", "bluscream"),
        ("../../etc/passwd", "etc_passwd"),
        ("a b/c", "a_b_c"),
        ("..", "account"),
        ("", "account"),
        ("...", "account"),
    ],
)
def test_slugify_account_is_path_safe(account: str, expected: str) -> None:
    """Account names reach the filesystem, so traversal must not survive slugification."""
    assert storage.slugify_account(account) == expected


def test_json_url_matches_slugified_path() -> None:
    assert storage.build_json_url("github", "Bluscream") == "/local/dev/github/bluscream.json"


def test_serialize_drops_only_the_derivable_count() -> None:
    """running_jobs_count is len(running_jobs); sponsors_count has no list to derive from."""
    data = _snapshot(running_jobs_count=0, running_jobs=[], sponsors_count=3)
    payload = storage._serialize("github", "Bluscream", data)

    assert "running_jobs_count" not in payload
    assert payload["sponsors_count"] == 3
    assert payload["json_url"] == "/local/dev/github/bluscream.json"


def test_serialize_redacts_credential_shaped_keys() -> None:
    """/local is unauthenticated, so nothing credential-shaped may reach the file."""
    data = _snapshot(
        repos=[RepoData(name="r", full_name="o/r", url="u", extra={"api_token": "SECRET"})],
        orgs=[OrgData(name="o", extra={"Authorization": "Bearer SECRET", "role": "admin"})],
    )
    data.profile.extra = {"private_key": "SECRET", "bio": "kept"}

    payload = storage._serialize("github", "Bluscream", data)
    blob = json.dumps(payload)

    assert "SECRET" not in blob
    assert payload["profile"]["extra"]["bio"] == "kept"
    assert payload["orgs"][0]["extra"]["role"] == "admin"


def test_write_json_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "github" / "bluscream.json"
    storage._write_json(target, {"hello": "world"})

    assert json.loads(target.read_text()) == {"hello": "world"}
    assert [p.name for p in target.parent.iterdir() if p.name.startswith(".tmp")] == []


def test_write_json_preserves_previous_file_when_serialising_fails(tmp_path: Path) -> None:
    """A failed write must not truncate the snapshot that was already published."""
    target = tmp_path / "bluscream.json"
    storage._write_json(target, {"generation": 1})

    class Unserialisable:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        storage._write_json(target, {"bad": Unserialisable()})

    assert json.loads(target.read_text()) == {"generation": 1}
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp")] == []
