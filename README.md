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
| `ASANA_WORKSPACES` | optional, comma-separated workspace gids to limit to |
| `JIRA_BASE_URL` | `https://your-site.atlassian.net` |
| `JIRA_EMAIL` | the account the token belongs to |
| `JIRA_API_TOKEN` | https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_JQL` | optional, overrides the default filter |
| `DAYPLAN_DB` | SQLite path, defaults to `~/.local/share/dayplan/dayplan.sqlite` |
| `DAYPLAN_SYNC_INTERVAL_MINUTES` | `>0` makes the server sync on a timer |

`dayplan doctor` prints what is configured and what is missing.

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

### Jira default filter

```
assignee = currentUser() AND status NOT IN ("Under Client Review", Closed,
  "Issue Closed", Done, "ON HOLD", "Waiting for Client", WAITING)
ORDER BY priority DESC, due ASC, created ASC, project ASC
```

### Known gap: the TickTick Inbox

The TickTick Open API does not return the Inbox from its project list, so
Inbox tasks are invisible. If you keep things there, find the inbox project id
in the web app URL (it looks like `inbox123456789`) and set
`TICKTICK_EXTRA_PROJECT_IDS=inbox123456789`.

Asana priority is a custom field rather than a native one, so all Asana tasks
come in at priority 0. TickTick and Jira priorities are normalized to 0-3.

## Running it on the ZimaBlade

```bash
cp .env.example .env    # then fill in the three tokens
docker compose up -d --build
docker compose logs -f dayplan
```

Then open `http://<zimablade>:8787`.

The container keeps its SQLite file in the `dayplan-data` volume mounted at
`/data`, so rebuilds do not lose your plan. `TZ` is set to `America/Bogota` in
the compose file: dayplan decides what "today" means from the container clock,
so if that is wrong every due date shifts by a day.

**dayplan has no authentication.** Keep the published port on the LAN, or put
it behind something that does auth. Do not expose 8787 to the internet.

To run the CLI against the hosted instance:

```bash
docker compose exec dayplan dayplan today
docker compose exec dayplan dayplan sync
```

## Running it locally

```bash
uv sync
uv run dayplan doctor
uv run dayplan sync
uv run dayplan serve          # http://127.0.0.1:8787
```

## Web UI

Two columns. **Pending** on the left is everything not yet placed on a day,
sorted overdue first, then by due date, then priority. **Plan** on the right is
one day, in your order.

- Drag a card from Pending into Plan to schedule it, at the position you drop it.
- Drag within Plan to reorder.
- Drag back out to Pending to unschedule it.
- The checkbox, the minutes box and the note field on a planned card save on change.
- `‹` `›` and the date picker move between days. Keys: `/` focus filter, `s` sync, `t` today.

## CLI

Task references are flexible: a short ref (`#12`), a Jira key (`TN-1171`), the
full id (`ticktick:6a98…`), or a unique piece of the title. Days accept
`today`, `tomorrow`, `+3`, `-1` or `2026-09-05`.

```bash
dayplan doctor                          # what is configured
dayplan sync                            # pull everything
dayplan sync -s jira                    # pull one source

dayplan list                            # everything open
dayplan list --pending -s jira          # unscheduled Jira only
dayplan list -q invoice --json          # search, machine readable
dayplan show '#12'

dayplan add '#12' TN-1171 --day today   # schedule, appended
dayplan add '#12' --day tomorrow --pos 0
dayplan order today '#12' TN-1171 '#9'  # set the exact order
dayplan move '#12' --day +1 --pos 2
dayplan drop '#12'                      # back to pending

dayplan note '#12' 'check the logs first'
dayplan est '#12' 45
dayplan done '#12'                      # local only
dayplan done '#12' --undo

dayplan today                           # the ordered plan
dayplan plan tomorrow
dayplan summary                         # JSON snapshot, for an agent
dayplan summary --text
```

`dayplan order` only needs the tasks you care about: anything already on that
day that you leave out keeps its relative order and is appended after, so a
partial reorder never silently drops work.

## HTTP API

`GET /api/docs` has the generated reference. The useful ones:

| Method | Path | Does |
| --- | --- | --- |
| `GET` | `/api/state?day=YYYY-MM-DD` | everything the UI needs in one call |
| `GET` | `/api/tasks?source=&day=&unplanned=&q=` | filtered task list |
| `GET` | `/api/summary?day=` | workload snapshot |
| `POST` | `/api/sync` | pull now, body `{"sources": ["jira"]}` optional |
| `PUT` | `/api/plan/{day}/order` | body `{"ids": [...]}`, sets the order |
| `POST` | `/api/plan/{day}/tasks` | body `{"task_id": "...", "position": 0}` |
| `DELETE` | `/api/plan/tasks/{task_id}` | unschedule |
| `PATCH` | `/api/tasks/{task_id}/plan` | body `{"note", "est_minutes", "done", "day"}` |
| `GET` | `/api/health` | liveness + configured sources |

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
