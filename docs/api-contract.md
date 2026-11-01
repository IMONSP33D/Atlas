# Atlas API Contract

This is the only document Perseus needs. Keep a copy of it in the Perseus project so the `atlas_execute` tool definition stays in sync.

Base URL: `https://<atlas-host>:8443`
Auth: `Authorization: Bearer <word-seed-key>` on every request.
All bodies are JSON. All timestamps are ISO-8601 UTC.

## Endpoints

| Method | Path                         | Purpose |
|--------|------------------------------|---------|
| GET    | `/v1/health`                 | Liveness + version |
| GET    | `/v1/images`                 | Image catalog (runtimes, versions, warm/cold) |
| POST   | `/v1/execute`                | Run code (one-shot or inside a session) |
| POST   | `/v1/sessions`               | Open a session explicitly |
| GET    | `/v1/sessions/{id}`          | Session status |
| DELETE | `/v1/sessions/{id}`          | Close and revert |
| POST   | `/v1/sessions/{id}/screenshot` | Capture screen |
| POST   | `/v1/cape/submit`            | Submit a sample to CAPE |
| GET    | `/v1/cape/{task_id}`         | CAPE task status / report |

Most agent traffic goes through `/v1/execute` alone; the session endpoints exist for explicit control.

---

## GET /v1/images

```json
{
  "images": [
    { "runtime": "ephemeral", "id": "ephemeral", "build": "atlas-ephemeral:2026.11.1", "state": "ready", "default": true },
    { "runtime": "windows", "id": "win11-24h2", "build": "10.0.26100.2314", "state": "warm", "default": true },
    { "runtime": "windows", "id": "win10-22h2", "build": "10.0.19045.5131", "state": "cold" },
    { "runtime": "linux",   "id": "ubuntu-24.04", "build": "24.04.1+6.8.0-47", "state": "warm", "default": true },
    { "runtime": "cape",    "id": "cape-win10",   "build": "cape-2.4.1/win10-22h2", "state": "ready", "default": true }
  ],
  "limits": {
    "max_timeout_sec": 600,
    "session_idle_sec": 1800,
    "session_idle_warn_sec": 1500,
    "max_sessions": 2,
    "max_upload_bytes": 104857600,
    "max_files": 25
  }
}
```

`state`: `ready` (ephemeral/cape, always available), `warm` (VM booted at golden, attach in seconds), `cold` (needs boot), `busy` (held by a session), `unavailable` (disabled in config or failed health check).

---

## POST /v1/execute

### Request

```json
{
  "runtime": "ephemeral | linux | windows",
  "version": "win11-24h2",
  "mode": "execute | session",
  "session_id": null,
  "language": "python | node | powershell | bash | cmd | dotnet | rust | c | cpp | csharp | go | binary",
  "code": "print('hi')",
  "entrypoint": null,
  "args": [],
  "env": {},
  "stdin": null,
  "timeout_sec": 60,
  "network": false,
  "files": [
    { "name": "data.csv", "content_b64": "...", "sha256": "..." }
  ],
  "screenshot": {
    "on_complete": false,
    "on_error": false,
    "source": "guest | hypervisor | auto"
  },
  "request_id": "perseus-7f3a..."
}
```

Field rules:

- `runtime` required. `version` optional; omitted → catalog default for that runtime; ignored for `ephemeral`.
- `mode` default `execute`. `execute` with no `session_id` = one-shot (ephemeral container, or VM session that auto-closes and reverts). `session` with no `session_id` = open a session and run; response carries the new `session_id`. Either mode with a `session_id` = run inside that session.
- `language` picks the runner inside the guest. `binary` means `entrypoint` names a file from `files` to execute directly (Windows: `.exe`/`.msi`/`.bat`; Linux: executable bit set by Atlas).
- `code` or `entrypoint` required, not both. `entrypoint` is a path relative to the input directory.
- `timeout_sec` 1–600. Default 60.
- `network`: `false` | `true` | `{ "allow": ["pypi.org"] }`. Perseus has already clamped this to the human-set ceiling; Atlas enforces what it receives.
- `files`: ≤ 25 entries, ≤ 100 MB total decoded. `sha256` optional but checked if present (`FILE_HASH_MISMATCH`).
- `screenshot.source` `auto` = guest first, hypervisor fallback. VM runtimes only; ignored for ephemeral.
- `request_id` is echoed back for correlation; Perseus should make it unique.

