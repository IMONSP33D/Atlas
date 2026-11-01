# Atlas

Atlas is a self-hosted code-execution backend for Perseus agents. An agent sends code (and optionally files) in a tool call; Atlas runs it in an isolated environment and returns the execution status, stdout/stderr, and optionally a screenshot. Nothing else comes back out.

Atlas is a personal extension of Perseus. It will eventually be bundled with Perseus and other components into **Olympus**.

## Runtimes

| Runtime     | What it is                                   | State            | Startup   |
|-------------|----------------------------------------------|------------------|-----------|
| `ephemeral` | Throwaway Linux container, polyglot toolchain | None (per call)  | < 1 s     |
| `linux`     | Full Linux VM on Proxmox, pinned build        | Session + revert | warm: ~3 s / cold: ~30 s |
| `windows`   | Full Windows VM on Proxmox, pinned build      | Session + revert | warm: ~5 s / cold: ~60 s |
| `cape`      | CAPE sandbox detonation, returns report       | None (per call)  | minutes   |

## Repository layout

```
atlas/
├── README.md                     this file
├── PROJECT-INSTRUCTIONS.md       custom instructions for the Atlas claude.ai Project
├── docs/
│   ├── architecture.md           design: tiers, Proxmox, virtio channel, sessions, security
│   ├── api-contract.md           HTTP API + JSON schemas (the only thing Perseus needs)
│   ├── errors.md                 full error catalog returned to agents
│   └── decisions.md              decision log + open questions
├── config/
│   ├── atlas.example.yaml        Atlas daemon configuration
│   └── images.example.yaml       image catalog (what runtimes/versions exist, which are warm)
├── scripts/
│   ├── proxmox-host-setup.sh     bare-metal Proxmox host prep
│   ├── build-golden-windows.ps1  runs inside a Windows VM to prepare a golden image
│   ├── build-golden-linux.sh     runs inside a Linux VM to prepare a golden image
│   └── atlas-keygen.py           generates word-seed API keys
├── ephemeral/
│   ├── Dockerfile                polyglot image for the ephemeral tier
│   └── runner.sh                 per-language entrypoint inside the container
└── guest-agent/
    └── README.md                 design of the in-VM agent (virtio-serial transport)
```

## Hard limits (v1)

- Max execution timeout: **10 min** per call
- Session idle revert: **30 min** (warning emitted at 25 min)
- Max concurrent sessions: **2**, and **never Windows + Linux at the same time**
- File upload cap: **100 MB** per call (sum of all files)
- Files do not come back out; only status / output / screenshots
- All sessions are killed on Atlas restart
- Network: **off** by default; agent may request, Perseus policy clamps, Atlas enforces

## Status

Design phase. Nothing here is deployed yet. See `docs/decisions.md` for open items.
