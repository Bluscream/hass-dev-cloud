"""Constants for the Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "dev_cloud"
PLATFORMS: Final = ["sensor", "button"]

# Configuration keys
CONF_PLATFORM: Final = "platform"
CONF_ACCOUNT_NAME: Final = "account_name"
CONF_INSTANCE_URL: Final = "instance_url"
CONF_API_TOKEN: Final = "api_token"  # noqa: S105 - config key name, not a secret
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_EVENTS: Final = "enable_events"
CONF_INCLUDE_NON_OWNED_ORGS: Final = "include_non_owned_orgs"
CONF_DETAILED_RESULTS: Final = "detailed_results"

# Default values
DEFAULT_SCAN_INTERVAL_AUTHENTICATED: Final = 600  # 10 minutes
DEFAULT_SCAN_INTERVAL_ANONYMOUS: Final = 1800  # 30 minutes
DEFAULT_ENABLE_EVENTS: Final = True
# Off by default: most org memberships are in somebody else's organisation, and folding
# their repositories into the account's totals makes those totals describe other people.
DEFAULT_INCLUDE_NON_OWNED_ORGS: Final = False
# On by default so existing entries keep the data they already report. Turning it off trades
# the per-item lists for the summary totals the API hands back in one or two requests.
DEFAULT_DETAILED_RESULTS: Final = True
MIN_SCAN_INTERVAL: Final = 60

# Running CI jobs have to be queried per repository on every forge, so the fan-out is capped
# to the most recently active repositories and the concurrency is bounded to stay well inside
# API rate limits. Running work virtually always sits on a recently pushed repository.
RUNNING_JOBS_REPO_LIMIT: Final = 20
RUNNING_JOBS_CONCURRENCY: Final = 5

# Home Assistant caps a single entity's state attributes at 16 KiB, and every attribute is
# written to the recorder on each state change. The unread notification list is the only
# attribute here holding per-item detail, so it is capped; the full list is always in the
# published JSON snapshot.
NOTIFICATION_ATTRIBUTE_LIMIT: Final = 20

# GitHub's traffic endpoints cost four requests per repository and need push access, so a
# whole account cannot be swept in one poll. Each sweep takes a fixed handful, least recently
# fetched first, and works its way round. The deadline is GitHub's own fourteen-day
# retention: at ten repositories per ten-minute poll, six hundred are covered in ten hours,
# which is comfortably inside it. A constant per-sweep cost is also what lets the scheduler
# measure the resource once and pace it against the remaining budget like any other.
TRAFFIC_REPOS_PER_SWEEP: Final = 10
TRAFFIC_CONCURRENCY: Final = 4
# Roughly two years of per-day buckets, and only days with traffic are kept.
TRAFFIC_HISTORY_DAYS: Final = 730
# A repository the token cannot push to answers 403 forever. Retrying weekly picks up newly
# granted access without spending every sweep rediscovering the same refusals.
TRAFFIC_FORBIDDEN_RETRY_DAYS: Final = 7
# Referring sites listed on the traffic sensors. Same reasoning as the notification cap:
# attributes are recorded on every state change, and the full set is in the snapshot.
TRAFFIC_REFERRER_ATTRIBUTE_LIMIT: Final = 10

# Event names
#
# Two, deliberately. Twenty-nine separate event types meant every consumer had to enumerate
# all of them to see anything, and a busy poll fired hundreds of individual events. What a
# change *is* now lives in the payload's `kind` field instead of in the event type.
#
# dev_cloud_update carries a batch of changes; see events.py for the chunking. A
# notification is the one thing already shaped like a notification, so it keeps its own type
# rather than arriving as a line inside a digest.
EVENT_DEV_CLOUD_UPDATE: Final = "dev_cloud_update"
EVENT_DEV_CLOUD_NOTIFICATION: Final = "dev_cloud_notification"

# Platforms
PLATFORM_GITHUB: Final = "github"
PLATFORM_GITLAB: Final = "gitlab"
PLATFORM_GITEA: Final = "gitea"
PLATFORM_DOCKERHUB: Final = "dockerhub"
PLATFORM_NPM: Final = "npm"
PLATFORM_PYPI: Final = "pypi"
PLATFORM_NUGET: Final = "nuget"

SUPPORTED_PLATFORMS: Final = {
    PLATFORM_GITHUB: "GitHub",
    PLATFORM_GITLAB: "GitLab",
    PLATFORM_GITEA: "Gitea / Forgejo",
    PLATFORM_DOCKERHUB: "Docker Hub",
    PLATFORM_NPM: "NPM",
    PLATFORM_PYPI: "PyPI (pip)",
    PLATFORM_NUGET: "NuGet",
}

PLATFORM_ICONS: Final = {
    PLATFORM_GITHUB: "mdi:github",
    PLATFORM_GITLAB: "mdi:gitlab",
    PLATFORM_GITEA: "mdi:git",
    PLATFORM_DOCKERHUB: "mdi:docker",
    PLATFORM_NPM: "mdi:npm",
    PLATFORM_PYPI: "mdi:language-python",
    PLATFORM_NUGET: "mdi:microsoft-visual-studio-code",
}

# Pre-defined public instances for Gitea / Forgejo
CONF_INSTANCE_PRESET: Final = "instance_preset"
INSTANCE_CUSTOM: Final = "custom"

GITEA_PRESETS: Final = {
    "codeberg": {"name": "Codeberg", "url": "https://codeberg.org"},
    "gitea_com": {"name": "Gitea", "url": "https://gitea.com"},
    "disroot": {"name": "Disroot", "url": "https://git.disroot.org"},
    INSTANCE_CUSTOM: {"name": "Custom Instance...", "url": ""},
}

DEFAULT_URLS: Final = {
    PLATFORM_GITHUB: "https://api.github.com",
    PLATFORM_GITLAB: "https://gitlab.com",
    PLATFORM_GITEA: "https://codeberg.org",
    PLATFORM_DOCKERHUB: "https://hub.docker.com",
    PLATFORM_NPM: "https://registry.npmjs.org",
    PLATFORM_PYPI: "https://pypi.org",
    PLATFORM_NUGET: "https://azuresearch-usnc.nuget.org",
}
