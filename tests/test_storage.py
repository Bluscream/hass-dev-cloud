"""Tests for the JSON snapshot exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dev_cloud import storage
from yarl import URL
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
    url = storage.build_json_url("github", "Bluscream")
    assert isinstance(url, URL)
    assert str(url) == "/local/dev/github/bluscream.json"


def test_json_url_escapes_an_account_name_that_survives_slugification() -> None:
    """Slugification already removes the dangerous characters; the URL type is the backstop."""
    assert str(storage.build_json_url("github", "a b/c")) == "/local/dev/github/a_b_c.json"


def test_serialize_keeps_the_running_jobs_count_beside_its_list() -> None:
    """It looks derivable from len(running_jobs) and is not.

    None means "this platform has no CI, or there is no token" and 0 means "none are
    running"; an empty list reads the same either way. Dropping the count left it
    unrestorable, so after a reload the Running Jobs sensor was never registered and Home
    Assistant reported the entity as no longer provided by the integration.
    """
    data = _snapshot(running_jobs_count=0, running_jobs=[], sponsors_count=3)
    payload = storage.build_snapshot("github", "Bluscream", data)

    assert payload["running_jobs_count"] == 0
    assert payload["sponsors_count"] == 3
    assert payload["json_url"] == "/local/dev/github/bluscream.json"


def test_serialize_omits_the_running_jobs_count_a_platform_does_not_have() -> None:
    """None is pruned, so its absence is what tells a restore there is no CI here."""
    payload = storage.build_snapshot("npm", "bluscream", _snapshot(running_jobs_count=None))
    assert "running_jobs_count" not in payload


def test_serialize_redacts_credential_shaped_keys() -> None:
    """/local is unauthenticated, so nothing credential-shaped may reach the file."""
    data = _snapshot(
        repos=[RepoData(name="r", full_name="o/r", url="u", extra={"api_token": "SECRET"})],
        orgs=[OrgData(name="o", extra={"Authorization": "Bearer SECRET", "role": "admin"})],
    )
    data.profile.extra = {"private_key": "SECRET", "bio": "kept"}

    payload = storage.build_snapshot("github", "Bluscream", data)
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


def test_write_json_is_minified(tmp_path: Path) -> None:
    """These files are consumed by programs; whitespace is a megabyte-scale cost here."""
    target = tmp_path / "snapshot.json"
    storage._write_json(target, {"a": 1, "b": [{"c": 2}]})

    raw = target.read_text()
    assert raw == '{"a":1,"b":[{"c":2}]}'
    assert "\n" not in raw


def test_serialize_prunes_values_that_say_nothing() -> None:
    data = _snapshot(repos=[RepoData(name="r", full_name="o/r", url="u")])
    payload = storage.build_snapshot("github", "Bluscream", data)

    assert "orgs" not in payload, "an empty list carries no information"
    assert "error" not in payload
    repo = payload["repos"][0]
    assert "upstream" not in repo, "a null field carries no information"
    assert repo["full_name"] == "o/r"


def test_serialize_keeps_zero_and_false() -> None:
    """A zero count and is_fork=false are answers, not absences."""
    data = _snapshot(
        repos=[RepoData(name="r", full_name="o/r", url="u", stars=0, is_fork=False)],
        sponsors_count=0,
    )
    payload = storage.build_snapshot("github", "Bluscream", data)

    assert payload["sponsors_count"] == 0
    assert payload["repos"][0]["stars"] == 0
    assert payload["repos"][0]["is_fork"] is False


# --- the browsable index page ----------------------------------------------------------


def test_the_index_lists_every_account_snapshot_in_the_directory(tmp_path: Path) -> None:
    (tmp_path / "bluscream.json").write_text("{}", encoding="utf-8")
    (tmp_path / "someone_else.json").write_text("{}", encoding="utf-8")

    storage._write_index(tmp_path)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert '["bluscream","someone_else"]' in page.replace(", ", ",")
    assert storage._ACCOUNTS_PLACEHOLDER not in page, "the placeholder must be substituted"


def test_the_index_ignores_a_half_written_snapshot(tmp_path: Path) -> None:
    """Writes are staged through a .tmp-*.json sibling, and pathlib's glob matches those."""
    (tmp_path / "bluscream.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".tmp-abc123.json").write_text("{}", encoding="utf-8")

    assert storage._account_slugs(tmp_path) == ["bluscream"]


def test_the_index_is_left_alone_when_it_would_not_change(tmp_path: Path) -> None:
    """It is rewritten every poll otherwise, busting every browser cache for no new bytes."""
    (tmp_path / "bluscream.json").write_text("{}", encoding="utf-8")
    storage._write_index(tmp_path)

    target = tmp_path / "index.html"
    stamped = target.stat().st_mtime_ns
    target_bytes = target.read_bytes()

    storage._write_index(tmp_path)

    assert target.stat().st_mtime_ns == stamped, "untouched, not rewritten identically"
    assert target.read_bytes() == target_bytes


def test_the_index_is_rewritten_when_an_account_appears(tmp_path: Path) -> None:
    (tmp_path / "bluscream.json").write_text("{}", encoding="utf-8")
    storage._write_index(tmp_path)

    (tmp_path / "second.json").write_text("{}", encoding="utf-8")
    storage._write_index(tmp_path)

    assert "second" in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_an_empty_platform_directory_still_produces_a_valid_page(tmp_path: Path) -> None:
    storage._write_index(tmp_path)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "const ACCOUNTS = [];" in page
