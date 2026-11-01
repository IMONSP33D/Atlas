"""Scripted backend for tests and for developing against the contract with no infra.

Default behaviour: echo. `code` becomes stdout, exit 0. Special-case prefixes let
tests drive every outcome:

    #exit N        -> exit code N (status error, EXEC_NONZERO_EXIT)
    #timeout       -> status timeout
    #oom           -> status killed, EXEC_KILLED_OOM
    #sleep S       -> await S seconds before returning (for SESSION_BUSY tests)
    #raise CODE    -> raise AtlasError(CODE)
"""
from __future__ import annotations

import asyncio
import base64
import itertools
from dataclasses import dataclass, field

from ..config import ImageEntry
from ..errors import AtlasError, Notice
from ..validation import NetworkPolicy
from .base import ExecResult, ExecSpec, GuestHandle, Screenshot

_PNG_1X1 = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154"
                  "789c6360000002000154a24f5d0000000049454e44ae426082")
).decode()


@dataclass
class FakeBackend:
    runtimes: frozenset[str] = frozenset({"ephemeral", "linux", "windows"})
    cold: set[str] = field(default_factory=set)        # entry ids reported as cold
    unavailable: set[str] = field(default_factory=set)
    opened: list[GuestHandle] = field(default_factory=list)
    closed: list[GuestHandle] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    async def open(self, entry: ImageEntry, network: NetworkPolicy) -> GuestHandle:
        if entry.id in self.unavailable:
            raise AtlasError("VM_UNAVAILABLE", f"{entry.id} failed health check", reason="health_check")
        cold = entry.is_vm and (entry.id in self.cold or not entry.warm)
        h = GuestHandle(entry, network, ref=f"fake-{next(self._ids)}",
                        cold_boot_ms=30_000 if cold else None)
        self.opened.append(h)
        return h

    async def execute(self, handle: GuestHandle, spec: ExecSpec) -> ExecResult:
        code = (spec.code or "").strip()
        first = code.splitlines()[0] if code else ""
        if first.startswith("#raise "):
            raise AtlasError(first.split(None, 1)[1].strip())
        if first.startswith("#sleep "):
            await asyncio.sleep(float(first.split()[1]))
            code = "\n".join(code.splitlines()[1:])
            first = code.splitlines()[0] if code else ""
        if first.startswith("#exit "):
            n = int(first.split()[1])
            return ExecResult("error", n, "", f"exited {n}\n", wall_time_ms=5,
                              errors=[Notice("EXEC_NONZERO_EXIT", f"process exited {n}", {"exit_code": n})])
        if first == "#timeout":
            return ExecResult("timeout", None, "partial\n", "", wall_time_ms=spec.timeout_sec * 1000,
                              errors=[Notice("EXEC_TIMEOUT", f"hit timeout_sec={spec.timeout_sec}", {})])
        if first == "#oom":
            return ExecResult("killed", None, "", "", wall_time_ms=5,
                              errors=[Notice("EXEC_KILLED_OOM", "memory limit hit", {})])
        if spec.entrypoint:
            return ExecResult("ok", 0, f"ran {spec.entrypoint} {' '.join(spec.args)}\n", "", wall_time_ms=5)
        return ExecResult("ok", 0, code + ("\n" if code else ""), "", wall_time_ms=5, cpu_time_ms=1,
                          peak_memory_bytes=1 << 20)

    async def screenshot(self, handle: GuestHandle, source: str,
                         window_title: str | None, region: list[int] | None) -> Screenshot:
        if window_title == "nope":
            raise AtlasError("SCREENSHOT_WINDOW_NOT_FOUND", "no window matched", window_title=window_title)
        return Screenshot("guest" if source != "hypervisor" else "hypervisor", "png", 1, 1, _PNG_1X1)

    async def close(self, handle: GuestHandle) -> bool:
        self.closed.append(handle)
        return handle.entry.is_vm

    async def entry_state(self, entry: ImageEntry) -> str:
        if entry.id in self.unavailable:
            return "unavailable"
        if entry.runtime == "ephemeral":
            return "ready"
        if entry.id in self.cold or not entry.warm:
            return "cold"
        return "warm"
