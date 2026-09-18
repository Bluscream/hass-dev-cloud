"""Provider registry and factory for Developer Cloud Services."""

from __future__ import annotations

from aiohttp import ClientSession

from ..const import (
    PLATFORM_DOCKERHUB,
    PLATFORM_GITEA,
    PLATFORM_GITHUB,
    PLATFORM_GITLAB,
    PLATFORM_NPM,
    PLATFORM_NUGET,
    PLATFORM_PYPI,
)
from .base import (
    BaseDevCloudProvider,
    DevCloudAuthError,
    DevCloudNotFoundError,
    DevCloudProviderError,
    DevCloudRateLimitError,
)
from .dockerhub import DockerHubProvider
from .gitea import GiteaProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider
from .npm import NPMProvider
from .nuget import NuGetProvider
from .pypi import PyPIProvider

PROVIDER_REGISTRY: dict[str, type[BaseDevCloudProvider]] = {
    PLATFORM_GITHUB: GitHubProvider,
    PLATFORM_GITLAB: GitLabProvider,
    PLATFORM_GITEA: GiteaProvider,
    PLATFORM_DOCKERHUB: DockerHubProvider,
    PLATFORM_NPM: NPMProvider,
    PLATFORM_PYPI: PyPIProvider,
    PLATFORM_NUGET: NuGetProvider,
}


def get_provider(
    platform: str,
    session: ClientSession,
    account_name: str,
    instance_url: str | None = None,
    api_token: str | None = None,
) -> BaseDevCloudProvider:
    """Return an instantiated provider for the given platform."""
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if not provider_cls:
        raise ValueError(f"Unsupported platform: {platform}")
    return provider_cls(
        session=session,
        account_name=account_name,
        base_url=instance_url,
        api_token=api_token,
    )


__all__ = [
    "BaseDevCloudProvider",
    "DevCloudAuthError",
    "DevCloudNotFoundError",
    "DevCloudProviderError",
    "DevCloudRateLimitError",
    "PROVIDER_REGISTRY",
    "get_provider",
]
