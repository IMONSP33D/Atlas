"""Typed error catalog. Mirrors docs/errors.md; tests/test_errors_catalog.py enforces parity.

Every error carries a `suggested_action` the agent can act on. Warnings share the
shape but never set `status: error`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Action(str, Enum):
    RETRY = "retry"
    RETRY_LATER = "retry_later"
    FIX_REQUEST = "fix_request"
    DOWNGRADE = "downgrade"
    NEW_SESSION = "new_session"
    ASK_HUMAN = "ask_human"
    NONE = "none"


_RETRYABLE = {Action.RETRY, Action.RETRY_LATER}


@dataclass(frozen=True)
class ErrorDef:
    code: str
    http: int                 # HTTP status; 200 for Class E / warnings
    action: Action
    cls: str                  # catalog class letter, or "W" for warnings
    exec_status: str | None = None   # Class E: value of top-level `status`
    warning: bool = False

    @property
    def retryable(self) -> bool:
        return self.action in _RETRYABLE


def _e(code: str, http: int, action: Action, cls: str, **kw: Any) -> ErrorDef:
    return ErrorDef(code, http, action, cls, **kw)


A, F, R, RL, D, NS, AH, N = (
    Action.ASK_HUMAN, Action.FIX_REQUEST, Action.RETRY, Action.RETRY_LATER,
    Action.DOWNGRADE, Action.NEW_SESSION, Action.ASK_HUMAN, Action.NONE,
)

_DEFS: list[ErrorDef] = [
    # Class A — Auth / transport
    _e("AUTH_MISSING", 401, AH, "A"),
    _e("AUTH_INVALID", 401, AH, "A"),
    _e("AUTH_REVOKED", 403, AH, "A"),
    _e("RATE_LIMITED", 429, R, "A"),
    # Class B — Request validation
    _e("BAD_JSON", 400, F, "B"),
    _e("SCHEMA_INVALID", 400, F, "B"),
    _e("RUNTIME_UNKNOWN", 400, F, "B"),
    _e("VERSION_UNKNOWN", 400, F, "B"),
    _e("LANGUAGE_UNSUPPORTED", 400, F, "B"),
    _e("CODE_AND_ENTRYPOINT", 400, F, "B"),
    _e("CODE_MISSING", 400, F, "B"),
    _e("ENTRYPOINT_NOT_FOUND", 400, F, "B"),
    _e("TIMEOUT_OUT_OF_RANGE", 400, F, "B"),
    _e("MODE_INVALID", 400, F, "B"),
    _e("SESSION_ID_INVALID", 400, F, "B"),
    _e("NETWORK_SPEC_INVALID", 400, F, "B"),
    _e("SCREENSHOT_UNSUPPORTED", 400, F, "B"),
    _e("CAPE_FILE_REQUIRED", 400, F, "B"),
    _e("CAPE_OPTIONS_INVALID", 400, F, "B"),
    # Class C — Files
    _e("FILE_TOO_LARGE", 413, F, "C"),
    _e("FILE_COUNT_EXCEEDED", 400, F, "C"),
    _e("FILE_NAME_INVALID", 400, F, "C"),
    _e("FILE_NAME_DUPLICATE", 400, F, "C"),
    _e("FILE_B64_INVALID", 400, F, "C"),
    _e("FILE_HASH_MISMATCH", 400, F, "C"),
    _e("FILE_TRANSFER_FAILED", 500, R, "C"),
    # Class D — Sessions
    _e("SESSION_NOT_FOUND", 404, NS, "D"),
    _e("SESSION_IDLE_REVERTED", 409, NS, "D"),
    _e("SESSION_BUSY", 409, R, "D"),
    _e("SESSION_LIMIT", 409, RL, "D"),
    _e("RUNTIME_EXCLUSIVE", 409, RL, "D"),
    _e("SESSION_RUNTIME_MISMATCH", 409, F, "D"),
    _e("SESSION_REVERTING", 409, R, "D"),
    _e("SESSION_NETWORK_MISMATCH", 409, F, "D"),
    # Class E — Execution (HTTP 200, status != ok)
    _e("EXEC_NONZERO_EXIT", 200, N, "E", exec_status="error"),
    _e("EXEC_TIMEOUT", 200, F, "E", exec_status="timeout"),
    _e("EXEC_KILLED_OOM", 200, F, "E", exec_status="killed"),
    _e("EXEC_KILLED_PIDS", 200, F, "E", exec_status="killed"),
    _e("EXEC_KILLED_ADMIN", 200, AH, "E", exec_status="killed"),
    _e("EXEC_SPAWN_FAILED", 200, F, "E", exec_status="error"),
    _e("EXEC_COMPILE_FAILED", 200, F, "E", exec_status="error"),
    _e("EXEC_OUTPUT_TRUNCATED", 200, N, "E", warning=True),
    _e("EXEC_STDIN_REJECTED", 200, F, "E", exec_status="error"),
    _e("EXEC_GUI_NO_SESSION", 200, AH, "E", exec_status="error"),
    _e("EXEC_ELEVATION_DENIED", 200, AH, "E", exec_status="error"),
    # Class F — Network policy
    _e("NETWORK_DENIED", 403, AH, "F"),
    _e("NETWORK_ALLOWLIST_REJECTED", 403, AH, "F"),
    _e("NETWORK_UNAVAILABLE", 503, RL, "F"),
    _e("NETWORK_ATTACH_FAILED", 500, R, "F"),
    # Class G — Guest / hypervisor
    _e("GUEST_AGENT_UNREACHABLE", 503, RL, "G"),
    _e("GUEST_AGENT_PROTOCOL", 500, R, "G"),
    _e("GUEST_AGENT_VERSION", 500, AH, "G"),
    _e("GUEST_CRASHED", 500, NS, "G"),
    _e("GUEST_HUNG", 500, NS, "G"),
    _e("VM_START_FAILED", 503, RL, "G"),
    _e("VM_SNAPSHOT_MISSING", 500, AH, "G"),
    _e("VM_ROLLBACK_FAILED", 500, AH, "G"),
    _e("VM_UNAVAILABLE", 503, D, "G"),
    _e("PROXMOX_UNREACHABLE", 503, AH, "G"),
    _e("PROXMOX_AUTH", 500, AH, "G"),
    _e("HOST_RESOURCES", 503, D, "G"),
    # Class H — Ephemeral tier
    _e("DOCKER_UNAVAILABLE", 503, AH, "H"),
    _e("EPHEMERAL_IMAGE_MISSING", 500, AH, "H"),
    _e("EPHEMERAL_START_FAILED", 500, R, "H"),
    # Class I — Screenshots
    _e("SCREENSHOT_GUEST_FALLBACK", 200, N, "I", warning=True),
    _e("SCREENSHOT_FAILED", 500, R, "I"),
    _e("SCREENSHOT_WINDOW_NOT_FOUND", 400, F, "I"),
    _e("SCREENSHOT_DOWNSCALED", 200, N, "I", warning=True),
    # Class J — CAPE
    _e("CAPE_UNREACHABLE", 503, AH, "J"),
    _e("CAPE_SUBMIT_REJECTED", 400, F, "J"),
    _e("CAPE_TASK_NOT_FOUND", 404, F, "J"),
    _e("CAPE_ANALYSIS_FAILED", 200, R, "J"),
    _e("CAPE_NO_MACHINE", 503, RL, "J"),
    _e("CAPE_REPORT_TOO_LARGE", 200, N, "J", warning=True),
    _e("CAPE_CALLBACK_FAILED", 200, N, "J", warning=True),
    # Class K — Atlas internal
    _e("INTERNAL", 500, R, "K"),
    _e("SHUTTING_DOWN", 503, RL, "K"),
    _e("CONFIG_INVALID", 503, AH, "K"),
    _e("DISK_FULL", 503, AH, "K"),
    # Warnings-only table
    _e("SESSION_IDLE_WARNING", 200, N, "W", warning=True),
    _e("SESSION_IDLE_RESET", 200, N, "W", warning=True),
    _e("COLD_START", 200, N, "W", warning=True),
    _e("VERSION_DEFAULTED", 200, N, "W", warning=True),
    _e("NETWORK_CLAMPED", 200, N, "W", warning=True),
    _e("DEPRECATED_VERSION", 200, N, "W", warning=True),
]

CATALOG: dict[str, ErrorDef] = {d.code: d for d in _DEFS}
assert len(CATALOG) == len(_DEFS), "duplicate error code in registry"


# ---------------------------------------------------------------------------

@dataclass
class Notice:
    """One entry of the `errors` or `warnings` array."""
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def definition(self) -> ErrorDef:
        return CATALOG[self.code]

    def to_dict(self) -> dict[str, Any]:
        d = self.definition
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "retryable": d.retryable,
            "suggested_action": d.action.value,
        }


class AtlasError(Exception):
    """Raise anywhere; the API layer turns it into the standard envelope."""

    def __init__(self, code: str, message: str | None = None, **data: Any) -> None:
        if code not in CATALOG:
            raise KeyError(f"unknown Atlas error code {code!r}; add it to docs/errors.md first")
        self.notice = Notice(code, message or code.replace("_", " ").lower(), dict(data))
        super().__init__(f"{code}: {self.notice.message}")

    @property
    def code(self) -> str:
        return self.notice.code

    @property
    def definition(self) -> ErrorDef:
        return CATALOG[self.code]

    @property
    def http_status(self) -> int:
        return self.definition.http


def warning(code: str, message: str, **data: Any) -> Notice:
    d = CATALOG[code]
    if not d.warning:
        raise ValueError(f"{code} is not a warning code")
    return Notice(code, message, dict(data))
