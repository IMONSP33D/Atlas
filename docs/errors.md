# Atlas Error Catalog

Every error is returned as `{ "code", "message", "data", "retryable", "suggested_action" }` inside the `errors` array. `suggested_action` is one of:

- `retry` — same request, after `data.retry_after_sec` if present
- `retry_later` — capacity problem; back off
- `fix_request` — the request is malformed or violates a limit; the agent should change it
- `downgrade` — try `runtime: "ephemeral"` instead
- `new_session` — session is gone; open a new one and redo state
- `ask_human` — policy or infrastructure; the agent cannot fix this alone
- `none` — informational

## Class A — Auth / transport (HTTP 401 / 403 / 429)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `AUTH_MISSING` | 401 | No bearer token | ask_human |
| `AUTH_INVALID` | 401 | Token doesn't match any configured key | ask_human |
| `AUTH_REVOKED` | 403 | Key was valid but has been revoked in config | ask_human |
| `RATE_LIMITED` | 429 | Per-key request rate exceeded; `data.retry_after_sec` | retry |

## Class B — Request validation (HTTP 400)

| Code | Meaning | Action |
|------|---------|--------|
| `BAD_JSON` | Body is not valid JSON | fix_request |
| `SCHEMA_INVALID` | Missing/extra/wrong-typed field; `data.field`, `data.detail` | fix_request |
| `RUNTIME_UNKNOWN` | `runtime` not one of ephemeral/linux/windows/cape | fix_request |
| `VERSION_UNKNOWN` | `version` not in catalog for that runtime; `data.available[]` | fix_request |
| `LANGUAGE_UNSUPPORTED` | `language` not available in that runtime/version; `data.available[]` | fix_request |
| `CODE_AND_ENTRYPOINT` | Both `code` and `entrypoint` given | fix_request |
| `CODE_MISSING` | Neither `code` nor `entrypoint` given | fix_request |
| `ENTRYPOINT_NOT_FOUND` | `entrypoint` doesn't name an uploaded file | fix_request |
| `TIMEOUT_OUT_OF_RANGE` | `timeout_sec` < 1 or > 900 | fix_request |
| `MODE_INVALID` | `mode` not execute/session, or `session` mode on ephemeral/cape | fix_request |
| `SESSION_ID_INVALID` | Malformed session id | fix_request |
| `NETWORK_SPEC_INVALID` | `network` not bool or `{allow:[...]}`, or allowlist entry not a hostname | fix_request |
| `SCREENSHOT_UNSUPPORTED` | Screenshot requested on ephemeral | fix_request |
| `CAPE_FILE_REQUIRED` | CAPE submit without exactly one file | fix_request |
| `CAPE_OPTIONS_INVALID` | Unknown package / bad option string; `data.detail` | fix_request |

## Class C — Files (HTTP 400 / 413)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `FILE_TOO_LARGE` | 413 | Sum of decoded files > 100 MB; `data.bytes`, `data.limit` | fix_request |
| `FILE_COUNT_EXCEEDED` | 400 | More than 25 files | fix_request |
| `FILE_NAME_INVALID` | 400 | Path traversal, absolute path, or reserved name | fix_request |
| `FILE_NAME_DUPLICATE` | 400 | Two files with the same name | fix_request |
| `FILE_B64_INVALID` | 400 | Content not valid base64 | fix_request |
| `FILE_HASH_MISMATCH` | 400 | Provided sha256 ≠ computed; `data.name`, `data.expected`, `data.actual` | fix_request |
| `FILE_TRANSFER_FAILED` | 500 | Push to guest over virtio failed mid-stream; `data.name` | retry |

