"""Data models for Developer Cloud Services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProfileData:
    """User or organization profile data."""

    username: str
    display_name: str | None = None
    user_id: str | int | None = None
    avatar_url: str | None = None
    profile_url: str | None = None
    bio: str | None = None
    location: str | None = None
    company: str | None = None
    blog: str | None = None
    email: str | None = None
    created_at: str | None = None
    # followers/following have no companion list, so they stay as reported by the API.
    # Repo and paste totals deliberately do *not* live here: the repos/pastes lists are
    # fetched in full, so any count would just be a second copy of len().
    followers: int | None = None
    following: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrgData:
    """Organization membership data."""

    name: str
    org_id: str | int | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    url: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RepoData:
    """Repository metadata."""

    name: str
    full_name: str
    url: str
    description: str | None = None
    is_fork: bool = False
    is_private: bool = False
    is_archived: bool = False
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    open_issues: int = 0
    primary_language: str | None = None
    default_branch: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pushed_at: str | None = None
    upstream: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PasteData:
    """Snippet, gist, or paste metadata."""

    paste_id: str
    title: str | None = None
    url: str | None = None
    is_public: bool = True
    files_count: int = 1
    comments_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PackageData:
    """Package / container registry metadata."""

    name: str
    version: str | None = None
    url: str | None = None
    description: str | None = None
    downloads_total: int | None = None
    pull_count: int | None = None
    star_count: int | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NotificationData:
    """Unread or active notification item."""

    notification_id: str
    title: str
    reason: str | None = None
    repository: str | None = None
    url: str | None = None
    unread: bool = True
    updated_at: str | None = None
    subject_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DevCloudData:
    """Aggregated snapshot of all monitored items for one account."""

    profile: ProfileData
    orgs: list[OrgData] = field(default_factory=list)
    repos: list[RepoData] = field(default_factory=list)
    pastes: list[PasteData] = field(default_factory=list)
    packages: list[PackageData] = field(default_factory=list)
    notifications: list[NotificationData] = field(default_factory=list)
    open_issues: list[dict[str, Any]] = field(default_factory=list)
    open_prs: list[dict[str, Any]] = field(default_factory=list)
    # Sponsorships expose only totals over the API, so there is no list to count.
    sponsors_count: int | None = None
    sponsoring_count: int | None = None
    # Kept off the JSON dump (see storage.py) but needed in-process: None means the platform
    # has no CI or no token, which len(running_jobs) cannot distinguish from "none running".
    running_jobs_count: int | None = None
    running_jobs: list[dict[str, Any]] = field(default_factory=list)
    # Each release carries its own `assets` list, so release, asset, and download totals are
    # all derived from this one structure rather than stored alongside it.
    releases: list[dict[str, Any]] = field(default_factory=list)
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    # Per-resource polling state (effective interval, measured request cost, age), so the
    # adaptive scheduler's decisions are visible rather than opaque.
    scheduling: dict[str, Any] = field(default_factory=dict)
    raw_status: str = "ok"
    error: str | None = None
