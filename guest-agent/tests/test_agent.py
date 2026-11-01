import asyncio
import base64
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from atlas_agent import execs
from atlas_agent.protocol import Frame, FrameReader, encode_frame

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = Path(__file__).resolve().parents[1]


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"  # zombie awaiting a non-reaping init counts as gone


def test_protocol_is_vendored_verbatim():
    vendored = (AGENT_DIR / "atlas_agent" / "protocol.py").read_bytes()
    upstream = (ROOT / "src" / "atlas" / "guest" / "protocol.py").read_bytes()
    first, rest = vendored.split(b"\n", 1)
    assert first.startswith(b"# VENDORED")
    assert rest == upstream


async def test_hello_ping_and_unknown_verb(host):
    r = await host.request("hello", atlas_version="1.0")
    assert r.type == "hello.reply"
    assert r.body["agent_version"] == "1.0"
    assert r.body["os"] == "linux" and r.body["session_type"] in ("interactive", "service")
    assert isinstance(r.body["display"], bool) and r.body["build"]
    p = await host.request("ping")
    assert p.body["ok"] is True and p.body["uptime_sec"] >= 0
    e = await host.request("get_file", name="x")
    assert e.is_error and e.body["code"] == "GUEST_AGENT_PROTOCOL"
    assert (await host.request("prepare_revert")).body == {"ok": True}


async def push(host, name, data: bytes, sha=None, chunk=10):
    await host.request("put_file_begin", name=name, size=len(data), sha256=sha or hashlib.sha256(data).hexdigest())
    for off in range(0, len(data), chunk):
        r = await host.request("put_file_chunk", name=name, offset=off,
                               data_b64=base64.b64encode(data[off:off + chunk]).decode())
        assert r.body == {"ok": True}
    return await host.request("put_file_end", name=name)


async def test_put_file_round_trip(host, sandbox):
    data = os.urandom(95)
    r = await push(host, "blob.bin", data)
    assert r.body == {"ok": True, "sha256": hashlib.sha256(data).hexdigest()}
    assert (sandbox / "work" / "in" / "blob.bin").read_bytes() == data


async def test_put_file_hash_mismatch(host, sandbox):
    r = await push(host, "bad.bin", b"hello", sha="0" * 64)
    assert r.is_error and r.body["code"] == "FILE_HASH_MISMATCH"
    assert r.body["data"]["actual"] == hashlib.sha256(b"hello").hexdigest()
    assert not (sandbox / "work" / "in" / "bad.bin").exists()


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "..", "", "/etc/passwd", "x..y"])
async def test_put_file_bad_name(host, name):
    r = await host.request("put_file_begin", name=name, size=1, sha256="0" * 64)
    assert r.is_error and r.body["code"] == "FILE_NAME_INVALID"


async def test_exec_python_streams_output(host):
    eid, done = await host.exec(code="import sys\nprint('hello')\nprint('oops', file=sys.stderr)\n", args=[])
    d = (await done()).body
    assert d["reason"] == "exit" and d["exit_code"] == 0 and d["detail"] is None
    assert host.output(eid, "stdout") == b"hello\n" and host.output(eid, "stderr") == b"oops\n"
    assert d["wall_time_ms"] >= 0 and d["cpu_time_ms"] >= 0 and d["peak_memory_bytes"] > 0
    assert d["stdout_truncated"] is False and d["stderr_truncated"] is False
    assert host.count("exec_done") == 1
    assert (host.agent.workdir / "code.py").exists()


async def test_exec_args_env_stdin(host):
    eid, done = await host.exec(code="import sys,os\nprint(sys.argv[1], os.environ['FOO'], sys.stdin.read())",
                                args=["A"], env={"FOO": "bar"}, stdin="in")
    assert (await done()).body["exit_code"] == 0
    assert host.output(eid, "stdout") == b"A bar in\n"


async def test_exec_nonzero_exit(host):
    _, done = await host.exec(code="import sys; sys.exit(3)")
    d = (await done()).body
    assert d["reason"] == "exit" and d["exit_code"] == 3


async def test_exec_entrypoint_from_input_dir(host):
    await push(host, "main.py", b"print('from input')")
    eid, done = await host.exec(entrypoint="main.py", code=None)
    assert (await done()).body["exit_code"] == 0
    assert host.output(eid, "stdout") == b"from input\n"


async def test_exec_timeout_kills_process_group(host):
    code = ("import subprocess, time\n"
            "c = subprocess.Popen(['sleep', '30'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(30)\n")
    eid, done = await host.exec(code=code, timeout_sec=1)
    d = (await done()).body
    assert d["reason"] == "timeout" and d["exit_code"] is None
    assert 1000 <= d["wall_time_ms"] < 6000
    child = int(host.output(eid, "stdout").split()[0])
    await asyncio.sleep(0.2)
    assert not alive(child)


