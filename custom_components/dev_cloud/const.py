"""Constants for the Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "dev_cloud"
PLATFORMS: Final = ["sensor"]

# Configuration keys
CONF_PLATFORM: Final = "platform"
CONF_ACCOUNT_NAME: Final = "account_name"
CONF_INSTANCE_URL: Final = "instance_url"
CONF_API_TOKEN: Final = "api_token"  # noqa: S105 - config key name, not a secret
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_EVENTS: Final = "enable_events"
CONF_INCLUDE_NON_OWNED_ORGS: Final = "include_non_owned_orgs"

# Default values
DEFAULT_SCAN_INTERVAL_AUTHENTICATED: Final = 600  # 10 minutes
DEFAULT_SCAN_INTERVAL_ANONYMOUS: Final = 1800  # 30 minutes
DEFAULT_ENABLE_EVENTS: Final = True
# Off by default: most org memberships are in somebody else's organisation, and folding
# their repositories into the account's totals makes those totals describe other people.
DEFAULT_INCLUDE_NON_OWNED_ORGS: Final = False
MIN_SCAN_INTERVAL: Final = 60

# Running CI jobs have to be queried per repository on every forge, so the fan-out is capped
# to the most recently active repositories and the concurrency is bounded to stay well inside
# API rate limits. Running work virtually always sits on a recently pushed repository.
RUNNING_JOBS_REPO_LIMIT: Final = 20
RUNNING_JOBS_CONCURRENCY: Final = 5

# Event names
EVENT_DEV_CLOUD: Final = "dev_cloud_event"
EVENT_NEW_REPO: Final = "dev_cloud_new_repo"
EVENT_NEW_PACKAGE: Final = "dev_cloud_new_package"

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
