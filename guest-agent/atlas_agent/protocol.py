# VENDORED verbatim from src/atlas/guest/protocol.py (Atlas repo); do not edit here, edit upstream and re-copy.
"""Frame codec for the virtio-serial channel (guest-agent/README.md, "Transport").

    frame := uint32_be(length) || utf8(json(payload))
    payload := {"type": <verb or event>, "id": <request id or null>, ...fields}

- `type` is the verb (host→guest request) or event name (guest→host). Replies carry
  the request's `id` and `type: "<verb>.reply"` or `type: "error"`.
- Unsolicited guest events (`heartbeat`, `exec_output`, `exec_done`) have `id: null`.
- Max frame 4 MiB. File pushes are chunked (CHUNK_BYTES per put_file_chunk).

This module has no dependencies beyond the stdlib on purpose: the in-VM agent
vendors it, and the agent must not drag the daemon's deps into the golden image.
"""
from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_MAJOR = 1
PROTOCOL_VERSION = "1.0"
MAX_FRAME = 4 * 1024 * 1024
CHUNK_BYTES = 1 * 1024 * 1024          # raw bytes per put_file_chunk (~1.37 MiB after b64)
HEARTBEAT_INTERVAL_SEC = 5
_HDR = struct.Struct(">I")

VERBS = frozenset({
    "hello", "ping", "put_file_begin", "put_file_chunk", "put_file_end",
    "exec", "kill", "screenshot", "prepare_revert",
})
EVENTS = frozenset({"heartbeat", "exec_output", "exec_done"})
EXEC_DONE_REASONS = frozenset({"exit", "timeout", "killed", "oom", "spawn_failed", "compile_failed"})


class FrameError(Exception):
    """Malformed frame. Host maps this to GUEST_AGENT_PROTOCOL."""


class FrameTooLarge(FrameError):
    pass


@dataclass
class Frame:
    type: str
    id: str | None = None
    body: dict[str, Any] = field(default_factory=dict)

    # ---- constructors
    @classmethod
    def request(cls, verb: str, id: str, **body: Any) -> "Frame":
        if verb not in VERBS:
            raise ValueError(f"unknown verb {verb!r}")
        return cls(verb, id, body)

    @classmethod
    def reply(cls, req: "Frame", **body: Any) -> "Frame":
        return cls(f"{req.type}.reply", req.id, body)

    @classmethod
    def error(cls, req: "Frame | None", code: str, message: str, **data: Any) -> "Frame":
        return cls("error", req.id if req else None, {"code": code, "message": message, "data": data})

    @classmethod
    def event(cls, name: str, **body: Any) -> "Frame":
        if name not in EVENTS:
            raise ValueError(f"unknown event {name!r}")
        return cls(name, None, body)

    # ---- predicates
    @property
    def is_event(self) -> bool:
        return self.type in EVENTS

    @property
    def is_error(self) -> bool:
        return self.type == "error"

    def is_reply_to(self, req: "Frame") -> bool:
        return self.id == req.id and (self.type == f"{req.type}.reply" or self.is_error)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, **self.body}

    @classmethod
    def from_dict(cls, d: Any) -> "Frame":
        if not isinstance(d, dict):
            raise FrameError("payload is not a JSON object")
        t = d.get("type")
        if not isinstance(t, str) or not t:
            raise FrameError("payload missing 'type'")
        i = d.get("id")
        if i is not None and not isinstance(i, str):
            raise FrameError("'id' must be a string or null")
        body = {k: v for k, v in d.items() if k not in ("type", "id")}
        return cls(t, i, body)


# --------------------------------------------------------------------------- bytes <-> frame

def encode_frame(frame: Frame) -> bytes:
    payload = json.dumps(frame.to_dict(), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise FrameTooLarge(f"frame is {len(payload)} bytes; max {MAX_FRAME}")
    return _HDR.pack(len(payload)) + payload


def decode_frame(payload: bytes) -> Frame:
    try:
        return Frame.from_dict(json.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"bad JSON payload: {exc}") from exc


class FrameReader:
    """Incremental parser for a byte stream. Feed bytes, pull complete frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        out: list[Frame] = []
        while True:
            if len(self._buf) < _HDR.size:
                return out
            (n,) = _HDR.unpack_from(self._buf)
            if n > MAX_FRAME:
                raise FrameTooLarge(f"announced frame length {n} exceeds {MAX_FRAME}")
            if len(self._buf) < _HDR.size + n:
                return out
            payload = bytes(self._buf[_HDR.size:_HDR.size + n])
            del self._buf[:_HDR.size + n]
            out.append(decode_frame(payload))


# --------------------------------------------------------------------------- asyncio helpers

async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """Read exactly one frame. Raises asyncio.IncompleteReadError on EOF."""
    hdr = await reader.readexactly(_HDR.size)
    (n,) = _HDR.unpack(hdr)
    if n > MAX_FRAME:
        raise FrameTooLarge(f"announced frame length {n} exceeds {MAX_FRAME}")
    return decode_frame(await reader.readexactly(n))


async def write_frame(writer: asyncio.StreamWriter, frame: Frame) -> None:
    writer.write(encode_frame(frame))
    await writer.drain()


def major_compatible(agent_version: str) -> bool:
    """hello handshake: refuse a guest whose major differs (GUEST_AGENT_VERSION)."""
    try:
        return int(str(agent_version).split(".")[0]) == PROTOCOL_MAJOR
    except ValueError:
        return False
