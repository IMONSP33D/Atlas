# Bring Your Own VM

Atlas does not build, license, or own VM images. **You supply the VMs**; Atlas drives them. Any Proxmox VM that meets the requirements below can be registered in `config/images.yaml` and becomes a catalog entry the agent can target.

This keeps Atlas out of the business of Windows licensing, distro choice, and toolchain opinions. The scripts in `scripts/examples/` show one way to prepare a VM; they are references, not requirements.

## Requirements checklist

A VM is Atlas-ready when all of these hold. `atlas images validate` (planned) checks 1–6 automatically; 7–8 are on you.

| # | Requirement | How Atlas checks |
|---|-------------|------------------|
| 1 | Lives on the Proxmox node Atlas is configured for, with a numeric `vmid` | `GET /nodes/{node}/qemu/{vmid}/status/current` |
| 2 | Has a snapshot named `golden` (configurable: `proxmox.golden_snapshot_name`) | snapshot list |
| 3 | `golden` was taken **with RAM state** (`--vmstate 1`) if the entry is to be marked `warm`; without RAM state it can only be `cold` | snapshot `vmstate` flag |
| 4 | Has a virtio-serial port named `org.atlas.agent` wired to a host socket at `<virtio_socket_dir>/<vmid>.atlas` | VM config `args:` contains the chardev/virtserialport pair |
| 5 | Has **no network device** in its config (`net0` absent). Atlas hot-attaches a NIC on `egress_bridge` only when a session's policy permits it | VM config has no `netN` keys |
| 6 | The Atlas guest agent is installed and starts automatically; it answers `hello` on the virtio channel within `guest_boot_timeout_sec` of boot (or immediately, if warm) | `hello` handshake; major version must match |
| 7 | Toolchains for every language you list under `languages:` in `images.yaml` are installed and on PATH for the agent's user | not checked; `LANGUAGE_UNSUPPORTED` is returned from config, `EXEC_SPAWN_FAILED` at runtime if you lied |
| 8 | (Windows, if you want GUI/screenshots) the agent runs in an interactive session (Session 1) as an auto-logon user | `hello` reports `session_type` and `display`; Atlas warns if screenshots are requested on a VM without a display |

## Registering a VM

```yaml
- runtime: windows          # or linux
  id: win11-24h2            # stable alias the agent uses as `version`
  build: 10.0.26100.2314    # whatever you want echoed back; Atlas does not verify it
  vmid: 201
  default: true
  enabled: true
  warm: true
  languages: [python, node, powershell, cmd, dotnet, csharp, rust, c, cpp, go, binary]
  notes: free text
```

`build` is **your** label. Atlas echoes it on every response and logs it; it never inspects the guest to confirm it. If you lie to yourself here, the agent's results are mislabelled. Suggested convention: the OS build string (`winver` / `uname -r`), or a hash of your own image-build pipeline.

## Minimum guest agent install

Whatever else is in the VM, the guest agent needs:

- **Windows:** `vioserial` driver (from virtio-win), `C:\Atlas\agent\atlas-agent.exe` started at logon via scheduled task, running as the interactive user with highest privileges. Directories `C:\Atlas\in` and `C:\Atlas\work`.
- **Linux:** the kernel's `virtio_console` module (in every mainline kernel), the agent as a systemd service running as an unprivileged user with read/write on `/dev/virtio-ports/org.atlas.agent` (udev rule), directories `/work` (working dir) and `/work/in` (uploaded files), owned by the agent user. Xvfb or a real display if screenshots are wanted.

See `guest-agent/README.md` for the protocol and `scripts/examples/` for complete install scripts.

## Things Atlas will do to your VM

Be aware before you register a VM you care about:

- `qm snapshot rollback golden` after **every** session. Anything not in the golden snapshot is destroyed, every time.
- `qm start` / `qm stop` as the warm/cold state demands.
- Hot-attach and detach `net0` on the configured egress bridge when a session is granted network.
- `qm monitor screendump` for hypervisor-side screenshots.
- Nothing else. Atlas never modifies the golden snapshot, never takes new snapshots, never touches disks or other VM config.

## Things Atlas will not do

- Build or patch an image. When Windows Update moves the build, you rebuild/re-snapshot and update `build:` in `images.yaml`.
- Verify the `build` string.
- Manage licensing.
- Install toolchains.
- Manage CAPE's analysis VMs (CAPE's own machinery config owns those; Atlas only talks to CAPE's REST API).

## Recommended VM shape (for the reference scripts)

q35, OVMF, virtio-scsi-single, discard on, 4 GB RAM / 4 cores for Windows, 2 GB / 2 cores for Linux, no NIC, `serial0: socket`, plus the virtio-serial `args:` line. Exact `qm create` is printed by `scripts/proxmox-host-setup.sh`.
