"""Docker backend for the `ephemeral` tier (docs/architecture.md section 2.2).

One container per execute() call, driven through the docker CLI (no SDK). All
subprocess access goes through an injectable ``runner`` so tests use a fake.

Container contract (ephemeral/runner.sh): ``/runner <language> <file> [args...]``
with agent code at /work/code.<ext> and uploads at /work/in (read-only).

Design notes
------------
* ``--rm`` is deliberately omitted: after ``--rm`` the container is gone and
  ``State.OOMKilled`` cannot be read. We ``docker inspect`` after the run and
  ``docker rm -f`` in a ``finally``.
* Compile failures: runner.sh exits 101 for compiler errors. 101 is only
  interpreted as EXEC_COMPILE_FAILED for compiled languages; for interpreted
  ones it is the program's own exit code.
* Exit 125 is docker's "could not start container". A program may also exit 125,
  so it is treated as EPHEMERAL_START_FAILED only when ``docker inspect`` finds
  no such container (i.e. the container never existed).
* cpu_time_ms and peak_memory_bytes are always None (not available from the CLI
  without cgroup access / ``docker stats`` sampling).
* Staged files are made world-readable (dirs 0755, files 0644, entrypoint 0755)
  because the container runs as uid 1000; the staging *root* is 0700.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from ..config import EphemeralConfig, ImageEntry, LimitsConfig
from ..errors import AtlasError, Notice, warning
from ..validation import NetworkPolicy
from .base import ExecResult, ExecSpec, GuestHandle, Screenshot

# Same table as ephemeral/runner.sh callers use.
EXTENSIONS: dict[str, str] = {
    "python": "py", "node": "js", "bash": "sh", "powershell": "ps1",
    "csharp": "cs", "dotnet": "cs", "rust": "rs", "c": "c", "cpp": "cpp", "go": "go",
    "binary": "bin",
}
COMPILED = frozenset({"go", "rust", "c", "cpp", "csharp", "dotnet"})
RUNNER_COMPILE_FAILED = 101      # keep in sync with ephemeral/runner.sh
DOCKER_COULD_NOT_START = 125
KILL_GRACE_SEC = 5
CONTROL_TIMEOUT_SEC = 15
STATE_CACHE_SEC = 10.0

# (argv, stdin_bytes, timeout_sec) -> (returncode, stdout, stderr)
CommandRunner = Callable[[Sequence[str], "bytes | None", float], Awaitable["tuple[int, bytes, bytes]"]]


class CommandTimeout(Exception):
    """Raised by a runner when the timeout elapses; carries partial output."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__("command timed out")
        self.stdout = stdout
        self.stderr = stderr


class SubprocessRunner:
    """Real runner. Keeps at most ``cap`` bytes per stream (rest is drained and dropped)
    so a runaway program cannot exhaust daemon memory."""

    def __init__(self, cap: int = 1 << 20) -> None:
        self.cap = cap

    async def __call__(self, argv: Sequence[str], stdin: bytes | None, timeout: float) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = bytearray(), bytearray()

        async def drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
            while chunk := await stream.read(65536):
                room = self.cap - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])

        async def feed() -> None:
            assert proc.stdin is not None and stdin is not None
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        tasks = [asyncio.create_task(drain(proc.stdout, out)), asyncio.create_task(drain(proc.stderr, err))]  # type: ignore[arg-type]
        if stdin is not None:
            tasks.append(asyncio.create_task(feed()))
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, proc.wait()), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.gather(*tasks, return_exceptions=True)
            await proc.wait()
            raise CommandTimeout(bytes(out), bytes(err)) from None
        return proc.returncode if proc.returncode is not None else -1, bytes(out), bytes(err)


