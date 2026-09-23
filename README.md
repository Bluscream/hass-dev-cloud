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
| Views | views | **total** |
| Clones | clones | **total** |
| Last Updated | — | — *(diagnostic)* |

Downloads, Pulls, Views and Clones are totals: a download once served is never un-served, so
the change between periods is meaningful. They are `total` rather than `total_increasing`
because these figures *can* fall — an asset gets deleted, a traffic day ages out of the
retention window — and `total_increasing` would read that fall as a counter reset and add the
whole figure again as if it were new. Everything else counts things that can be deleted, so
it is a measurement: Home Assistant records mean, min and max rather than an accumulating
sum.

**Every one of these produces long-term statistics**, exactly as `sensor.speedtest_download`
does — hourly buckets kept for years after the detailed history has been purged, so a
year-scale graph of stars or downloads works without keeping a year of raw states. Nothing
extra is needed for this: a sensor earns statistics by declaring a state class, and every
sensor above does.

Attributes hold aggregates only. The profile sensor additionally carries `json_url`, the
link to the snapshot; it is not repeated on every entity, because it is the same URL for all
of them. **Last Updated** is a timestamp rather than an age, so the frontend renders a live
"3 minutes ago" by itself instead of the value having to be re-recorded every poll; its
attributes carry the per-resource schedule, so a stalled collection is visible without
opening the JSON.

---

## Buttons

| Button | What it does |
| :--- | :--- |
| Force Refresh *(diagnostic)* | Clears every resource's schedule and polls immediately |

Resources are normally paced against the API budget, which is what keeps the integration
inside it but also means a change made a moment ago can take an hour to show. This is the
override. It clears the minimum-cache floors and the failure backoff, but **not** the
rate-limit ceiling: with the allowance spent nothing would succeed before it resets, and a
button press is not an argument against that. Traffic stays a rotating sweep when forced —
the next section is why it has to be.

---

## Repository traffic

GitHub's traffic graphs — views, clones, referring sites and popular paths — answer who
looked at a repository, and then forget: every endpoint serves a rolling **fourteen-day**
window and nothing older. This integration keeps its own copy, so the history grows for as
long as it runs.

Two properties of that API shape the whole design.

**Four requests per repository, and push access required.** Sweeping six hundred
repositories at once is roughly half an hourly quota, so a sweep takes a fixed handful
(`TRAFFIC_REPOS_PER_SWEEP`, default 10), least recently fetched first, and works its way
round. Fourteen days of retention is the real deadline: as long as every repository is
revisited inside it nothing is lost, and ten repositories every ten minutes covers six
hundred in about ten hours. A constant per-sweep cost is also what lets the scheduler measure
the resource once and pace it against the budget like any other. Repositories the token
cannot push to answer 403 forever; they are remembered and retried weekly rather than
rediscovered every sweep.

**Views and clones are exact per day; referrers and paths are not.** A daily bucket can be
accumulated honestly — the count for the 20th is the count for the 20th whenever you ask, and
re-fetching a day still in progress replaces the partial figure rather than adding to it.
Referrers and paths come back as totals over *overlapping* rolling windows, so summing
successive fetches would count one visit up to fourteen times. Those keep the latest window
plus `first_seen` and `last_seen`, which is both truthful and enough to answer "is this a
referring site we have never had before".

Days with no traffic are not stored: six hundred quiet repositories would otherwise be a
quarter of a million empty buckets in the snapshot.

If the quota runs out partway through a sweep it **stops** rather than spending the rest of
the batch collecting refusals. What was fetched is kept, the repositories it did not reach
keep their place at the front of the rotation, and the refusal itself tells the scheduler the
allowance is gone, so every resource waits for the reset.

---

## The browsable index

`/local/dev/<platform>/index.html` renders the same data for a person rather than a program:
profile, headline totals, snapshot and resource-schedule metadata, and top-25 leaderboards —
repositories by stars, downloads, releases, assets, forks, watchers, issues, PRs and
advisories; organisations by repository count and stars; releases and individual assets by
downloads; views, clones and referring sites; packages and gists.

One self-contained file with no external requests: `/local` is served by Home Assistant
itself, often on a LAN with no route out, and a page about your own data should not need
somebody else's CDN to render. Accounts are discovered from the directory, so a second
account on the same platform appears as a switcher with no code change. It is rewritten only
when it would actually differ.

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

Fired on the Home Assistant bus when **Enable events** is on. There are **two event types**,
not one per kind of change: a consumer that had to enumerate twenty-nine of them to see
anything was enumerating them wrong, and a busy poll fired hundreds of individual events.
What a change *is* lives in the payload instead.

### `dev_cloud_update`

One poll's changes, batched. Every payload carries:

| Field | Meaning |
| :--- | :--- |
| `platform`, `account` | Which entry this came from |
| `run_id` | The poll's own timestamp — the same on every chunk of one poll |
| `chunk`, `chunks` | Position in the run, so three parts of one digest are not three digests |
| `count`, `total` | Changes in this chunk, and in the whole poll |
| `changes` | The list below |

Chunks are sized by **serialising**, not estimated per item: a repository description and a
security advisory differ by two orders of magnitude, so any per-item guess is wrong in one
direction or the other. Each chunk is kept well under the recorder's 32 KiB ceiling. A single
change too large to ever fit loses its `old`/`new` and gains `detail: {truncated: true}`,
rather than being dropped — the full item is always in the snapshot.

Each entry in `changes`:

