"""Contract tests: Perseus-shaped requests in, exact envelopes out."""
from __future__ import annotations

import base64
import hashlib

import pytest

from atlas.api import create_app
from atlas.config import ConfigError


def b64(s: bytes) -> str:
    return base64.b64encode(s).decode()


def execute(**over):
    body = {"runtime": "ephemeral", "language": "python", "code": "print('hi')", "request_id": "perseus-1"}
    body.update(over)
    return body


def err_code(resp) -> str:
    assert resp.json()["errors"], resp.text
    return resp.json()["errors"][0]["code"]


# --------------------------------------------------------------------------- health / auth

async def test_health_unauthenticated(client):
    r = await client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_auth_missing(client):
    r = await client.get("/v1/images")
    assert r.status_code == 401 and err_code(r) == "AUTH_MISSING"
    assert r.json()["errors"][0]["suggested_action"] == "ask_human"


async def test_auth_invalid(client):
    r = await client.get("/v1/images", headers={"Authorization": "Bearer wrong-wrong-wrong-wrong-wrong"})
    assert r.status_code == 401 and err_code(r) == "AUTH_INVALID"


async def test_auth_revoked(cfg, catalog, backend, clock, auth):
    cfg.auth.keys[0].revoked = True
    from httpx import ASGITransport, AsyncClient
    app = create_app(cfg, catalog, {rt: backend for rt in backend.runtimes}, clock=clock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://a") as c:
        r = await c.get("/v1/images", headers=auth)
    assert r.status_code == 403 and err_code(r) == "AUTH_REVOKED"


async def test_rate_limited(client, auth, cfg, clock):
    cfg.auth.keys[0].rate_limit_per_min = 3
    for _ in range(3):
        assert (await client.get("/v1/images", headers=auth)).status_code == 200
    r = await client.get("/v1/images", headers=auth)
    assert r.status_code == 429 and err_code(r) == "RATE_LIMITED"
    assert r.json()["errors"][0]["data"]["retry_after_sec"] >= 1
    clock.advance(60)
    assert (await client.get("/v1/images", headers=auth)).status_code == 200


# --------------------------------------------------------------------------- images

async def test_images_catalog(client, auth):
    r = await client.get("/v1/images", headers=auth)
    body = r.json()
    by_id = {i["id"]: i for i in body["images"]}
    assert by_id["ephemeral"]["state"] == "ready" and by_id["ephemeral"]["default"] is True
    assert by_id["win11-24h2"]["state"] == "warm"
    assert by_id["win10-22h2"]["state"] == "cold" and "default" not in by_id["win10-22h2"]
    assert body["limits"] == {"max_timeout_sec": 900, "session_idle_sec": 900, "session_idle_warn_sec": 600,
                              "max_sessions": 2, "max_upload_bytes": 104857600, "max_files": 25}


async def test_images_busy_state(client, auth):
    r = await client.post("/v1/sessions", json={"runtime": "windows"}, headers=auth)
    assert r.status_code == 201
    r = await client.get("/v1/images", headers=auth)
    assert {i["id"]: i["state"] for i in r.json()["images"]}["win11-24h2"] == "busy"


# --------------------------------------------------------------------------- execute: happy path

async def test_execute_ephemeral_one_shot(client, auth, backend):
    r = await client.post("/v1/execute", json=execute(), headers=auth)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "ok" and b["exit_code"] == 0 and b["stdout"] == "print('hi')\n"
    assert b["request_id"] == "perseus-1"
    assert b["runtime"] == "ephemeral" and b["build"] == "atlas-ephemeral:2026.11.1"
    assert b["session"] is None and b["session_id"] is None
    assert b["network"] == {"requested": False, "effective": False, "allow": []}
    assert b["errors"] == [] and b["warnings"] == []
    assert len(backend.opened) == 1 and len(backend.closed) == 1   # container torn down


async def test_execute_nonzero_exit_is_http_200(client, auth):
    r = await client.post("/v1/execute", json=execute(code="#exit 3"), headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "error" and b["exit_code"] == 3
    assert b["errors"][0]["code"] == "EXEC_NONZERO_EXIT" and b["errors"][0]["suggested_action"] == "none"


async def test_execute_timeout_status(client, auth):
    r = await client.post("/v1/execute", json=execute(code="#timeout", timeout_sec=5), headers=auth)
    b = r.json()
    assert r.status_code == 200 and b["status"] == "timeout" and b["stdout"] == "partial\n"
    assert b["errors"][0]["code"] == "EXEC_TIMEOUT"


async def test_execute_vm_one_shot_reverts(client, auth, backend):
    r = await client.post("/v1/execute", json=execute(runtime="windows", language="powershell",
                                                       code="Get-Date"), headers=auth)
    b = r.json()
    assert r.status_code == 200 and b["version"] == "win11-24h2" and b["build"] == "10.0.26100.2314"
    assert b["session_id"] is None
    assert [w["code"] for w in b["warnings"]] == ["VERSION_DEFAULTED"]
    assert backend.closed and backend.closed[-1].entry.id == "win11-24h2"
    r = await client.get("/v1/images", headers=auth)
    assert {i["id"]: i["state"] for i in r.json()["images"]}["win11-24h2"] == "warm"


async def test_execute_session_mode_opens_and_reuses(client, auth, backend):
    r = await client.post("/v1/execute", json=execute(runtime="linux", version="ubuntu-24.04", language="bash",
                                                       mode="session", code="echo 1"), headers=auth)
    b = r.json()
    assert r.status_code == 200, r.text
    sid = b["session_id"]
    assert sid.startswith("ses_") and b["session"]["executions"] == 1
    assert b["session"]["idle_revert_in_seconds"] == 900

    r = await client.post("/v1/execute", json=execute(runtime="linux", version="ubuntu-24.04", language="bash",
                                                       session_id=sid, code="echo 2"), headers=auth)
    b = r.json()
    assert b["session_id"] == sid and b["session"]["executions"] == 2
    assert len(backend.opened) == 1 and backend.closed == []


async def test_execute_files_and_entrypoint(client, auth):
    data = b"hello"
    files = [{"name": "run.sh", "content_b64": b64(data), "sha256": hashlib.sha256(data).hexdigest()}]
    r = await client.post("/v1/execute", json=execute(code=None, entrypoint="run.sh", language="bash",
                                                       args=["--x"], files=files), headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["stdout"] == "ran run.sh --x\n"


async def test_execute_screenshot_on_complete(client, auth):
    r = await client.post("/v1/execute", json=execute(runtime="windows", language="python",
                                                       screenshot={"on_complete": True}), headers=auth)
    b = r.json()
    assert r.status_code == 200
    assert b["screenshot"]["format"] == "png" and b["screenshot"]["source"] == "guest"


# --------------------------------------------------------------------------- execute: validation

@pytest.mark.parametrize("over,code,http", [
    ({"runtime": "solaris"}, "RUNTIME_UNKNOWN", 400),
    ({"runtime": "windows", "version": "win7"}, "VERSION_UNKNOWN", 400),
    ({"language": "cmd"}, "LANGUAGE_UNSUPPORTED", 400),            # cmd not in ephemeral
    ({"code": "x", "entrypoint": "a.py"}, "CODE_AND_ENTRYPOINT", 400),
    ({"code": None}, "CODE_MISSING", 400),
    ({"code": None, "entrypoint": "nope.py"}, "ENTRYPOINT_NOT_FOUND", 400),
    ({"timeout_sec": 0}, "TIMEOUT_OUT_OF_RANGE", 400),
    ({"timeout_sec": 901}, "TIMEOUT_OUT_OF_RANGE", 400),
    ({"mode": "interactive"}, "MODE_INVALID", 400),
    ({"mode": "session"}, "MODE_INVALID", 400),                      # session on ephemeral
    ({"runtime": "cape", "language": "binary", "code": None, "entrypoint": "x"}, "MODE_INVALID", 400),
    ({"session_id": "abc"}, "SESSION_ID_INVALID", 400),
    ({"network": {"allow": ["not a host!"]}}, "NETWORK_SPEC_INVALID", 400),
    ({"network": {"deny": []}}, "NETWORK_SPEC_INVALID", 400),
    ({"network": "yes"}, "SCHEMA_INVALID", 400),
    ({"screenshot": {"on_complete": True}}, "SCREENSHOT_UNSUPPORTED", 400),
    ({"bogus_field": 1}, "SCHEMA_INVALID", 400),
    ({"timeout_sec": "sixty"}, "SCHEMA_INVALID", 400),
    ({"files": [{"name": "../etc/passwd", "content_b64": "aGk="}]}, "FILE_NAME_INVALID", 400),
    ({"files": [{"name": "C:\\x.exe", "content_b64": "aGk="}]}, "FILE_NAME_INVALID", 400),
    ({"files": [{"name": "CON", "content_b64": "aGk="}]}, "FILE_NAME_INVALID", 400),
    ({"files": [{"name": "a", "content_b64": "aGk="}, {"name": "a", "content_b64": "aGk="}]}, "FILE_NAME_DUPLICATE", 400),
    ({"files": [{"name": "a", "content_b64": "%%%"}]}, "FILE_B64_INVALID", 400),
    ({"files": [{"name": "a", "content_b64": "aGk=", "sha256": "00"}]}, "FILE_HASH_MISMATCH", 400),
    ({"files": [{"name": f"f{i}", "content_b64": "aGk="} for i in range(26)]}, "FILE_COUNT_EXCEEDED", 400),
])
async def test_execute_validation(client, auth, over, code, http):
    r = await client.post("/v1/execute", json=execute(**over), headers=auth)
    assert r.status_code == http, r.text
    b = r.json()
    assert err_code(r) == code
    assert b["status"] == "error" and b["request_id"] == "perseus-1" and "stdout" not in b
    assert b["errors"][0]["suggested_action"] == "fix_request"


async def test_version_unknown_lists_available(client, auth):
    r = await client.post("/v1/execute", json=execute(runtime="windows", version="win7"), headers=auth)
    assert r.json()["errors"][0]["data"]["available"] == ["win11-24h2", "win10-22h2"]


async def test_file_too_large_is_413(client, auth, cfg):
    cfg.limits.max_upload_bytes = 10
    r = await client.post("/v1/execute", json=execute(files=[{"name": "big", "content_b64": b64(b"x" * 11)}]),
                          headers=auth)
    assert r.status_code == 413 and err_code(r) == "FILE_TOO_LARGE"
    assert r.json()["errors"][0]["data"] == {"bytes": 11, "limit": 10}


async def test_bad_json(client, auth):
    r = await client.post("/v1/execute", content=b"{not json", headers={**auth, "content-type": "application/json"})
    assert r.status_code == 400 and err_code(r) == "BAD_JSON"


async def test_network_allowlist_echoed(client, auth):
    r = await client.post("/v1/execute", json=execute(network={"allow": ["PyPI.org", "files.pythonhosted.org"]}),
                          headers=auth)
    assert r.json()["network"] == {"requested": True, "effective": True,
                                   "allow": ["files.pythonhosted.org", "pypi.org"]}


# --------------------------------------------------------------------------- config invalid

async def test_config_invalid_serves_health_only(backend):
    from httpx import ASGITransport, AsyncClient
    app = create_app(None, None, {}, config_error=ConfigError("atlas.yaml", ["auth.keys: too short"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://a") as c:
        h = await c.get("/v1/health")
        assert h.status_code == 200 and h.json()["config"] == "invalid"
        r = await c.get("/v1/images", headers={"Authorization": "Bearer x"})
        assert r.status_code == 503 and err_code(r) == "CONFIG_INVALID"
        assert r.json()["errors"][0]["data"]["problems"] == ["auth.keys: too short"]
