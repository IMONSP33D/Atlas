# Atlas

Atlas is a self-hosted code-execution backend for Perseus agents. An agent sends code (and optionally files) in a tool call; Atlas runs it in an isolated environment and returns the execution status, stdout/stderr, and optionally a screenshot. Nothing else comes back out.

Atlas is a personal extension of Perseus. It will eventually be bundled with Perseus and other components into **Olympus**.

**Atlas is bring-your-own-VM.** It does not build, license, or ship OS images. You prepare Proxmox VMs that meet the requirements in `docs/byo-vm.md` (guest agent installed, virtio-serial port, no NIC, a `golden` snapshot), register them in `config/images.yaml`, and Atlas drives them: boot, run, revert. The only image Atlas ships is the `ephemeral` Docker container.

## Runtimes

| Runtime     | What it is                                   | State            | Startup   |
|-------------|----------------------------------------------|------------------|-----------|
| `ephemeral` | Throwaway Linux container, polyglot toolchain (shipped) | None (per call)  | < 1 s     |
| `linux`     | Your Linux VM on Proxmox, pinned build        | Session + revert | warm: ~3 s / cold: ~30 s |
| `windows`   | Your Windows VM on Proxmox, pinned build      | Session + revert | warm: ~5 s / cold: ~60 s |
| `cape`      | Your CAPE sandbox; Atlas relays the report    | None (per call)  | minutes   |

## Repository layout

```
atlas/
├── README.md                     this file
├── PROJECT-INSTRUCTIONS.md       custom instructions for the Atlas claude.ai Project
├── docs/
│   ├── architecture.md           design: tiers, Proxmox, virtio channel, sessions, security
│   ├── api-contract.md           HTTP API + JSON schemas (the only thing Perseus needs)
│   ├── errors.md                 full error catalog returned to agents
│   ├── byo-vm.md                 what a VM must have before Atlas can drive it
│   └── decisions.md              decision log + open questions
├── config/
│   ├── atlas.example.yaml        Atlas daemon configuration
│   └── images.example.yaml       image catalog (your VMs: runtime, alias, build, vmid, warm)
├── scripts/
│   ├── proxmox-host-setup.sh     bare-metal Proxmox host prep
│   ├── atlas-keygen.py           generates word-seed API keys
│   └── examples/                 OPTIONAL reference scripts for preparing a VM
│       ├── build-golden-windows.ps1
│       └── build-golden-linux.sh
├── ephemeral/
│   ├── Dockerfile                polyglot image for the ephemeral tier
│   └── runner.sh                 per-language entrypoint inside the container
└── guest-agent/
    └── README.md                 design of the in-VM agent (virtio-serial transport)
```

## Hard limits (v1)

- Max execution timeout: **15 min** per call
- Session idle revert: **15 min** (warning emitted at 10 min)
- Max concurrent sessions: **2**, and **never Windows + Linux at the same time**
- File upload cap: **100 MB** per call (sum of all files)
- Files do not come back out; only status / output / screenshots
- All sessions are killed on Atlas restart
- Network: **off** by default; agent may request, Perseus policy clamps, Atlas enforces

## Status

Design phase. Nothing here is deployed yet. See `docs/decisions.md` for open items.
