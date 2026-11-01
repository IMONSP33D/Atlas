"""exec verb: per-language command table and process supervisor."""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from .protocol import Frame

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

WIN = sys.platform == "win32"
SIGTERM, SIGKILL = getattr(signal, "SIGTERM", 15), getattr(signal, "SIGKILL", 9)
STREAM_CAP = 1024 * 1024
KILL_GRACE_SEC = 2.0
MAX_TIMEOUT_SEC = 900
VCVARS = r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"

Emit = Callable[[Frame], Awaitable[None]]
Plan = tuple[list[list[str]], "list[str] | Callable[[], list[str]]"]


class PlanError(Exception):
    """Reported as exec_done reason=spawn_failed."""


def valid_name(name: object) -> bool:
    """Bare file name only: no separators, no '..', no NUL, no drive colon."""
    return (isinstance(name, str) and name not in ("", ".", "..") and ".." not in name
            and not any(c in name for c in "/\\\0:"))


# --------------------------------------------------------------------------- Linux table
# Mirrors ephemeral/runner.sh. Each entry: (src, build_dir, input_dir) -> (compile_steps, run_argv).
# run_argv may be a zero-arg callable evaluated after the compile steps (dotnet needs the built dll).

def _dotnet(src: str, build: str, indir: str) -> Plan:
    if sorted(Path(indir).glob("*.csproj")):
        steps = [["dotnet", "build", indir, "-c", "Release", "-o", build, "--nologo", "-v", "q"]]
    else:
        proj = Path(build, "proj")
        proj.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, proj / "Program.cs")
        (proj / "proj.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
            "<TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable>"
            "<ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>\n")
        steps = [["dotnet", "build", str(proj), "-c", "Release", "-o", build, "--nologo", "-v", "q"]]

    def run() -> list[str]:
        dlls = sorted(Path(build).glob("*.dll"))
        if not dlls:
            raise PlanError("dotnet build produced no .dll")
        return ["dotnet", str(dlls[0])]
    return steps, run


def _compiled(cc: list[str], tail: list[str] | None = None, exe: str = "a.out"):
    def plan(src: str, build: str, indir: str) -> Plan:
        out = os.path.join(build, exe)
        return [[*cc, "-o", out, src, *(tail or [])]], [out]
    return plan


LINUX_TABLE: dict[str, Callable[[str, str, str], Plan]] = {
    "python": lambda s, b, i: ([], ["python3", "-I", s]),
    "node": lambda s, b, i: ([], ["node", s]),
    "bash": lambda s, b, i: ([], ["bash", s]),
    "powershell": lambda s, b, i: ([], ["pwsh", "-NoLogo", "-NonInteractive", "-File", s]),
    "go": lambda s, b, i: ([["go", "build", "-o", os.path.join(b, "a.out"), s]], [os.path.join(b, "a.out")]),
    "rust": _compiled(["rustc", "-O"]),
    "c": _compiled(["gcc", "-O2"], ["-lm"]),
    "cpp": _compiled(["g++", "-O2", "-std=c++20"]),
    "csharp": _dotnet,
    "dotnet": _dotnet,
    "binary": lambda s, b, i: ([], [s]),
}


# --------------------------------------------------------------------------- Windows table (UNTESTED)

def _cl(src: str, build: str, indir: str) -> Plan:
    exe = os.path.join(build, "a.exe")
    return [["cmd", "/c", f'call "{VCVARS}" && cl /nologo /O2 /Fe:"{exe}" "{src}"']], [exe]


WINDOWS_TABLE: dict[str, Callable[[str, str, str], Plan]] = {
    "python": lambda s, b, i: ([], ["py", "-3.12", s]),
    "node": lambda s, b, i: ([], ["node", s]),
    "powershell": lambda s, b, i: ([], ["pwsh", "-NoLogo", "-NonInteractive", "-File", s]),
    "cmd": lambda s, b, i: ([], ["cmd", "/c", s]),
    "c": _cl,
    "cpp": _cl,
    "rust": _compiled(["rustc", "-O"], exe="a.exe"),
    "go": lambda s, b, i: ([["go", "build", "-o", os.path.join(b, "a.exe"), s]], [os.path.join(b, "a.exe")]),
    "csharp": _dotnet,
    "dotnet": _dotnet,
    "binary": lambda s, b, i: ([], [s]),
}

EXT = {"python": "py", "node": "js", "bash": "sh", "powershell": "ps1", "go": "go", "rust": "rs",
       "c": "c", "cpp": "cpp", "csharp": "cs", "dotnet": "cs", "cmd": "bat", "binary": "bin"}


def table() -> dict[str, Callable[[str, str, str], Plan]]:
    return WINDOWS_TABLE if WIN else LINUX_TABLE


def _children_rusage() -> tuple[float, int] | None:
    if resource is None:
        return None
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    return r.ru_utime + r.ru_stime, r.ru_maxrss * scale


# --------------------------------------------------------------------------- supervisor

