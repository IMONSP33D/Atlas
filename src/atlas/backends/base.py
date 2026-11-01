from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..config import ImageEntry
from ..errors import Notice
from ..validation import DecodedFile, NetworkPolicy


@dataclass
class GuestHandle:
    """An opened guest: a container, or a VM attached at its golden snapshot."""
    entry: ImageEntry
    network: NetworkPolicy
    ref: str                      # container id / vmid; opaque to the daemon
    cold_boot_ms: int | None = None


@dataclass
class ExecSpec:
    language: str
    code: str | None
    entrypoint: str | None
    args: list[str]
    env: dict[str, str]
    stdin: str | None
    timeout_sec: int
    files: list[DecodedFile]


@dataclass
class ExecResult:
    status: str                   # ok | error | timeout | killed
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    wall_time_ms: int = 0
    cpu_time_ms: int | None = None
    peak_memory_bytes: int | None = None
    errors: list[Notice] = field(default_factory=list)
    warnings: list[Notice] = field(default_factory=list)


@dataclass
class Screenshot:
    source: str
    format: str
    width: int
    height: int
    content_b64: str
    warnings: list[Notice] = field(default_factory=list)


class Backend(Protocol):
    runtimes: frozenset[str]

    async def open(self, entry: ImageEntry, network: NetworkPolicy) -> GuestHandle: ...
    async def execute(self, handle: GuestHandle, spec: ExecSpec) -> ExecResult: ...
    async def screenshot(self, handle: GuestHandle, source: str,
                         window_title: str | None, region: list[int] | None) -> Screenshot: ...
    async def close(self, handle: GuestHandle) -> bool:
        """Tear down / revert to golden. Returns True if a revert happened."""
        ...
    async def entry_state(self, entry: ImageEntry) -> str:
        """ready | warm | cold | unavailable (busy is decided by the session manager)."""
        ...
