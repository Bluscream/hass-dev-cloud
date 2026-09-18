"""Constants for the Developer Cloud Services (dev_cloud) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "dev_cloud"
PLATFORMS: Final = ["sensor"]

# Configuration keys
CONF_PLATFORM: Final = "platform"
CONF_ACCOUNT_NAME: Final = "account_name"
CONF_INSTANCE_URL: Final = "instance_url"
CONF_API_TOKEN: Final = "api_token"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_EVENTS: Final = "enable_events"

# Default values
DEFAULT_SCAN_INTERVAL_AUTHENTICATED: Final = 600  # 10 minutes
DEFAULT_SCAN_INTERVAL_ANONYMOUS: Final = 1800  # 30 minutes
DEFAULT_ENABLE_EVENTS: Final = True
MIN_SCAN_INTERVAL: Final = 60

# Event names
EVENT_DEV_CLOUD: Final = "dev_cloud_event"
EVENT_NEW_REPO: Final = "dev_cloud_new_repo"
EVENT_NEW_PACKAGE: Final = "dev_cloud_new_package"

# Platforms
PLATFORM_GITHUB: Final = "github"
PLATFORM_GITLAB: Final = "gitlab"
PLATFORM_GITEA: Final = "gitea"
PLATFORM_CODEBERG: Final = "codeberg"
PLATFORM_DOCKERHUB: Final = "dockerhub"
PLATFORM_NPM: Final = "npm"
PLATFORM_PYPI: Final = "pypi"
PLATFORM_NUGET: Final = "nuget"

SUPPORTED_PLATFORMS: Final = {
    PLATFORM_GITHUB: "GitHub",
    PLATFORM_GITLAB: "GitLab",
    PLATFORM_GITEA: "Gitea / Forgejo",
    PLATFORM_CODEBERG: "Codeberg",
    PLATFORM_DOCKERHUB: "Docker Hub",
    PLATFORM_NPM: "NPM",
    PLATFORM_PYPI: "PyPI (pip)",
    PLATFORM_NUGET: "NuGet",
}

DEFAULT_URLS: Final = {
    PLATFORM_GITHUB: "https://api.github.com",
    PLATFORM_GITLAB: "https://gitlab.com",
    PLATFORM_GITEA: "https://gitea.com",
    PLATFORM_CODEBERG: "https://codeberg.org",
    PLATFORM_DOCKERHUB: "https://hub.docker.com",
    PLATFORM_NPM: "https://registry.npmjs.org",
    PLATFORM_PYPI: "https://pypi.org",
    PLATFORM_NUGET: "https://api.nuget.org",
}