## Class D — Sessions (HTTP 404 / 409)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `SESSION_NOT_FOUND` | 404 | Unknown id; `data.reason` ∈ `never_existed`, `closed`, `idle_reverted`, `atlas_restarted`, `crashed` | new_session |
| `SESSION_IDLE_REVERTED` | 409 | Session hit 15 min idle and was reverted; state is gone | new_session |
| `SESSION_BUSY` | 409 | An execution is already running in this session (one at a time) | retry |
| `SESSION_LIMIT` | 409 | 2 sessions already active; `data.active[]` | retry_later |
| `RUNTIME_EXCLUSIVE` | 409 | A Windows session is active and Linux was requested, or vice versa; `data.blocking_session` | retry_later |
| `SESSION_RUNTIME_MISMATCH` | 409 | `session_id` belongs to a different runtime/version than requested | fix_request |
| `SESSION_REVERTING` | 409 | Session is mid-rollback; `data.retry_after_sec` | retry |
| `SESSION_NETWORK_MISMATCH` | 409 | Network policy on this call differs from the one the session was opened with (policy is fixed per session) | fix_request |

## Class E — Execution (HTTP 200, `status` ≠ ok)

These are reported in `errors` with HTTP 200 because the request was valid and the guest ran it.

| Code | `status` | Meaning | Action |
|------|----------|---------|--------|
| `EXEC_NONZERO_EXIT` | error | Process exited non-zero; `exit_code` set | none |
| `EXEC_TIMEOUT` | timeout | Hit `timeout_sec`; process tree killed; partial output returned | fix_request |
| `EXEC_KILLED_OOM` | killed | Guest/container memory limit hit | fix_request |
| `EXEC_KILLED_PIDS` | killed | Process/thread limit hit (fork bomb guard) | fix_request |
| `EXEC_KILLED_ADMIN` | killed | Killed from the dashboard | ask_human |
| `EXEC_SPAWN_FAILED` | error | Runner couldn't start the interpreter/compiler; `data.detail` | fix_request |
| `EXEC_COMPILE_FAILED` | error | Compiled languages only; compiler stderr is in `stderr` | fix_request |
| `EXEC_OUTPUT_TRUNCATED` | (warning) | stdout/stderr exceeded 1 MB | none |
| `EXEC_STDIN_REJECTED` | error | Program wanted interactive input; no TTY | fix_request |
| `EXEC_GUI_NO_SESSION` | error | Windows: program needs interactive desktop but agent isn't in Session 1 | ask_human |
| `EXEC_ELEVATION_DENIED` | error | Windows: UAC prompt appeared and policy doesn't auto-approve | ask_human |

## Class F — Network policy (HTTP 403 / 503)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `NETWORK_DENIED` | 403 | Request asked for network, Perseus clamp (or Atlas config) says no | ask_human |
| `NETWORK_ALLOWLIST_REJECTED` | 403 | Hostnames requested aren't within the human-set allowlist; `data.rejected[]` | ask_human |
| `NETWORK_UNAVAILABLE` | 503 | Policy permits network but Atlas cannot provide it (egress bridge/DNS down, or NIC hot-attach / egress bridge not implemented in this build); code was **not** run; `data.reason` | retry_later |
| `NETWORK_ATTACH_FAILED` | 500 | Could not hot-attach NIC to VM | retry |

## Class G — Guest / hypervisor (HTTP 500 / 503)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `GUEST_AGENT_UNREACHABLE` | 503 | No heartbeat on virtio channel within 30 s of boot | retry_later |
| `GUEST_AGENT_PROTOCOL` | 500 | Malformed frame from guest; `data.detail` | retry |
| `GUEST_AGENT_VERSION` | 500 | Guest agent version incompatible with Atlas; golden image needs rebuild | ask_human |
| `GUEST_CRASHED` | 500 | VM stopped unexpectedly mid-execution; session reverted | new_session |
| `GUEST_HUNG` | 500 | Guest stopped heartbeating during execution, or an exec outlived `timeout_sec` + grace without `exec_done`; forced revert | new_session |
| `VM_START_FAILED` | 503 | `qm start` failed; `data.proxmox_error` | retry_later |
| `VM_SNAPSHOT_MISSING` | 500 | Catalog entry has no `golden` snapshot | ask_human |
| `VM_ROLLBACK_FAILED` | 500 | Snapshot rollback failed; entry marked `unavailable` until fixed | ask_human |
| `VM_UNAVAILABLE` | 503 | Entry disabled, held by a session, failed health check, or quarantined after a failed rollback; `data.reason` ∈ `disabled`, `busy`, `health_check` (+ `data.problems[]`), `rollback_failed`, `no_default` | downgrade |
| `PROXMOX_UNREACHABLE` | 503 | Atlas can't reach the Proxmox API | ask_human |
| `PROXMOX_AUTH` | 500 | Proxmox token rejected | ask_human |
| `HOST_RESOURCES` | 503 | Host RAM/CPU below reserve; refusing to boot a cold VM; `data.free_mb` | downgrade |

