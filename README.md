# dayplan

Pulls the tasks assigned to you in **TickTick**, **Asana** and **Jira** into one
SQLite cache, then lets you drag them into the order you actually want to work
through them. There is a web UI for the dragging and a CLI/HTTP API so an agent
can reorder and annotate the same plan.

v1 is **read-only towards the sources**: it pulls, it never pushes. Ticking a
task off in dayplan is local only; the task stays open in TickTick/Asana/Jira
until you close it there. The next sync will notice it is gone and mark it
closed here.

## How it works

Three providers hit their APIs with a token and return open tasks. Everything
lands in one `tasks` table. A separate `plan` table holds the part that is
yours and yours only: which day a task sits on, its position in that day, a
note, and a time estimate. Because the plan is keyed on a stable task id,
re-syncing never disturbs your ordering.

```
TickTick ─┐
Asana    ─┼─> sync ─> tasks ──┐
Jira     ─┘                   ├─> web UI (drag & drop)
              plan  ──────────┴─> CLI / HTTP API
```

## Configuration

All three sources use a plain API token in the environment. Copy
`.env.example` to `.env` (for Docker) or to `~/.config/dayplan/env` (for a
local run) and fill it in.

| Variable | What it is |
| --- | --- |
| `TICKTICK_TOKEN` | TickTick Open API access token |
| `ASANA_TOKEN` | Asana Personal Access Token, https://app.asana.com/0/my-apps |
| `ASANA_WORKSPACES` | optional, comma-separated workspace gids to limit to — **leave unset** unless you mean it, see below |
| `JIRA_BASE_URL` | site URL, or the gateway for a scoped token — see below |
| `JIRA_SITE_URL` | optional, only for issue links when using the gateway |
| `JIRA_EMAIL` | the account the token belongs to |
| `JIRA_API_TOKEN` | https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_JQL` | optional, overrides the default filter |
| `DAYPLAN_DB` | SQLite path, defaults to `~/.local/share/dayplan/dayplan.sqlite` |
| `DAYPLAN_SYNC_INTERVAL_MINUTES` | `>0` makes the server sync on a timer |

`dayplan doctor` prints what is configured and what is missing.

### Narrowing what each source contributes

By default every source hands over everything open and assigned to you, which
gets noisy fast. Three knobs trim it:

**TickTick — drop future clutter.** `TICKTICK_DUE_WITHIN_DAYS=0` keeps only
what is due today or already overdue; `7` would keep the week. Recurring bills
and appointments months out stop crowding the list. Undated tasks are kept
(`TICKTICK_INCLUDE_UNDATED`, default true) on the grounds that an undated task
is not future clutter.

**Asana — one project, some columns.** `ASANA_PROJECTS=<gid>` narrows Asana to
those projects and ignores `ASANA_WORKSPACES` entirely. `ASANA_SECTIONS` then
keeps only the named board columns, so a project whose later columns are
archives (`Work Completed`, `Finalized`, `Decided Not to Pursue`) contributes
only live work. The project gid is the long number in the board URL:
`app.asana.com/1/<workspace>/project/<PROJECT_GID>/board/<view>`.

**Asana — subtasks.** `ASANA_INCLUDE_SUBTASKS` (default true) pulls the open
subtasks of every matching task. Subtasks are not project members, so they
never show up in a project listing and no section filter applies to them —
they are included because their parent matched. A subtask assigned to someone
else is skipped; an unassigned one under your task is kept. Each subtask
carries its parent in the project badge (`Project › Parent task`), because
without that a subtask like "See if we can export user names" reads as an
orphan.

`dayplan doctor` prints the active filters for each source.

### Do not narrow ASANA_WORKSPACES by accident

Leaving `ASANA_WORKSPACES` unset scans every workspace the token can see,
which is almost always what you want. Pinning it to the wrong gid yields zero
tasks **and no error**, since Asana answers HTTP 200 with an empty list — see
the integration status section. If you do want to narrow it, read the gids
from the API rather than from any other tool's view of your workspaces:

```bash
curl -H "Authorization: Bearer $ASANA_TOKEN" \
  'https://app.asana.com/api/1.0/users/me?opt_fields=workspaces.gid,workspaces.name'
