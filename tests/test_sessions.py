"""Class D rules, driven through the HTTP layer with an injected clock."""
from __future__ import annotations

import asyncio

import pytest


def ex(**over):
    body = {"runtime": "linux", "version": "ubuntu-24.04", "language": "bash", "code": "true", "request_id": "r"}
    body.update(over)
    return body


def code(resp) -> str:
    return resp.json()["errors"][0]["code"]


async def open_session(client, auth, runtime="linux", **over):
    body = {"runtime": runtime, **over}
    r = await client.post("/v1/sessions", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def test_open_and_status(client, auth, clock):
    s = await open_session(client, auth)
    assert s["state"] == "active" and s["session"]["executions"] == 0
    assert s["session"]["started_at"] == "2026-11-01T18:00:00Z"
    assert [w["code"] for w in s["warnings"]] == ["VERSION_DEFAULTED"]
    clock.advance(120)
    r = await client.get(f"/v1/sessions/{s['session_id']}", headers=auth)
    b = r.json()
    assert b["session"]["idle_seconds"] == 120 and b["session"]["idle_revert_in_seconds"] == 780
    assert b["warnings"] == []


async def test_cold_start_warning(client, auth, backend):
    s = await open_session(client, auth, runtime="windows", version="win10-22h2")
    assert [w["code"] for w in s["warnings"]] == ["COLD_START"]


async def test_session_limit(client, auth):
    await open_session(client, auth, runtime="windows", version="win11-24h2")
    await open_session(client, auth, runtime="windows", version="win10-22h2")
    r = await client.post("/v1/sessions", json={"runtime": "windows", "version": "win11-24h2"}, headers=auth)
    # third session: limit (2) wins over "entry busy"
    assert r.status_code == 409 and code(r) == "SESSION_LIMIT"
    assert len(r.json()["errors"][0]["data"]["active"]) == 2
    assert r.json()["errors"][0]["suggested_action"] == "retry_later"


async def test_runtime_exclusive(client, auth):
    s = await open_session(client, auth, runtime="windows")
    r = await client.post("/v1/sessions", json={"runtime": "linux"}, headers=auth)
    assert r.status_code == 409 and code(r) == "RUNTIME_EXCLUSIVE"
    assert r.json()["errors"][0]["data"]["blocking_session"] == s["session_id"]
    # one-shot VM execute is also blocked, since it needs a session internally
    r = await client.post("/v1/execute", json=ex(), headers=auth)
    assert r.status_code == 409 and code(r) == "RUNTIME_EXCLUSIVE"
    # ephemeral is unaffected
    r = await client.post("/v1/execute", json=ex(runtime="ephemeral", version=None), headers=auth)
    assert r.status_code == 200


async def test_same_entry_held(client, auth):
    await open_session(client, auth, runtime="windows", version="win11-24h2")
    r = await client.post("/v1/sessions", json={"runtime": "windows", "version": "win11-24h2"}, headers=auth)
    assert r.status_code == 503 and code(r) == "VM_UNAVAILABLE"
    assert r.json()["errors"][0]["data"]["reason"] == "busy"


async def test_session_not_found_reasons(client, auth):
    r = await client.get("/v1/sessions/ses_01JBX0000000000000000000AB", headers=auth)
    assert r.status_code == 404 and code(r) == "SESSION_NOT_FOUND"
    assert r.json()["errors"][0]["data"]["reason"] == "never_existed"

    s = await open_session(client, auth)
    sid = s["session_id"]
    r = await client.delete(f"/v1/sessions/{sid}", headers=auth)
    assert r.json() == {"session_id": sid, "state": "closed", "reverted": True}
    r = await client.delete(f"/v1/sessions/{sid}", headers=auth)        # idempotent
    assert r.status_code == 200 and r.json()["state"] == "closed"
    r = await client.get(f"/v1/sessions/{sid}", headers=auth)
    assert code(r) == "SESSION_NOT_FOUND" and r.json()["errors"][0]["data"]["reason"] == "closed"


async def test_idle_warning_then_revert(client, auth, clock, backend):
    s = await open_session(client, auth)
    sid = s["session_id"]
    clock.advance(600)
    r = await client.get(f"/v1/sessions/{sid}", headers=auth)
    w = r.json()["warnings"]
    assert w[0]["code"] == "SESSION_IDLE_WARNING" and w[0]["data"]["seconds_remaining"] == 300

    # activity after a warning emits SESSION_IDLE_RESET and resets the timer
    r = await client.post("/v1/execute", json=ex(session_id=sid), headers=auth)
    assert r.status_code == 200
    assert "SESSION_IDLE_RESET" in [x["code"] for x in r.json()["warnings"]]
    assert r.json()["session"]["idle_revert_in_seconds"] == 900

    clock.advance(900)
    r = await client.post("/v1/execute", json=ex(session_id=sid), headers=auth)
    assert r.status_code == 409 and code(r) == "SESSION_IDLE_REVERTED"
    assert r.json()["errors"][0]["suggested_action"] == "new_session"
    assert backend.closed[-1].entry.id == "ubuntu-24.04"
    # second lookup gives the same answer
    r = await client.get(f"/v1/sessions/{sid}", headers=auth)
    assert code(r) == "SESSION_IDLE_REVERTED"


async def test_session_busy(client, auth):
    s = await open_session(client, auth)
    sid = s["session_id"]
    slow = client.post("/v1/execute", json=ex(session_id=sid, code="#sleep 0.3\necho"), headers=auth)
    t = asyncio.create_task(slow)
    await asyncio.sleep(0.05)
    r = await client.post("/v1/execute", json=ex(session_id=sid), headers=auth)
    assert r.status_code == 409 and code(r) == "SESSION_BUSY"
    assert (await t).status_code == 200


async def test_runtime_and_network_mismatch(client, auth):
    s = await open_session(client, auth, network=True)
    sid = s["session_id"]
    r = await client.post("/v1/execute", json=ex(session_id=sid, runtime="windows", version="win11-24h2",
                                                 language="powershell", network=True), headers=auth)
    assert r.status_code == 409 and code(r) == "SESSION_RUNTIME_MISMATCH"
    r = await client.post("/v1/execute", json=ex(session_id=sid, network=False), headers=auth)
    assert r.status_code == 409 and code(r) == "SESSION_NETWORK_MISMATCH"
    r = await client.post("/v1/execute", json=ex(session_id=sid, network=True), headers=auth)
    assert r.status_code == 200


async def test_screenshot_endpoint(client, auth):
    s = await open_session(client, auth, runtime="windows")
    sid = s["session_id"]
    r = await client.post(f"/v1/sessions/{sid}/screenshot", json={"source": "auto"}, headers=auth)
    assert r.status_code == 200 and r.json()["format"] == "png"
    r = await client.post(f"/v1/sessions/{sid}/screenshot", json={"source": "guest", "window_title": "nope"}, headers=auth)
    assert r.status_code == 400 and code(r) == "SCREENSHOT_WINDOW_NOT_FOUND"
    r = await client.post(f"/v1/sessions/{sid}/screenshot", json={"source": "hypervisor", "window_title": "x"}, headers=auth)
    assert code(r) == "SCHEMA_INVALID"
    r = await client.get(f"/v1/sessions/{sid}", headers=auth)
    assert r.json()["session"]["executions"] == 0


async def test_shutdown_kills_sessions(app, client, auth, backend):
    await open_session(client, auth)
    await open_session(client, auth, runtime="linux", version="ubuntu-24.04") if False else None
    mgr = app.state.atlas.sessions
    await mgr.shutdown()
    assert mgr.sessions == {} and len(backend.closed) == 1
    assert all(v == "atlas_restarted" for v in mgr.tombstones.values())
    r = await client.get("/v1/images", headers=auth)
    assert r.status_code == 503 and code(r) == "SHUTTING_DOWN"
