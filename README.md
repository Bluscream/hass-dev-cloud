# Developer Cloud Services (`dev_cloud`)

A Home Assistant integration that tracks developer accounts — repositories, releases,
downloads, open issues, CI runs — across seven platforms, one config entry per account.

Add as many accounts as you like, on as many platforms as you like.

| Platform | Notes |
| :--- | :--- |
| **GitHub** | Richest support: releases, branches, tags, issues, PRs, sponsors, Actions |
| **GitLab** | gitlab.com or self-hosted |
| **Gitea / Forgejo** | Self-hosted, Codeberg, Disroot, or any instance |
| **Docker Hub** | Container images |
| **npm**, **PyPI**, **NuGet** | Package registries |

---

## How it works

Each account is polled on a schedule, and the result is published two ways:

- **Sensors** carry counts and scalar metrics — numbers for dashboards and automations.
- **A JSON snapshot** at `/local/dev/<platform>/<account>.json` carries everything else:
  every repository, release, asset, branch, tag, issue and pull request.

The split exists because Home Assistant caps state attributes at 16 KiB and writes them to
the recorder on every state change. A full GitHub account is well over a megabyte, so it
belongs in a file, not in the state machine.

The snapshot is also how the integration **remembers what it has already fetched**, so a
restart or a redeploy resumes the previous schedule instead of re-downloading the account.

---

## Sensors

The profile sensor always exists. Every other sensor appears only if that data was actually
collected for the account — so PyPI gets no Forks sensor, and Docker Hub gets no Issues
sensor. **An empty result still counts as collected**: zero unread notifications reports
`0` rather than removing the sensor.

| Sensor | Unit | State class |
| :--- | :--- | :--- |
| Profile | — | — |
| Repositories | repos | measurement |
| Organizations | orgs | measurement |
| Pastes / Gists / Snippets | pastes | measurement |
| Packages | packages | measurement |
| Notifications | notifications | measurement |
| Open Issues | issues | measurement |
| Open Pull Requests | PRs | measurement |
| Stars | stars | measurement |
| Watchers | watchers | measurement |
| Forks | forks | measurement |
| Releases | releases | measurement |
| Assets | assets | measurement |
| Downloads | downloads | **total** |
| Pulls | pulls | **total** |
| Sponsors | sponsors | measurement |
| Running Jobs | jobs | measurement |

Only Downloads and Pulls are totals: a download once served is never un-served, so the
change between periods is meaningful. Everything else counts things that can be deleted, so
it is a measurement — Home Assistant records mean, min and max rather than an accumulating
sum.

Attributes hold aggregates only. The profile sensor additionally carries `json_url`, the
link to the snapshot; it is not repeated on every entity, because it is the same URL for all
of them.

---

## The JSON snapshot

Served at `/local/dev/<platform>/<account>.json`. Written atomically, minified, with empty
and null values omitted.

Everything about a repository hangs off that repository:

```jsonc
{
  "platform": "github",
  "account": "Bluscream",
  "fetched_at": "2026-09-18T21:41:57+00:00",
  "profile": { "username": "Bluscream", "followers": 296 },
  "repos": [
    {
      "full_name": "Bluscream/VRCOSC-Modules",
      "stars": 42, "forks": 5, "watchers": 3,
      "issues":   [ { "number": 12, "title": "…" } ],
      "prs":      [ { "number": 34, "title": "…" } ],
      "releases": [
        { "tag": "2026.0615.8",
          "assets": [ { "name": "VRCOSC-Modules.zip", "downloads": 508 } ] }
      ],
      "branches": [ { "name": "main", "sha": "…" } ],
      "tags":     [ { "name": "v1.0", "sha": "…" } ]
    }
  ],
  "orgs": [ { "name": "…", "is_owned": true, "repos": [ /* same shape */ ] } ]
}
```

Two rules govern the format:

**No count you could take yourself.** Every list is fetched to completion, so totals are
derived by measuring them. There is no `releases_count` beside `releases`, and a release
does not name the repository it already hangs off. The only counts that survive are ones
with no list to measure — sponsors, for instance, where the API exposes a total and nothing
else.

**Nothing duplicated.** A repository's `open_issues` disappears once its `issues` and `prs`
are both listed, because GitHub's count is exactly the sum of the two.

> [!IMPORTANT]
> `/local` is served **without authentication**. Anyone who can reach Home Assistant can read
> these files, including private repository names. Keys that look like credentials
> (`token`, `secret`, `api_key`, …) are redacted before writing, but nothing else is.