```

### Getting the TickTick token

TickTick's Open API uses OAuth, so the token has to be minted interactively
once, on a machine with a browser:

```bash
mise use -g npm:@ticktick/ticktick-cli   # already installed here
ticktick auth login                      # opens the browser
jq -r .access_token ~/.config/ticktick-cli/config.json
```

Paste that value into `TICKTICK_TOKEN`. These tokens are long-lived but not
eternal (roughly six months), and there is no refresh path in this app: when
TickTick starts returning 401, repeat the two commands above.

### Which Jira token, and which scopes

Atlassian offers two kinds of API token. Both authenticate identically (HTTP
Basic, `email:token`) — **the only difference is the base URL**, which is why
picking the wrong one produces confusing 401s.

**Token without scopes.** There are no scopes to choose: it is a password
replacement that carries your full account permissions. Point
`JIRA_BASE_URL` at the site:

```
JIRA_BASE_URL=https://your-site.atlassian.net
```

**Token with scopes.** Grant exactly two, both read-only:

| Scope | Needed for |
| --- | --- |
| `read:jira-work` | `POST /rest/api/3/search/jql`, the issue search |
| `read:jira-user` | `GET /rest/api/3/myself`, the credential check |

(The granular equivalent of `read:jira-user` is `read:user:jira`.) A scoped
token **must** go through the gateway, not the site:

```
JIRA_BASE_URL=https://api.atlassian.com/ex/jira/<your-cloud-id>
```

Find your cloudId at `https://your-site.atlassian.net/_edge/tenant_info`.
Scopes are fixed at creation — to change them you create a new token.

This is the better choice for dayplan, because v1 never writes: a token
holding only those two scopes cannot modify Jira even if it leaks. The
gateway address cannot build `/browse/` links, so dayplan asks Jira for the
real site URL via `/serverInfo`; set `JIRA_SITE_URL` to skip that call.

`dayplan doctor` prints which mode it detected.

### Jira filter

The shipped default is deliberately generic, because named statuses are
per-project while `statusCategory` works on any workflow:

```
assignee = currentUser() AND statusCategory != Done
ORDER BY priority DESC, due ASC, created ASC, project ASC
```

Set `JIRA_JQL` to exclude the parked states your own workflow uses, for
example anything waiting on a client or on hold:

```
JIRA_JQL=assignee = currentUser() AND status NOT IN (Done, Closed, "On Hold", "Waiting for Client") ORDER BY priority DESC, due ASC
```

### Known gap: the TickTick Inbox

The TickTick Open API does not return the Inbox from its project list, so
Inbox tasks are invisible. If you keep things there, find the inbox project id
in the web app URL (it looks like `inbox123456789`) and set
`TICKTICK_EXTRA_PROJECT_IDS=inbox123456789`.

Asana priority is a custom field rather than a native one, so all Asana tasks
come in at priority 0. TickTick and Jira priorities are normalized to 0-3.

## Running it on the ZimaBlade

It lives on the host as a registered ZimaOS app and is reached over Tailscale
at `https://<tsdproxy-name>.<your-tailnet>.ts.net`.

```bash
ssh zima
cd /DATA/AppData/dayplan
cp .env.example .env    # then fill in the tokens
DOCKER_CONFIG=/DATA/.docker docker compose up -d --build
DOCKER_CONFIG=/DATA/.docker docker compose logs -f dayplan
```

`DOCKER_CONFIG=/DATA/.docker` is not optional on ZimaOS: `/root` is a
read-only filesystem, so the Docker CLI dies with
`mkdir /root/.docker: read-only file system` without it. The first build takes
about ten minutes on this hardware.

The container keeps its SQLite file in the `dayplan-data` volume mounted at
`/data`, so rebuilds do not lose your plan. `TZ` defaults to `America/Bogota`:
dayplan decides what "today" means from the container clock, so if that is
wrong every due date shifts by a day.

To run the CLI against the hosted instance:

```bash
ssh zima 'docker exec dayplan dayplan list'
ssh zima 'docker exec dayplan dayplan sync'
```

### Registering it as a ZimaOS app

Running it with bare `docker compose` works but leaves it invisible in the
ZimaOS dashboard: the app-management API lists the project, yet without
`store_info` the UI cannot draw a tile and the status stays `unknown`.
`store_info` comes from the **`x-casaos` block in the compose file**.

