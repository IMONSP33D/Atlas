"""Session table and scheduling rules.

Rules enforced here (architecture.md / errors.md Class D):
  - max_sessions concurrently active
  - exclusive runtime pairs (windows vs linux)
  - one execution at a time per session
  - idle warning at session_idle_warn_sec, revert at session_idle_sec
  - network policy fixed for the life of a session
  - every session killed on shutdown

Idle handling is lazy + reaper: `touch`/`get` evaluate idleness against the
injected clock, and `reap()` reverts anything expired. Tests drive the clock.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .backends.base import Backend, GuestHandle
from .config import ImageEntry, LimitsConfig
from .errors import AtlasError, Notice, warning
from .validation import NetworkPolicy

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_session_id(now_ms: int | None = None) -> str:
    """ses_ + ULID (48-bit ms timestamp, 80 bits random), Crockford base32."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    rnd = secrets.randbits(80)
    n = (ts << 80) | rnd
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[n & 31])
        n >>= 5
    return "ses_" + "".join(reversed(chars))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Session:
    id: str
    entry: ImageEntry
    network: NetworkPolicy
    handle: GuestHandle
    started_mono: float
    started_at: datetime
    last_activity_mono: float
    last_activity_at: datetime
    executions: int = 0
    state: str = "active"           # active | reverting | closed
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    warned: bool = False

    @property
    def runtime(self) -> str:
        return self.entry.runtime

    def block(self, idle_limit: int, now_mono: float) -> dict:
        idle = max(0, int(now_mono - self.last_activity_mono))
        return {
            "mode": "session",
            "started_at": _iso(self.started_at),
            "last_activity_at": _iso(self.last_activity_at),
            "idle_seconds": idle,
            "idle_revert_in_seconds": max(0, idle_limit - idle),
            "executions": self.executions,
        }


