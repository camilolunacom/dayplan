# Current task display API

`GET /api/current-task` returns the first open task in the same order as the main Dayplan list. Manually positioned tasks come first, followed by acknowledged unranked tasks. New, untriaged tasks are excluded. No Toggl timer is consulted.

- `200 OK`: exact task title as `text/plain; charset=utf-8`, without JSON, added newline, project metadata, IDs, or links.
- `204 No Content`: the main plan is empty; the response body is empty.
- Both responses use `Cache-Control: no-store`.
- Reads do not reorder, acknowledge, or modify tasks.
- Unsupported write methods return `405 Method Not Allowed`.

## Public deployment authentication

The public deployment uses a path-specific Cloudflare Access application with a Service Auth policy. Each client sends its own `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers on every request. Browser login is not required for these clients. Credentials are not query parameters and must not be committed, printed, or embedded in distributed firmware.

Use separate revocable tokens for hardware and desktop clients. Validate HTTPS certificates and handle authentication errors without treating an HTML login/error page as a task title. Only display a response with status 200 and a plain-text content type; clear the display on 204, and mark stale data or connectivity failures explicitly.

The tunnel must select the exact endpoint path before the general website ingress and validate the endpoint Access application's audience there. Other paths retain the website application's audience and email login policy. A token for the endpoint must not authorize the website, state API, or task mutation routes. The application itself remains unauthenticated on trusted LAN/Tailnet paths; Cloudflare provides public-path authentication.

Never disable the entire website's Access protection or tunnel JWT validation to enable this API.