`deploy/zimaos-app.yml` is the ZimaOS-owned definition. It differs from the
repo's `docker-compose.yml` in four ways that all matter:

- **`image: localhost:5000/dayplan:0.1.0`.** The ZimaOS installer always
  pulls and ignores `pull_policy`, walking a chain of public mirrors and
  aborting when none has the image — so a bare local tag cannot be installed
  at all. `deploy/zimaos-registry.yml` runs a loopback registry (itself a
  ZimaOS app, shown in the dashboard as *Image Registry*) to serve it. It is
  permanent build infrastructure rather than something you use directly:
  bound to `127.0.0.1:5000` only, and needed for every rebuild. Uninstalling
  it deletes the stored images.
- **`env_file: /DATA/config/dayplan/.env`**, outside `/DATA/AppData`.
- **A bind mount** at `/DATA/AppData/dayplan/data` instead of a named volume.
- **Both `x-casaos` blocks**, which is what produces the tile.

Layout on the host, chosen so an uninstall cannot destroy anything
irreplaceable:

| Path | Holds | Survives uninstall |
| --- | --- | --- |
| `/DATA/src/dayplan/` | build source | yes |
| `/DATA/config/dayplan/.env` | tokens, mode 600 | yes |
| `/DATA/AppData/dayplan/data/` | `dayplan.sqlite` — tasks **and your plan** | **no** |
| `/var/lib/casaos/apps/dayplan/` | the compose ZimaOS owns | no |

Deploying a new build:

```bash
ssh zima
export DOCKER_CONFIG=/DATA/.docker
cd /DATA/src/dayplan && docker compose build          # ~10 min on this box
docker tag dayplan:0.1.0 localhost:5000/dayplan:0.1.0
docker push localhost:5000/dayplan:0.1.0
PORT=$(cat /var/run/casaos/app-management.url)
curl -s -X POST "$PORT/v2/app_management/compose" \
     -H 'Content-Type: application/yaml' \
     --data-binary @/DATA/src/dayplan/deploy/zimaos-app.yml
```

The POST returns 200 and installs asynchronously, so 200 does not mean it
worked. Confirm with `status: running` **and** `store_info` present:

```bash
curl -s "$PORT/v2/app_management/compose" \
  | python3 -c 'import json,sys; a=json.load(sys.stdin)["data"]["dayplan"]; \
      print(a["status"], "store_info" in a)'
```

> **Uninstalling dayplan from the ZimaOS UI deletes
> `/DATA/AppData/dayplan/`, including the SQLite file with your hand-made
> plan, and removes the image.** Copy `data/dayplan.sqlite` out first.

### Tailscale exposure (TSDProxy)

The compose labels hand the container to the TSDProxy instance already running
on this host:

```yaml
tsdproxy.enable: "true"
tsdproxy.name: "dash"
tsdproxy.port.1: "443/https:8787/http"
tsdproxy.funnel: "false"
```

Three things about that setup are easy to get wrong:

- **`network_mode: bridge` plus a published port are both required** if your
  TSDProxy sets `targetHostname: host.docker.internal`, because it then
  reaches apps through the *host's* published port rather than the container
  IP. A compose-created network would be unreachable, and the proxy logs
  nothing wrong.
- **`tsdproxy.name` must be unique.** With `preventDuplicates: true` a clash
  is rejected outright rather than silently misrouted. Note that TSDProxy's
  own dashboard occupies a name too, so check
  `tailscale status` before picking one.
- **Node state is keyed by name** at `datadir/<provider>/<name>/`. Reusing an
  existing name inherits that tailnet node, same address and MagicDNS name; a
  new name registers a new node and leaves the old one offline, which also
  means a later request for the old name gets a `-1` suffix.

Funnel is off, so this is tailnet-only. Leave it that way: dayplan has no
authentication of its own, and neither does the LAN port on 8787.

## Running it locally

```bash
uv sync
uv run dayplan doctor
uv run dayplan sync
uv run dayplan serve          # http://127.0.0.1:8787
```

### Tests

The web UI has tests; they need no dependency beyond Node, which is already
here for the TickTick CLI.

```bash
node --test tests/*.test.mjs
```