async def test_kill_verb(host):
    eid, done = await host.exec(code="import time; print('up', flush=True); time.sleep(30)", timeout_sec=60)
    await host.wait_for(lambda f: f.type == "exec_output" and f.body["exec_id"] == eid)
    r = await host.request("kill", exec_id=eid)
    assert r.body["ok"] is True and r.body["killed"] is True
    d = (await done()).body
    assert d["reason"] == "killed" and d["exit_code"] is None
    assert host.output(eid, "stdout") == b"up\n"
    assert (await host.request("kill", exec_id=eid)).body["killed"] is False


async def test_spawn_failed(host, monkeypatch):
    monkeypatch.setitem(execs.LINUX_TABLE, "python", lambda s, b, i: ([], ["/nonexistent/interp", s]))
    _, done = await host.exec(code="print(1)")
    d = (await done()).body
    assert d["reason"] == "spawn_failed" and "/nonexistent/interp" in d["detail"] and d["exit_code"] is None


async def test_unsupported_language_and_binary_without_entrypoint(host):
    _, done = await host.exec(language="cobol", code="x")
    assert (await done()).body["reason"] == "spawn_failed"
    _, done = await host.exec(language="binary", code="x")
    assert "entrypoint" in (await done()).body["detail"]


async def test_output_truncation_at_1mib(host):
    eid, done = await host.exec(code="import sys; sys.stdout.write('x' * 3_000_000); sys.stderr.write('e')", timeout_sec=30)
    d = (await done()).body
    assert d["reason"] == "exit" and d["exit_code"] == 0
    assert len(host.output(eid, "stdout")) == 1024 * 1024
    assert d["stdout_truncated"] is True and d["stderr_truncated"] is False
    assert host.output(eid, "stderr") == b"e"


async def test_heartbeats_during_long_exec(host):
    eid, done = await host.exec(code="import time; time.sleep(1.5)", timeout_sec=10)
    before = host.count("heartbeat")
    d = (await done()).body
    assert d["reason"] == "exit"
    ts = [f for f in host.frames if f.type == "heartbeat"]
    assert host.count("heartbeat") - before >= 4  # 0.2 s interval over ~1.5 s
    assert set(ts[0].body) == {"ts", "load", "mem_free_mb"} and ts[0].id is None


async def test_second_exec_rejected_while_running(host):
    eid, done = await host.exec(code="import time; time.sleep(30)", timeout_sec=60)
    r = await host.request("exec", exec_id="other", language="python", code="1")
    assert r.is_error
    await host.request("kill", exec_id=eid)
    await done()


@pytest.mark.skipif(not shutil.which("gcc"), reason="gcc not installed")
async def test_compile_failed_c(host):
    eid, done = await host.exec(language="c", code="int main( { broken", timeout_sec=30)
    d = (await done()).body
    assert d["reason"] == "compile_failed" and d["exit_code"] not in (0, None)
    assert b"error" in host.output(eid, "stderr")
    assert host.output(eid, "stdout") == b""


@pytest.mark.skipif(not shutil.which("gcc"), reason="gcc not installed")
async def test_compile_and_run_c(host):
    eid, done = await host.exec(language="c", code='#include <stdio.h>\nint main(){puts("hi");return 0;}', timeout_sec=30)
    d = (await done()).body
    assert d["reason"] == "exit" and d["exit_code"] == 0 and host.output(eid, "stdout") == b"hi\n"


async def test_screenshot_failure_is_typed(host, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)  # force ImportError
    monkeypatch.setattr(shutil, "which", lambda n: None)
    r = await host.request("screenshot")
    assert r.is_error and r.body["code"] == "SCREENSHOT_FAILED" and "scrot" in r.body["data"]["detail"]
    r = await host.request("screenshot", window_title="nope")
    assert r.is_error and r.body["code"] == "SCREENSHOT_WINDOW_NOT_FOUND"


def test_cli_socket_mode_subprocess(sandbox):
    """python -m atlas_agent connects to a Unix socket as a client and answers hello."""
    import socket
    path = str(sandbox / "cli.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    srv.settimeout(15)
    proc = subprocess.Popen([sys.executable, "-m", "atlas_agent", "--channel", path, "--workdir", str(sandbox / "w")],
                            cwd=AGENT_DIR)
    try:
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(encode_frame(Frame("hello", "h1", {"atlas_version": "x"})))
        fr, got = FrameReader(), None
        while got is None:
            for f in fr.feed(conn.recv(65536)):
                if f.type == "hello.reply":
                    got = f
        assert got.id == "h1" and got.body["agent_version"] == "1.0"
        conn.close()  # EOF -> agent exits in socket mode
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        srv.close()
