"""Mock Proxmox VE REST API (the subset ProxmoxBackend uses) plus a MockGuest launcher.

    python -m atlas.mock.proxmox_server [--port 8006] [--socket-dir /tmp/atlas-mock] [--vmids 201,202]

starts the API (plain HTTP, token auth) and one MockGuest per VM at
<socket-dir>/<vmid>.atlas, for poking with curl or a REPL. Tests build a `MockPVE`
state object, wrap it with `create_app`, and either talk to it through httpx's
ASGITransport or hand `ASGIProxmoxAPI(state)` to ProxmoxBackend as its `api`.

Failure injection lives on the state object: `fail_start`, `fail_rollback`,
`fail_stop`, `fail_monitor` (each None, "http" or "task"), plus `offline`.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .guest import MockGuest

TOKEN_ID = "atlas@pve!atlas"
TOKEN_SECRET = "mock-secret"
PPM_2X2 = b"P6\n2 2\n255\n" + bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])


def good_config(vmid: int, socket_dir: str | os.PathLike[str] = "/var/run/qemu-server") -> dict[str, Any]:
    """A VM config that satisfies docs/byo-vm.md: serial port wired, no NIC."""
    return {
        "name": f"vm{vmid}",
        "memory": 2048,
        "cores": 2,
        "machine": "q35",
        "scsi0": "local-zfs:vm-%d-disk-0,discard=on" % vmid,
        "serial0": "socket",
        "args": (f"-chardev socket,id=atlas0,path={socket_dir}/{vmid}.atlas,server=on,wait=off "
                 "-device virtio-serial-pci "
                 "-device virtserialport,chardev=atlas0,name=org.atlas.agent"),
    }


@dataclass
class MockVM:
    vmid: int
    status: str = "stopped"                 # running | stopped
    config: dict[str, Any] = field(default_factory=dict)
    # name -> {"vmstate": 0|1}
    snapshots: dict[str, dict[str, Any]] = field(default_factory=lambda: {"golden": {"vmstate": 1}})


@dataclass
class _Task:
    kind: str
    vmid: int
    polls_left: int
    exitstatus: str = "OK"


class MockPVE:
    def __init__(self, node: str = "pve", token_id: str = TOKEN_ID, token_secret: str = TOKEN_SECRET,
                 free_mem_mb: int = 16384, task_polls: int = 1) -> None:
        self.node = node
        self.token_id = token_id
        self.token_secret = token_secret
        self.free_mem_mb = free_mem_mb
        self.task_polls = task_polls              # "running" answers before a task reports stopped
        self.vms: dict[int, MockVM] = {}
        self.tasks: dict[str, _Task] = {}
        self.calls: list[tuple[str, str]] = []    # (METHOD, path-after-/api2/json)
        self.fail_start: str | None = None        # None | "http" | "task"
        self.fail_stop: str | None = None
        self.fail_rollback: str | None = None
        self.fail_monitor: str | None = None      # None | "http"
        self.monitor_mode = "inline"              # inline: image b64 in `data`; file: only write the file
        self.screendump_bytes = PPM_2X2
        self.offline = False                      # ASGIProxmoxAPI raises a connection error
        self.rollback_count = 0
        self._seq = 0

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}

    def add_vm(self, vmid: int, *, status: str = "stopped", config: dict[str, Any] | None = None,
               snapshots: dict[str, dict[str, Any]] | None = None,
               socket_dir: str | os.PathLike[str] = "/var/run/qemu-server") -> MockVM:
        vm = MockVM(vmid, status, config if config is not None else good_config(vmid, socket_dir))
        if snapshots is not None:
            vm.snapshots = snapshots
        self.vms[vmid] = vm
        return vm

    def new_task(self, kind: str, vmid: int, exitstatus: str = "OK") -> str:
        self._seq += 1
        upid = f"UPID:{self.node}:{self._seq:08X}:0000ABCD:5F000000:{kind}:{vmid}:{self.token_id}:"
        self.tasks[upid] = _Task(kind, vmid, self.task_polls, exitstatus)
        return upid


# --------------------------------------------------------------------------- app

def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"data": None, "message": msg}, status_code=status)


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"data": data})


async def _params(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode()
    if not raw:
        return {}
    if raw.lstrip().startswith("{"):
        return {k: str(v) for k, v in json.loads(raw).items()}
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


def create_app(state: MockPVE) -> FastAPI:
    app = FastAPI(title="mock-pve")
    app.state.pve = state
    base = "/api2/json"

    @app.middleware("http")
    async def auth_and_log(request: Request, call_next):
        if request.url.path.startswith(base):
            state.calls.append((request.method, request.url.path[len(base):]))
            expected = f"PVEAPIToken={state.token_id}={state.token_secret}"
            if request.headers.get("authorization") != expected:
                return _err(401, "authentication failure")
        return await call_next(request)

    def vm_or_err(node: str, vmid: int) -> MockVM | JSONResponse:
        if node != state.node:
            return _err(500, f"hostname lookup '{node}' failed - failed to get address info for: {node}")
        vm = state.vms.get(vmid)
        if vm is None:
            return _err(500, f"Configuration file 'nodes/{node}/qemu-server/{vmid}.conf' does not exist")
        return vm

    @app.get(base + "/nodes/{node}/status")
    async def node_status(node: str):
        if node != state.node:
            return _err(500, f"hostname lookup '{node}' failed")
        free = state.free_mem_mb * 1024 * 1024
        total = 64 * 1024 ** 3
        return _ok({"memory": {"total": total, "free": free, "used": total - free}, "cpu": 0.01})

    @app.get(base + "/nodes/{node}/qemu/{vmid}/status/current")
    async def status_current(node: str, vmid: int):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        return _ok({"vmid": vmid, "status": vm.status, "qmpstatus": vm.status, "name": vm.config.get("name")})

    @app.post(base + "/nodes/{node}/qemu/{vmid}/status/start")
    async def status_start(node: str, vmid: int):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        if state.fail_start == "http":
            return _err(500, "start failed: mock-injected failure")
        if vm.status == "running":
            return _err(500, f"VM {vmid} already running")
        if state.fail_start == "task":
            return _ok(state.new_task("qmstart", vmid, exitstatus="start failed: mock-injected failure"))
        vm.status = "running"
        return _ok(state.new_task("qmstart", vmid))

    @app.post(base + "/nodes/{node}/qemu/{vmid}/status/stop")
    async def status_stop(node: str, vmid: int):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        if state.fail_stop == "http":
            return _err(500, "stop failed: mock-injected failure")
        vm.status = "stopped"
        return _ok(state.new_task("qmstop", vmid))

    @app.get(base + "/nodes/{node}/qemu/{vmid}/config")
    async def get_config(node: str, vmid: int):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        return _ok({**vm.config, "digest": "0" * 40})

    @app.get(base + "/nodes/{node}/qemu/{vmid}/snapshot")
    async def list_snapshots(node: str, vmid: int):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        rows = [{"name": n, "description": "", "snaptime": 1_700_000_000, **meta} for n, meta in vm.snapshots.items()]
        rows.append({"name": "current", "description": "You are here!", "digest": "0" * 40})
        return _ok(rows)

    @app.post(base + "/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback")
    async def rollback(node: str, vmid: int, name: str):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        if name not in vm.snapshots:
            return _err(500, f"snapshot '{name}' does not exist")
        if state.fail_rollback == "http":
            return _err(500, "rollback failed: mock-injected failure")
        if state.fail_rollback == "task":
            return _ok(state.new_task("qmrollback", vmid, exitstatus="rollback failed: mock-injected failure"))
        state.rollback_count += 1
        # Like PVE: a snapshot with RAM state resumes running; without, the VM ends up stopped.
        vm.status = "running" if vm.snapshots[name].get("vmstate") else "stopped"
        return _ok(state.new_task("qmrollback", vmid))

    @app.get(base + "/nodes/{node}/tasks/{upid}/status")
    async def task_status(node: str, upid: str):
        t = state.tasks.get(upid)
        if t is None:
            return _err(500, f"unable to parse worker upid '{upid}'")
        if t.polls_left > 0:
            t.polls_left -= 1
            return _ok({"status": "running", "upid": upid, "type": t.kind, "node": node})
        return _ok({"status": "stopped", "exitstatus": t.exitstatus, "upid": upid, "type": t.kind, "node": node})

    @app.post(base + "/nodes/{node}/qemu/{vmid}/monitor")
    async def monitor(node: str, vmid: int, request: Request):
        vm = vm_or_err(node, vmid)
        if isinstance(vm, JSONResponse):
            return vm
        cmd = (await _params(request)).get("command", "")
        if state.fail_monitor == "http":
            return _err(500, "monitor failed: mock-injected failure")
        if vm.status != "running":
            return _err(500, f"VM {vmid} not running")
        if cmd.startswith("screendump "):
            path = cmd.split(None, 1)[1].strip()
            Path(path).write_bytes(state.screendump_bytes)
            if state.monitor_mode == "inline":
                return _ok(base64.b64encode(state.screendump_bytes).decode())
            return _ok("")
        return _ok(f"unsupported monitor command: {cmd}")

    return app


# --------------------------------------------------------------------------- proxmoxer-style client over the ASGI app

class _Resource:
    def __init__(self, api: "ASGIProxmoxAPI", path: str) -> None:
        self._api, self._path = api, path

    def __call__(self, resource_id: Any = None) -> "_Resource":
        if resource_id in (None, ""):
            return self
        return _Resource(self._api, f"{self._path}/{str(resource_id).strip('/')}")

    def __getattr__(self, name: str) -> "_Resource":
        if name.startswith("_"):
            raise AttributeError(name)
        return _Resource(self._api, f"{self._path}/{name}")

    def get(self, **params: Any) -> Any:
        return self._api.request("GET", self._path, params)

    def post(self, **params: Any) -> Any:
        return self._api.request("POST", self._path, params)


class ASGIProxmoxAPI(_Resource):
    """Duck-types proxmoxer.ProxmoxAPI (`api("nodes/pve/qemu/1/status/current").get()`), serving
    requests from the in-process mock app on a private event-loop thread, so it can be called
    from blocking code (the backend runs it via asyncio.to_thread). Errors surface as
    proxmoxer.core.ResourceException / requests.ConnectionError like the real client."""

    def __init__(self, state: MockPVE, *, token_secret: str | None = None) -> None:
        super().__init__(self, "")
        self.state = state
        self._secret = token_secret if token_secret is not None else state.token_secret
        self._app = create_app(state)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mock-pve-asgi")
        self._thread.start()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    def request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        import httpx
        from proxmoxer.core import ResourceException
        from requests.exceptions import ConnectionError as ReqConnectionError

        if self.state.offline:
            raise ReqConnectionError("mock Proxmox is offline")

        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mock-pve") as c:
                headers = {"Authorization": f"PVEAPIToken={self.state.token_id}={self._secret}"}
                url = "/api2/json" + path
                if method == "GET":
                    return await c.get(url, params=params, headers=headers)
                return await c.post(url, data=params, headers=headers)

        r = asyncio.run_coroutine_threadsafe(go(), self._loop).result()
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise ResourceException(r.status_code, r.reason_phrase, str(body.get("message", "")))
        return body.get("data")


# --------------------------------------------------------------------------- CLI

async def _amain(args: argparse.Namespace) -> None:
    import uvicorn

    sock_dir = Path(args.socket_dir)
    state = MockPVE(node=args.node)
    guests = []
    for raw in args.vmids.split(","):
        vmid = int(raw)
        state.add_vm(vmid, status="running" if vmid == int(args.vmids.split(",")[0]) else "stopped",
                     socket_dir=sock_dir)
        guests.append(await MockGuest(sock_dir / f"{vmid}.atlas", heartbeat_interval=1.0).start())
    print(f"mock PVE   : http://127.0.0.1:{args.port}/api2/json  node={args.node}")
    print(f"  auth     : Authorization: PVEAPIToken={state.token_id}={state.token_secret}")
    print(f"mock guests: {', '.join(str(g.path) for g in guests)}")
    print(f"  try      : curl -H 'Authorization: PVEAPIToken={state.token_id}={state.token_secret}' "
          f"http://127.0.0.1:{args.port}/api2/json/nodes/{args.node}/qemu/{args.vmids.split(',')[0]}/status/current")
    print("  guest    : exec scripting is described in src/atlas/mock/guest.py")
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=args.port, log_level="warning"))
    try:
        await server.serve()
    finally:
        for g in guests:
            await g.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atlas.mock.proxmox_server")
    ap.add_argument("--port", type=int, default=8006)
    ap.add_argument("--node", default="pve")
    ap.add_argument("--socket-dir", default="/tmp/atlas-mock")
    ap.add_argument("--vmids", default="201,202", help="comma list; the first starts running, rest stopped")
    args = ap.parse_args(argv)
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
