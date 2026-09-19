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
| Security Alerts | alerts | measurement |
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
      "tags":     [ { "name": "v1.0", "sha": "…" } ],
      "security_alerts": [
        { "number": 14, "severity": "HIGH", "package": "jsonwebtoken", "ecosystem": "NPM",
          "ghsa": "GHSA-8cf7-32gw-wr33", "cve": "CVE-2022-23539", "cvss": 8.1,
          "summary": "…", "url": "https://github.com/advisories/…" }
      ]
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

## Polling, budgets and rate limits

### Two layers

Polling happens at two levels, and the coarser one wins.

**The coordinator** wakes on the entry's scan interval — 10 minutes authenticated, 30
anonymous, adjustable down to 60 seconds. Nothing is fetched between wakeups.

**Each resource** then decides for itself whether it is due. A resource is a collection the
provider knows how to fetch: `repos`, `orgs`, `notifications`, `repo_detail` and so on. Every
provider declares, per resource, a minimum interval for authenticated and anonymous use:

```python
"notifications": ResourcePolicy(authenticated=300, anonymous=None),
"repos":         ResourcePolicy(authenticated=900, anonymous=3600),
```

`None` means the resource is unavailable in that mode and is never fetched at all — which is
also what stops its sensor being created.

So a resource with a 300-second interval on an entry polling every 600 seconds refreshes
every 600 seconds. Lowering the scan interval makes the per-resource floors the binding
constraint instead.

### The scheduler only ever slows things down

A declared interval is a floor, never a target. Four things can push a resource further out,
and nothing brings it in.

**1. The remaining budget.** Rate-limit headers are recorded as they arrive. The sustainable
request rate is

```
rate = (remaining × 0.25) ÷ seconds_until_reset
```

— a quarter of what is left, so the integration never spends the whole allowance and leaves
room for the config flow, other tools sharing the token, and you. If a resource's measured
cost cannot fit at that rate, its interval stretches until it can:

```
interval = clamp(max(declared_floor, cost ÷ rate), floor, 6 hours)
```

**2. Exhaustion, which is not the same as ignorance.** A budget with `0` remaining and one
that has never been observed are different states. Unknown keeps the declared floor.
Exhausted waits for the reset instead of retrying into a wall.

**3. Failures.** Only a successful fetch used to update the timestamp, which meant a failing
resource was retried on *every* poll — and against a rate-limited endpoint the retries are
what prolong the block. Consecutive failures now double the interval, up to four doublings,
and a success clears it.

**4. Nothing.** There is no mechanism that shortens an interval below its declared floor.

### Cost is measured, not guessed

Every HTTP call and GraphQL query increments a counter, and each resource records what it
actually spent. A resource never yet fetched is assumed to cost 5, deliberately pessimistic
so the first polls back off rather than stampede.

### Quotas are tracked separately

A platform can meter several allowances independently, and conflating them hides the one that
matters. GitHub bills **REST per request** (5000/hour authenticated, 60 anonymous) and
**GraphQL in points** (5000/hour) against entirely separate budgets that drain and reset on
their own schedules.

Each resource declares which it spends, so a healthy REST reading cannot mask an exhausted
GraphQL one:

| Quota | GitHub resources |
| :--- | :--- |
| `rest` | profile, repos, orgs, pastes, notifications, issues, prs, org_repos, running_jobs |
| `graphql` | repo_detail, org_repo_detail, sponsors |

### GraphQL is billed on what you ask for

This is the part that is easy to get wrong. GitHub charges roughly **one point per hundred
nodes a query requests** — computed from the `first:` values multiplied down each path,
*whether or not that many exist*. A query asking for 100 releases on a repository with two
still pays for 100.

The repository walk therefore uses small pages and completes the overflow with follow-up
queries, rather than asking for the maximum allowed:

```
nodes per repository = 1 + 10 releases + (10 × 10 assets) + (2 × 50 refs) = 211
                     ≈ 2.11 points

930 repositories     ≈ 1,960 points of the 5,000/hour budget
```

