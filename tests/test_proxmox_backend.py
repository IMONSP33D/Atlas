"""ProxmoxBackend end to end against the mock PVE API and a real Unix-socket MockGuest."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atlas.backends import proxmox as px
from atlas.backends.base import ExecSpec
from atlas.backends.proxmox import ProxmoxBackend
from atlas.config import ImageEntry, LimitsConfig, ProxmoxConfig, ScreenshotsConfig
from atlas.errors import AtlasError
from atlas.mock.guest import MockGuest
from atlas.mock.proxmox_server import ASGIProxmoxAPI, MockPVE, good_config
from atlas.validation import DecodedFile, NetworkPolicy

NO_NET = NetworkPolicy(False)
COLD = ImageEntry(runtime="linux", id="lin-cold", build="1", vmid=201, default=True, warm=False,
                  languages=["python"])
WARM = ImageEntry(runtime="windows", id="win-warm", build="1", vmid=202, default=True, warm=True,
                  languages=["python"])


@dataclass
class Rig:
    pve: MockPVE
    api: ASGIProxmoxAPI
    guests: dict[int, MockGuest]
    backend: ProxmoxBackend
    sockdir: Path
    config: ProxmoxConfig
    extra: dict = field(default_factory=dict)


@pytest.fixture
async def rig():
    sockdir = Path(tempfile.mkdtemp(prefix="atl"))      # short path: AF_UNIX limit
    pve = MockPVE(task_polls=1)
    pve.add_vm(201, status="stopped", socket_dir=sockdir)
    pve.add_vm(202, status="running", socket_dir=sockdir)
    api = ASGIProxmoxAPI(pve)
    guests = {v: await MockGuest(sockdir / f"{v}.atlas").start() for v in (201, 202)}
    config = ProxmoxConfig(host="mock", token_id=pve.token_id, token_secret=pve.token_secret, node="pve",
                           storage="local", virtio_socket_dir=sockdir)

    async def factory(vmid: int):
        if pve.vms[vmid].status != "running":
            raise ConnectionRefusedError("vm not running")
        return await asyncio.open_unix_connection(str(sockdir / f"{vmid}.atlas"))

    def make(limits: LimitsConfig | None = None, shots: ScreenshotsConfig | None = None, **kw) -> ProxmoxBackend:
        opts = dict(boot_timeout_sec=2, hung_after_sec=0.5, poll_interval_sec=0.01, exec_grace_sec=2,
                    request_timeout_sec=2)
        opts.update(kw)
        return ProxmoxBackend(config, limits or LimitsConfig(),
                              shots or ScreenshotsConfig.model_construct(default_source="auto",
                                                                         guest_timeout_sec=0.3, format="png"),
                              [COLD, WARM], api=api, channel_factory=factory, **opts)

    r = Rig(pve, api, guests, make(), sockdir, config)
    r.extra["make"] = make
    yield r
    for g in guests.values():
        await g.stop()
    api.close()
    shutil.rmtree(sockdir, ignore_errors=True)


def spec(code: str = "", files=(), timeout: int = 5, entrypoint: str | None = None) -> ExecSpec:
    return ExecSpec("python", code if not entrypoint else None, entrypoint, [], {}, None, timeout, list(files))


async def code_of(coro) -> str:
    with pytest.raises(AtlasError) as ei:
        await coro
    return ei.value.code


# --------------------------------------------------------------------------- validation

async def test_validate_good_vm(rig):
    assert await rig.backend.validate_entry(COLD) == []
    assert await rig.backend.validate_entry(WARM) == []


async def test_validate_missing_snapshot(rig):
    rig.pve.vms[201].snapshots.clear()
    probs = await rig.backend.validate_entry(COLD)
    assert len(probs) == 1 and "VM_SNAPSHOT_MISSING" in probs[0] and "golden" in probs[0]


async def test_validate_nic_present(rig):
    rig.pve.vms[201].config["net0"] = "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"
    probs = await rig.backend.validate_entry(COLD)
    assert len(probs) == 1 and "no NIC" in probs[0] and "net0" in probs[0]


async def test_validate_no_serial_port(rig):
    del rig.pve.vms[201].config["args"]
    probs = await rig.backend.validate_entry(COLD)
    assert len(probs) == 1 and "org.atlas.agent" in probs[0]


async def test_validate_wrong_socket_path(rig):
    rig.pve.vms[201].config["args"] = good_config(201, "/elsewhere")["args"]
    probs = await rig.backend.validate_entry(COLD)
    assert len(probs) == 1 and "does not match" in probs[0]


async def test_validate_missing_vm(rig):
    ghost = COLD.model_copy(update={"vmid": 999})
    probs = await rig.backend.validate_entry(ghost)
    assert len(probs) == 1 and "999" in probs[0]


async def test_validate_warm_needs_vmstate_and_running(rig):
    rig.pve.vms[202].snapshots["golden"] = {"vmstate": 0}
    rig.pve.vms[202].status = "stopped"
    probs = await rig.backend.validate_entry(WARM)
    assert any("RAM state" in p for p in probs) and any("warm but VM is stopped" in p for p in probs)


async def test_validate_reports_all_problems_together(rig):
    v = rig.pve.vms[201]
    v.snapshots.clear()
    v.config["net1"] = "e1000"
    del v.config["args"]
    assert len(await rig.backend.validate_entry(COLD)) == 3


async def test_proxmox_auth_and_unreachable_become_problems(rig):
    bad = ProxmoxBackend(rig.config, LimitsConfig(), ScreenshotsConfig(), [COLD],
                         api=ASGIProxmoxAPI(rig.pve, token_secret="wrong"))
    assert "PROXMOX_AUTH" in (await bad.validate_entry(COLD))[0]
    rig.pve.offline = True
    assert "PROXMOX_UNREACHABLE" in (await rig.backend.validate_entry(COLD))[0]
    assert await rig.backend.entry_state(COLD) == "unavailable"


# --------------------------------------------------------------------------- entry_state

async def test_entry_state_warm_cold_unavailable(rig):
    assert await rig.backend.entry_state(WARM) == "warm"
    assert await rig.backend.entry_state(COLD) == "cold"
    rig.pve.vms[201].config["net0"] = "virtio"
    assert await rig.backend.entry_state(COLD) == "unavailable"


async def test_stopped_warm_entry_is_cold_not_unavailable(rig):
    rig.pve.vms[202].status = "stopped"
    assert await rig.backend.entry_state(WARM) == "cold"


# --------------------------------------------------------------------------- open

async def test_open_cold_starts_vm_and_reports_boot_time(rig):
    h = await rig.backend.open(COLD, NO_NET)
    assert h.ref == "201" and h.cold_boot_ms is not None and h.cold_boot_ms >= 0
    assert rig.pve.vms[201].status == "running"
    assert ("POST", "/nodes/pve/qemu/201/status/start") in rig.pve.calls
    assert ("GET", "/nodes/pve/status") in rig.pve.calls     # RAM check ran first


async def test_open_warm_does_not_start(rig):
    h = await rig.backend.open(WARM, NO_NET)
    assert h.cold_boot_ms is None
    assert not any(c[1].endswith("status/start") for c in rig.pve.calls)


async def test_open_with_network_is_refused_before_touching_the_vm(rig):
    code = await code_of(rig.backend.open(COLD, NetworkPolicy(True, ("pypi.org",))))
    assert code == "NETWORK_UNAVAILABLE"
    assert rig.pve.vms[201].status == "stopped" and rig.pve.calls == []


async def test_open_host_resources(rig):
    rig.pve.free_mem_mb = 1024
    with pytest.raises(AtlasError) as ei:
        await rig.backend.open(COLD, NO_NET)
    assert ei.value.code == "HOST_RESOURCES" and ei.value.notice.data["free_mb"] == 1024
    assert rig.pve.vms[201].status == "stopped"


@pytest.mark.parametrize("mode", ["http", "task"])
async def test_open_start_failure(rig, mode):
    rig.pve.fail_start = mode
    with pytest.raises(AtlasError) as ei:
        await rig.backend.open(COLD, NO_NET)
    assert ei.value.code == "VM_START_FAILED" and "mock-injected" in ei.value.notice.data["proxmox_error"]


async def test_open_refuses_vm_with_nic(rig):
    rig.pve.vms[201].config["net0"] = "virtio"
    assert await code_of(rig.backend.open(COLD, NO_NET)) == "VM_UNAVAILABLE"
    assert rig.pve.vms[201].status == "stopped"


async def test_open_missing_snapshot(rig):
    rig.pve.vms[201].snapshots.clear()
    assert await code_of(rig.backend.open(COLD, NO_NET)) == "VM_SNAPSHOT_MISSING"


async def test_open_version_mismatch(rig):
    rig.guests[201].agent_version = "2.0"
    with pytest.raises(AtlasError) as ei:
        await rig.backend.open(COLD, NO_NET)
    assert ei.value.code == "GUEST_AGENT_VERSION" and ei.value.notice.data["agent_version"] == "2.0"
    assert rig.pve.vms[201].status == "stopped"          # VM we started is stopped again


async def test_open_no_heartbeat_is_unreachable(rig):
    g = rig.guests[201]
    g.heartbeats, g.answer_hello = False, False
    b = rig.extra["make"](boot_timeout_sec=0.4)
    assert await code_of(b.open(COLD, NO_NET)) == "GUEST_AGENT_UNREACHABLE"


async def test_open_socket_never_appears_is_unreachable(rig):
    async def refuse(vmid):
        raise ConnectionRefusedError
    b = rig.extra["make"](boot_timeout_sec=0.3)
    b._channel_factory = refuse
    assert await code_of(b.open(WARM, NO_NET)) == "GUEST_AGENT_UNREACHABLE"


async def test_open_twice_is_refused(rig):
    await rig.backend.open(WARM, NO_NET)
    assert await code_of(rig.backend.open(WARM, NO_NET)) == "VM_UNAVAILABLE"


# --------------------------------------------------------------------------- execute

async def test_execute_echo_roundtrip(rig):
    h = await rig.backend.open(WARM, NO_NET)
    r = await rig.backend.execute(h, spec("print('hi')"))
    assert (r.status, r.exit_code, r.stdout, r.errors, r.warnings) == ("ok", 0, "print('hi')\n", [], [])
    assert r.wall_time_ms == 7 and r.cpu_time_ms == 3 and r.peak_memory_bytes == 1 << 20
    sent = [f for f in rig.guests[202].received if f.type == "exec"][0]
    assert sent.body["language"] == "python" and sent.body["timeout_sec"] == 5 and sent.body["exec_id"] == sent.id


async def test_execute_entrypoint(rig):
    h = await rig.backend.open(WARM, NO_NET)
    r = await rig.backend.execute(h, spec(entrypoint="main.py"))
    assert r.stdout.startswith("ran main.py")


async def test_execute_pushes_files_in_chunks(rig, monkeypatch):
    monkeypatch.setattr(px, "CHUNK_BYTES", 4)
    h = await rig.backend.open(WARM, NO_NET)
    data = b"0123456789"
    f = DecodedFile("data.bin", data, hashlib.sha256(data).hexdigest())
    empty = DecodedFile("empty.txt", b"", hashlib.sha256(b"").hexdigest())
    r = await rig.backend.execute(h, spec("#cat data.bin", files=[f, empty]))
    assert r.stdout == "0123456789"
    assert rig.guests[202].files == {"data.bin": data, "empty.txt": b""}
    chunks = [fr for fr in rig.guests[202].received if fr.type == "put_file_chunk"]
    assert [c.body["offset"] for c in chunks] == [0, 4, 8]
    assert base64.b64decode(chunks[-1].body["data_b64"]) == b"89"


async def test_file_transfer_failure(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].fail_file_hash = True
    f = DecodedFile("a.txt", b"x", hashlib.sha256(b"x").hexdigest())
    with pytest.raises(AtlasError) as ei:
        await rig.backend.execute(h, spec("hi", files=[f]))
    assert ei.value.code == "FILE_TRANSFER_FAILED" and ei.value.notice.data["name"] == "a.txt"


@pytest.mark.parametrize("code,status,exit_code,err", [
    ("#exit 3", "error", 3, "EXEC_NONZERO_EXIT"),
    ("#timeout", "timeout", None, "EXEC_TIMEOUT"),
    ("#killed", "killed", None, "EXEC_KILLED_ADMIN"),
    ("#oom", "killed", None, "EXEC_KILLED_OOM"),
    ("#spawn_failed", "error", None, "EXEC_SPAWN_FAILED"),
    ("#compile_failed", "error", 1, "EXEC_COMPILE_FAILED"),
])
async def test_exec_reason_mapping(rig, code, status, exit_code, err):
    h = await rig.backend.open(WARM, NO_NET)
    r = await rig.backend.execute(h, spec(code))
    assert (r.status, r.exit_code) == (status, exit_code)
    assert [e.code for e in r.errors] == [err]
    if err == "EXEC_NONZERO_EXIT":
        assert r.stderr == "exited 3\n" and r.errors[0].data["exit_code"] == 3
    if err == "EXEC_TIMEOUT":
        assert r.stdout == "partial\n"
    if err == "EXEC_COMPILE_FAILED":
        assert "expected" in r.stderr


async def test_output_truncation(rig):
    b = rig.extra["make"](limits=LimitsConfig(max_output_bytes=100_000))
    h = await b.open(WARM, NO_NET)
    r = await b.execute(h, spec("#flood 300000"))
    assert r.status == "ok" and len(r.stdout) == 100_000 and r.stdout_truncated and not r.stderr_truncated
    assert [w.code for w in r.warnings] == ["EXEC_OUTPUT_TRUNCATED"] and r.warnings[0].data["stream"] == "stdout"


async def test_heartbeats_during_exec_are_ignored(rig):
    h = await rig.backend.open(WARM, NO_NET)
    # 0.8 s of silence from the program but heartbeats every 50 ms: longer than hung_after (0.5)
    r = await rig.backend.execute(h, spec("#sleep 0.8\nafter"))
    assert r.status == "ok" and r.stdout == "after\n"


async def test_guest_hung(rig):
    h = await rig.backend.open(WARM, NO_NET)
    assert await code_of(rig.backend.execute(h, spec("#hang"))) == "GUEST_HUNG"
    assert await code_of(rig.backend.execute(h, spec("again"))) == "GUEST_HUNG"      # handle is dead


async def test_exec_outliving_timeout_plus_grace_is_hung(rig):
    b = rig.extra["make"](exec_grace_sec=0.3)
    h = await b.open(WARM, NO_NET)
    assert await code_of(b.execute(h, spec("#sleep 3\nx", timeout=1))) == "GUEST_HUNG"


async def test_guest_eof_is_crash(rig):
    h = await rig.backend.open(WARM, NO_NET)
    assert await code_of(rig.backend.execute(h, spec("#crash"))) == "GUEST_CRASHED"
    assert await code_of(rig.backend.execute(h, spec("again"))) == "GUEST_CRASHED"


async def test_malformed_frame(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].corrupt_next_frame = True
    assert await code_of(rig.backend.execute(h, spec("#sleep 0.3\nx"))) == "GUEST_AGENT_PROTOCOL"


async def test_exec_refused_by_guest_is_protocol_error(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].exec_running = True            # guest answers `busy` error frame
    assert await code_of(rig.backend.execute(h, spec("x"))) == "GUEST_AGENT_PROTOCOL"


async def test_execute_on_unknown_handle(rig):
    h = await rig.backend.open(WARM, NO_NET)
    await rig.backend.close(h)
    assert await code_of(rig.backend.execute(h, spec("x"))) == "SESSION_NOT_FOUND"


# --------------------------------------------------------------------------- close

async def test_close_warm_rolls_back_and_restarts(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.pve.vms[202].snapshots["golden"] = {"vmstate": 0}       # rollback will leave it stopped
    assert await rig.backend.close(h) is True
    assert rig.pve.rollback_count == 1 and rig.pve.vms[202].status == "running"
    assert rig.guests[202].prepare_reverts == 1
    assert ("POST", "/nodes/pve/qemu/202/snapshot/golden/rollback") in rig.pve.calls
    assert any(c[0] == "GET" and "/tasks/" in c[1] for c in rig.pve.calls)   # polled the UPID


async def test_close_warm_with_vmstate_stays_running_without_extra_start(rig):
    h = await rig.backend.open(WARM, NO_NET)
    await rig.backend.close(h)
    assert rig.pve.vms[202].status == "running"
    assert not any(c[1].endswith("status/start") for c in rig.pve.calls)


async def test_close_cold_leaves_vm_stopped(rig):
    h = await rig.backend.open(COLD, NO_NET)
    assert await rig.backend.close(h) is True
    assert rig.pve.rollback_count == 1 and rig.pve.vms[201].status == "stopped"


async def test_close_cold_with_vmstate_snapshot_is_stopped_afterwards(rig):
    rig.pve.vms[201].snapshots["golden"] = {"vmstate": 1}       # rollback resumes it running
    h = await rig.backend.open(COLD, NO_NET)
    await rig.backend.close(h)
    assert rig.pve.vms[201].status == "stopped"


async def test_close_after_hung_guest_skips_prepare_revert(rig):
    h = await rig.backend.open(WARM, NO_NET)
    await code_of(rig.backend.execute(h, spec("#hang")))
    assert await rig.backend.close(h) is True and rig.pve.rollback_count == 1


@pytest.mark.parametrize("mode", ["http", "task"])
async def test_rollback_failure_marks_entry_unavailable(rig, mode):
    h = await rig.backend.open(WARM, NO_NET)
    rig.pve.fail_rollback = mode
    with pytest.raises(AtlasError) as ei:
        await rig.backend.close(h)
    assert ei.value.code == "VM_ROLLBACK_FAILED" and "mock-injected" in ei.value.notice.data["proxmox_error"]
    assert await rig.backend.entry_state(WARM) == "unavailable"
    assert any("VM_ROLLBACK_FAILED" in p for p in await rig.backend.validate_entry(WARM))
    assert await code_of(rig.backend.open(WARM, NO_NET)) == "VM_UNAVAILABLE"
    rig.pve.fail_rollback = None
    rig.backend.clear_failed(202)
    assert await rig.backend.entry_state(WARM) == "warm"


# --------------------------------------------------------------------------- screenshots

def _png(w: int, h: int, noisy: bool = False) -> bytes:
    from PIL import Image
    im = Image.new("RGB", (w, h), (10, 20, 30))
    if noisy:
        import os
        im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


async def test_screenshot_guest(rig):
    h = await rig.backend.open(WARM, NO_NET)
    s = await rig.backend.screenshot(h, "guest", "Calc", [0, 0, 1, 1])
    assert (s.source, s.format, s.warnings) == ("guest", "png", [])
    assert base64.b64decode(s.content_b64).startswith(b"\x89PNG")
    req = [f for f in rig.guests[202].received if f.type == "screenshot"][0]
    assert req.body == {"window_title": "Calc", "region": [0, 0, 1, 1]}


async def test_screenshot_auto_uses_guest_when_healthy(rig):
    h = await rig.backend.open(WARM, NO_NET)
    assert (await rig.backend.screenshot(h, "auto", None, None)).source == "guest"


@pytest.mark.parametrize("mode", ["fail", "hang"])
async def test_screenshot_auto_falls_back_to_hypervisor(rig, mode):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].screenshot_mode = mode
    s = await rig.backend.screenshot(h, "auto", None, None)
    assert s.source == "hypervisor" and s.format == "png" and (s.width, s.height) == (2, 2)
    assert [w.code for w in s.warnings] == ["SCREENSHOT_GUEST_FALLBACK"]
    assert ("POST", "/nodes/pve/qemu/202/monitor") in rig.pve.calls


async def test_screenshot_fallback_when_guest_is_dead(rig):
    h = await rig.backend.open(WARM, NO_NET)
    await code_of(rig.backend.execute(h, spec("#crash")))
    s = await rig.backend.screenshot(h, "auto", None, None)
    assert s.source == "hypervisor" and s.warnings[0].code == "SCREENSHOT_GUEST_FALLBACK"


async def test_screenshot_hypervisor_ppm_passthrough_without_pillow(rig, monkeypatch):
    monkeypatch.setattr(px, "_pil", lambda: None)
    h = await rig.backend.open(WARM, NO_NET)
    s = await rig.backend.screenshot(h, "hypervisor", None, None)
    assert s.format == "ppm" and s.warnings == [] and base64.b64decode(s.content_b64).startswith(b"P6")


async def test_screenshot_hypervisor_reads_file_when_api_returns_text(rig):
    rig.pve.monitor_mode = "file"
    h = await rig.backend.open(WARM, NO_NET)
    s = await rig.backend.screenshot(h, "hypervisor", None, None)
    assert s.source == "hypervisor" and s.format == "png"


async def test_screenshot_both_fail(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].screenshot_mode = "fail"
    rig.pve.fail_monitor = "http"
    with pytest.raises(AtlasError) as ei:
        await rig.backend.screenshot(h, "auto", None, None)
    d = ei.value.notice.data
    assert ei.value.code == "SCREENSHOT_FAILED" and "capture failed" in d["guest_error"] and "mock-injected" in d["hypervisor_error"]


async def test_screenshot_explicit_source_failures(rig):
    h = await rig.backend.open(WARM, NO_NET)
    rig.guests[202].screenshot_mode = "fail"
    with pytest.raises(AtlasError) as ei:
        await rig.backend.screenshot(h, "guest", None, None)
    assert ei.value.code == "SCREENSHOT_FAILED" and "guest_error" in ei.value.notice.data
    rig.pve.fail_monitor = "http"
    with pytest.raises(AtlasError) as ei:
        await rig.backend.screenshot(h, "hypervisor", None, None)
    assert "hypervisor_error" in ei.value.notice.data


async def test_screenshot_window_not_found_does_not_fall_back(rig):
    h = await rig.backend.open(WARM, NO_NET)
    assert await code_of(rig.backend.screenshot(h, "auto", "nope", None)) == "SCREENSHOT_WINDOW_NOT_FOUND"
    assert not any(c[1].endswith("/monitor") for c in rig.pve.calls)


async def test_screenshot_downscaled_when_over_cap(rig):
    big = _png(400, 400, noisy=True)
    rig.guests[202].screenshot_png, rig.guests[202].screenshot_size = big, (400, 400)
    b = rig.extra["make"](limits=LimitsConfig(max_screenshot_bytes=len(big) // 4))
    h = await b.open(WARM, NO_NET)
    s = await b.screenshot(h, "guest", None, None)
    assert [w.code for w in s.warnings] == ["SCREENSHOT_DOWNSCALED"]
    assert s.warnings[0].data["original_size"] == {"width": 400, "height": 400, "bytes": len(big)}
    assert len(base64.b64decode(s.content_b64)) <= len(big) // 4 and s.width < 400


async def test_screenshot_over_cap_without_pillow_fails_guest_then_falls_back(rig, monkeypatch):
    big = _png(200, 200, noisy=True)
    rig.guests[202].screenshot_png = big
    b = rig.extra["make"](limits=LimitsConfig(max_screenshot_bytes=len(big) - 1))
    h = await b.open(WARM, NO_NET)
    monkeypatch.setattr(px, "_pil", lambda: None)
    rig.pve.screendump_bytes = b"P6\n1 1\n255\n\x00\x00\x00"
    s = await b.screenshot(h, "auto", None, None)
    assert s.source == "hypervisor" and s.warnings[0].code == "SCREENSHOT_GUEST_FALLBACK"


# --------------------------------------------------------------------------- adapter

async def test_adapter_maps_auth_and_connection_errors(rig):
    bad = ProxmoxBackend(rig.config, LimitsConfig(), ScreenshotsConfig(), [COLD],
                         api=ASGIProxmoxAPI(rig.pve, token_secret="wrong"))
    assert await code_of(bad.pve.get("nodes/pve/status")) == "PROXMOX_AUTH"
    rig.pve.offline = True
    assert await code_of(rig.backend.pve.get("nodes/pve/status")) == "PROXMOX_UNREACHABLE"


async def test_backend_only_uses_documented_endpoints(rig):
    h = await rig.backend.open(COLD, NO_NET)
    await rig.backend.execute(h, spec("x"))
    await rig.backend.screenshot(h, "hypervisor", None, None)
    await rig.backend.close(h)
    allowed = {
        ("GET", "/nodes/pve/status"), ("GET", "/nodes/pve/qemu/201/status/current"),
        ("GET", "/nodes/pve/qemu/201/config"), ("GET", "/nodes/pve/qemu/201/snapshot"),
        ("POST", "/nodes/pve/qemu/201/status/start"), ("POST", "/nodes/pve/qemu/201/status/stop"),
        ("POST", "/nodes/pve/qemu/201/snapshot/golden/rollback"), ("POST", "/nodes/pve/qemu/201/monitor"),
    }
    for m, p in rig.pve.calls:
        assert (m, p) in allowed or (m == "GET" and "/tasks/" in p and p.endswith("/status")), (m, p)


# --------------------------------------------------------------------------- CLI

def test_cli_images_validate_all_good(example_configs, capsys):
    from atlas import cli

    pve = MockPVE()
    for vmid in (201, 202, 301):
        pve.add_vm(vmid, status="running")
    cfg_path, img_path = example_configs
    cfg = cli.load_atlas_config(cfg_path)
    entries = [e for e in cli.load_image_catalog(img_path).images if e.is_vm]
    api = ASGIProxmoxAPI(pve)
    try:
        backend = ProxmoxBackend(cfg.proxmox, cfg.limits, cfg.screenshots, entries, api=api)
        assert cli.cmd_images_validate(argparse.Namespace(config=cfg_path, images=img_path), backend) == 0
        assert "3/3 entries ready" in capsys.readouterr().out
        del pve.vms[202].config["args"]
        pve.vms[301].config["net0"] = "virtio"
        assert cli.cmd_images_validate(argparse.Namespace(config=cfg_path, images=img_path), backend) == 1
        out = capsys.readouterr().out
        assert "1/3 entries ready" in out and "no NIC" in out and "org.atlas.agent" in out
    finally:
        api.close()
