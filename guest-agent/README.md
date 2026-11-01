# Atlas Guest Agent

The guest agent runs inside every `linux` and `windows` golden image. It is the only thing Atlas talks to inside a VM, and it talks only over virtio-serial. It has no listening sockets.

## Transport

- Device: `/dev/virtio-ports/org.atlas.agent` (Linux), `\\.\Global\org.atlas.agent` (Windows, vioserial driver)
- Host side: Unix socket `/var/run/qemu-server/<vmid>.atlas` created by the `-chardev socket` arg on the VM
- Framing: 4-byte big-endian length + JSON payload (UTF-8). Max frame 4 MiB; file pushes are chunked.
- One request in flight at a time. The guest also emits unsolicited `heartbeat` frames every 5 s.

## Verbs (host → guest)

| Verb | Payload | Reply |
|------|---------|-------|
| `hello` | `{ "atlas_version" }` | `{ "agent_version", "os", "build", "session_type": "interactive\|service", "display": true\|false }` |
| `put_file_begin` | `{ "name", "size", "sha256" }` | `{ "ok" }` |
| `put_file_chunk` | `{ "name", "offset", "data_b64" }` | `{ "ok" }` |
| `put_file_end` | `{ "name" }` | `{ "ok", "sha256" }` — agent verifies hash; mismatch → error |
| `exec` | `{ "exec_id", "language", "code" \| "entrypoint", "args", "env", "stdin", "timeout_sec" }` | streamed: `exec_output` frames, then `exec_done` |
| `kill` | `{ "exec_id" }` | `{ "ok" }` |
| `screenshot` | `{ "window_title", "region" }` | `{ "format", "width", "height", "data_b64" }` |
| `ping` | `{}` | `{ "ok", "uptime_sec" }` |
| `prepare_revert` | `{}` | `{ "ok" }` — flush logs; host rolls back right after |

## Frames (guest → host, unsolicited)

- `heartbeat` `{ "ts", "load", "mem_free_mb" }` every 5 s
- `exec_output` `{ "exec_id", "stream": "stdout\|stderr", "data_b64" }` as output arrives (so timeouts still return partial output)
- `exec_done` `{ "exec_id", "exit_code", "wall_time_ms", "cpu_time_ms", "peak_memory_bytes", "reason": "exit\|timeout\|killed\|oom\|spawn_failed\|compile_failed", "detail" }`

## What the agent deliberately cannot do

- Return a file. There is no `get_file` verb. Only `exec_output`, `exec_done`, and `screenshot` carry data out.
- Open a network socket. The guest has no NIC unless Atlas hot-attaches one for an allow-listed session.
- Survive a revert. Everything it wrote is discarded on rollback.

## Execution details

- Working dir: `/work` (Linux) or `C:\Atlas\work` (Windows). Inputs at `/work/in` or `C:\Atlas\in`.
- The agent writes `code` to `code.<ext>` in the working dir, then runs the same per-language table as `ephemeral/runner.sh` (on Windows: `py -3.12`, `node`, `pwsh`, `cmd /c`, `cl.exe` via `vcvars64.bat`, `rustc`, `go build`, `dotnet build`).
- Process tree is created in its own job object (Windows) / cgroup or process group (Linux) so `kill` and timeouts take the whole tree.
- Windows agent runs in the interactive session (Session 1) as the auto-logon user so GUI programs and DXGI screenshots work. A helper `atlas-elevate.exe` runs commands with an admin token when `exec.elevate: true` (UAC prompt pre-approved in golden image).
- Output capped at 1 MiB per stream in the agent as well as in Atlas; the agent sets `truncated: true` in `exec_done`.

## Screenshot

- Windows: DXGI desktop duplication, fallback GDI `BitBlt`. `window_title` uses substring match on top-level windows and captures that window's rect.
- Linux: `scrot` against `DISPLAY=:99` (Xvfb started by the service), or `grim` if Wayland is in use.
- Encoded PNG; agent downscales to ≤ 5 MB before sending.

## Versioning

`hello` exchanges versions. Atlas refuses a guest whose major version differs (`GUEST_AGENT_VERSION`), which forces a golden-image rebuild rather than silently running a mismatched protocol.

## Implementation notes

Python 3.12 with no third-party deps beyond `pywin32`/`mss` on Windows and `mss` on Linux is enough for v1; package with PyInstaller into `atlas-agent.exe` for Windows so the golden image doesn't depend on the system Python the agent's *jobs* use. Keep the agent under ~600 lines; it is a dumb pipe with a process supervisor.

## Running locally

The reference implementation is the stdlib-only package in `guest-agent/atlas_agent/` (Python 3.12+). In production `--channel` is the virtio char device; for development it also accepts a Unix socket path, in which case the agent connects to it as a client, so the host side must be the listener (QEMU's `-chardev socket,server=on` does this on a real VM; on a laptop use the fake host in `tests/fakehost.py`).

```bash
cd guest-agent
python -m atlas_agent --channel /tmp/atlas.sock --workdir /tmp/atlas-work   # input dir defaults to <workdir>/in
python -m pytest -q                                                         # needs pytest + pytest-asyncio
```

Notes: `protocol.py` in the package is a vendored copy of `src/atlas/guest/protocol.py` (a test enforces byte identity). `exec` is acknowledged immediately with `exec.reply {ok, exec_id}`; output and `exec_done` follow as events, which is what lets `kill` and `ping` be served mid-exec. Optional: `mss` (screenshots), `Pillow` (downscale oversized PNGs), `scrot`/`xdotool` on PATH as fallbacks.
