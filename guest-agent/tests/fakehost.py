"""Fake Atlas host: listens on a Unix socket, the agent connects as client (dev mode)."""
from __future__ import annotations

import asyncio
import base64
import itertools

from atlas_agent.protocol import Frame, FrameReader, encode_frame


class FakeHost:
    def __init__(self, path: str):
        self.path = path
        self.frames: list[Frame] = []
        self._cond = asyncio.Condition()
        self._connected = asyncio.Event()
        self._ids = itertools.count(1)
        self._server = None
        self._writer = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._on_conn, self.path)

    async def stop(self) -> None:
        if self._writer:
            self._writer.close()
        self._server.close()

    async def _on_conn(self, reader, writer) -> None:
        self._writer = writer
        self._connected.set()
        fr = FrameReader()
        while data := await reader.read(65536):
            async with self._cond:
                self.frames.extend(fr.feed(data))
                self._cond.notify_all()

    async def wait_for(self, pred, timeout: float = 10.0) -> Frame:
        async def scan():
            async with self._cond:
                while True:
                    for f in self.frames:
                        if pred(f):
                            return f
                    await self._cond.wait()
        return await asyncio.wait_for(scan(), timeout)

    async def send(self, verb: str, **body) -> Frame:
        await asyncio.wait_for(self._connected.wait(), 10)
        req = Frame(verb, f"r{next(self._ids)}", body)  # raw Frame so tests can send unknown verbs
        self._writer.write(encode_frame(req))
        await self._writer.drain()
        return req

    async def request(self, verb: str, timeout: float = 10.0, **body) -> Frame:
        req = await self.send(verb, **body)
        return await self.wait_for(lambda f: f.id == req.id and f.type != "heartbeat", timeout)

    async def exec(self, language="python", code="", timeout=20.0, **body):
        """Start an exec; returns (exec_id, wait_done())."""
        eid = f"e{next(self._ids)}"
        rep = await self.request("exec", exec_id=eid, language=language, code=code, **body)
        assert rep.type == "exec.reply", rep
        async def done() -> Frame:
            return await self.wait_for(lambda f: f.type == "exec_done" and f.body["exec_id"] == eid, timeout)
        return eid, done

    def output(self, eid: str, stream: str) -> bytes:
        return b"".join(base64.b64decode(f.body["data_b64"]) for f in self.frames
                        if f.type == "exec_output" and f.body["exec_id"] == eid and f.body["stream"] == stream)

    def count(self, type_: str) -> int:
        return sum(1 for f in self.frames if f.type == type_)
