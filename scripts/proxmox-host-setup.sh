#!/usr/bin/env bash
# Atlas: Proxmox VE host preparation.
# Run as root on a fresh Proxmox VE 8.x install. Idempotent where practical.
#
# What this does:
#   1. Creates an isolated egress bridge (vmbr1) with no uplink; Atlas does NAT
#      + nftables allowlisting on it when a policy permits network.
#   2. Installs Docker for the ephemeral tier.
#   3. Creates a Proxmox API token for Atlas with the minimum privileges.
#   4. Creates the atlas system user, dirs, and a systemd unit placeholder.
#   5. Prints the next manual steps (VM templates, golden snapshots).
#
# Review every variable below before running.
set -euo pipefail

ATLAS_USER=atlas
ATLAS_HOME=/opt/atlas
ATLAS_ETC=/etc/atlas
ATLAS_LOG=/var/log/atlas
EGRESS_BRIDGE=vmbr1
EGRESS_CIDR=10.99.0.1/24
PVE_NODE=$(hostname -s)
PVE_TOKEN_USER="atlas@pve"
PVE_TOKEN_NAME="atlas"

log() { printf '\n\033[1;34m[atlas]\033[0m %s\n' "$*"; }

require_root() { [[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }; }

check_pve() {
  command -v pvesh >/dev/null || { echo "pvesh not found; is this a Proxmox host?"; exit 1; }
  log "Proxmox VE $(pveversion | cut -d/ -f2) on node ${PVE_NODE}"
}

check_virt() {
  if ! grep -qE 'vmx|svm' /proc/cpuinfo; then
    echo "No VT-x/AMD-V. Proxmox must run on bare metal for Atlas."; exit 1
  fi
}

setup_bridge() {
  log "Configuring isolated egress bridge ${EGRESS_BRIDGE}"
  if grep -q "iface ${EGRESS_BRIDGE}" /etc/network/interfaces; then
    echo "  already present, skipping"
    return
  fi
  cat >> /etc/network/interfaces <<EOF

# Atlas isolated egress bridge (no physical uplink; Atlas NATs + filters)
auto ${EGRESS_BRIDGE}
iface ${EGRESS_BRIDGE} inet static
    address ${EGRESS_CIDR}
    bridge-ports none
    bridge-stp off
    bridge-fd 0
EOF
  ifreload -a || ifup "${EGRESS_BRIDGE}"
}

setup_nft() {
  log "Installing nftables base table (Atlas manages rules at runtime)"
  apt-get install -y -qq nftables >/dev/null
  cat > /etc/nftables.d/atlas.nft <<'EOF'
table inet atlas_egress {
  set allow_v4 { type ipv4_addr; flags timeout; }
  chain forward {
    type filter hook forward priority 0; policy drop;
    # Atlas inserts per-session rules here. Default: guests forward nothing.
    iifname "vmbr1" ip daddr @allow_v4 tcp dport { 80, 443 } accept
    iifname "vmbr1" ip daddr 10.99.0.1 udp dport 53 accept
    ct state established,related accept
  }
}
EOF
  mkdir -p /etc/nftables.d
  grep -q 'include "/etc/nftables.d/\*.nft"' /etc/nftables.conf || \
    echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf
  systemctl enable --now nftables
  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
}

setup_docker() {
  log "Installing Docker for the ephemeral tier"
  if command -v docker >/dev/null; then echo "  already installed"; return; fi
  apt-get install -y -qq ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io >/dev/null
  systemctl enable --now docker
}

setup_user_dirs() {
  log "Creating ${ATLAS_USER} user and directories"
  id "${ATLAS_USER}" >/dev/null 2>&1 || useradd -r -m -d "${ATLAS_HOME}" -s /usr/sbin/nologin "${ATLAS_USER}"
  usermod -aG docker "${ATLAS_USER}"
  mkdir -p "${ATLAS_ETC}/tls" "${ATLAS_LOG}" "${ATLAS_HOME}/scratch"
  chown -R "${ATLAS_USER}:${ATLAS_USER}" "${ATLAS_ETC}" "${ATLAS_LOG}" "${ATLAS_HOME}"
  chmod 700 "${ATLAS_ETC}"
}

setup_pve_token() {
  log "Creating Proxmox role, user, and API token for Atlas"
  pveum role add AtlasRole -privs "VM.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback VM.Config.Network VM.Monitor VM.Console Datastore.Audit Sys.Audit" 2>/dev/null || true
  pveum user add "${PVE_TOKEN_USER}" 2>/dev/null || true
  pveum aclmod / -user "${PVE_TOKEN_USER}" -role AtlasRole
  echo
  echo "  Token (save the secret; it is shown once):"
  pveum user token add "${PVE_TOKEN_USER}" "${PVE_TOKEN_NAME}" -privsep 0 || echo "  token exists; delete and re-add to rotate"
}

setup_systemd() {
  log "Writing systemd unit placeholder"
  cat > /etc/systemd/system/atlas.service <<EOF
[Unit]
Description=Atlas code-execution daemon
After=network-online.target docker.service pve-cluster.service
Wants=network-online.target

[Service]
User=${ATLAS_USER}
WorkingDirectory=${ATLAS_HOME}
Environment=ATLAS_CONFIG=${ATLAS_ETC}/atlas.yaml
Environment=ATLAS_IMAGES=${ATLAS_ETC}/images.yaml
ExecStart=${ATLAS_HOME}/venv/bin/python -m atlas
Restart=on-failure
RestartSec=5
# All sessions are killed on restart by design.

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

next_steps() {
  cat <<EOF

=== Host prepared. Manual steps remaining ===

1. Copy config/atlas.example.yaml -> ${ATLAS_ETC}/atlas.yaml and fill in
   the Proxmox token secret above and an API key from scripts/atlas-keygen.py.
2. Copy config/images.example.yaml -> ${ATLAS_ETC}/images.yaml.
3. Build the ephemeral image on this host:
     docker build -t atlas-ephemeral:2026.11.1 ephemeral/
4. Create VM templates (one per catalog entry). For each:
     qm create <vmid> --name atlas-win11-24h2 --memory 4096 --cores 4 \\
       --machine q35 --bios ovmf --efidisk0 local-zfs:1 --tpmstate0 local-zfs:1,version=v2.0 \\
       --scsihw virtio-scsi-single --scsi0 local-zfs:80,discard=on,ssd=1 \\
       --net0 none --serial0 socket \\
       --args "-chardev socket,path=/var/run/qemu-server/<vmid>.atlas,server=on,wait=off,id=atlas0 \\
               -device virtio-serial-pci -device virtserialport,chardev=atlas0,name=org.atlas.agent"
   Install the OS, run scripts/build-golden-windows.ps1 (or build-golden-linux.sh)
   inside it, shut down, then:
     qm snapshot <vmid> golden --vmstate 1
   (--vmstate 1 keeps RAM so 'warm' rollback is seconds, not a reboot.)
5. Deploy the Atlas daemon into ${ATLAS_HOME} and: systemctl enable --now atlas
6. CAPE: install CAPEv2 on a separate VM/host, configure its proxmox machinery
   module against this node, and set cape.api_base in atlas.yaml.

EOF
}

require_root
check_pve
check_virt
setup_bridge
setup_nft
setup_docker
setup_user_dirs
setup_pve_token
setup_systemd
next_steps
