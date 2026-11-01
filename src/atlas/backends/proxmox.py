"""Proxmox VE backend for the `linux` and `windows` runtimes.

Control plane: the Proxmox REST API via proxmoxer (blocking, run in threads).
Data plane: the guest agent over the per-VM virtio-serial Unix socket, using the
frame codec in atlas.guest.protocol. See docs/architecture.md 2.3-2.6 and
docs/byo-vm.md.

Proxmox endpoints used (all under /api2/json, node = proxmox.node):

    GET  nodes/{node}/status                                host RAM check before cold boots
    GET  nodes/{node}/qemu/{vmid}/status/current            exists / running?
    GET  nodes/{node}/qemu/{vmid}/config                    NIC + serial-port validation
    GET  nodes/{node}/qemu/{vmid}/snapshot                  golden snapshot (+ vmstate) present?
    POST nodes/{node}/qemu/{vmid}/status/start              cold boot / warm restart after rollback
    POST nodes/{node}/qemu/{vmid}/status/stop               stop a cold VM after rollback; cleanup on failed open
    POST nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback  revert to golden
    GET  nodes/{node}/tasks/{upid}/status                   wait for start/stop/rollback tasks
    POST nodes/{node}/qemu/{vmid}/monitor                   `screendump <file>` (hypervisor screenshots)
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import re
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import ImageEntry, LimitsConfig, ProxmoxConfig, ScreenshotsConfig
from ..errors import AtlasError, Notice, warning
from ..guest.protocol import CHUNK_BYTES, Frame, FrameError, major_compatible, read_frame, write_frame
from ..validation import DecodedFile, NetworkPolicy
from .base import ExecResult, ExecSpec, GuestHandle, Screenshot

ChannelFactory = Callable[[int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_NIC_KEY = re.compile(r"^net\d+$")
_AGENT_PORT = "org.atlas.agent"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_NOT_FOUND_CODES = {"SCREENSHOT_WINDOW_NOT_FOUND", "window_not_found"}


def _pil() -> Any | None:
    """Pillow if installed (optional dependency); tests monkeypatch this to None."""
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


# --------------------------------------------------------------------------- Proxmox adapter

class PveError(Exception):
    """A Proxmox API call came back with an HTTP error. Callers map it to a specific Atlas code."""

    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(f"{status}: {message}")


class PveClient:
    """Thin async adapter over a proxmoxer-style object: `api("a/b/c").get(**params)`."""

    def __init__(self, api: Any) -> None:
        self._api = api

    async def get(self, path: str, **params: Any) -> Any:
        return await asyncio.to_thread(self._call, "get", path, params)

    async def post(self, path: str, **params: Any) -> Any:
        return await asyncio.to_thread(self._call, "post", path, params)

    def _call(self, method: str, path: str, params: dict[str, Any]) -> Any:
        try:
            return getattr(self._api(path), method)(**params)
        except AtlasError:
            raise
        except Exception as exc:  # translate proxmoxer / requests failures
            raise self._translate(exc) from exc

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            if status in (401, 403):
                return AtlasError("PROXMOX_AUTH", f"Proxmox rejected the API token ({status})", http_status=status)
            msg = " ".join(str(p) for p in (getattr(exc, "status_message", ""), getattr(exc, "content", "")) if p)
            return PveError(status, msg or str(exc))
        if type(exc).__name__ == "AuthenticationError":
            return AtlasError("PROXMOX_AUTH", f"Proxmox authentication failed: {exc}")
        if isinstance(exc, OSError):   # requests.ConnectionError / Timeout derive from OSError
            return AtlasError("PROXMOX_UNREACHABLE", f"cannot reach Proxmox API: {exc}", detail=str(exc))
        return AtlasError("INTERNAL", f"unexpected Proxmox client error: {exc!r}")


def make_proxmox_api(config: ProxmoxConfig) -> Any:
    """Build a real proxmoxer.ProxmoxAPI with token auth. Not exercised offline."""
    from proxmoxer import ProxmoxAPI

    user, _, token_name = config.token_id.partition("!")
    return ProxmoxAPI(config.host, port=config.port, user=user, token_name=token_name,
                      token_value=config.token_secret, verify_ssl=config.verify_tls, backend="https")


# --------------------------------------------------------------------------- guest channel

class _ChannelEOF(Exception):
    pass


_EOF = object()


class _Channel:
    """Frames from the guest are pumped into a queue by one task, so a timed-out recv() can
    never cancel a read halfway through a frame and desynchronise the stream."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            while True:
                self._q.put_nowait(await read_frame(self._reader))
        except FrameError as exc:
            self._q.put_nowait(exc)
        except (asyncio.IncompleteReadError, OSError):
            self._q.put_nowait(_EOF)

    async def recv(self, timeout: float) -> Frame:
        item = await asyncio.wait_for(self._q.get(), max(timeout, 0.001))
        if item is _EOF or isinstance(item, FrameError):
            self._q.put_nowait(item)           # terminal; stays terminal
            if item is _EOF:
                raise _ChannelEOF()
            raise item
        return item

    async def send(self, frame: Frame) -> None:
        try:
            await write_frame(self._writer, frame)
        except OSError as exc:
            raise _ChannelEOF() from exc

    async def aclose(self) -> None:
        self._pump_task.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