class SessionManager:
    def __init__(self, limits: LimitsConfig, backends: dict[str, Backend],
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.limits = limits
        self.backends = backends
        self.clock = clock
        self.wall = wall
        self.sessions: dict[str, Session] = {}
        self.tombstones: dict[str, str] = {}     # id -> SESSION_NOT_FOUND reason
        self.shutting_down = False
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------------ open / lookup

    async def open(self, entry: ImageEntry, network: NetworkPolicy) -> tuple[Session, list[Notice]]:
        if self.shutting_down:
            raise AtlasError("SHUTTING_DOWN", "Atlas is restarting")
        await self.reap()
        active = [s for s in self.sessions.values() if s.state != "closed"]
        if len(active) >= self.limits.max_sessions:
            raise AtlasError("SESSION_LIMIT", f"{self.limits.max_sessions} sessions already active",
                             active=[{"session_id": s.id, "runtime": s.runtime, "version": s.entry.id} for s in active])
        for s in active:
            if s.runtime in self.limits.exclusive_with(entry.runtime):
                raise AtlasError("RUNTIME_EXCLUSIVE",
                                 f"a {s.runtime} session is active; {entry.runtime} cannot run at the same time",
                                 blocking_session=s.id)
            if s.entry.id == entry.id and s.entry.runtime == entry.runtime:
                raise AtlasError("VM_UNAVAILABLE", f"{entry.runtime}/{entry.id} is held by session {s.id}",
                                 reason="busy", session_id=s.id)

        backend = self.backends[entry.runtime]
        handle = await backend.open(entry, network)
        now, wall = self.clock(), self.wall()
        s = Session(new_session_id(int(wall.timestamp() * 1000)), entry, network, handle, now, wall, now, wall)
        self.sessions[s.id] = s
        warns: list[Notice] = []
        if handle.cold_boot_ms is not None:
            warns.append(warning("COLD_START", f"{entry.id} booted from cold", boot_ms=handle.cold_boot_ms))
        return s, warns

    async def get(self, session_id: str) -> Session:
        await self.reap()
        s = self.sessions.get(session_id)
        if s is None:
            reason = self.tombstones.get(session_id, "never_existed")
            if reason == "idle_reverted":
                raise AtlasError("SESSION_IDLE_REVERTED", "session was idle for too long and has been reverted",
                                 session_id=session_id)
            raise AtlasError("SESSION_NOT_FOUND", f"no such session ({reason})", reason=reason)
        if s.state == "reverting":
            raise AtlasError("SESSION_REVERTING", "session is mid-rollback", retry_after_sec=5)
        return s

    def check_compatible(self, s: Session, entry: ImageEntry, network: NetworkPolicy) -> None:
        if (s.entry.runtime, s.entry.id) != (entry.runtime, entry.id):
            raise AtlasError("SESSION_RUNTIME_MISMATCH",
                             f"session is {s.entry.runtime}/{s.entry.id}, request is {entry.runtime}/{entry.id}",
                             session_runtime=s.entry.runtime, session_version=s.entry.id)
        if network != s.network:
            raise AtlasError("SESSION_NETWORK_MISMATCH", "network policy is fixed when a session opens",
                             session_network=s.network.to_dict())

    # ------------------------------------------------------------------ activity

    def acquire(self, s: Session) -> asyncio.Lock:
        """Caller does `async with mgr.acquire(s)`; raises SESSION_BUSY instead of waiting."""
        if s.lock.locked():
            raise AtlasError("SESSION_BUSY", "an execution is already running in this session")
        return s.lock

    def touch(self, s: Session) -> list[Notice]:
        """Record activity; return idle warnings relevant to this call."""
        now = self.clock()
        idle = now - s.last_activity_mono
        warns: list[Notice] = []
        if s.warned:
            warns.append(warning("SESSION_IDLE_RESET", "idle timer reset by this call"))
            s.warned = False
        s.last_activity_mono = now
        s.last_activity_at = self.wall()
        s.executions += 1
        return warns

    def idle_warnings(self, s: Session) -> list[Notice]:
        idle = self.clock() - s.last_activity_mono
        if idle >= self.limits.session_idle_warn_sec:
            s.warned = True
            return [warning("SESSION_IDLE_WARNING", "session will be reverted if it stays idle",
                            seconds_remaining=max(0, int(self.limits.session_idle_sec - idle)))]
        return []

    # ------------------------------------------------------------------ close / reap

    async def close(self, session_id: str, reason: str = "closed") -> bool:
        s = self.sessions.get(session_id)
        if s is None:
            return False
        if s.state == "closed":
            return True
        s.state = "reverting"
        try:
            reverted = await self.backends[s.runtime].close(s.handle)
        except Exception:
            reverted = False
            raise
        finally:
            s.state = "closed"
            self.sessions.pop(session_id, None)
            self.tombstones[session_id] = reason
        return reverted

    async def reap(self) -> list[str]:
        now = self.clock()
        expired = [s.id for s in self.sessions.values()
                   if s.state == "active" and now - s.last_activity_mono >= self.limits.session_idle_sec]
        if self.limits.session_hard_lifetime_sec:
            expired += [s.id for s in self.sessions.values()
                        if s.state == "active" and s.id not in expired
                        and now - s.started_mono >= self.limits.session_hard_lifetime_sec]
        for sid in expired:
            try:
                await self.close(sid, reason="idle_reverted")
            except Exception:
                self.tombstones[sid] = "crashed"
        return expired

    def busy_entry_ids(self) -> set[tuple[str, str]]:
        return {(s.entry.runtime, s.entry.id) for s in self.sessions.values() if s.state != "closed"}

    async def start_reaper(self, interval_sec: float = 5.0) -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(interval_sec)
                try:
                    await self.reap()
                except Exception:
                    pass
        self._reaper = asyncio.create_task(loop())

    async def shutdown(self) -> None:
        self.shutting_down = True
        if self._reaper:
            self._reaper.cancel()
        for sid in list(self.sessions):
            try:
                await self.close(sid, reason="atlas_restarted")
            except Exception:
                self.tombstones[sid] = "atlas_restarted"