## Class H — Ephemeral tier (HTTP 500 / 503)

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `DOCKER_UNAVAILABLE` | 503 | Docker daemon not responding | ask_human |
| `EPHEMERAL_IMAGE_MISSING` | 500 | `atlas-ephemeral` image not built | ask_human |
| `EPHEMERAL_START_FAILED` | 500 | `docker run` failed; `data.detail` | retry |

## Class I — Screenshots (HTTP 200 with warning, or 500)

| Code | Meaning | Action |
|------|---------|--------|
| `SCREENSHOT_GUEST_FALLBACK` | (warning) Guest didn't answer in 3 s; hypervisor screendump returned instead | none |
| `SCREENSHOT_FAILED` | Both sources failed; `data.guest_error`, `data.hypervisor_error` | retry |
| `SCREENSHOT_WINDOW_NOT_FOUND` | `window_title` matched nothing | fix_request |
| `SCREENSHOT_DOWNSCALED` | (warning) Image exceeded 5 MB and was downscaled; `data.original_size` = `{ width, height, bytes }` | none |

## Class J — CAPE (HTTP 4xx / 5xx / 200)

| Code | Meaning | Action |
|------|---------|--------|
| `CAPE_UNREACHABLE` | CAPE REST API not responding | ask_human |
| `CAPE_SUBMIT_REJECTED` | CAPE refused the sample; `data.detail` | fix_request |
| `CAPE_TASK_NOT_FOUND` | Unknown task id | fix_request |
| `CAPE_ANALYSIS_FAILED` | CAPE reported failure; `data.cape_status` | retry |
| `CAPE_NO_MACHINE` | No analysis VM available; `data.retry_after_sec` | retry_later |
| `CAPE_REPORT_TOO_LARGE` | (warning) Report exceeded 50 MB after stripping; summary returned, full report left on CAPE host | none |
| `CAPE_CALLBACK_FAILED` | (warning) Perseus callback URL returned non-2xx; report still available via GET | none |

## Class K — Atlas internal (HTTP 500 / 503)

| Code | Meaning | Action |
|------|---------|--------|
| `INTERNAL` | Unhandled exception; `data.trace_id` for the Atlas log | retry |
| `SHUTTING_DOWN` | Atlas is restarting; all sessions will be killed | retry_later |
| `CONFIG_INVALID` | Atlas started with a bad config; only `/v1/health` works | ask_human |
| `DISK_FULL` | Scratch or snapshot storage below reserve | ask_human |

## Warnings (non-fatal, HTTP 200)

| Code | When |
|------|------|
| `SESSION_IDLE_WARNING` | Idle ≥ 10 min; `data.seconds_remaining` |
| `SESSION_IDLE_RESET` | Informational: this call reset the idle timer after a warning was issued |
| `COLD_START` | Session opened on a cold entry; `data.boot_ms` |
| `VERSION_DEFAULTED` | `version` omitted; `data.version` says what was chosen |
| `NETWORK_CLAMPED` | Effective network narrower than requested (should not happen if Perseus clamps, but Atlas reports it) |
| `EXEC_OUTPUT_TRUNCATED` | See Class E |
| `SCREENSHOT_GUEST_FALLBACK` | See Class I |
| `SCREENSHOT_DOWNSCALED` | See Class I |
| `CAPE_REPORT_TOO_LARGE` | See Class J |
| `CAPE_CALLBACK_FAILED` | See Class J |
| `DEPRECATED_VERSION` | Catalog entry scheduled for removal; `data.removal_date` |