@dataclass
class _Guest:
    vmid: int
    channel: _Channel
    started_by_us: bool
    hello: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dead: str | None = None            # Atlas code to raise on further guest use


@dataclass
class _Problem:
    kind: str       # missing_vm | proxmox | snapshot | vmstate | serial | nic | warm_stopped
    text: str


# --------------------------------------------------------------------------- backend

class ProxmoxBackend:
    runtimes: frozenset[str] = frozenset({"linux", "windows"})

    def __init__(self, config: ProxmoxConfig, limits: LimitsConfig, screenshots: ScreenshotsConfig,
                 catalog_entries: list[ImageEntry], api: Any = None,
                 channel_factory: ChannelFactory | None = None, *,
                 host_reserve_mb: int = 4096,
                 boot_timeout_sec: float | None = None,
                 hung_after_sec: float | None = None,
                 request_timeout_sec: float = 30.0,
                 exec_grace_sec: float = 15.0,
                 task_timeout_sec: float = 120.0,
                 poll_interval_sec: float = 0.5,
                 prepare_revert_timeout_sec: float = 3.0) -> None:
        self.config = config
        self.limits = limits
        self.screenshots = screenshots
        self.entries = {e.id: e for e in catalog_entries if e.is_vm}
        self.host_reserve_mb = host_reserve_mb
        self.boot_timeout = float(boot_timeout_sec if boot_timeout_sec is not None else config.guest_boot_timeout_sec)
        self.hung_after = float(hung_after_sec if hung_after_sec is not None
                                else config.guest_heartbeat_interval_sec * config.guest_heartbeat_miss_limit)
        self.request_timeout = request_timeout_sec
        self.exec_grace = exec_grace_sec
        self.task_timeout = task_timeout_sec
        self.poll_interval = poll_interval_sec
        self.prepare_revert_timeout = prepare_revert_timeout_sec
        self._api = api if api is not None else make_proxmox_api(config)
        self.pve = PveClient(self._api)
        self._channel_factory = channel_factory or self._default_channel
        self._guests: dict[str, _Guest] = {}
        self.failed_vmids: set[int] = set()     # rollback failed: unavailable until clear_failed()

    # ------------------------------------------------------------------ paths
    def _vm(self, vmid: int, tail: str = "") -> str:
        return f"nodes/{self.config.node}/qemu/{vmid}" + (f"/{tail}" if tail else "")

    def socket_path(self, vmid: int) -> Path:
        return Path(self.config.virtio_socket_dir) / f"{vmid}.atlas"

    async def _default_channel(self, vmid: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_unix_connection(str(self.socket_path(vmid)))

    def clear_failed(self, vmid: int) -> None:
        """Operator fixed the VM; let entry_state/open consider it again."""
        self.failed_vmids.discard(vmid)

    # ------------------------------------------------------------------ validation
    async def _inspect(self, entry: ImageEntry) -> tuple[str | None, list[_Problem]]:
        """Returns (vm status or None, problems). Never raises for Proxmox-side trouble."""
        vmid = entry.vmid
        probs: list[_Problem] = []
        try:
            try:
                cur = await self.pve.get(self._vm(vmid, "status/current"))
            except PveError as exc:
                return None, [_Problem("missing_vm", f"VM {vmid} not found on node {self.config.node}: {exc.message}")]
            status = (cur or {}).get("status")
            try:
                cfg = await self.pve.get(self._vm(vmid, "config")) or {}
                snaps = await self.pve.get(self._vm(vmid, "snapshot")) or []
            except PveError as exc:
                return status, [_Problem("proxmox", f"cannot read VM {vmid} config/snapshots: {exc.message}")]
        except AtlasError as exc:
            return None, [_Problem("proxmox", f"{exc.code}: {exc.notice.message}")]

        golden = self.config.golden_snapshot_name
        snap = next((s for s in snaps if s.get("name") == golden), None)
        if snap is None:
            probs.append(_Problem("snapshot", f"VM_SNAPSHOT_MISSING: no snapshot named '{golden}'"))
        elif entry.warm and not int(snap.get("vmstate") or 0):
            probs.append(_Problem("vmstate", f"snapshot '{golden}' has no RAM state (vmstate); "
                                             "a warm entry needs it (take it with --vmstate 1)"))

        args = str(cfg.get("args") or "")
        expected = str(self.socket_path(vmid))
        if _AGENT_PORT not in args or "virtserialport" not in args:
            probs.append(_Problem("serial", f"no virtio-serial port '{_AGENT_PORT}' in VM args "
                                            f"(need a chardev + virtserialport wired to {expected})"))
        elif expected not in args:
            probs.append(_Problem("serial", f"virtio-serial chardev socket path does not match {expected}"))

        nics = sorted(k for k in cfg if _NIC_KEY.match(k))
        if nics:
            probs.append(_Problem("nic", f"guest must have no NIC; found {', '.join(nics)}"))

        if entry.warm and status != "running":
            probs.append(_Problem("warm_stopped", f"entry is warm but VM is {status}"))
        return status, probs

    async def validate_entry(self, entry: ImageEntry) -> list[str]:
        """Human-readable problems with this VM; empty list means Atlas-ready."""
        _, probs = await self._inspect(entry)
        out = [p.text for p in probs]
        if entry.vmid in self.failed_vmids:
            out.append("VM_ROLLBACK_FAILED: last rollback failed; operator must fix and clear")
        return out

    async def entry_state(self, entry: ImageEntry) -> str:
        if entry.vmid in self.failed_vmids:
            return "unavailable"
        status, probs = await self._inspect(entry)
        # A warm entry whose VM is merely stopped is cold, not broken: open() boots it.
        if any(p.kind != "warm_stopped" for p in probs):
            return "unavailable"
        return "warm" if status == "running" else "cold"

    # ------------------------------------------------------------------ tasks
    async def _wait_task(self, upid: Any) -> str | None:
        """Poll a UPID until it stops. Returns None on success, else the exit status text."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return None          # some endpoints answer synchronously
        deadline = time.monotonic() + self.task_timeout
        while True:
            st = await self.pve.get(f"nodes/{self.config.node}/tasks/{upid}/status") or {}
            if st.get("status") == "stopped":
                exit_status = str(st.get("exitstatus", ""))
                return None if exit_status.startswith(("OK", "WARNINGS")) else (exit_status or "unknown failure")
            if time.monotonic() >= deadline:
                return f"task did not finish within {self.task_timeout:g}s"
            await asyncio.sleep(self.poll_interval)

    async def _vm_status(self, vmid: int) -> str:
        cur = await self.pve.get(self._vm(vmid, "status/current"))
        return str((cur or {}).get("status"))

    # ------------------------------------------------------------------ open
    async def open(self, entry: ImageEntry, network: NetworkPolicy) -> GuestHandle:
        vmid = entry.vmid
        assert vmid is not None
        ref = str(vmid)
        if vmid in self.failed_vmids:
            raise AtlasError("VM_UNAVAILABLE", f"{entry.id} failed its last rollback", reason="rollback_failed")
        if ref in self._guests:
            raise AtlasError("VM_UNAVAILABLE", f"{entry.id} already has an open session", reason="busy")
        if network.enabled:
            # Atlas refuses rather than downgrades: never run networked work without a NIC policy.
            raise AtlasError("NETWORK_UNAVAILABLE", "NIC hot-attach not implemented", reason="hot_attach_unimplemented")

        status, probs = await self._inspect(entry)
        hard = [p for p in probs if p.kind != "warm_stopped"]
        if any(p.kind == "snapshot" for p in hard):
            raise AtlasError("VM_SNAPSHOT_MISSING", f"{entry.id} has no '{self.config.golden_snapshot_name}' snapshot",
                             vmid=vmid)
        if hard:
            raise AtlasError("VM_UNAVAILABLE", f"{entry.id} failed validation: {hard[0].text}",
                             reason="health_check", problems=[p.text for p in hard])

        started_at: float | None = None
        if status != "running":
            await self._check_host_ram()
            started_at = time.monotonic()
            await self._start_vm(vmid)
        try:
            guest = await self._attach(vmid, started_by_us=started_at is not None)
        except BaseException:
            if started_at is not None:
                await self._best_effort_stop(vmid)
            raise
        self._guests[ref] = guest
        cold_ms = int((time.monotonic() - started_at) * 1000) if started_at is not None else None
        return GuestHandle(entry, network, ref=ref, cold_boot_ms=cold_ms)

    async def _check_host_ram(self) -> None:
        st = await self.pve.get(f"nodes/{self.config.node}/status") or {}
        free_mb = int(((st.get("memory") or {}).get("free") or 0) // (1024 * 1024))
        if free_mb < self.host_reserve_mb:
            raise AtlasError("HOST_RESOURCES",
                             f"{free_mb} MB free on host; reserve is {self.host_reserve_mb} MB",
                             free_mb=free_mb, reserve_mb=self.host_reserve_mb)

    async def _start_vm(self, vmid: int) -> None:
        try:
            upid = await self.pve.post(self._vm(vmid, "status/start"))
            failure = await self._wait_task(upid)
        except PveError as exc:
            raise AtlasError("VM_START_FAILED", f"Proxmox could not start VM {vmid}", proxmox_error=exc.message) from exc
        if failure:
            raise AtlasError("VM_START_FAILED", f"Proxmox could not start VM {vmid}", proxmox_error=failure)

    async def _best_effort_stop(self, vmid: int) -> None:
        try:
            await self._wait_task(await self.pve.post(self._vm(vmid, "status/stop")))
        except Exception:
            pass

    async def _attach(self, vmid: int, started_by_us: bool) -> _Guest:
        deadline = time.monotonic() + self.boot_timeout
        unreachable = AtlasError("GUEST_AGENT_UNREACHABLE",
                                 f"no guest agent response from VM {vmid} within {self.boot_timeout:g}s",
                                 vmid=vmid, timeout_sec=self.boot_timeout)
        channel: _Channel | None = None
        while channel is None:
            try:
                r, w = await asyncio.wait_for(self._channel_factory(vmid), max(deadline - time.monotonic(), 0.001))
                channel = _Channel(r, w)
            except (OSError, asyncio.TimeoutError):
                if time.monotonic() >= deadline:
                    raise unreachable from None
                await asyncio.sleep(min(0.1, self.poll_interval))
        try:
            hello = await self._handshake(channel, deadline, unreachable)
        except BaseException:
            await channel.aclose()
            raise
        return _Guest(vmid, channel, started_by_us, hello)

    async def _handshake(self, ch: _Channel, deadline: float, unreachable: AtlasError) -> dict[str, Any]:
        """Send hello; a heartbeat proves the agent is alive, so re-send once in case the first
        hello was written before the guest opened its port."""
        from ..guest.protocol import PROTOCOL_VERSION
        ids: set[str] = set()

        async def hello() -> None:
            req = Frame.request("hello", uuid.uuid4().hex, atlas_version=PROTOCOL_VERSION)
            ids.add(req.id)  # type: ignore[arg-type]
            await ch.send(req)

        resent = False
        try:
            await hello()
            while True:
                try:
                    f = await ch.recv(deadline - time.monotonic())
                except TimeoutError:
                    raise unreachable from None
                if f.id in ids and (f.is_error or f.type == "hello.reply"):
                    if f.is_error:
                        raise AtlasError("GUEST_AGENT_PROTOCOL", "guest rejected hello", detail=f.body)
                    ver = f.body.get("agent_version")
                    if not major_compatible(str(ver)):
                        raise AtlasError("GUEST_AGENT_VERSION",
                                         f"guest agent {ver!r} is incompatible with Atlas protocol {PROTOCOL_VERSION}",
                                         agent_version=ver, atlas_version=PROTOCOL_VERSION)
                    return dict(f.body)
                if f.type == "heartbeat" and not resent:
                    resent = True
                    await hello()
        except _ChannelEOF:
            raise unreachable from None
        except FrameError as exc:
            raise AtlasError("GUEST_AGENT_PROTOCOL", "malformed frame from guest", detail=str(exc)) from exc

    # ------------------------------------------------------------------ guest requests
    def _guest(self, handle: GuestHandle) -> _Guest:
        g = self._guests.get(handle.ref)
        if g is None:
            raise AtlasError("SESSION_NOT_FOUND", f"no open guest for {handle.ref}", reason="not_open")
        if g.dead:
            raise AtlasError(g.dead, f"guest {handle.ref} is no longer usable ({g.dead})", vmid=g.vmid)
        return g

    def _die(self, g: _Guest, code: str) -> None:
        g.dead = code

    async def _request(self, g: _Guest, verb: str, timeout: float, **body: Any) -> Frame:
        """One request, one reply. Heartbeats and unrelated frames are skipped. Raises TimeoutError
        if no reply in `timeout`; EOF / malformed frames become GUEST_CRASHED / GUEST_AGENT_PROTOCOL."""
        req = Frame.request(verb, uuid.uuid4().hex, **body)
        deadline = time.monotonic() + timeout
        try:
            await g.channel.send(req)
            while True:
                f = await g.channel.recv(deadline - time.monotonic())
                if f.id == req.id and (f.is_error or f.type == f"{verb}.reply"):
                    return f
        except _ChannelEOF:
            self._die(g, "GUEST_CRASHED")
            raise AtlasError("GUEST_CRASHED", "guest closed the channel", vmid=g.vmid) from None
        except FrameError as exc:
            self._die(g, "GUEST_AGENT_PROTOCOL")
            raise AtlasError("GUEST_AGENT_PROTOCOL", "malformed frame from guest", detail=str(exc)) from exc

    # ------------------------------------------------------------------ execute
    async def execute(self, handle: GuestHandle, spec: ExecSpec) -> ExecResult:
        g = self._guest(handle)
        async with g.lock:
            for f in spec.files:
                await self._push_file(g, f)
            return await self._run(g, spec)

    async def _push_file(self, g: _Guest, f: DecodedFile) -> None:
        def fail(detail: str) -> AtlasError:
            return AtlasError("FILE_TRANSFER_FAILED", f"could not push {f.name} to the guest", name=f.name, detail=detail)

        async def step(verb: str, **body: Any) -> Frame:
            try:
                r = await self._request(g, verb, self.request_timeout, name=f.name, **body)
            except TimeoutError:
                raise fail(f"timeout waiting for {verb}") from None
            if r.is_error:
                raise fail(f"{verb}: {r.body.get('code')}: {r.body.get('message')}")
            return r

        await step("put_file_begin", size=len(f.content), sha256=f.sha256)
        for off in range(0, len(f.content), CHUNK_BYTES):
            await step("put_file_chunk", offset=off,
                       data_b64=base64.b64encode(f.content[off:off + CHUNK_BYTES]).decode())
        end = await step("put_file_end")
        got = end.body.get("sha256")
        if got is not None and got != f.sha256:
            raise fail(f"sha256 mismatch (guest {got})")

    async def _run(self, g: _Guest, spec: ExecSpec) -> ExecResult:
        cap = self.limits.max_output_bytes
        exec_id = uuid.uuid4().hex
        body: dict[str, Any] = {"exec_id": exec_id, "language": spec.language, "args": spec.args,
                                "env": spec.env, "stdin": spec.stdin, "timeout_sec": spec.timeout_sec}
        if spec.code is not None:
            body["code"] = spec.code
        if spec.entrypoint is not None:
            body["entrypoint"] = spec.entrypoint
        req = Frame.request("exec", exec_id, **body)

        bufs = {"stdout": bytearray(), "stderr": bytearray()}
        trunc = {"stdout": False, "stderr": False}
        hard_deadline = time.monotonic() + spec.timeout_sec + self.exec_grace
        t0 = time.monotonic()
        done: Frame | None = None
        try:
            await g.channel.send(req)
            while done is None:
                left = hard_deadline - time.monotonic()
                try:
                    f = await g.channel.recv(min(self.hung_after, left))
                except TimeoutError:
                    why = ("exec outlived its timeout plus grace" if time.monotonic() >= hard_deadline
                           else f"no frame from guest for {self.hung_after:g}s")
                    self._die(g, "GUEST_HUNG")
                    await self._try_kill(g, exec_id)
                    raise AtlasError("GUEST_HUNG", f"guest stopped responding: {why}", vmid=g.vmid) from None
                if f.type == "heartbeat":
                    continue
                if f.type == "exec_output" and f.body.get("exec_id") == exec_id:
                    stream = f.body.get("stream")
                    if stream not in bufs:
                        raise FrameError(f"exec_output with bad stream {stream!r}")
                    try:
                        data = base64.b64decode(f.body.get("data_b64", ""), validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise FrameError(f"exec_output has invalid base64: {exc}") from exc
                    room = cap - len(bufs[stream])
                    if len(data) > room:
                        trunc[stream] = True
                    bufs[stream] += data[:max(room, 0)]
                elif f.type == "exec_done" and f.body.get("exec_id") == exec_id:
                    done = f
                elif f.id == exec_id and f.is_error:
                    # The guest acks exec with exec.reply before any events; an error frame means
                    # it refused (busy, missing exec_id/code) and no events will follow.
                    raise AtlasError("GUEST_AGENT_PROTOCOL", "guest refused exec", detail=f.body)
                # exec.reply {ok: true}: ack, nothing to do
        except _ChannelEOF:
            self._die(g, "GUEST_CRASHED")
            raise AtlasError("GUEST_CRASHED", "guest closed the channel mid-execution", vmid=g.vmid) from None
        except FrameError as exc:
            self._die(g, "GUEST_AGENT_PROTOCOL")
            raise AtlasError("GUEST_AGENT_PROTOCOL", "malformed frame from guest", detail=str(exc)) from exc

        return self._build_result(done, bufs, trunc, cap, int((time.monotonic() - t0) * 1000))

    async def _try_kill(self, g: _Guest, exec_id: str) -> None:
        try:
            await g.channel.send(Frame.request("kill", uuid.uuid4().hex, exec_id=exec_id))
        except Exception:
            pass

    def _build_result(self, done: Frame, bufs: dict[str, bytearray], trunc: dict[str, bool],
                      cap: int, host_wall_ms: int) -> ExecResult:
        d = done.body
        reason, code = d.get("reason"), d.get("exit_code")
        detail = d.get("detail")
        for s in ("stdout", "stderr"):          # the guest agent caps output too and flags it
            if d.get(f"{s}_truncated"):
                trunc[s] = True
        extra = {"detail": detail} if detail else {}
        errors: list[Notice] = []
        if reason == "exit":
            if code == 0:
                status = "ok"
            else:
                status = "error"
                errors.append(Notice("EXEC_NONZERO_EXIT", f"process exited {code}", {"exit_code": code, **extra}))
        elif reason == "timeout":
            status = "timeout"
            errors.append(Notice("EXEC_TIMEOUT", "execution hit timeout_sec", dict(extra)))
        elif reason == "killed":
            status = "killed"
            errors.append(Notice("EXEC_KILLED_ADMIN", "execution was killed", dict(extra)))
        elif reason == "oom":
            status = "killed"
            errors.append(Notice("EXEC_KILLED_OOM", "memory limit hit", dict(extra)))
        elif reason == "spawn_failed":
            status = "error"
            errors.append(Notice("EXEC_SPAWN_FAILED", "could not start the program", dict(extra)))
        elif reason == "compile_failed":
            status = "error"
            errors.append(Notice("EXEC_COMPILE_FAILED", "compilation failed", {"exit_code": code, **extra}))
        else:
            raise AtlasError("GUEST_AGENT_PROTOCOL", "exec_done with unknown reason", detail=f"reason={reason!r}")

        warns = [warning("EXEC_OUTPUT_TRUNCATED", f"{s} exceeded {cap} bytes and was truncated", stream=s, limit_bytes=cap)
                 for s in ("stdout", "stderr") if trunc[s]]
        return ExecResult(
            status=status, exit_code=code if isinstance(code, int) else None,
            stdout=bytes(bufs["stdout"]).decode("utf-8", "replace"),
            stderr=bytes(bufs["stderr"]).decode("utf-8", "replace"),
            stdout_truncated=trunc["stdout"], stderr_truncated=trunc["stderr"],
            wall_time_ms=int(d.get("wall_time_ms", host_wall_ms)),
            cpu_time_ms=d.get("cpu_time_ms"), peak_memory_bytes=d.get("peak_memory_bytes"),
            errors=errors, warnings=warns)

    # ------------------------------------------------------------------ screenshots
    async def screenshot(self, handle: GuestHandle, source: str,
                         window_title: str | None, region: list[int] | None) -> Screenshot:
        src = source if source in ("guest", "hypervisor", "auto") else self.screenshots.default_source
        vmid = int(handle.ref)
        if src == "guest":
            return await self._wrap_failure("guest", self._shot_guest(handle, window_title, region))
        if src == "hypervisor":
            return await self._wrap_failure("hypervisor", self._shot_hypervisor(vmid))
        guest_err: str
        try:
            return await self._shot_guest(handle, window_title, region)
        except AtlasError as exc:
            if exc.code == "SCREENSHOT_WINDOW_NOT_FOUND":
                raise
            guest_err = f"{exc.code}: {exc.notice.message}"
        except _ShotError as exc:
            guest_err = str(exc)
        try:
            shot = await self._shot_hypervisor(vmid)
        except (AtlasError, _ShotError) as exc:
            raise AtlasError("SCREENSHOT_FAILED", "both screenshot sources failed", guest_error=guest_err,
                             hypervisor_error=_msg(exc)) from None
        shot.warnings.insert(0, warning("SCREENSHOT_GUEST_FALLBACK",
                                        "guest did not provide a screenshot; hypervisor screendump returned",
                                        guest_error=guest_err))
        return shot

    async def _wrap_failure(self, which: str, coro: Awaitable[Screenshot]) -> Screenshot:
        try:
            return await coro
        except _ShotError as exc:
            raise AtlasError("SCREENSHOT_FAILED", f"{which} screenshot failed", **{f"{which}_error": str(exc)}) from None
        except AtlasError as exc:
            if exc.code in ("SCREENSHOT_WINDOW_NOT_FOUND", "SCREENSHOT_FAILED"):
                raise
            raise AtlasError("SCREENSHOT_FAILED", f"{which} screenshot failed",
                             **{f"{which}_error": _msg(exc)}) from None

    async def _shot_guest(self, handle: GuestHandle, window_title: str | None, region: list[int] | None) -> Screenshot:
        g = self._guest(handle)      # AtlasError if dead -> counts as guest failure
        try:
            async with g.lock:
                r = await self._request(g, "screenshot", float(self.screenshots.guest_timeout_sec),
                                        window_title=window_title, region=region)
        except TimeoutError:
            raise _ShotError(f"guest did not answer within {self.screenshots.guest_timeout_sec}s") from None
        if r.is_error:
            code = str(r.body.get("code"))
            if code in _NOT_FOUND_CODES:
                raise AtlasError("SCREENSHOT_WINDOW_NOT_FOUND", str(r.body.get("message") or "no window matched"),
                                 window_title=window_title)
            raise _ShotError(f"{code}: {r.body.get('message')}")
        try:
            raw = base64.b64decode(r.body.get("data_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            raise _ShotError("guest returned invalid base64") from None
        fmt = str(r.body.get("format") or "png")
        return self._finalize("guest", fmt, int(r.body.get("width") or 0), int(r.body.get("height") or 0), raw)

    async def _shot_hypervisor(self, vmid: int) -> Screenshot:
        tmp = Path(tempfile.gettempdir()) / f"atlas-shot-{vmid}-{uuid.uuid4().hex}.ppm"
        try:
            try:
                resp = await self.pve.post(self._vm(vmid, "monitor"), command=f"screendump {tmp}")
            except PveError as exc:
                raise _ShotError(f"monitor screendump failed: {exc.message}") from None
            raw = _image_from_monitor(resp)
            if raw is None:
                # Real PVE answers with monitor text and leaves the file on the node; works when
                # the daemon runs on the PVE host (or shares /tmp with it).
                try:
                    raw = tmp.read_bytes()
                except OSError:
                    raise _ShotError("screendump produced no image the daemon can read "
                                     "(is Atlas running on the Proxmox node?)") from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        fmt, w, h = _image_info(raw)
        if fmt == "ppm":
            raw, fmt = _ppm_to_png(raw) or (raw, "ppm")
        return self._finalize("hypervisor", fmt, w, h, raw)

    def _finalize(self, source: str, fmt: str, w: int, h: int, raw: bytes) -> Screenshot:
        if not raw:
            raise _ShotError("empty image")
        info_fmt, iw, ih = _image_info(raw)
        if info_fmt != "unknown":
            fmt, w, h = info_fmt, iw, ih
        warns: list[Notice] = []
        cap = self.limits.max_screenshot_bytes
        if len(raw) > cap:
            original = {"width": w, "height": h, "bytes": len(raw)}
            Image = _pil()
            if Image is None:
                raise _ShotError(f"image is {len(raw)} bytes (> {cap}) and Pillow is not installed to downscale it")
            raw, w, h = _downscale(Image, raw, cap)
            fmt = "png"
            warns.append(warning("SCREENSHOT_DOWNSCALED", f"image exceeded {cap} bytes and was downscaled",
                                 original_size=original))
        return Screenshot(source, fmt, w, h, base64.b64encode(raw).decode(), warns)

    # ------------------------------------------------------------------ close
    async def close(self, handle: GuestHandle) -> bool:
        vmid = int(handle.ref)
        entry = handle.entry
        g = self._guests.pop(handle.ref, None)
        if g is not None:
            if not g.dead:
                try:
                    async with asyncio.timeout(self.prepare_revert_timeout):
                        await self._request(g, "prepare_revert", self.prepare_revert_timeout)
                except (TimeoutError, AtlasError):
                    pass            # best effort; the rollback happens regardless
            await g.channel.aclose()
        golden = self.config.golden_snapshot_name
        try:
            upid = await self.pve.post(self._vm(vmid, f"snapshot/{golden}/rollback"))
            failure = await self._wait_task(upid)
            if failure:
                raise PveError(500, failure)
            # Normalise power state: a vmstate snapshot resumes running, a plain one leaves it stopped.
            status = await self._vm_status(vmid)
            if entry.warm and status != "running":
                await self._start_vm(vmid)
            elif not entry.warm and status == "running":
                await self._wait_task(await self.pve.post(self._vm(vmid, "status/stop")))
        except (PveError, AtlasError) as exc:
            self.failed_vmids.add(vmid)
            raise AtlasError("VM_ROLLBACK_FAILED", f"rollback of VM {vmid} to '{golden}' failed; entry unavailable",
                             vmid=vmid, proxmox_error=_msg(exc)) from exc
        return True


# --------------------------------------------------------------------------- image helpers

class _ShotError(Exception):
    pass


def _msg(exc: Exception) -> str:
    if isinstance(exc, AtlasError):
        return f"{exc.code}: {exc.notice.message}"
    return str(exc)


def _image_from_monitor(resp: Any) -> bytes | None:
    """The monitor endpoint normally returns text; a proxy/mock may return the image itself."""
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp) if _image_info(bytes(resp))[0] != "unknown" else None
    if isinstance(resp, str) and len(resp) >= 16:
        try:
            raw = base64.b64decode(resp, validate=True)
        except (binascii.Error, ValueError):
            return None
        return raw if _image_info(raw)[0] != "unknown" else None
    return None


def _image_info(raw: bytes) -> tuple[str, int, int]:
    if raw.startswith(_PNG_MAGIC) and len(raw) >= 24:
        w, h = struct.unpack(">II", raw[16:24])
        return "png", w, h
    if raw[:2] == b"P6":
        m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", raw)
        if m:
            return "ppm", int(m.group(1)), int(m.group(2))
    return "unknown", 0, 0


def _ppm_to_png(raw: bytes) -> tuple[bytes, str] | None:
    Image = _pil()
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            out = io.BytesIO()
            im.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue(), "png"
    except Exception:
        return None


def _downscale(Image: Any, raw: bytes, cap: int) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        while True:
            w, h = max(1, w // 2), max(1, h // 2)
            out = io.BytesIO()
            im.resize((w, h)).save(out, format="PNG", optimize=True)
            if out.tell() <= cap or (w == 1 and h == 1):
                return out.getvalue(), w, h