Asking for 100 refs instead of 50 would bill every repository for refs it does not have, at
2,890 points. The lists stay complete either way — a repository with more than fifty tags
gets a second query.

For comparison, the same data over REST costs one request per repository for releases and
two more for branches and tags: around 2,800 requests against the REST allowance.

### Dependencies

Resources declare what they are derived from, so a chain can be reused rather than rebuilt:

```
org_repo_detail  ←  org_repos  ←  orgs
repo_detail      ←  repos
issues, prs      ←  repos
running_jobs     ←  repos
```

Refreshing a parent marks its children due, because they were built from inputs that have
since moved. A `min_cache` floor outranks that, so refreshing the repository list does not
drag every organisation's releases along on the next poll.

### What a skipped resource does

Nothing is lost. A resource that is not due keeps serving its last value, so the snapshot and
the sensors are always complete — they are just not all equally fresh. The `resources` block
records exactly how fresh each one is.

### Reading the schedule

Every decision above is published, so pacing is inspectable rather than opaque:

```jsonc
"resources": {
  "notifications": { "fetched_at": "2026-09-18T21:41:37+00:00", "next_due_in": 280.0,
                     "interval": 300, "min_cache": 300.0,
                     "cost": 1, "quota": "rest", "failures": 0 },
  "repo_detail":   { "fetched_at": "2026-09-18T21:01:57+00:00", "next_due_in": 1200.0,
                     "interval": 3600, "min_cache": 1800,
                     "cost": 16, "quota": "graphql", "failures": 0 }
}
```

`next_due_in` is computed by the same code path that decides whether to fetch, so it is the
decision rather than a second implementation of it.

That block is read back on startup. Without it every reload would begin with all timestamps
at zero and refetch the entire account — which is how a handful of redeploys in one afternoon
exhausted a GraphQL budget during development.

### If you are hitting limits

- Turn **Get detailed results** off for accounts you want counted but not catalogued.
- Raise the scan interval; the per-resource floors already prevent most work, but the
  coordinator still wakes.
- Check the `failures` and `next_due_in` fields in the snapshot before assuming something is
  broken — a resource may simply be waiting out a budget.

## Events

Fired on the Home Assistant bus when **Enable events** is on. Every payload carries
`platform` and `account`, and **the whole item** — so an automation never has to look
anything up.

| Event | Payload |
| :--- | :--- |
| `dev_cloud_new_repo` | `repository`, `repo` (with its releases, branches, tags) |
| `dev_cloud_repo_removed` | `repository`, `repo` — its last known state |
| `dev_cloud_repo_changed` | `repository`, `old`, `new`, `changed` (field names) |
| `dev_cloud_repo_renamed` | `repository`, `old`, `new`, `previous_name`, `name` |
| `dev_cloud_repo_archived` | `repository`, `old`, `new`, `archived` |
| `dev_cloud_repo_visibility_changed` | `repository`, `old`, `new`, `private` |
| `dev_cloud_stars_changed` | `repository`, `old`, `new`, `stars`, `previous_stars`, `delta` |
| `dev_cloud_forks_changed` | as above, with `forks` / `previous_forks` |
| `dev_cloud_new_release` | `repository`, `tag`, `release` (with its assets) |
| `dev_cloud_release_removed` | `repository`, `tag`, `release` |
| `dev_cloud_release_changed` | `repository`, `tag`, `old`, `new`, `changed` |
| `dev_cloud_new_branch` / `_branch_removed` | `repository`, `name`, `branch` |
| `dev_cloud_new_tag` / `_tag_removed` | `repository`, `name`, `tag` |
| `dev_cloud_new_issue` / `_issue_closed` | `repository`, `issue` |
| `dev_cloud_new_pull_request` / `_pull_request_closed` | `repository`, `pull_request` |
| `dev_cloud_new_package` / `_package_removed` | `name`, `package` |
| `dev_cloud_package_changed` | `name`, `old`, `new`, `changed` |
| `dev_cloud_new_org` / `_org_removed` | `name`, `organization` |
| `dev_cloud_new_downloads` | `repository`, `delta`, `total`, `previous_total`, `assets`, `breakdown` |
| `dev_cloud_new_pulls` | `name`, `package`, `delta`, `pulls`, `previous_pulls` |
| `dev_cloud_new_security_alert` | `repository`, `alert`, `severity`, `package`, `ecosystem`, `ghsa`, `cve`, `cvss`, `summary`, `url` |
| `dev_cloud_security_alerts_resolved` | `repository`, `resolved`, `remaining`, `alerts` |
| `dev_cloud_new_notification` | `title`, `repository`, `url`, `reason`, `subject_type`, `notification` |