class Execution:
    def __init__(self, body: dict, workdir: Path, input_dir: Path, display: str | None, emit: Emit):
        self.body, self.workdir, self.input_dir, self.display, self.emit = body, workdir, input_dir, display, emit
        self.exec_id = str(body.get("exec_id", ""))
        self._kill = asyncio.Event()
        self._sent = {"stdout": 0, "stderr": 0}
        self._trunc = {"stdout": False, "stderr": False}

    def kill(self) -> None:
        self._kill.set()

    def _prepare(self) -> Plan:
        lang = self.body.get("language")
        plan = table().get(lang) if isinstance(lang, str) else None
        if plan is None:
            raise PlanError(f"unsupported language: {lang!r}")
        self.workdir.mkdir(parents=True, exist_ok=True)
        build = self.workdir / "build"
        build.mkdir(exist_ok=True)
        if self.body.get("entrypoint") is not None:
            if not valid_name(self.body["entrypoint"]):
                raise PlanError(f"invalid entrypoint name: {self.body['entrypoint']!r}")
            src = self.input_dir / self.body["entrypoint"]
            if not src.is_file():
                raise PlanError(f"entrypoint not found in input dir: {src.name}")
        else:
            if lang == "binary":
                raise PlanError("language 'binary' requires an entrypoint")
            src = self.workdir / f"code.{EXT.get(lang, 'txt')}"
            src.write_text(self.body.get("code") or "", encoding="utf-8")
        if lang == "binary":
            src.chmod(src.stat().st_mode | 0o111)
        steps, run = plan(str(src), str(build), str(self.input_dir))
        args = [str(a) for a in self.body.get("args") or []]
        if callable(run):
            inner = run
            return steps, lambda: [*inner(), *args]
        return steps, [*run, *args]

    async def run(self) -> None:
        t0 = time.monotonic()
        ru0 = _children_rusage()
        reason, detail, code = "exit", None, None
        try:
            timeout = min(float(self.body.get("timeout_sec") or 30), MAX_TIMEOUT_SEC)
            steps, run = self._prepare()
            deadline = t0 + timeout  # one deadline covers compile + run
            for i in range(len(steps) + 1):
                compiling = i < len(steps)
                argv = steps[i] if compiling else (run() if callable(run) else run)
                rc, status, why = await self._supervise(argv, deadline, compiling)
                if status:
                    reason, detail = status, why
                    break
                code = rc
                if compiling and rc != 0:
                    reason, detail = "compile_failed", f"{argv[0]} exited {rc}"
                    break
                if not compiling and rc == -SIGKILL:  # not sent by us, so most likely the OOM killer
                    reason, detail = "oom", "killed by SIGKILL (assumed OOM)"
        except PlanError as e:
            reason, detail = "spawn_failed", str(e)
        except Exception as e:  # never leave the host without an exec_done
            reason, detail = "spawn_failed", f"agent error: {type(e).__name__}: {e}"
        ru1 = _children_rusage()
        await self.emit(Frame.event(
            "exec_done", exec_id=self.exec_id,
            exit_code=code if reason in ("exit", "compile_failed", "oom") else None,
            wall_time_ms=int((time.monotonic() - t0) * 1000),
            cpu_time_ms=int((ru1[0] - ru0[0]) * 1000) if ru0 and ru1 else None,
            # RUSAGE_CHILDREN maxrss is a high-water mark over every child this agent has reaped.
            peak_memory_bytes=ru1[1] if ru1 else None,
            reason=reason, detail=detail,
            stdout_truncated=self._trunc["stdout"], stderr_truncated=self._trunc["stderr"]))

    async def _supervise(self, argv: list[str], deadline: float, compiling: bool):
        if self._kill.is_set():
            return None, "killed", "kill requested"
        env = {**os.environ, **{str(k): str(v) for k, v in (self.body.get("env") or {}).items()}}
        if self.display:
            env.setdefault("DISPLAY", self.display)
        stdin_data = None if compiling else self.body.get("stdin")
        # POSIX: setsid so the whole tree is one process group. Windows: new process group;
        # TODO job object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) instead of taskkill /T.
        kw: dict = {"creationflags": 0x200} if WIN else {"start_new_session": True}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.workdir, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL, **kw)
        except (OSError, ValueError) as e:
            return None, "spawn_failed", f"cannot start {argv[0]!r}: {e}"
        pumps = [asyncio.create_task(self._pump(proc.stdout, "stdout", compiling)),
                 asyncio.create_task(self._pump(proc.stderr, "stderr", compiling))]
        if stdin_data is not None:
            pumps.append(asyncio.create_task(self._feed(proc, str(stdin_data))))
        waiter = asyncio.create_task(proc.wait())
        killed = asyncio.create_task(self._kill.wait())
        await asyncio.wait({waiter, killed}, timeout=max(0.0, deadline - time.monotonic()),
                           return_when=asyncio.FIRST_COMPLETED)
        status = why = None
        if not waiter.done():
            status = "killed" if self._kill.is_set() else "timeout"
            why = "kill requested" if status == "killed" else "timeout_sec exceeded"
            await self._terminate(proc, waiter)
        killed.cancel()
        rc = await waiter
        self._signal_group(proc.pid, SIGKILL)  # leftover grandchildren must not outlive the exec or hold the pipes
        _, pending = await asyncio.wait(pumps, timeout=KILL_GRACE_SEC)
        for p in pending:
            p.cancel()
        return rc, status, why

    async def _terminate(self, proc, waiter) -> None:
        if WIN:
            await (await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(proc.pid))).wait()
            return
        self._signal_group(proc.pid, SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), KILL_GRACE_SEC)
        except asyncio.TimeoutError:
            pass
        self._signal_group(proc.pid, SIGKILL)

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> None:
        if WIN:
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def _feed(self, proc, data: str) -> None:
        try:
            proc.stdin.write(data.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _pump(self, stream, name: str, as_stderr: bool) -> None:
        name = "stderr" if as_stderr else name  # compiler diagnostics always surface as stderr
        while chunk := await stream.read(65536):
            room = STREAM_CAP - self._sent[name]
            if len(chunk) > room:
                self._trunc[name] = True
                chunk = chunk[:max(room, 0)]
            if not chunk:
                continue
            self._sent[name] += len(chunk)
            await self.emit(Frame.event("exec_output", exec_id=self.exec_id, stream=name,
                                        data_b64=base64.b64encode(chunk).decode()))