---

## Options

Available when adding an account and afterwards under **Configure**.

| Option | Default | Effect |
| :--- | :--- | :--- |
| Scan interval | 10 min authenticated / 30 min anonymous | How often the account is polled at all |
| Enable events | on | Fire events for new repositories and packages |
| Get detailed results | on | See below |
| Totals include non-owned organisations | off | See below |

### Get detailed results

**On**, every repository, organisation, paste, issue, pull request, release, branch and tag
is enumerated. **Off**, the provider stops after the profile request and publishes the
summary totals that response already carried — a couple of requests instead of hundreds.

Turn it off for accounts you want counted but not catalogued.

### Totals include non-owned organisations

Organisation repositories are always in the snapshot. This decides whether they feed the
**totals** — stars, forks, watchers, releases, assets, downloads.

- Organisations you own or administer **always** count.
- Organisations you are only a member of count **only** when this is enabled.
- Where a platform exposes no membership role (GitLab, Gitea), ownership is unknown and the
  organisation is treated as not owned.

Off by default, because most memberships are in somebody else's organisation. Enabling it on
an account that belongs to, say, EpicGames will report their stars as yours.

---

## Polling

Every collection is fetched to completion, which costs requests — so each is refreshed on its
own schedule rather than all of them on every poll.

Each provider declares, per resource, a minimum interval for authenticated and anonymous use.
The scheduler then only ever makes those intervals **longer**:

- **Budget-aware.** Rate-limit headers are tracked per quota. GitHub bills REST per request
  and GraphQL in points, against separate allowances, so a healthy REST budget cannot mask an
  exhausted GraphQL one. When an allowance is spent, the resource waits for the reset rather
  than retrying into it.
- **Cost-measured.** What a resource actually spent is measured, not guessed.
- **Backed off on failure.** Repeated failures widen the interval, because against a
  rate-limited endpoint the retries are what prolong the block.
- **Chained.** Resources declare what they are derived from — releases from repositories,
  organisation releases from organisations. Refreshing a parent marks its children due,
  subject to their own floor, so refreshing the repository list does not drag every
  organisation's releases along with it.

Skipped resources keep serving their last known value, so the snapshot is always complete.

The whole schedule is published in the snapshot's `resources` block — when each collection
was last fetched, when it is next due, what it cost, and which quota it spends:

```jsonc
"resources": {
  "notifications": { "fetched_at": "…", "next_due_in": 280.0, "interval": 300,
                     "cost": 1, "quota": "rest", "failures": 0 },
  "repo_detail":   { "fetched_at": "…", "next_due_in": 1200.0, "interval": 3600,
                     "cost": 16, "quota": "graphql", "failures": 0 }
}
```

That block is read back on startup, which is what stops a redeploy re-downloading everything.

### GitHub specifics

Releases, assets, branches, tags and watcher counts come from a single GraphQL walk rather
than a request per repository. GraphQL bills on what a query *asks for*, so page sizes are
chosen against that budget rather than against the maximum allowed.

If GraphQL is unavailable — its budget spent, or the token cannot use it — releases fall back
to the REST endpoint, which bills the separate REST allowance at one request per repository.
Branches and tags are not fetched on that path; each would be another request per repository,
which is the cost the fallback exists to avoid.

---

## Events

Fired when **Enable events** is on:

- `dev_cloud_new_repo` — a repository appeared
- `dev_cloud_new_package` — a package appeared

No events fire until a baseline exists, so the first successful poll does not announce every
existing repository at once.

---

## Development

```bash
./scripts/build.sh lint    # ruff format check, ruff, mypy --strict, pytest
./scripts/build.sh test    # tests only
./scripts/build.sh all     # the gate, then deploy and reload every entry
```

The gate must pass before a deploy; `build.sh` stops on the first failure.

Changes to the providers, coordinator, sensors and storage hot-reload when the config entries
reload. Changes to `__init__.py` need a Home Assistant restart — it is the one module that
cannot reload itself.

### Layout

```
custom_components/dev_cloud/
├── coordinator.py          polling, events, snapshot restore
├── storage.py              snapshot writing and loading
├── aggregation.py          which repositories and releases count toward totals
├── sensor.py               entities and the registration table
├── models.py               data models and snapshot rehydration
└── providers/
    ├── base.py             HTTP, pagination, per-resource scheduling entry point
    ├── scheduling.py       intervals, quotas, backoff, dependency chain
    └── github/             queries.py, releases.py, __init__.py
```
