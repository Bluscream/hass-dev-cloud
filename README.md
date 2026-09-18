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

## Sensors

Sensors are created only where the platform actually returned data, so an account with no
packages gets no Packages sensor rather than one reading zero. A sensor that gains data later
appears on the next reload of the entry.

| Sensor | State |
| :--- | :--- |
| Profile | Display name, with the avatar as `entity_picture` |
| Repositories | Repositories owned by the account |
| Organizations | Organisation memberships |
| Pastes / Gists / Snippets | Gists, snippets or pastes |
| Packages | Published packages |
| Notifications | Unread notifications |
| Open Issues / Open Pull Requests | Open across the account |
| Stars / Watchers / Forks | Summed across counted repositories |
| Releases / Assets / Downloads | Summed across counted repositories |
| Running Jobs | CI workflows or pipelines currently running |
| Sponsors | Active sponsorships |
| Pulls | Container image pulls |

### Attributes and the JSON snapshot

State attributes hold **only** aggregated counts and scalar metrics. Home Assistant caps
attributes at 16 KiB and writes them to the recorder on every state change, so the full
detail is exported instead to:

```
/local/dev/<platform>/<account>.json
```

That file carries the complete picture — every repository, organisation (with its own
repositories), paste, package, notification, issue, pull request, release and release asset.
Nothing in it is truncated, so counts are derived by measuring the lists rather than stored
beside them. The profile sensor carries a `json_url` attribute pointing at it.

> [!NOTE]
> `/local` is served **without authentication**. Anyone who can reach Home Assistant can read
> these files, including private repository names. Keys that look like credentials are
> redacted before the file is written, but the rest is published as-is.

## Options

| Option | Default | Effect |
| :--- | :--- | :--- |
| Scan interval | 10 min (authenticated) / 30 min (anonymous) | Base polling interval |
| Enable events | On | Fire events for new repositories and packages |
| Totals include non-owned organisations | Off | See below |

### Organisation totals

Each organisation's repositories are fetched and included in the JSON snapshot regardless.
Whether they feed the **totals** (stars, forks, watchers, releases, assets, downloads) is
what this option controls:

- Organisations you own or administer **always** count.
- Organisations you are only a member of count **only** when this is enabled.
- Where a platform exposes no membership role (GitLab, Gitea), ownership is unknown and the
  organisation is treated as not owned.

It is off by default because most memberships are in somebody else's organisation, and
counting those makes the totals describe other people rather than you.

## Polling

Each resource is refreshed on its own schedule rather than everything on every poll, because
the lists are fetched to completion and that costs requests. Providers declare a minimum
interval per resource, separately for authenticated and anonymous use. The scheduler then
*lengthens* that interval — never shortens it — when the platform's remaining rate-limit
budget cannot comfortably absorb the resource's measured request cost before the quota
resets. Skipped resources keep serving their last known value.

The per-resource interval, measured cost and age are published on the profile sensor's
`scheduling` attribute and in the JSON snapshot.

## Events

When enabled in the integration options, fires:
- `dev_cloud_new_repo` on newly detected repositories.
- `dev_cloud_new_package` on newly published packages.

No events are fired until a baseline exists, so the first successful poll does not announce
every existing repository at once.

## Development

```bash
./scripts/build.sh lint     # format check, ruff, mypy --strict, pytest
./scripts/build.sh test     # tests only
./scripts/build.sh all      # gate, then deploy to Home Assistant and reload entries
```

Changes to `coordinator.py` and the providers hot-reload on an entry reload. Changes to
`__init__.py` need a Home Assistant restart.
