# Atlas Architecture

## 1. Purpose

Give a Perseus agent a way to run code and get back *only* the result of running it. Atlas is the untrusted-code boundary: everything the agent writes is treated as hostile, and the design assumes the guest will try to escape, reach the network, or persist.

## 2. Components

```
┌──────────────┐  HTTPS + API key   ┌─────────────────────────────────────┐
│   Perseus    │ ─────────────────► │  Atlas daemon (control plane)        │
│  (agent +    │ ◄───────────────── │  - REST API                          │
│   tool layer)│   JSON result      │  - session manager                   │
└──────────────┘                    │  - scheduler (warm pool, limits)     │
                                    │  - Proxmox client (pvesh / API)      │
                                    │  - dashboard (read-only, live stats) │
                                    └───────┬──────────┬──────────┬───────┘
                                            │          │          │
                              docker run    │   virtio-serial     │  CAPE REST
                                            ▼          ▼          ▼
                                    ┌───────────┐ ┌──────────┐ ┌──────────┐
                                    │ ephemeral │ │ Proxmox  │ │  CAPE    │
                                    │ container │ │ VMs      │ │ sandbox  │
                                    │           │ │ win/linux│ │ (own VMs)│
                                    └───────────┘ └──────────┘ └──────────┘
```

### 2.1 Atlas daemon

A single long-running service on the Proxmox host (or a dedicated LXC/VM on it with the Proxmox API reachable). Responsibilities:

- Terminate the API (auth, validation, rate limiting)
- Own the session table (in memory; wiped on restart by design)
- Enforce limits: timeouts, idle, concurrency, exclusivity (no win+linux overlap)
- Clamp network policy to what Perseus passed; refuse rather than downgrade
- Drive Proxmox: start/stop, `snapshot rollback`, QEMU monitor `screendump`
- Speak to the guest agent over virtio-serial
- Dispatch `cape` runs to the CAPE REST API and relay the report
- Serve the dashboard

Suggested implementation: Python (FastAPI + asyncio) or Go. Python gets you `proxmoxer` and faster iteration; Go gets you a single static binary. Either is fine; Python is the assumption in the scripts.

### 2.2 Ephemeral tier

Docker on the Atlas host. One container per call:

```
docker run --rm --network none --memory 2g --cpus 2 --pids-limit 512 \
  --read-only --tmpfs /work:size=512m --tmpfs /tmp:size=256m \
  --security-opt no-new-privileges --cap-drop ALL \
  -v <staged-files>:/work/in:ro  atlas-ephemeral:<tag>  <entrypoint>
```

- `--network none` unless the clamped policy says allow (then a dedicated bridge with egress allowlist via nftables, never the host network)
- Working dir is tmpfs; nothing persists, nothing is copied out
- Image contents: see `ephemeral/Dockerfile`
- The `version` field is ignored for this tier; response reports the image tag as `build`

### 2.3 VM tiers (`linux`, `windows`)

Proxmox VE on bare metal. Each catalog entry is one VM template with a named golden snapshot (`golden`). A **session** is:

1. Scheduler checks limits (concurrency, exclusivity) → may return `SESSION_LIMIT` / `RUNTIME_EXCLUSIVE`
2. If the VM is **warm** (already booted and sitting at `golden` state), attach immediately
3. If **cold**, `qm start`, wait for guest-agent heartbeat on the virtio channel
4. Session ID issued; executions run against the same guest state
5. On close / idle timeout / Atlas restart / hard error: `qm snapshot rollback golden`, and if the entry is marked warm, leave it running at that state for the next session

**Warm vs cold** is config, not something the agent controls. The dashboard shows which entries are warm and lets you flip them. Warm costs RAM continuously; on a 32 GB box expect one warm Windows and one warm Linux at most. On a server stack, keep the whole catalog warm.

### 2.4 Guest agent + virtio-serial

The guest agent is a small service inside every golden image. It talks to the host over a **virtio-serial** channel (`/dev/virtio-ports/org.atlas.agent` on Linux, `\\.\Global\org.atlas.agent` via the vioserial driver on Windows). This is deliberate:

- The guest needs **no network interface at all** to receive work and return results, so `network: false` means the NIC is simply absent
- There is no listener inside the guest, so nothing for guest code to attack
- The host side is a Unix socket Proxmox exposes per VM

Protocol: length-prefixed JSON frames, one request/response at a time, plus an unsolicited `heartbeat` from the guest every 5 s. See `guest-agent/README.md`.

**Windows note:** the guest agent must run in an interactive session (Session 1), not as a Session 0 service, if the agent needs to launch GUI programs or take in-guest screenshots. Simplest: auto-logon a dedicated user and start the agent from a scheduled task at logon, running as that user with high integrity. Programs that need admin use a helper that elevates via a pre-granted token.

### 2.5 Screenshots

Two sources, both exposed through the same `screenshot` action:

| Source       | How                                              | Pros / cons |
|--------------|--------------------------------------------------|-------------|
| `hypervisor` | QEMU monitor `screendump` via Proxmox API        | Works even if the guest is hung or at a UAC prompt; lower fidelity; no window targeting |
| `guest`      | Guest agent captures via GDI/DXGI (Win) or scrot/grim (Linux) | Full fidelity, can target a window or region; needs a healthy guest |

