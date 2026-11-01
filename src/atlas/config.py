"""atlas.yaml and images.yaml models.

Loading either file with a problem raises ConfigError. The daemon catches it,
serves only /v1/health, and every other route returns CONFIG_INVALID.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Runtime = Literal["ephemeral", "linux", "windows", "cape"]
VM_RUNTIMES: frozenset[str] = frozenset({"linux", "windows"})
LANGUAGES: tuple[str, ...] = (
    "python", "node", "powershell", "bash", "cmd", "dotnet", "rust",
    "c", "cpp", "csharp", "go", "binary",
)


class ConfigError(Exception):
    def __init__(self, path: str, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- atlas.yaml

class TlsConfig(_Strict):
    cert: Path
    key: Path


class ServerConfig(_Strict):
    bind: str = "0.0.0.0"
    port: int = Field(8443, ge=1, le=65535)
    tls: TlsConfig | None = None
    request_body_limit_mb: int = Field(140, ge=1)


class ApiKey(_Strict):
    name: str
    hash: str
    revoked: bool = False
    rate_limit_per_min: int = Field(120, ge=1)

    @field_validator("hash")
    @classmethod
    def _argon2id(cls, v: str) -> str:
        if not v.startswith("$argon2id$"):
            raise ValueError("hash must be an argon2id string (scripts/atlas-keygen.py)")
        if "REPLACE_ME" in v:
            raise ValueError("hash still contains REPLACE_ME")
        return v


class AuthConfig(_Strict):
    keys: list[ApiKey] = Field(min_length=1)

    @field_validator("keys")
    @classmethod
    def _unique_names(cls, v: list[ApiKey]) -> list[ApiKey]:
        names = [k.name for k in v]
        if len(set(names)) != len(names):
            raise ValueError("duplicate key name")
        return v


class LimitsConfig(_Strict):
    max_timeout_sec: int = Field(900, ge=1)
    default_timeout_sec: int = Field(60, ge=1)
    session_idle_sec: int = Field(900, ge=1)
    session_idle_warn_sec: int = Field(600, ge=0)
    session_hard_lifetime_sec: int = Field(0, ge=0)
    max_sessions: int = Field(2, ge=1)
    exclusive_runtimes: list[list[str]] = Field(default_factory=lambda: [["windows", "linux"]])
    max_upload_bytes: int = Field(104_857_600, ge=1)
    max_files: int = Field(25, ge=1)
    max_output_bytes: int = Field(1_048_576, ge=1)
    max_screenshot_bytes: int = Field(5_242_880, ge=1)

    @model_validator(mode="after")
    def _cross(self) -> "LimitsConfig":
        if self.default_timeout_sec > self.max_timeout_sec:
            raise ValueError("default_timeout_sec > max_timeout_sec")
        if self.session_idle_warn_sec >= self.session_idle_sec:
            raise ValueError("session_idle_warn_sec must be < session_idle_sec")
        for pair in self.exclusive_runtimes:
            if len(pair) != 2 or any(r not in VM_RUNTIMES for r in pair):
                raise ValueError(f"exclusive_runtimes entries must be pairs of VM runtimes: {pair}")
        return self

    def exclusive_with(self, runtime: str) -> set[str]:
        out: set[str] = set()
        for a, b in self.exclusive_runtimes:
            if runtime == a:
                out.add(b)
            elif runtime == b:
                out.add(a)
        return out


class ProxmoxConfig(_Strict):
    host: str
    port: int = 8006
    token_id: str
    token_secret: str
    verify_tls: bool = False
    node: str
    storage: str
    golden_snapshot_name: str = "golden"
    virtio_socket_dir: Path = Path("/var/run/qemu-server")
    guest_boot_timeout_sec: int = Field(120, ge=1)
    guest_heartbeat_interval_sec: int = Field(5, ge=1)
    guest_heartbeat_miss_limit: int = Field(6, ge=1)


class NetworkConfig(_Strict):
    egress_bridge: str = "vmbr1"
    dns_listen: str = "10.99.0.1"
    nft_table: str = "atlas_egress"
    host_reserve_mb: int = Field(4096, ge=0)


class EphemeralConfig(_Strict):
    image: str = "atlas-ephemeral:latest"
    memory_mb: int = Field(2048, ge=64)
    cpus: int = Field(2, ge=1)
    pids_limit: int = Field(512, ge=16)
    work_tmpfs_mb: int = Field(512, ge=16)


class CapeConfig(_Strict):
    enabled: bool = False
    api_base: str | None = None
    api_token: str | None = None
    default_timeout_sec: int = Field(200, ge=1)
    poll_interval_sec: int = Field(15, ge=1)
    max_report_mb: int = Field(50, ge=1)
    strip_sections: list[str] = Field(default_factory=lambda: ["dropped", "procmemory", "memory"])

    @model_validator(mode="after")
    def _enabled_needs_endpoint(self) -> "CapeConfig":
        if self.enabled and not (self.api_base and self.api_token):
            raise ValueError("cape.enabled requires api_base and api_token")
        return self


class ScreenshotsConfig(_Strict):
    default_source: Literal["guest", "hypervisor", "auto"] = "auto"
    guest_timeout_sec: int = Field(3, ge=1)
    format: Literal["png"] = "png"


class DashboardConfig(_Strict):
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = Field(8444, ge=1, le=65535)
    history_rows: int = Field(50, ge=1)


class LoggingConfig(_Strict):
    path: Path = Path("/var/log/atlas/atlas.jsonl")
    level: Literal["debug", "info", "warning", "error"] = "info"
    log_code: bool = False
    log_output: bool = False


class AtlasConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    proxmox: ProxmoxConfig | None = None
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    ephemeral: EphemeralConfig = Field(default_factory=EphemeralConfig)
    cape: CapeConfig = Field(default_factory=CapeConfig)
    screenshots: ScreenshotsConfig = Field(default_factory=ScreenshotsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# --------------------------------------------------------------------------- images.yaml

class ImageEntry(_Strict):
    runtime: Runtime
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    build: str
    default: bool = False
    enabled: bool = True
    languages: list[str] = Field(min_length=1)
    notes: str | None = None
    deprecated: bool = False
    removal_date: str | None = None
    # VM runtimes
    vmid: int | None = Field(None, ge=100)
    warm: bool | None = None
    memory_mb: int | None = Field(None, ge=256)
    cores: int | None = Field(None, ge=1)
    # cape
    cape_machine_tag: str | None = None

    @field_validator("languages")
    @classmethod
    def _known_langs(cls, v: list[str]) -> list[str]:
        bad = [l for l in v if l not in LANGUAGES]
        if bad:
            raise ValueError(f"unknown languages {bad}; known: {list(LANGUAGES)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate language")
        return v

    @model_validator(mode="after")
    def _per_runtime(self) -> "ImageEntry":
        vm_fields = {"vmid": self.vmid, "warm": self.warm, "memory_mb": self.memory_mb, "cores": self.cores}
        if self.runtime in VM_RUNTIMES:
            if self.vmid is None:
                raise ValueError(f"{self.runtime} entry needs vmid")
            if self.warm is None:
                self.warm = False
            if self.cape_machine_tag is not None:
                raise ValueError("cape_machine_tag only valid on cape entries")
        else:
            set_vm = [k for k, v in vm_fields.items() if v is not None]
            if set_vm:
                raise ValueError(f"{self.runtime} entry may not set {set_vm}")
            if self.runtime == "cape" and not self.cape_machine_tag:
                raise ValueError("cape entry needs cape_machine_tag")
            if self.runtime == "ephemeral" and self.cape_machine_tag is not None:
                raise ValueError("cape_machine_tag only valid on cape entries")
        if self.deprecated and not self.removal_date:
            raise ValueError("deprecated entry needs removal_date")
        return self

    @property
    def is_vm(self) -> bool:
        return self.runtime in VM_RUNTIMES


class ImageCatalog(_Strict):
    images: list[ImageEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross(self) -> "ImageCatalog":
        seen: set[tuple[str, str]] = set()
        vmids: dict[int, str] = {}
        by_runtime: dict[str, list[ImageEntry]] = {}
        for e in self.images:
            key = (e.runtime, e.id)
            if key in seen:
                raise ValueError(f"duplicate entry {e.runtime}/{e.id}")
            seen.add(key)
            if e.vmid is not None:
                if e.vmid in vmids:
                    raise ValueError(f"vmid {e.vmid} used by both {vmids[e.vmid]} and {e.id}")
                vmids[e.vmid] = e.id
            by_runtime.setdefault(e.runtime, []).append(e)
        eph = by_runtime.get("ephemeral", [])
        if len(eph) != 1:
            raise ValueError("exactly one ephemeral entry is required")
        for rt, entries in by_runtime.items():
            enabled = [e for e in entries if e.enabled]
            defaults = [e for e in enabled if e.default]
            if enabled and len(defaults) != 1:
                raise ValueError(f"runtime {rt} needs exactly one enabled default entry (found {len(defaults)})")
        return self

    def for_runtime(self, runtime: str) -> list[ImageEntry]:
        return [e for e in self.images if e.runtime == runtime]

    def default_for(self, runtime: str) -> ImageEntry | None:
        for e in self.images:
            if e.runtime == runtime and e.enabled and e.default:
                return e
        return None

    def get(self, runtime: str, version: str) -> ImageEntry | None:
        for e in self.images:
            if e.runtime == runtime and e.id == version:
                return e
        return None


# --------------------------------------------------------------------------- loading

def _format_errors(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        out.append(f"{loc}: {e['msg']}")
    return out


def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(str(path), [f"cannot read: {exc}"]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(str(path), [f"YAML parse error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ConfigError(str(path), ["top level must be a mapping"])
    return data


def load_atlas_config(path: str | Path) -> AtlasConfig:
    p = Path(path)
    try:
        return AtlasConfig.model_validate(_load_yaml(p))
    except ValidationError as exc:
        raise ConfigError(str(p), _format_errors(exc)) from exc


def load_image_catalog(path: str | Path, atlas: AtlasConfig | None = None) -> ImageCatalog:
    p = Path(path)
    try:
        cat = ImageCatalog.model_validate(_load_yaml(p))
    except ValidationError as exc:
        raise ConfigError(str(p), _format_errors(exc)) from exc
    if atlas is not None:
        problems = []
        if any(e.is_vm for e in cat.images) and atlas.proxmox is None:
            problems.append("images.yaml has linux/windows entries but atlas.yaml has no proxmox section")
        if any(e.runtime == "cape" and e.enabled for e in cat.images) and not atlas.cape.enabled:
            problems.append("images.yaml has an enabled cape entry but atlas.yaml cape.enabled is false")
        if problems:
            raise ConfigError(str(p), problems)
    return cat
