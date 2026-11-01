from __future__ import annotations

import asyncio
import struct

import pytest

from atlas.guest.protocol import (
    MAX_FRAME, Frame, FrameError, FrameReader, FrameTooLarge,
    decode_frame, encode_frame, major_compatible, read_frame, write_frame,
)


def test_roundtrip():
    f = Frame.request("exec", "r1", exec_id="e1", language="python", code="print(1)", timeout_sec=5)
    b = encode_frame(f)
    assert struct.unpack(">I", b[:4])[0] == len(b) - 4
    g = decode_frame(b[4:])
    assert g == f and not g.is_event and g.body["code"] == "print(1)"


def test_reply_and_error_match_request():
    req = Frame.request("ping", "abc")
    assert Frame.reply(req, ok=True, uptime_sec=3).is_reply_to(req)
    err = Frame.error(req, "EXEC_SPAWN_FAILED", "no interpreter")
    assert err.is_reply_to(req) and err.is_error and err.body["code"] == "EXEC_SPAWN_FAILED"
    assert not Frame.reply(Frame.request("ping", "other")).is_reply_to(req)


def test_events_have_null_id():
    e = Frame.event("heartbeat", ts=1, load=0.1, mem_free_mb=512)
    assert e.id is None and e.is_event
    with pytest.raises(ValueError):
        Frame.event("not_an_event")
    with pytest.raises(ValueError):
        Frame.request("get_file", "x")   # deliberately absent verb


def test_reader_handles_split_and_coalesced_frames():
    frames = [Frame.request("ping", str(i)) for i in range(3)]
    data = b"".join(encode_frame(f) for f in frames)
    r = FrameReader()
    got = []
    for i in range(0, len(data), 5):          # feed 5 bytes at a time
        got += r.feed(data[i:i + 5])
    assert got == frames
    assert FrameReader().feed(data) == frames  # all at once


def test_too_large_rejected_before_buffering():
    r = FrameReader()
    with pytest.raises(FrameTooLarge):
        r.feed(struct.pack(">I", MAX_FRAME + 1))
    with pytest.raises(FrameTooLarge):
        encode_frame(Frame.request("exec", "x", code="a" * (MAX_FRAME + 1)))


@pytest.mark.parametrize("payload", [b"not json", b"[1,2]", b'{"id":"x"}', b'{"type":"ping","id":5}', b"\xff\xfe"])
def test_malformed_payloads(payload):
    with pytest.raises(FrameError):
        decode_frame(payload)


def test_version_gate():
    assert major_compatible("1.3")
    assert not major_compatible("2.0")
    assert not major_compatible("garbage")


async def test_asyncio_helpers():
    rd = asyncio.StreamReader()

    class W:
        def __init__(self): self.buf = b""
        def write(self, b): self.buf += b
        async def drain(self): pass

    w = W()
    await write_frame(w, Frame.request("hello", "h1", atlas_version="0.1.0"))
    rd.feed_data(w.buf)
    f = await read_frame(rd)
    assert f.type == "hello" and f.body == {"atlas_version": "0.1.0"}
    rd.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(rd)