Default to `guest`, fall back to `hypervisor` automatically if the guest doesn't respond within 3 s, and tell the agent which one it got. Returned as base64 PNG, capped at 5 MB; larger images are downscaled.

### 2.6 File ingress

- Agent attaches files as base64 in the call (cap 100 MB total, 25 files)
- Atlas writes them to a scratch dir, hashes them, and pushes them over the virtio channel (chunked, 4 MB frames) or bind-mounts them for the ephemeral tier
- Files land at a fixed path: `/work/in/` (Linux, ephemeral) or `C:\Atlas\in\` (Windows)
- **Nothing is ever read back out of the guest except the execution result and screenshots.** The guest agent has no "send file" verb. This is an intentional exfiltration limit.

### 2.7 CAPE runtime

CAPEv2 runs on the same Proxmox host using its `proxmox` machinery module, with its own set of analysis VMs (not shared with the Atlas session pool). Atlas does not manage those VMs.

Flow for `runtime: "cape"`:

1. Agent passes a single file (the sample) and optional `cape_options` (package, timeout, route)
2. Atlas submits via CAPE's REST API (`/apiv2/tasks/create/file/`)
3. Atlas polls task status; typical analysis is 2–10 min, so this is **always async** — the call returns a `task_id` and the agent polls `GET /cape/{task_id}` or Atlas pushes to a Perseus callback URL
4. On completion Atlas fetches the JSON report, strips the dropped-files section (files don't come out), and returns the report plus the screenshot gallery CAPE captured

CAPE's own network routing (internet / inetsim / none) is set per call from the clamped network policy; `network: false` maps to `route: none`.

## 3. Sessions and limits

| Limit                         | Value   | Behavior when hit |
|-------------------------------|---------|-------------------|
| Execution timeout (max)       | 10 min  | Process tree killed; `EXEC_TIMEOUT` |
| Session idle                  | 30 min  | Session reverted; `SESSION_IDLE_REVERTED` on next call |
| Idle warning                  | 25 min  | Any response after 25 min idle carries `warnings: ["SESSION_IDLE_WARNING"]` with seconds remaining |
| Concurrent sessions           | 2       | `SESSION_LIMIT` |
| Windows + Linux concurrently  | never   | `RUNTIME_EXCLUSIVE` |
| Sessions on Atlas restart     | killed  | `SESSION_NOT_FOUND` with `reason: "atlas_restarted"` |

Open question (see decisions.md): whether a session also has a hard maximum lifetime regardless of activity. Current assumption: no, idle only.

## 4. Network policy

Three layers, each only able to narrow:

1. **Agent request**: `network: false | true | {"allow": ["pypi.org", "github.com"]}`
2. **Perseus clamp**: per-tool human setting — `deny` (default), `allowlist`, `allow`. Perseus rewrites the request to the intersection.
3. **Atlas enforcement**: for VMs, attaches/detaches the NIC and programs nftables on the Atlas bridge; for ephemeral, chooses `--network none` or the egress bridge. DNS is served by Atlas and only resolves allowlisted names.

Atlas never silently downgrades. If it cannot honor the policy it received (e.g. the bridge is down), it returns `NETWORK_UNAVAILABLE` and does not run the code.

## 5. Authentication

- Single API key per Perseus instance, generated by `scripts/atlas-keygen.py`
- Format: 5 words from a 7776-word list, hyphen-joined, e.g. `apple-pie-red-car-purple` (≈64 bits). Good enough for a LAN-only service; the key is presented as `Authorization: Bearer <key>`
- Stored at rest as an argon2id hash in `atlas.yaml`; the plaintext is shown once at generation
- TLS: self-signed cert generated on first run; Perseus pins the fingerprint. If Atlas ever leaves the LAN, put it behind a reverse proxy with a real cert and switch to mTLS

## 6. Security posture (summary)

- Guests have no NIC unless policy grants one; even then, egress only, allowlisted, no route to the Atlas host or Perseus
- Guest ↔ host is virtio-serial only; the guest agent exposes no inbound verb that returns files
- Every session ends in a snapshot rollback; the golden state is the only persistent state
- Ephemeral containers run read-only, no caps, no new privs, PID- and memory-limited
- API key hashed at rest; dashboard is read-only and bound to localhost / LAN only
- Atlas logs every call (who, runtime, build, hash of code, hash of inputs, exit code, duration) to a local append-only log — never the code or output itself unless `log_output: true` in config

## 7. Hardware assumptions

- **Proxmox must be bare metal.** Nested under Hyper-V is possible but slow and unreliable for snapshot timing. Local Windows box becomes a client; Proxmox goes on a dedicated machine or a re-imaged/dual-booted drive.
- Minimum: 32 GB RAM, 8 cores, NVMe. Enough for 1 warm Windows (4 GB) + 1 warm Linux (2 GB) + CAPE's analysis VM (4 GB) + ephemeral containers + headroom.
- Comfortable: 64–128 GB, 16+ cores. Whole catalog warm.