### Response (200)

```json
{
  "request_id": "perseus-7f3a...",
  "status": "ok | error | timeout | killed",
  "runtime": "windows",
  "version": "win11-24h2",
  "build": "10.0.26100.2314",
  "session_id": "ses_01JBX...",
  "session": {
    "mode": "session",
    "started_at": "2026-11-01T18:02:11Z",
    "last_activity_at": "2026-11-01T18:04:50Z",
    "idle_seconds": 0,
    "idle_revert_in_seconds": 1800,
    "executions": 3
  },
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "wall_time_ms": 1832,
  "cpu_time_ms": 410,
  "peak_memory_bytes": 48234496,
  "network": { "requested": false, "effective": false, "allow": [] },
  "screenshot": {
    "source": "guest",
    "format": "png",
    "width": 1920,
    "height": 1080,
    "content_b64": "..."
  },
  "warnings": [],
  "errors": []
}
```

- `status` is about the *execution*, not the HTTP call. A non-zero exit is `status: "error"` with `exit_code` set; the HTTP status is still 200.
- `stdout`/`stderr` capped at 1 MB each; `*_truncated` tells the agent it lost output.
- `session` is `null` for one-shot ephemeral runs.
- `warnings` is a list of `{ "code": "SESSION_IDLE_WARNING", "message": "...", "data": { "seconds_remaining": 240 } }`. Non-fatal.
- `errors` is a list of the same shape and is populated when HTTP status is 4xx/5xx or `status` is not `ok`. See `errors.md`.

### Response (4xx / 5xx)

Same envelope, with `status: "error"`, no `stdout`/`stderr`, and `errors` populated. HTTP status maps from the error class (see `errors.md`).

---

## POST /v1/sessions

Open without running anything (useful to pre-warm before a multi-step task).

Request: `{ "runtime": "windows", "version": "win11-24h2", "network": false }`
Response: the `session` block above plus `session_id`, `runtime`, `version`, `build`.

## GET /v1/sessions/{id}

Returns the `session` block, `state: "active | reverting | closed"`, and `warnings`.

## DELETE /v1/sessions/{id}

Closes and reverts. Idempotent. Response `{ "session_id": "...", "state": "closed", "reverted": true }`.

## POST /v1/sessions/{id}/screenshot

Request: `{ "source": "auto", "window_title": null, "region": null }`
Response: the `screenshot` block above. `window_title` and `region` only work with `source: guest`.

---

## POST /v1/cape/submit

```json
{
  "file": { "name": "sample.exe", "content_b64": "...", "sha256": "..." },
  "version": "cape-win10",
  "network": false,
  "cape_options": {
    "package": null,
    "timeout_sec": 200,
    "options": "",
    "tags": ""
  },
  "callback_url": "https://perseus.local/atlas/callback",
  "request_id": "perseus-..."
}
```

Response (202): `{ "task_id": "cape_1234", "state": "pending", "eta_seconds": 300 }`

## GET /v1/cape/{task_id}

```json
{
  "task_id": "cape_1234",
  "state": "pending | running | processing | reported | failed",
  "runtime": "cape",
  "version": "cape-win10",
  "build": "cape-2.4.1/win10-22h2",
  "report": { "...": "CAPE JSON report, with `dropped` and `procmemory` sections removed" },
  "screenshots": [ { "index": 0, "content_b64": "..." } ],
  "errors": []
}
```

`report` is present only when `state` is `reported`. If `callback_url` was given, Atlas POSTs this same body to it once.

---

## Perseus tool definition (reference)

Perseus's agent tool should expose roughly:

```
atlas_execute(runtime, code | entrypoint, language, version?, mode?, session_id?, files?, timeout_sec?, network?, screenshot?)
atlas_session_close(session_id)
atlas_screenshot(session_id, window_title?)
atlas_images()
atlas_cape_submit(file, network?, options?)
atlas_cape_status(task_id)
```

Perseus's tool layer is responsible for: clamping `network` to the human policy before the call, surfacing `warnings` to the agent, and mapping `errors[].code` to agent-readable guidance (retry / downgrade to ephemeral / ask the human).
