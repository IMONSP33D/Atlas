"""MockGuest: a scripted guest agent listening on a Unix socket.

Speaks the real frame protocol (atlas.guest.protocol), so ProxmoxBackend can be
exercised end to end with no VM. Behaviour is driven by the first line of `code`
(same spirit as FakeBackend):

    #exit N            -> exec_done reason=exit, exit_code=N
    #timeout           -> partial output, exec_done reason=timeout
    #oom               -> exec_done reason=oom
    #killed            -> exec_done reason=killed
    #spawn_failed      -> exec_done reason=spawn_failed
    #compile_failed    -> stderr text, exec_done reason=compile_failed
    #sleep S           -> sleep S seconds (heartbeats keep flowing), then run the rest
    #flood N           -> emit N bytes of "x" on stdout in 64 KiB events, then exit 0
    #stderr TEXT       -> TEXT on stderr, exit 0
    #cat NAME          -> stdout = content of a previously pushed file NAME
    #crash             -> close the connection mid-exec (no exec_done)
    #hang              -> go completely silent (no heartbeats, no replies)
    anything else      -> echo: stdout = code
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from ..guest.protocol import (
    PROTOCOL_VERSION, Frame, FrameError, read_frame, write_frame,
)

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154"
    "789c6360000002000154a24f5d0000000049454e44ae426082"
)


class MockGuest:
    def __init__(self, socket_path: str | os.PathLike[str], *,
                 heartbeat_interval: float = 0.05,
                 agent_version: str = PROTOCOL_VERSION,
                 heartbeats: bool = True,
                 answer_hello: bool = True,
                 screenshot_mode: str = "ok",      # ok | fail | hang | no_window
                 screenshot_png: bytes = PNG_1X1,
                 screenshot_size: tuple[int, int] = (1, 1),
                 corrupt_next_frame: bool = False,
                 fail_file_hash: bool = False) -> None:
        self.path = Path(socket_path)
        self.heartbeat_interval = heartbeat_interval
        self.agent_version = agent_version
        self.heartbeats = heartbeats
        self.answer_hello = answer_hello
        self.exec_running = False
        self.screenshot_mode = screenshot_mode
        self.screenshot_png = screenshot_png
        self.screenshot_size = screenshot_size
        self.corrupt_next_frame = corrupt_next_frame
        self.fail_file_hash = fail_file_hash
        self.hung = False
        self.files: dict[str, bytes] = {}
        self.received: list[Frame] = []
        self.prepare_reverts = 0
        self._partial: dict[str, bytearray] = {}
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> "MockGuest":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._on_conn, path=str(self.path))
        return self

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for w in list(self._conns):
            w.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def __aenter__(self) -> "MockGuest":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------ connection
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        lock = asyncio.Lock()
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)

        async def send(f: Frame) -> None:
            if self.hung:
                return
            try:
                async with lock:
                    if self.corrupt_next_frame:
                        self.corrupt_next_frame = False
                        junk = b"not json at all"
                        writer.write(len(junk).to_bytes(4, "big") + junk)
                        await writer.drain()
                        return
                    await write_frame(writer, f)
            except ConnectionError:
                pass

        async def beat() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                if self.heartbeats and not self.hung:
                    await send(Frame.event("heartbeat", ts=time.time(), load=0.0, mem_free_mb=512))

        hb = asyncio.create_task(beat())
        try:
            while True:
                try:
                    req = await read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError, FrameError):
                    return
                self.received.append(req)
                if self.hung:
                    continue
                await self._handle(req, send, writer)
        except asyncio.CancelledError:
            return
        finally:
            hb.cancel()
            self._conns.discard(writer)
            if task:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    # ------------------------------------------------------------------ verbs
    async def _handle(self, req: Frame, send, writer: asyncio.StreamWriter) -> None:
        t, b = req.type, req.body
        if t == "hello":
            if self.answer_hello:
                await send(Frame.reply(req, agent_version=self.agent_version, os="mockos", build="1",
                                       session_type="interactive", display=True))
        elif t == "ping":
            await send(Frame.reply(req, ok=True, uptime_sec=1))
        elif t == "prepare_revert":
            self.prepare_reverts += 1
            await send(Frame.reply(req, ok=True))
        elif t == "put_file_begin":
            self._partial[b["name"]] = bytearray()
            await send(Frame.reply(req, ok=True))
        elif t == "put_file_chunk":
            buf = self._partial.get(b["name"])
            if buf is None or b["offset"] != len(buf):
                await send(Frame.error(req, "bad_chunk", "unexpected offset"))
                return
            buf += base64.b64decode(b["data_b64"])
            await send(Frame.reply(req, ok=True))
        elif t == "put_file_end":
            data = bytes(self._partial.pop(b["name"], b""))
            self.files[b["name"]] = data
            if self.fail_file_hash:
                await send(Frame.error(req, "hash_mismatch", "sha256 mismatch"))
            else:
                await send(Frame.reply(req, ok=True, sha256=hashlib.sha256(data).hexdigest()))
        elif t == "screenshot":
            await self._screenshot(req, send)
        elif t == "kill":
            await send(Frame.reply(req, ok=True, killed=self.exec_running))
        elif t == "exec":
            if self.exec_running:
                await send(Frame.error(req, "busy", "another exec is running"))
                return
            if not b.get("exec_id") or ("code" not in b and "entrypoint" not in b):
                await send(Frame.error(req, "bad_request", "exec needs exec_id and code or entrypoint"))
                return
            self.exec_running = True
            await send(Frame.reply(req, ok=True, exec_id=b["exec_id"]))
            task = asyncio.create_task(self._exec(req, send, writer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await send(Frame.error(req, "unknown_verb", t))

    async def _screenshot(self, req: Frame, send) -> None:
        mode = self.screenshot_mode
        if mode == "hang":
            return
        if mode == "fail":
            await send(Frame.error(req, "SCREENSHOT_FAILED", "capture failed"))
        elif mode == "no_window" or req.body.get("window_title") == "nope":
            await send(Frame.error(req, "SCREENSHOT_WINDOW_NOT_FOUND", "no window matched"))
        else:
            w, h = self.screenshot_size
            await send(Frame.reply(req, format="png", width=w, height=h,
                                   data_b64=base64.b64encode(self.screenshot_png).decode()))

    async def _exec(self, req: Frame, send, writer: asyncio.StreamWriter) -> None:
        b = req.body
        exec_id = b["exec_id"]
        code = (b.get("code") or "").strip()
        lines = code.splitlines()
        first = lines[0] if lines else ""

        async def out(stream: str, data: bytes) -> None:
            await send(Frame.event("exec_output", exec_id=exec_id, stream=stream,
                                   data_b64=base64.b64encode(data).decode()))

        async def done(reason: str, exit_code: int | None) -> None:
            await send(Frame.event("exec_done", exec_id=exec_id, exit_code=exit_code, wall_time_ms=7,
                                   cpu_time_ms=3, peak_memory_bytes=1 << 20, reason=reason, detail=None,
                                   stdout_truncated=False, stderr_truncated=False))

        try:
            await self._script(first, lines, code, b, out, done, writer)
        finally:
            self.exec_running = False

    async def _script(self, first, lines, code, b, out, done, writer) -> None:
        if first.startswith("#sleep "):
            await asyncio.sleep(float(first.split()[1]))
            lines = lines[1:]
            code = "\n".join(lines)
            first = lines[0] if lines else ""
        if first == "#hang":
            self.hung = True
            return
        if first == "#crash":
            writer.close()
            return
        if first.startswith("#exit "):
            n = int(first.split()[1])
            await out("stderr", f"exited {n}\n".encode())
            await done("exit", n)
        elif first == "#timeout":
            await out("stdout", b"partial\n")
            await done("timeout", None)
        elif first in ("#oom", "#killed", "#spawn_failed"):
            await done(first[1:], None)
        elif first == "#compile_failed":
            await out("stderr", b"error: expected ';'\n")
            await done("compile_failed", 1)
        elif first.startswith("#flood "):
            remaining = int(first.split()[1])
            while remaining > 0:
                n = min(remaining, 65536)
                await out("stdout", b"x" * n)
                remaining -= n
            await done("exit", 0)
        elif first.startswith("#stderr "):
            await out("stderr", first[len("#stderr "):].encode())
            await done("exit", 0)
        elif first.startswith("#cat "):
            await out("stdout", self.files.get(first.split(None, 1)[1].strip(), b"<missing>"))
            await done("exit", 0)
        elif b.get("entrypoint"):
            await out("stdout", f"ran {b['entrypoint']} {' '.join(b.get('args') or [])}\n".encode())
            await done("exit", 0)
        else:
            await out("stdout", (code + ("\n" if code else "")).encode())
            await done("exit", 0)
