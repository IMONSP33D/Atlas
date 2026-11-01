# Atlas ephemeral tier: polyglot, no-network-required toolchain image.
# Build:  docker build -t atlas-ephemeral:2026.11.1 .
# Run (by Atlas):
#   docker run --rm --network none --memory 2g --cpus 2 --pids-limit 512 \
#     --read-only --tmpfs /work:size=512m --tmpfs /tmp:size=256m \
#     --security-opt no-new-privileges --cap-drop ALL \
#     -v <staged>:/work/in:ro atlas-ephemeral:2026.11.1 /runner <language> <entry>
#
# Everything an agent might import/compile without network must be baked in here.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo \
    GOPATH=/opt/go GOCACHE=/tmp/gocache GOFLAGS=-mod=mod \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 \
    PATH=/opt/cargo/bin:/usr/local/go/bin:/opt/go/bin:/usr/share/dotnet:$PATH

# --- base + C/C++ --------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget unzip git xz-utils \
      build-essential clang lld gdb cmake ninja-build pkg-config \
      libssl-dev zlib1g-dev libffi-dev libsqlite3-dev \
      bash coreutils jq bc \
    && rm -rf /var/lib/apt/lists/*

# --- Python --------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv python3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages \
      requests httpx numpy pandas scipy matplotlib pillow pyyaml \
      cryptography pycryptodome pefile yara-python capstone lief \
      scapy dpkt python-magic python-dateutil pytest

# --- Node ----------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g typescript ts-node tsx

# --- Go ------------------------------------------------------------------
ARG GO_VERSION=1.23.3
RUN curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz

# --- Rust ----------------------------------------------------------------
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path \
    && rustup component add clippy rustfmt \
    && chmod -R a+rwX /opt/rustup /opt/cargo

# --- .NET SDK + C# + PowerShell -----------------------------------------
# NuGet cache lives outside /root so the unprivileged `runner` user can read it,
# and the SDK's writable state (HOME, telemetry, first-run) is pointed at the
# /work tmpfs so `dotnet build` works under --read-only.
ENV NUGET_PACKAGES=/opt/nuget \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_HOME=/work/.dotnet
RUN curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -o /tmp/ms.deb \
    && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
    && apt-get update && apt-get install -y --no-install-recommends dotnet-sdk-8.0 powershell \
    && rm -rf /var/lib/apt/lists/* \
    # pre-warm a C# console template so offline `dotnet build` works
    && mkdir -p /opt/nuget /opt/dotnet-warm && cd /opt/dotnet-warm \
    && DOTNET_CLI_HOME=/tmp/dotnet-warm-home dotnet new console -n warm --force >/dev/null \
    && cd warm && DOTNET_CLI_HOME=/tmp/dotnet-warm-home dotnet build -c Release >/dev/null \
    && rm -rf /opt/dotnet-warm /tmp/dotnet-warm-home && chmod -R a+rX /opt/nuget

# --- runner --------------------------------------------------------------
# /runner <language> <entry-or-code-file>; prints to stdout/stderr, exits with program's code.
COPY runner.sh /runner
RUN chmod 755 /runner

RUN useradd -m -u 1000 -s /bin/bash runner \
    && mkdir -p /work && chown runner:runner /work
USER runner
ENV HOME=/work
WORKDIR /work
ENTRYPOINT ["/runner"]
