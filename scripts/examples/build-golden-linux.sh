#!/usr/bin/env bash
# Atlas (EXAMPLE, optional): one way to prepare an Ubuntu 24.04 VM that meets docs/byo-vm.md.
# Run as root inside the VM with a temporary NIC attached. Remove the NIC,
# shut down, then: qm snapshot <vmid> golden --vmstate 1
set -euo pipefail
AGENT_SRC="${1:-/mnt/atlas-guest-agent}"   # folder with the agent (python package or binary)
log() { printf '\n\033[1;36m[atlas]\033[0m %s\n' "$*"; }

log "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential clang gdb pkg-config cmake git curl wget unzip \
  python3 python3-pip python3-venv golang-go scrot xvfb x11-utils \
  ca-certificates apt-transport-https software-properties-common

log "Node 22"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -qq nodejs

log ".NET 8 SDK + PowerShell"
add-apt-repository -y ppa:dotnet/backports >/dev/null 2>&1 || true
apt-get update -qq
apt-get install -y -qq dotnet-sdk-8.0
curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -o /tmp/msprod.deb
dpkg -i /tmp/msprod.deb && apt-get update -qq && apt-get install -y -qq powershell

log "Rust (system-wide)"
export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
echo 'export PATH=/opt/cargo/bin:$PATH' > /etc/profile.d/rust.sh

log "Python libs"
pip3 install --break-system-packages requests numpy pandas pillow mss

log "atlas user + dirs"
id atlas >/dev/null 2>&1 || useradd -m -s /bin/bash atlas
mkdir -p /work/in /opt/atlas-agent   # no /work/out: files never leave the guest
chown -R atlas:atlas /work
cp -r "${AGENT_SRC}/." /opt/atlas-agent/

log "Guest agent service (virtio-serial, no network)"
cat > /etc/systemd/system/atlas-agent.service <<'EOF'
[Unit]
Description=Atlas guest agent
After=multi-user.target

[Service]
User=atlas
Environment=DISPLAY=:99
ExecStartPre=/bin/sh -c 'Xvfb :99 -screen 0 1920x1080x24 & sleep 1'
ExecStart=/usr/bin/python3 /opt/atlas-agent/agent.py --channel /dev/virtio-ports/org.atlas.agent
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/udev/rules.d/90-atlas-virtio.rules <<'EOF'
SUBSYSTEM=="virtio-ports", ATTR{name}=="org.atlas.agent", GROUP="atlas", MODE="0660"
EOF
systemctl daemon-reload
systemctl enable atlas-agent

log "Quiet the guest"
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades 2>/dev/null || true
systemctl disable --now snapd 2>/dev/null || true

log "Build string for images.yaml:"
. /etc/os-release
echo "${VERSION_ID}+$(uname -r)"
echo "Now: remove the NIC, shut down, then: qm snapshot <vmid> golden --vmstate 1"
