"""The mock PVE API and MockGuest, tested directly (httpx ASGITransport + real Unix socket)."""
from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from atlas.guest.protocol import Frame, read_frame, write_frame
from atlas.mock.guest import MockGuest
from atlas.mock.proxmox_server import MockPVE, create_app

B = "/api2/json/nodes/pve"


@pytest.fixture
async def env():
    pve = MockPVE(task_polls=1)
    pve.add_vm(201, status="stopped")
    app = create_app(pve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=pve.auth_header) as c:
        yield pve, c


async def test_auth_required(env):
    pve, _ = env
    app = create_app(pve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as anon:
        assert (await anon.get(f"{B}/qemu/201/status/current")).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "PVEAPIToken=atlas@pve!atlas=nope"}) as bad:
        assert (await bad.get(f"{B}/status")).status_code == 401


async def test_status_config_snapshots_node(env):
    pve, c = env
    assert (await c.get(f"{B}/qemu/201/status/current")).json()["data"]["status"] == "stopped"
    cfg = (await c.get(f"{B}/qemu/201/config")).json()["data"]
    assert "org.atlas.agent" in cfg["args"] and not any(k.startswith("net") for k in cfg)
    snaps = (await c.get(f"{B}/qemu/201/snapshot")).json()["data"]
    assert [s["name"] for s in snaps] == ["golden", "current"] and snaps[0]["vmstate"] == 1
    mem = (await c.get(f"{B}/status")).json()["data"]["memory"]
    assert mem["free"] == 16384 * 1024 * 1024


async def test_missing_vm_and_node_are_500_like_pve(env):
    _, c = env
    r = await c.get(f"{B}/qemu/999/status/current")
    assert r.status_code == 500 and "does not exist" in r.json()["message"]
    assert (await c.get("/api2/json/nodes/other/status")).status_code == 500


async def test_start_stop_and_task_polling(env):
    pve, c = env
    upid = (await c.post(f"{B}/qemu/201/status/start")).json()["data"]
    assert upid.startswith("UPID:pve:") and pve.vms[201].status == "running"
    first = (await c.get(f"{B}/tasks/{upid}/status")).json()["data"]
    second = (await c.get(f"{B}/tasks/{upid}/status")).json()["data"]
    assert first["status"] == "running"
    assert second["status"] == "stopped" and second["exitstatus"] == "OK"
    assert (await c.post(f"{B}/qemu/201/status/start")).status_code == 500      # already running
    await c.post(f"{B}/qemu/201/status/stop")
    assert pve.vms[201].status == "stopped"


async def test_rollback_semantics(env):
    pve, c = env
    pve.vms[201].status = "running"
    upid = (await c.post(f"{B}/qemu/201/snapshot/golden/rollback")).json()["data"]
    assert upid.startswith("UPID:") and pve.vms[201].status == "running"      # vmstate -> running
    pve.vms[201].snapshots["golden"]["vmstate"] = 0
    await c.post(f"{B}/qemu/201/snapshot/golden/rollback")
    assert pve.vms[201].status == "stopped" and pve.rollback_count == 2
    assert (await c.post(f"{B}/qemu/201/snapshot/nope/rollback")).status_code == 500


async def test_failure_injection(env):
    pve, c = env
    pve.fail_start = "http"
    assert (await c.post(f"{B}/qemu/201/status/start")).status_code == 500
    pve.fail_start = "task"
    upid = (await c.post(f"{B}/qemu/201/status/start")).json()["data"]
    for _ in range(2):
        st = (await c.get(f"{B}/tasks/{upid}/status")).json()["data"]
    assert st["status"] == "stopped" and "mock-injected" in st["exitstatus"]
    assert pve.vms[201].status == "stopped"
    pve.fail_rollback = "http"
    assert (await c.post(f"{B}/qemu/201/snapshot/golden/rollback")).status_code == 500


async def test_monitor_screendump_writes_file_and_returns_image(env):
    pve, c = env
    pve.vms[201].status = "running"
    d = Path(tempfile.mkdtemp(prefix="atl"))
    out = d / "s.ppm"
    try:
        r = await c.post(f"{B}/qemu/201/monitor", data={"command": f"screendump {out}"})
        assert base64.b64decode(r.json()["data"]) == pve.screendump_bytes == out.read_bytes()
    finally:
        shutil.rmtree(d)
    pve.vms[201].status = "stopped"
    assert (await c.post(f"{B}/qemu/201/monitor", data={"command": "screendump /x"})).status_code == 500


async def test_calls_are_logged(env):
    pve, c = env
    await c.get(f"{B}/status")
    assert pve.calls == [("GET", "/nodes/pve/status")]


# --------------------------------------------------------------------------- MockGuest

async def _talk(sock: Path, reqs: list[Frame]) -> list[Frame]:
    r, w = await asyncio.open_unix_connection(str(sock))
    for q in reqs:
        await write_frame(w, q)
    got: list[Frame] = []

    async def pull() -> None:
        while True:
            got.append(await read_frame(r))

    try:
        await asyncio.wait_for(pull(), 0.5)      # heartbeats never stop; collect for a fixed window
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    w.close()
    return got


async def test_mock_guest_protocol():
    d = Path(tempfile.mkdtemp(prefix="atl"))
    try:
        async with MockGuest(d / "1.atlas", heartbeat_interval=0.1) as g:
            hello = Frame.request("hello", "h", atlas_version="1.0")
            ex = Frame.request("exec", "e", exec_id="x1", language="python", code="#exit 2")
            frames = await _talk(g.path, [hello, ex])
            types = [f.type for f in frames if f.type != "heartbeat"]
            assert types == ["hello.reply", "exec.reply", "exec_output", "exec_done"]
            assert any(f.type == "heartbeat" for f in frames)
            done = next(f for f in frames if f.type == "exec_done").body
            assert done["reason"] == "exit" and done["exit_code"] == 2 and done["stdout_truncated"] is False
            assert frames[0].body["agent_version"] == "1.0"
            bad = await _talk(g.path, [Frame.request("exec", "b", language="python")])
            assert bad[0].is_error and bad[0].id == "b"
    finally:
        shutil.rmtree(d)


async def test_mock_guest_screenshot_and_no_get_file():
    d = Path(tempfile.mkdtemp(prefix="atl"))
    try:
        async with MockGuest(d / "1.atlas") as g:
            frames = await _talk(g.path, [Frame.request("screenshot", "s", window_title=None, region=None)])
            shot = next(f for f in frames if f.id == "s")
            assert shot.body["format"] == "png" and base64.b64decode(shot.body["data_b64"]).startswith(b"\x89PNG")
            with pytest.raises(ValueError):
                Frame.request("get_file", "x")
    finally:
        shutil.rmtree(d)