| Field | Meaning |
| :--- | :--- |
| `kind` | What happened: `stars_changed`, `new_release`, `new_referrer`, … |
| `thing` | Coarse category to pick an emoji from: `star`, `release`, `referrer`, … |
| `subject` | Already human-readable: a repository name, a tag, `#42`, a hostname |
| `repository` | Where it happened, when that is a different thing from `subject` |
| `url` | Somewhere to send a notification tap |
| `old`, `new` | Before and after, on anything that changed value rather than appeared |
| `delta` | Signed, present only when both sides are numeric |
| `first` | This metric just left zero — see below |
| `detail` | Small kind-specific extras: changed fields, a download breakdown |

`kind` is the full vocabulary the old event types carried, so nothing was lost by collapsing
them; `thing` exists so a consumer can render a line without knowing all of them.

### `dev_cloud_notification`

One per newly arrived unread notification, carrying the same envelope plus the change
itself. It keeps its own event type because a notification is already the unit a person acts
on, and burying it as a line in a digest defeats the point.

### Firsts

`first: true` marks a counter leaving zero: a repository's first star, first fork, first
watcher, first clone, first download, its first release ever, or a referring site never seen
before. Only the diff knows the previous value, so a consumer computing this itself would
need a history it does not have.

Two deliberate exceptions. A brand-new account announces nothing at all, so its existing
stars are not all "firsts". And a repository's **first traffic sweep** is a baseline rather
than news: GitHub returns fourteen days at once, and reporting that as a delta would announce
a fortnight of history as if it had just happened.

### How changes are detected

Against the **previous snapshot**, not the previous objects, and **after** a poll finishes
rather than while one is in flight. The provider mutates its cached objects in place, so
comparing live objects would compare a thing against itself. The snapshot is built every poll
anyway, so the diff is free — and because the same document is reloaded at startup, **events
survive a restart** rather than starting from no baseline.

A poll is routinely *partial*: each resource has its own schedule, and one that was not due
keeps serving its previous value. Those compare equal and produce nothing, so a partial
scrape reports exactly the parts that moved.

### What is deliberately not fired

**Nothing on the first poll of a fresh account.** Otherwise every existing repository,
package and organisation announces itself at once.

**Nothing from a collection that was not fetched.** A skipped, unavailable or failed resource
keeps its previous value and is not compared, so a failed request can never look like a mass
deletion.

**Nothing when a collection empties entirely** — at either level. An account whose whole
repository list vanished in one poll is a bad response, not 580 deletions. The same rule
applies *inside* a repository that survived: if its releases, branches, tags, issues, PRs or
advisories come back empty while the repository itself is still there, that is a rate-limited
or incomplete sub-request, not a mass deletion. Removals are only reported when something
else in the same collection survived to prove the response was real. A repository that was
genuinely deleted emits one `repo_removed` carrying its last known state, not forty lines
about its refs.

**New security alerts arrive individually; resolutions are batched.** A new advisory is
something to act on, so each gets its own change with the package, GHSA id, CVE and CVSS
score attached. Resolutions come in bulk — one dependency bump can clear dozens, and one
repository here has 68 open — so they are summarised per repository.

**Counters are batched per repository, not per asset.** Download counts tick upward
constantly and this account holds 3,581 release assets, so a change per asset would be
unusable — but an account-wide total is too coarse to act on. The middle ground is one
`new_downloads` per *repository* whose assets moved, carrying that repository's `delta`, its
running `total`, and a `breakdown` naming each asset and tag that contributed. `new_pulls`
works the same way, one per image. Only increases are reported; a falling count means
something was deleted, which `release_changed` covers.

Organisation repositories produce their own changes — a star, an advisory or a download on
one of them is the same occurrence as on any other repository. Whether they count towards the
*totals* sensors is a separate question, decided by the organisation option.

### Example automation

```yaml
triggers:
  - trigger: event
    event_type: dev_cloud_update
  - trigger: event
    event_type: dev_cloud_notification
variables:
  d: "{{ trigger.event.data }}"
  things: >-
    {{ {'repository': '🗃️', 'star': '⭐', 'fork': '🍴', 'watcher': '👁️',
        'release': '🚀', 'download': '📥', 'security': '🛡️', 'view': '📈',
        'clone': '📋', 'referrer': '🔗', 'notification': '🔔'} }}
actions:
  - action: notify.mobile_app_phone
    data:
      title: >-
        {% if trigger.event.event_type == 'dev_cloud_notification' %}
          🔔 {{ d.subject }}
        {% else %}
          ☁️ {{ d.count }} changes ({{ d.chunk }}/{{ d.chunks }})
        {% endif %}
      message: >-
        {% if trigger.event.event_type == 'dev_cloud_notification' %}
          {{ d.repository }}
        {% else %}
          {% set ns = namespace(lines=[]) %}
          {% for c in d.changes %}
            {% set sign = ('+' if c.delta > 0 else '') ~ c.delta if c.delta is defined else '' %}
            {% set ns.lines = ns.lines + [
                 things.get(c.thing, '•') ~ ' ' ~ c.subject ~ ' ' ~ sign] %}
          {% endfor %}
          {{ ns.lines | join('\n') }}
        {% endif %}
```

A `first` is worth separating out and sending louder:

```yaml
  - repeat:
      for_each: >-
        {{ d.changes | default([]) | selectattr('first', 'defined')
                                   | selectattr('first') | list }}
      sequence:
        - action: notify.mobile_app_phone
          data:
            title: "🎉 First {{ repeat.item.thing }} — {{ repeat.item.subject }}"
            message: "{{ repeat.item.repository }}"
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
