# Developer Cloud Services (`dev_cloud`) for Home Assistant

Monitor unlimited developer platforms and accounts directly from Home Assistant.

## Supported Platforms
- **GitHub** (Cloud)
- **GitLab** (GitLab.com or self-hosted instance)
- **Gitea / Forgejo** (Gitea.com or self-hosted instance, including homelab)
- **Codeberg** (Forgejo instance)
- **Docker Hub** (Container registry)
- **NPM** (JavaScript registry)
- **PyPI** (Python package index)
- **NuGet** (.NET package index)

## Sensors Created
- **Profile**: Live user avatar in `entity_picture`, username, display name, user ID, profile URL, bio, location, company, followers, following, rate limit remaining/reset.
- **Repositories**: Total repositories count, star count, forks count, with full repositories listing map in attributes (names, URLs, descriptions, stars, forks, upstream details, language).
- **Organizations**: Total organizations count with mapped list of org names, avatars, URLs, and descriptions.
- **Pastes / Gists**: Total pastes/gists/snippets count with title, URLs, public/private status, and file counts in attributes.
- **Packages**: Total packages count with downloads and pulls metrics.

## Events
When enabled in integration options, fires:
- `dev_cloud_new_repo` on newly detected repositories.
- `dev_cloud_new_package` on newly published packages.
