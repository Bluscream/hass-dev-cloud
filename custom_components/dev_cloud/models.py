"""Data models for Developer Cloud Services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, cast


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
    # True when the account owns/administers the org, False when it is only a member, and
    # None when the platform does not expose a role. Drives whether this org's repositories
    # feed the account totals (see CONF_INCLUDE_NON_OWNED_ORGS).
    is_owned: bool | None = None
    repos: list[RepoData] = field(default_factory=list)
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
    # Open issues and pull requests belonging to this repository. Nested here rather than
    # held in flat account-wide lists, so each one sits with the repository it is about and
    # the account totals are summed from them.
    issues: list[dict[str, Any]] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)
    # Releases carry their own assets, which carry their own download counts, so the whole
    # chain hangs off the repository it belongs to rather than being flattened.
    releases: list[dict[str, Any]] = field(default_factory=list)
    branches: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
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
    # Sponsorships expose only totals over the API, so there is no list to count.
    sponsors_count: int | None = None
    sponsoring_count: int | None = None
    # Kept off the JSON dump (see storage.py) but needed in-process: None means the platform
    # has no CI or no token, which len(running_jobs) cannot distinguish from "none running".
    running_jobs_count: int | None = None
    running_jobs: list[dict[str, Any]] = field(default_factory=list)
    # Resources this provider actually fetched at least once. An empty list cannot say
    # whether a collection is empty or was never collected, and those mean opposite things
    # to a sensor: "zero unread notifications" is a reading, "this platform has no
    # notifications" is an absence. Membership here is the difference.
    collected: set[str] = field(default_factory=set)
    # Totals the API reported for collections that were *not* enumerated, keyed by the field
    # they describe ("repos", "pastes", ...). Populated only when the matching list is absent,
    # so nothing here ever duplicates a len() a consumer could take itself.
    totals: dict[str, int] = field(default_factory=dict)
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    # Per-resource schedule: when each collection was last fetched, when it is next due,
    # and what it cost. Written into the snapshot so a reload resumes the previous schedule
    # instead of refetching everything, and so the pacing is inspectable rather than opaque.
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_status: str = "ok"
    error: str | None = None


#: Fields holding other models, so a restore rebuilds them rather than leaving raw dicts.
#: Plain `list[dict]` fields (releases, issues, running jobs) are deliberately absent — they
#: are already dictionaries by design.
_NESTED_MODELS: dict[tuple[type, str], type] = {
    (DevCloudData, "profile"): ProfileData,
    (DevCloudData, "orgs"): OrgData,
    (DevCloudData, "repos"): RepoData,
    (DevCloudData, "pastes"): PasteData,
    (DevCloudData, "packages"): PackageData,
    (DevCloudData, "notifications"): NotificationData,
    (OrgData, "repos"): RepoData,
}


def from_dict[T](cls: type[T], payload: Mapping[str, Any]) -> T:
    """Rebuild a model from its serialised form.

    The snapshot on disk is what the integration reloads its state from, so this is the
    inverse of `dataclasses.asdict`. Unknown keys are ignored and missing ones fall back to
    field defaults, because the writer prunes empty values and the schema moves between
    versions — a restore must never fail on a field that has since been added or dropped.
    """
    if not is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in payload:
            continue
        value = payload[f.name]
        nested = _NESTED_MODELS.get((cls, f.name))
        if nested is not None and isinstance(value, list):
            value = [from_dict(nested, item) for item in value if isinstance(item, Mapping)]
        elif nested is not None and isinstance(value, Mapping):
            value = from_dict(nested, value)
        kwargs[f.name] = value

    return cast("T", cls(**kwargs))