`delta` is signed, so one trigger covers a star gained and a star lost.

Issue and pull request lists hold only open ones, so a disappearance means closed or merged
rather than deleted.

### How changes are detected

Against the **previous snapshot**, not the previous objects. The provider mutates its cached
objects in place, so comparing live objects would compare a thing against itself. The
snapshot is built every poll anyway, so the diff is free — and because the same document is
reloaded at startup, **events survive a restart** rather than starting from no baseline.

### What is deliberately not fired

**Nothing on the first poll of a fresh account.** Otherwise every existing repository,
package and organisation announces itself at once.

**Nothing from a collection that was not fetched.** A skipped, unavailable or failed resource
keeps its previous value and is not compared, so a failed request can never look like a mass
deletion.

**Nothing when a collection empties entirely.** Everything vanishing in one poll is far more
likely to be a bad response than a real deletion of all of it.

**New security alerts arrive individually; resolutions are batched.** A new advisory is
something to act on, so each gets its own event with the package, GHSA id, CVE and CVSS
score attached. Resolutions come in bulk — one dependency bump can clear dozens, and one
repository here has 68 open — so they are summarised per repository.

**Counters are batched per repository, not per asset.** Download counts tick upward
constantly and this account holds 3,581 release assets, so an event per asset would be
unusable — but an account-wide total is too coarse to act on. The middle ground is one
`dev_cloud_new_downloads` per *repository* whose assets moved, carrying that repository's
`delta`, its running `total`, and a `breakdown` naming each asset and tag that contributed.
`dev_cloud_new_pulls` works the same way, one event per image. Only increases are reported;
a falling count means something was deleted, which `release_changed` covers.

Organisation repositories emit their own events — a star, an advisory or a download on one
of them is the same occurrence as on any other repository. Whether they count towards the
*totals* sensors is a separate question, decided by the organisation option.

### Example automation

```yaml
triggers:
  - trigger: event
    event_type: dev_cloud_stars_changed
  - trigger: event
    event_type: dev_cloud_new_notification
  - trigger: event
    event_type: dev_cloud_new_release
  - trigger: event
    event_type: dev_cloud_new_downloads
actions:
  - action: notify.mobile_app_phone
    data:
      title: >-
        {% set d = trigger.event.data %}
        {% if trigger.event.event_type == 'dev_cloud_stars_changed' %}
          ⭐ {{ d.repository }} {{ '+' if d.delta > 0 else '' }}{{ d.delta }}
        {% elif trigger.event.event_type == 'dev_cloud_new_release' %}
          🚀 {{ d.repository }} {{ d.tag }}
        {% elif trigger.event.event_type == 'dev_cloud_new_downloads' %}
          📥 {{ "{:,}".format(d.delta) }} new downloads in {{ d.repository }}
        {% else %}
          🔔 {{ d.repository or d.platform }}
        {% endif %}
      message: >-
        {% set d = trigger.event.data %}
        {{ d.title | default(d.new.description) | default(d.repository) }}
```

Events are the right trigger for anything you want pushed. A sensor says how many
notifications exist; the event says one just arrived and what it was — which a state trigger
cannot, because the sensor's attributes carry only counts.

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