`tests/harness.mjs` runs `static/app.js` in a `vm` context against a stub DOM,
a stub `fetch` and a fake clock, which is what lets a test step 60 seconds and
watch what the page asks the server for. No jsdom, no bundler, no framework.

## Web UI

Two columns side by side on a wide screen, stacked on anything narrower than
1100px.

**The list** is yours, in priority order. Position 1 renders as a large
featured card labelled *working on now* — it is the first card of the same
list, not a separate widget, which is what makes dragging something to the top
change what you are working on. Below the hand-ordered head, a divider marks
the tasks you have seen but not ranked; those keep a stable order and never
reshuffle themselves.

There are no estimates, no notes and no way to tick a task off here. All
three were invented rather than asked for, and none survived contact: an
estimate nobody fills in is noise on every row, a local tick never reached
the source so the next sync undid it, and a note duplicates a field the
source already has. What is left is the only part the sources cannot hold —
the order. A task leaves the list by being closed where it lives.

**New** is everything a sync brought in that you have never placed. It sits in
its own column precisely so a sync cannot disturb an arrangement you already
made. Drag one across (or hit `→`) to keep it; drag one back to untriage it,
which drops its place in the order.

- **Drag by the handle on the left** — the ⠿ strip, a real button spanning the
  full card height at no less than 44x40px. It is the only element with
  `touch-action: none`, so the rest of the card stays scrollable; anything
  smaller and a near miss lands on the card and pans the page instead. While a
  drag is in flight `body.dragging` blocks panning entirely, in case a finger
  slips off the handle. Reordering uses Pointer Events rather than HTML5
  drag-and-drop, which never fires on touch in any mobile browser.
- Drag to the top to change the current task; drag across to the other column
  to keep or untriage. Holding near the top or bottom of the screen
  auto-scrolls, since the finger holding a card cannot also scroll. That
  scrolling is scripted, which is why it still works while touch panning is
  locked — and it measures the edges of whichever element actually scrolls,
  not of the list, whose own edges are off screen when the layout is stacked.
- Keys: `/` focus filter, `s` sync, `i` integration status, `Esc` close.
- **It refreshes itself every 60 seconds**, so a tab left open picks up what
  the 15 minute background sync did without a reload. The polling is
  deliberately timid: it stops while the tab is hidden and catches up the
  moment it comes back, it skips a tick while a drag is in flight, it never has
  two `/api/state` requests open at once, and it only redraws when the tasks or
  a visible integration status actually changed — a redraw would otherwise
  throw away the Toggl button and the scroll position every minute. A failed
  poll stays quiet; only a load you asked for toasts.

