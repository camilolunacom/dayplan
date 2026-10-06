# Current task display API

`GET /api/current-task` returns the first open task in the same order as the main Dayplan list. Manually positioned tasks come first, followed by acknowledged unranked tasks. New, untriaged tasks are excluded. No Toggl timer is consulted.

- `200 OK`: minimal task metadata as `application/json`, with the exact title and stable task ID.
- `204 No Content`: the main plan is empty; the response body is empty.
- `503 Service Unavailable`: the selected task has an invalid configured Toggl project ID. Fix the mapping; the endpoint does not guess a replacement or silently unmap the task.
- Successful, empty, and invalid-mapping responses use `Cache-Control: no-store`.
- Reads do not reorder, acknowledge, or modify tasks.
- Unsupported write methods return `405 Method Not Allowed`.

```json
{
  "schema_version": 1,
  "task": {
    "id": "ticktick:stable-task-id",
    "title": "Diseñar café ☕",
    "project": "Source project label",
    "toggl_project": "Exact Toggl project name",
    "toggl_project_id": 123456
  }
}
```

`id` and `title` are required strings. `project`, `toggl_project`, and `toggl_project_id` are nullable; a mapped project ID must be a JSON integer from 1 through 9223372036854775807. Boolean, string, fractional, non-positive, and out-of-range IDs are invalid, not coerced. The mapping fields come from the existing Dayplan mapping, without guessing from the source-project label. A task without a mapping has null Toggl fields and remains trackable without a project. The response contains only the fields shown above, excluding notes, raw provider data, URLs, and timer state.

Mapping metadata is cached during task sync. After correcting an invalid mapping, run a successful sync for the task's source before retrying the endpoint. If that provider is unavailable, the cached invalid mapping remains and the endpoint continues to return 503; clients should retain the last successful task with a stale/error indicator rather than clear it as an empty plan.

## Public deployment authentication

The public deployment uses a path-specific Cloudflare Access application with a Service Auth policy. Each client sends its own `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers on every request. Browser login is not required for these clients. Credentials are not query parameters and must not be committed, printed, or embedded in distributed firmware.

Use separate revocable tokens for hardware and desktop clients. Validate HTTPS certificates and handle authentication errors without treating an HTML login/error page as a task title. Only display a response with status 200, an `application/json` content type, and a valid version-1 schema; reject unknown schema versions and invalid field types. Clear the display on 204, and mark stale data or connectivity failures explicitly.

The tunnel must select the exact endpoint path before the general website ingress and validate the endpoint Access application's audience there. Other paths retain the website application's audience and email login policy. A token for the endpoint must not authorize the website, state API, or task mutation routes. The application itself remains unauthenticated on trusted LAN/Tailnet paths; Cloudflare provides public-path authentication.

Never disable the entire website's Access protection or tunnel JWT validation to enable this API.
