"""Transport loop and verb handlers. A dumb pipe with a process supervisor (execs.py)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import platform
import shutil
import stat
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import AGENT_VERSION
from .execs import Execution, valid_name
from .protocol import (HEARTBEAT_INTERVAL_SEC, MAX_FRAME, Frame, FrameError, FrameReader,
                       FrameTooLarge, encode_frame)

log = logging.getLogger("atlas_agent")


class VerbError(Exception):
    def __init__(self, code: str, message: str, **data):
        super().__init__(message)
        self.code, self.data = code, data


# --------------------------------------------------------------------------- channel

class Channel:
    """read() -> bytes (b'' on EOF); write(bytes); close()."""

    def __init__(self, reader=None, writer=None, fd: int | None = None):
        self._r, self._w, self._fd = reader, writer, fd
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        if fd is not None:
            # Daemon thread: a blocked read on the char device must not stop the process from exiting.
            loop = asyncio.get_running_loop()
            threading.Thread(target=self._pump, args=(loop,), daemon=True).start()

    def _pump(self, loop) -> None:
        while True:
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                data = b""
            loop.call_soon_threadsafe(self._q.put_nowait, data)
            if not data:
                return

    async def read(self) -> bytes:
        return await self._r.read(65536) if self._r else await self._q.get()

    async def write(self, data: bytes) -> None:
        if self._w:
            self._w.write(data)
            await self._w.drain()
            return
        def blocking():
            mv = memoryview(data)
            while mv:
                mv = mv[os.write(self._fd, mv):]
        await asyncio.get_running_loop().run_in_executor(None, blocking)

    def close(self) -> None:
        if self._w:
            self._w.close()
        elif self._fd is not None:
            os.close(self._fd)


async def open_channel(path: str) -> Channel:
    """Unix socket -> connect as client (dev mode); anything else -> virtio char device, read+write."""
    if hasattr(stat, "S_ISSOCK") and os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode):
        r, w = await asyncio.open_unix_connection(path)
        return Channel(reader=r, writer=w)
    return Channel(fd=os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0)))


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, channel: Channel, workdir: str | Path = "/work", input_dir: str | Path | None = None,
                 display: str | None = None, heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC):
        self.ch = channel
        self.workdir = Path(workdir)
        self.input_dir = Path(input_dir) if input_dir else self.workdir / "in"
        self.display = display
        self.hb_interval = heartbeat_interval
        self.started = time.monotonic()
        self._wlock = asyncio.Lock()
        self._files: dict[str, dict] = {}
        self._exec: Execution | None = None
        self._exec_task: asyncio.Task | None = None

    async def send(self, frame: Frame) -> None:
        async with self._wlock:  # frames from heartbeat / exec pumps / replies must not interleave
            try:
                await self.ch.write(encode_frame(frame))
            except (ConnectionError, OSError) as e:
                log.warning("write failed: %s", e)

    async def serve(self) -> None:
        hb = asyncio.create_task(self._heartbeats())
        reader = FrameReader()
        try:
            while data := await self.ch.read():
                try:
                    frames = reader.feed(data)
                except FrameTooLarge as e:
                    await self.send(Frame.error(None, "GUEST_AGENT_PROTOCOL", "frame too large", detail=str(e)))
                    return  # stream is unrecoverable; host reconnects
                except FrameError as e:
                    # the offending frame was already consumed, so the stream stays in sync
                    await self.send(Frame.error(None, "GUEST_AGENT_PROTOCOL", "malformed frame", detail=str(e)))
                    continue
                for f in frames:
                    await self._dispatch(f)
        finally:
            hb.cancel()
            if self._exec:
                self._exec.kill()
            if self._exec_task:
                await asyncio.wait({self._exec_task}, timeout=5)

    async def _heartbeats(self) -> None:
        while True:
            load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
            await self.send(Frame.event("heartbeat", ts=time.time(), load=load, mem_free_mb=_mem_free_mb()))
            await asyncio.sleep(self.hb_interval)

    async def _dispatch(self, req: Frame) -> None:
        handler = getattr(self, f"v_{req.type}", None) if req.type.isidentifier() else None
        if handler is None or req.is_event or req.type.endswith(".reply"):
            await self.send(Frame.error(req, "GUEST_AGENT_PROTOCOL", f"unknown verb {req.type!r}"))
            return
        try:
            await self.send(Frame.reply(req, **(await handler(req.body) or {})))
        except VerbError as e:
            await self.send(Frame.error(req, e.code, str(e), **e.data))
        except Exception as e:
            log.exception("verb %s failed", req.type)
            await self.send(Frame.error(req, "GUEST_AGENT_PROTOCOL", f"internal error in {req.type}", detail=repr(e)))

    # ---- verbs
    async def v_hello(self, b):
        iface = bool(self.display) and Path("/tmp/.X11-unix", "X" + self.display.lstrip(":").split(".")[0]).exists()
        iface = iface or sys.platform == "win32"
        return {"agent_version": AGENT_VERSION, "os": "windows" if sys.platform == "win32" else "linux",
                "build": platform.platform(), "session_type": "interactive" if iface else "service",
                "display": iface}

    async def v_ping(self, b):
        return {"ok": True, "uptime_sec": int(time.monotonic() - self.started)}

    async def v_prepare_revert(self, b):
        for st in self._files.values():
            st["fh"].close()
        self._files.clear()
        if hasattr(os, "sync"):
            await asyncio.get_running_loop().run_in_executor(None, os.sync)
        return {"ok": True}

    async def v_put_file_begin(self, b):
        name, size, sha = b.get("name"), b.get("size"), b.get("sha256")
        if not valid_name(name):
            raise VerbError("FILE_NAME_INVALID", f"invalid file name {name!r}", name=name)
        if not isinstance(size, int) or size < 0 or not isinstance(sha, str):
            raise VerbError("GUEST_AGENT_PROTOCOL", "put_file_begin needs int size and str sha256")
        if name in self._files:
            self._files.pop(name)["fh"].close()
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self._files[name] = {"fh": open(self.input_dir / name, "wb"), "size": size, "sha": sha.lower(),
                             "h": hashlib.sha256(), "n": 0}
        return {"ok": True}

    async def v_put_file_chunk(self, b):
        st = self._files.get(b.get("name"))
        if st is None:
            raise VerbError("FILE_TRANSFER_FAILED", "chunk without put_file_begin", name=b.get("name"))
        try:
            data = base64.b64decode(b.get("data_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            raise VerbError("FILE_B64_INVALID", "data_b64 is not valid base64", name=b["name"])
        if b.get("offset") != st["n"] or st["n"] + len(data) > st["size"]:
            raise VerbError("FILE_TRANSFER_FAILED", "chunk offset/size out of sequence", name=b["name"],
                            expected_offset=st["n"])
        st["fh"].write(data)
        st["h"].update(data)
        st["n"] += len(data)
        return {"ok": True}

    async def v_put_file_end(self, b):
        name = b.get("name")
        st = self._files.pop(name, None)
        if st is None:
            raise VerbError("FILE_TRANSFER_FAILED", "put_file_end without put_file_begin", name=name)
        st["fh"].close()
        actual = st["h"].hexdigest()
        if st["n"] != st["size"]:
            raise VerbError("FILE_TRANSFER_FAILED", "short file", name=name, expected=st["size"], actual=st["n"])
        if actual != st["sha"]:
            (self.input_dir / name).unlink(missing_ok=True)
            raise VerbError("FILE_HASH_MISMATCH", "sha256 mismatch", name=name, expected=st["sha"], actual=actual)
        return {"ok": True, "sha256": actual}

    async def v_exec(self, b):
        if self._exec_task and not self._exec_task.done():
            raise VerbError("GUEST_AGENT_PROTOCOL", "exec already running (one at a time)")
        if not b.get("exec_id") or (b.get("code") is None and b.get("entrypoint") is None):
            raise VerbError("GUEST_AGENT_PROTOCOL", "exec needs exec_id and code or entrypoint")
        self._exec = Execution(b, self.workdir, self.input_dir, self.display, self.send)
        self._exec_task = asyncio.create_task(self._exec.run())
        return {"ok": True, "exec_id": b["exec_id"]}  # accepted; output and exec_done follow as events

    async def v_kill(self, b):
        running = self._exec and self._exec_task and not self._exec_task.done() and self._exec.exec_id == b.get("exec_id")
        if running:
            self._exec.kill()
        return {"ok": True, "killed": bool(running)}

    async def v_screenshot(self, b):
        png = await asyncio.get_running_loop().run_in_executor(None, self._shot, b.get("window_title"), b.get("region"))
        if len(png) * 4 // 3 + 1024 > MAX_FRAME:
            png = _downscale(png)
        w, h = struct.unpack(">II", png[16:24])
        return {"format": "png", "width": w, "height": h, "data_b64": base64.b64encode(png).decode()}

    def _shot(self, title, region) -> bytes:
        if sys.platform == "win32":
            raise VerbError("SCREENSHOT_FAILED", "windows screenshot not implemented", detail="DXGI/BitBlt stub")
        if self.display:
            os.environ["DISPLAY"] = self.display
        if title:
            region = _window_rect(title)
        errs = []
        try:
            import mss, mss.tools  # optional
            with mss.mss() as sct:
                mon = ({"left": region["x"], "top": region["y"], "width": region["width"], "height": region["height"]}
                       if region else sct.monitors[0])
                s = sct.grab(mon)
                return mss.tools.to_png(s.rgb, s.size)
        except Exception as e:
            errs.append(f"mss: {e!r}")
        if shutil.which("scrot"):
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "s.png")
                cmd = ["scrot", "-o", out] + (["-a", "{x},{y},{width},{height}".format(**region)] if region else [])
                import subprocess
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if p.returncode == 0 and os.path.exists(out):
                    return Path(out).read_bytes()
                errs.append(f"scrot: rc={p.returncode} {p.stderr.strip()}")
        else:
            errs.append("scrot: not installed")
        raise VerbError("SCREENSHOT_FAILED", "no screenshot backend worked", detail="; ".join(errs))


def _window_rect(title: str) -> dict:
    """Best effort via xdotool; absent tool or no match both mean SCREENSHOT_WINDOW_NOT_FOUND."""
    import subprocess
    if not shutil.which("xdotool"):
        raise VerbError("SCREENSHOT_WINDOW_NOT_FOUND", "window lookup unavailable (xdotool not installed)")
    ids = subprocess.run(["xdotool", "search", "--name", title], capture_output=True, text=True).stdout.split()
    if not ids:
        raise VerbError("SCREENSHOT_WINDOW_NOT_FOUND", f"no window matching {title!r}")
    g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", ids[0]], capture_output=True, text=True).stdout
    kv = dict(line.split("=", 1) for line in g.split() if "=" in line)
    return {"x": int(kv["X"]), "y": int(kv["Y"]), "width": int(kv["WIDTH"]), "height": int(kv["HEIGHT"])}


def _downscale(png: bytes) -> bytes:
    # stdlib cannot resample PNG; use Pillow if present. Note the effective cap is MAX_FRAME (4 MiB incl. base64), not 5 MB.
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        while len(png) * 4 // 3 + 1024 > MAX_FRAME:
            img = img.resize((max(1, img.width // 2), max(1, img.height // 2)))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png = buf.getvalue()
        return png
    except ImportError:
        raise VerbError("SCREENSHOT_FAILED", "screenshot exceeds frame limit and Pillow is not installed for downscaling",
                        detail=f"png_bytes={len(png)}")


def _mem_free_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None