Theming is [Flexoki](https://stephango.com/flexoki) and follows
`prefers-color-scheme`: paper and the 600 accents in light, black and the 400
accents in dark.

### Toggl Track, through the extension

The featured card carries a real Toggl Track button when you have the browser
extension — and only then. There is no Toggl API token anywhere in dayplan.

It works because the extension ships a generic integration, listed in its
settings as **DOM Integration**, that turns any `.toggl-root` element into a
timer button using `data-*` attributes. dayplan renders the empty slot and the
extension fills it, tracking through its own session. With no extension the
slot stays empty and `:empty` collapses it, so nothing shows.

One-time setup: in the extension's settings, under permissions, add dayplan's
host under **Custom Domain permissions** and pick **DOM Integration**, then
grant the permission.

Only the featured card gets a button, because that is the task you are
tracking. `deploy/toggl-projects.example.json` maps tasks onto Toggl
projects:

```json
{
  "asana": [{ "project_contains": "Open Path", "toggl_project": "Open Path" }],
  "jira":  [{ "parent": "ALHM-7", "toggl_project": "ALHM Epic 7" },
            { "key_prefix": "TN", "toggl_project": "Support" }]
}
```

**`toggl_project` is the project name, and it is the only field that works.**
From the shipped extension (4.11.21), both the content script and the
background handler resolve a project like this:

```js
case "resolve-project": {
  const { projectName: n, selectedWorkspaceId: r } = e.payload
  const d = Object.values(projects).filter(u => u.name === n)
  return d.find(u => u.workspace_id === r) ?? d[0]
}
```

The payload carries only `projectName`. `data-project-id` is read by
`dom-integration.js` and handed to `createTimerLink`, where nothing consumes
it — a dead parameter. Confirmed by experiment: the id alone produced
`project_id: null`; adding the name produced the right project.

Matching is exact, and among identically named projects it returns the
first. So **only give a rule a `toggl_project` when that name identifies
exactly one project.** Where it does not, leave it out and the task tracks
without a project, which beats tracking against some other client's project.
`toggl_project_id` is carried for reference and has no effect. `dayplan
doctor` reports how many rules are intentionally unprojected.

Matchers are `project_contains`, `title_contains`, `parent` (a Jira epic key),
`key_prefix` and `tag`; all of those present in a rule must hold. First match
wins in file order, so put epic rules above project-key rules. Anything
unmatched gets no project rather than a guessed one. Resolution happens at
**sync** time, where the Jira epic and Asana project are still available, so
editing the map means re-syncing. `dayplan doctor` prints how many rules
loaded.

## CLI

Task references are flexible: a short ref (`#12`), a Jira key (`TN-1171`), the
full id (`ticktick:6a98…`), or a unique piece of the title. Days accept
`today`, `tomorrow`, `+3`, `-1` or `2026-09-05`.

```bash
dayplan doctor                          # what is configured
dayplan sync                            # pull everything
dayplan sync -s jira                    # pull one source

dayplan list                            # the list, in order, ▶ marks current
dayplan list -s jira                    # one source
dayplan list -q invoice --json          # search, machine readable
dayplan new                             # untriaged arrivals
dayplan show '#12'

dayplan order '#12' TN-1171 '#9'        # pin these, in this order
dayplan top '#12'                       # make it the current task
dayplan unpin '#12'                     # back to the default order
dayplan keep '#12'                      # accept a new task into the list
dayplan dismiss '#12'                   # send it back to the new pile


dayplan current                         # just the one to work on now
dayplan current --json                  # same, for an agent
dayplan summary                         # JSON snapshot, for an agent
dayplan summary --text
```

`dayplan order` only needs the tasks you care about: anything already pinned
that you leave out keeps its relative order and follows after, so a partial
reorder never silently drops work.

## HTTP API

`GET /api/docs` has the generated reference. The useful ones:

| Method | Path | Does |
| --- | --- | --- |
| `GET` | `/api/state` | everything the UI needs: `tasks`, `new`, `current` |
| `GET` | `/api/tasks?source=&day=&unplanned=&q=` | filtered task list |
| `GET` | `/api/summary` | workload snapshot, including `current` |
| `GET` | `/api/integrations?history=` | per-source health, state and last error |
| `GET` | `/api/sync-log?limit=&source=` | raw recent sync attempts |
| `POST` | `/api/sync` | pull now, body `{"sources": ["jira"]}` optional |
| `PUT` | `/api/order` | body `{"ids": [...]}`, pins that prefix in that order |
| `DELETE` | `/api/order/{task_id}` | unpin, back to the default order |
| `POST` | `/api/accept` | body `{"task_id"}`, new → list |
| `POST` | `/api/dismiss` | body `{"task_id"}`, list → new |
| `GET` | `/api/health` | liveness + configured sources |

The web UI's CSS and JS are served at `/static/<file>?v=<content hash>`. The
document itself is sent `Cache-Control: no-cache` so it always revalidates and
therefore always points at the current hash, while the hashed URLs are
`immutable` for a year. Without that a browser can keep a deploy's worth of
stale CSS — which is exactly what happened on the tablet mid-development.
Unversioned assets like `icon.svg`, which the ZimaOS tile links to directly,
revalidate instead.

A failing provider never takes the others down: `/api/sync` returns per-source
counts and a per-source `errors` map, and tasks from a source that failed are
left exactly as they were.

### Why Jira is checked twice

`POST /rest/api/3/search/jql` answers **HTTP 200 with an empty issue list**
when the credentials are rejected: it silently treats the caller as anonymous,
and `assignee = currentUser()` then matches nothing. A bad Jira token would
therefore look exactly like "no work assigned to you". `/rest/api/3/myself`
does return 401, so dayplan calls that first on every Jira sync and fails
loudly instead.

## Roadmap

v2 is the write-back direction: completing in dayplan closes the task at the
source. That needs `tasks:write` on TickTick, a PAT with write scope on Asana,
and a transition id per Jira workflow, so it is deliberately out of v1.