def _dec(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


class DockerBackend:
    runtimes: frozenset[str] = frozenset({"ephemeral"})

    def __init__(self, config: EphemeralConfig, limits: LimitsConfig,
                 runner: CommandRunner | None = None, docker_bin: str = "docker",
                 scratch_root: str | Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.limits = limits
        self.docker_bin = docker_bin
        self.scratch_root = Path(scratch_root) if scratch_root else None
        self._run = runner or SubprocessRunner(cap=limits.max_output_bytes + 1)
        self._clock = clock
        self._probe_cache: tuple[float, str] | None = None   # (expires_at, "ok"|"daemon"|"image")

    # ------------------------------------------------------------------ helpers

    async def _ctl(self, *args: str, timeout: float = CONTROL_TIMEOUT_SEC) -> tuple[int, bytes, bytes] | None:
        """Run a short docker control command; None if it could not be run at all."""
        try:
            return await self._run([self.docker_bin, *args], None, timeout)
        except (OSError, CommandTimeout):
            return None

    async def _probe(self) -> str:
        now = self._clock()
        if self._probe_cache and now < self._probe_cache[0]:
            return self._probe_cache[1]
        res = await self._ctl("version", "--format", "{{.Server.Version}}")
        if res is None or res[0] != 0:
            state = "daemon"
        else:
            res = await self._ctl("image", "inspect", self.config.image)
            state = "ok" if res is not None and res[0] == 0 else "image"
        self._probe_cache = (now + STATE_CACHE_SEC, state)
        return state

    async def entry_state(self, entry: ImageEntry) -> str:
        return "ready" if await self._probe() == "ok" else "unavailable"

    # ------------------------------------------------------------------ Backend

    async def open(self, entry: ImageEntry, network: NetworkPolicy) -> GuestHandle:
        if network.enabled:
            raise AtlasError("NETWORK_UNAVAILABLE", "egress bridge not implemented",
                             reason="egress_bridge_not_implemented")
        state = await self._probe()
        if state == "daemon":
            raise AtlasError("DOCKER_UNAVAILABLE", "docker daemon is not reachable")
        if state == "image":
            raise AtlasError("EPHEMERAL_IMAGE_MISSING", f"image {self.config.image} not found",
                             image=self.config.image)
        try:
            if self.scratch_root:
                self.scratch_root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="atlas-eph-", dir=self.scratch_root))
            staging.chmod(0o700)
        except OSError as exc:
            raise AtlasError("FILE_TRANSFER_FAILED", "cannot create staging dir", detail=str(exc)) from exc
        return GuestHandle(entry, network, ref=str(staging))

    def _stage(self, staging: Path, spec: ExecSpec) -> tuple[str, Path | None]:
        """Write files/code. Returns (in-container path, host code file or None)."""
        lang = spec.language
        if lang not in EXTENSIONS:
            raise AtlasError("LANGUAGE_UNSUPPORTED", f"{lang} is not supported by the ephemeral tier", language=lang)
        try:
            indir = staging / "in"
            if indir.exists():
                shutil.rmtree(indir)
            for old in staging.glob("code.*"):
                old.unlink()
            indir.mkdir()
            indir.chmod(0o755)
            names: set[str] = set()
            for f in spec.files:
                _check_name(f.name)
                p = indir / f.name
                p.write_bytes(f.content)
                p.chmod(0o644)
                names.add(f.name)
            code_file: Path | None = None
            if spec.entrypoint:
                _check_name(spec.entrypoint)
                if spec.entrypoint not in names:
                    raise AtlasError("ENTRYPOINT_NOT_FOUND", f"{spec.entrypoint} was not uploaded",
                                     entrypoint=spec.entrypoint)
                (indir / spec.entrypoint).chmod(0o755)
                target = f"/work/in/{spec.entrypoint}"
            else:
                code_file = staging / f"code.{EXTENSIONS[lang]}"
                code_file.write_text(spec.code or "", encoding="utf-8")
                code_file.chmod(0o755 if lang == "binary" else 0o644)
                target = f"/work/{code_file.name}"
            return target, code_file
        except OSError as exc:
            raise AtlasError("FILE_TRANSFER_FAILED", "could not stage files", detail=str(exc)) from exc

    def build_argv(self, staging: Path, name: str, spec: ExecSpec, target: str,
                   code_file: Path | None) -> list[str]:
        c = self.config
        argv = [
            self.docker_bin, "run", "--name", name, "--stop-timeout", "2",
            "--network", "none",
            "--memory", f"{c.memory_mb}m", "--cpus", str(c.cpus), "--pids-limit", str(c.pids_limit),
            "--read-only",
            "--tmpfs", f"/work:size={c.work_tmpfs_mb}m", "--tmpfs", "/tmp:size=256m",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "-v", f"{staging / 'in'}:/work/in:ro",
        ]
        if code_file is not None:
            argv += ["-v", f"{code_file}:/work/{code_file.name}:ro"]
        for k, v in spec.env.items():
            argv += ["-e", f"{k}={v}"]
        if spec.stdin is not None:
            argv.append("-i")
        argv += [c.image, spec.language, target, *spec.args]
        return argv

    async def execute(self, handle: GuestHandle, spec: ExecSpec) -> ExecResult:
        staging = Path(handle.ref)
        target, code_file = self._stage(staging, spec)
        name = f"atlas-{uuid.uuid4().hex}"
        argv = self.build_argv(staging, name, spec, target, code_file)
        stdin = spec.stdin.encode("utf-8") if spec.stdin is not None else None
        t0 = self._clock()
        timed_out = False
        try:
            try:
                rc, out, err = await self._run(argv, stdin, spec.timeout_sec + KILL_GRACE_SEC)
            except CommandTimeout as ct:
                timed_out, rc, out, err = True, -1, ct.stdout, ct.stderr
                await self._ctl("kill", name)
            except OSError as exc:
                raise AtlasError("DOCKER_UNAVAILABLE", "cannot execute docker", detail=str(exc)) from exc
            wall_ms = int((self._clock() - t0) * 1000)

            oom, exit_code, existed = False, rc, False
            if not timed_out:
                insp = await self._ctl("inspect", name, "--format", "{{.State.OOMKilled}} {{.State.ExitCode}}")
                if insp is not None and insp[0] == 0:
                    parts = _dec(insp[1]).split()
                    if len(parts) == 2:
                        existed = True
                        oom = parts[0].lower() == "true"
                        try:
                            exit_code = int(parts[1])
                        except ValueError:
                            exit_code = rc
        finally:
            await self._ctl("rm", "-f", name)

        cap = self.limits.max_output_bytes
        out_t, err_t = len(out) > cap, len(err) > cap
        stdout, stderr = _dec(out[:cap]), _dec(err[:cap])
        warnings: list[Notice] = []
        if out_t or err_t:
            streams = [s for s, t in (("stdout", out_t), ("stderr", err_t)) if t]
            warnings.append(warning("EXEC_OUTPUT_TRUNCATED", f"{' and '.join(streams)} exceeded {cap} bytes",
                                    streams=streams, limit_bytes=cap))

        def result(status: str, code: int | None, *errors: Notice) -> ExecResult:
            return ExecResult(status, code, stdout, stderr, out_t, err_t, wall_ms,
                              cpu_time_ms=None, peak_memory_bytes=None,
                              errors=list(errors), warnings=warnings)

        if timed_out:
            return result("timeout", None, Notice("EXEC_TIMEOUT", f"hit timeout_sec={spec.timeout_sec}",
                                                  {"timeout_sec": spec.timeout_sec}))
        if rc == DOCKER_COULD_NOT_START and not existed:
            raise AtlasError("EPHEMERAL_START_FAILED", "docker could not start the container",
                             detail=stderr)
        if oom:
            return result("killed", None, Notice("EXEC_KILLED_OOM", f"memory limit {self.config.memory_mb} MB hit",
                                                 {"memory_mb": self.config.memory_mb}))
        if exit_code == 0:
            return result("ok", 0)
        if exit_code == RUNNER_COMPILE_FAILED and spec.language in COMPILED:
            return result("error", exit_code, Notice("EXEC_COMPILE_FAILED", "compilation failed",
                                                     {"exit_code": exit_code}))
        return result("error", exit_code, Notice("EXEC_NONZERO_EXIT", f"process exited {exit_code}",
                                                 {"exit_code": exit_code}))

    async def screenshot(self, handle: GuestHandle, source: str,
                         window_title: str | None, region: list[int] | None) -> Screenshot:
        raise AtlasError("SCREENSHOT_UNSUPPORTED", "the ephemeral tier has no display")

    async def close(self, handle: GuestHandle) -> bool:
        shutil.rmtree(handle.ref, ignore_errors=True)
        return False


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise AtlasError("FILE_TRANSFER_FAILED", "refusing unsafe file name", name=name)
