"""Class B / C validation. Produces a ResolvedExecute the scheduler and backends consume.

Order matters: the first failing rule is what the agent sees, so cheap and
structural checks come before catalog lookups and file decoding.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field

from .config import AtlasConfig, ImageCatalog, ImageEntry, VM_RUNTIMES
from .errors import AtlasError, Notice, warning
from .models import ExecuteRequest, FileIn, OpenSessionRequest

RUNTIMES = ("ephemeral", "linux", "windows", "cape")
MODES = ("execute", "session")
SESSION_ID_RE = re.compile(r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")  # ses_ + Crockford base32 ULID
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)*[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE
)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._ -]{0,254}$")


@dataclass(frozen=True)
class NetworkPolicy:
    enabled: bool
    allow: tuple[str, ...] = ()

    def to_dict(self, requested: "NetworkPolicy | None" = None) -> dict:
        req = requested or self
        return {"requested": req.enabled, "effective": self.enabled, "allow": list(self.allow)}


@dataclass
class DecodedFile:
    name: str
    content: bytes
    sha256: str


@dataclass
class ResolvedExecute:
    req: ExecuteRequest
    entry: ImageEntry
    mode: str
    timeout_sec: int
    network: NetworkPolicy
    files: list[DecodedFile]
    warnings: list[Notice] = field(default_factory=list)


# --------------------------------------------------------------------------- pieces

def parse_network(spec: bool | dict) -> NetworkPolicy:
    if spec is False:
        return NetworkPolicy(False)
    if spec is True:
        return NetworkPolicy(True)
    if isinstance(spec, dict):
        if set(spec) != {"allow"} or not isinstance(spec["allow"], list):
            raise AtlasError("NETWORK_SPEC_INVALID", "network must be true, false, or {\"allow\": [hostnames]}")
        hosts: list[str] = []
        for h in spec["allow"]:
            if not isinstance(h, str) or not _HOSTNAME_RE.match(h) or h.replace(".", "").isdigit():
                raise AtlasError("NETWORK_SPEC_INVALID", f"allowlist entry is not a hostname: {h!r}", entry=h)
            hosts.append(h.lower())
        if not hosts:
            raise AtlasError("NETWORK_SPEC_INVALID", "allowlist is empty; use network: false instead")
        return NetworkPolicy(True, tuple(sorted(set(hosts))))
    raise AtlasError("NETWORK_SPEC_INVALID", "network must be true, false, or {\"allow\": [hostnames]}")


def resolve_entry(catalog: ImageCatalog, runtime: str, version: str | None, warnings: list[Notice]) -> ImageEntry:
    if runtime not in RUNTIMES:
        raise AtlasError("RUNTIME_UNKNOWN", f"runtime must be one of {list(RUNTIMES)}", runtime=runtime)
    if runtime == "ephemeral":
        version = None  # ignored per contract
    available = [e.id for e in catalog.for_runtime(runtime) if e.enabled]
    if version is None:
        entry = catalog.default_for(runtime)
        if entry is None:
            raise AtlasError("VM_UNAVAILABLE", f"no enabled {runtime} entry in catalog", reason="no_default")
        if runtime != "ephemeral":
            warnings.append(warning("VERSION_DEFAULTED", f"version omitted; using {entry.id}", version=entry.id))
    else:
        entry = catalog.get(runtime, version)
        if entry is None:
            raise AtlasError("VERSION_UNKNOWN", f"unknown {runtime} version {version!r}", available=available)
        if not entry.enabled:
            raise AtlasError("VM_UNAVAILABLE", f"{runtime}/{version} is disabled in config", reason="disabled")
    if entry.deprecated:
        warnings.append(warning("DEPRECATED_VERSION", f"{entry.id} is scheduled for removal",
                                removal_date=entry.removal_date))
    return entry


def validate_filename(name: str) -> None:
    if not name or not _FILENAME_RE.match(name) or name in (".", ".."):
        raise AtlasError("FILE_NAME_INVALID", f"invalid file name {name!r}", name=name)
    if "/" in name or "\\" in name or ":" in name:
        raise AtlasError("FILE_NAME_INVALID", f"file name must be a bare name, not a path: {name!r}", name=name)
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise AtlasError("FILE_NAME_INVALID", f"reserved name {name!r}", name=name)


def decode_files(files: list[FileIn], cfg: AtlasConfig) -> list[DecodedFile]:
    if len(files) > cfg.limits.max_files:
        raise AtlasError("FILE_COUNT_EXCEEDED", f"{len(files)} files > limit {cfg.limits.max_files}",
                         count=len(files), limit=cfg.limits.max_files)
    seen: set[str] = set()
    out: list[DecodedFile] = []
    total = 0
    for f in files:
        validate_filename(f.name)
        if f.name in seen:
            raise AtlasError("FILE_NAME_DUPLICATE", f"duplicate file name {f.name!r}", name=f.name)
        seen.add(f.name)
        # Cheap pre-check before decoding: 4 b64 chars -> 3 bytes.
        total_est = total + (len(f.content_b64) * 3) // 4
        if total_est > cfg.limits.max_upload_bytes + 3 * cfg.limits.max_files:
            raise AtlasError("FILE_TOO_LARGE", "decoded files exceed upload limit",
                             bytes=total_est, limit=cfg.limits.max_upload_bytes)
        try:
            content = base64.b64decode(f.content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise AtlasError("FILE_B64_INVALID", f"{f.name}: content_b64 is not valid base64", name=f.name)
        total += len(content)
        if total > cfg.limits.max_upload_bytes:
            raise AtlasError("FILE_TOO_LARGE", "decoded files exceed upload limit",
                             bytes=total, limit=cfg.limits.max_upload_bytes)
        digest = hashlib.sha256(content).hexdigest()
        if f.sha256 is not None and f.sha256.lower() != digest:
            raise AtlasError("FILE_HASH_MISMATCH", f"{f.name}: sha256 mismatch",
                             name=f.name, expected=f.sha256.lower(), actual=digest)
        out.append(DecodedFile(f.name, content, digest))
    return out


# --------------------------------------------------------------------------- whole requests

def validate_execute(req: ExecuteRequest, cfg: AtlasConfig, catalog: ImageCatalog) -> ResolvedExecute:
    warnings: list[Notice] = []

    if req.mode not in MODES:
        raise AtlasError("MODE_INVALID", f"mode must be one of {list(MODES)}", mode=req.mode)
    if req.session_id is not None and not SESSION_ID_RE.match(req.session_id):
        raise AtlasError("SESSION_ID_INVALID", "session_id is malformed", session_id=req.session_id)

    entry = resolve_entry(catalog, req.runtime, req.version, warnings)
    if entry.runtime == "cape":
        raise AtlasError("MODE_INVALID", "cape runtime is driven via /v1/cape/submit, not /v1/execute")
    if entry.runtime not in VM_RUNTIMES and (req.mode == "session" or req.session_id):
        raise AtlasError("MODE_INVALID", f"sessions are not available on runtime {entry.runtime!r}")

    if req.language not in entry.languages:
        raise AtlasError("LANGUAGE_UNSUPPORTED", f"{req.language!r} not available on {entry.runtime}/{entry.id}",
                         available=entry.languages)

    if req.code is not None and req.entrypoint is not None:
        raise AtlasError("CODE_AND_ENTRYPOINT", "provide code or entrypoint, not both")
    if req.code is None and req.entrypoint is None:
        raise AtlasError("CODE_MISSING", "provide code or entrypoint")
    if req.language == "binary" and req.entrypoint is None:
        raise AtlasError("CODE_MISSING", "language 'binary' requires entrypoint naming an uploaded file")

    timeout = req.timeout_sec if req.timeout_sec is not None else cfg.limits.default_timeout_sec
    if not (1 <= timeout <= cfg.limits.max_timeout_sec):
        raise AtlasError("TIMEOUT_OUT_OF_RANGE", f"timeout_sec must be 1..{cfg.limits.max_timeout_sec}",
                         timeout_sec=timeout, max=cfg.limits.max_timeout_sec)

    network = parse_network(req.network)

    if req.screenshot.requested and entry.runtime == "ephemeral":
        raise AtlasError("SCREENSHOT_UNSUPPORTED", "screenshots are only available on VM runtimes")

    files = decode_files(req.files, cfg)
    if req.entrypoint is not None and req.entrypoint not in {f.name for f in files}:
        raise AtlasError("ENTRYPOINT_NOT_FOUND", f"entrypoint {req.entrypoint!r} is not among uploaded files",
                         entrypoint=req.entrypoint, files=[f.name for f in files])

    return ResolvedExecute(req, entry, req.mode, timeout, network, files, warnings)


def validate_open_session(req: OpenSessionRequest, catalog: ImageCatalog) -> tuple[ImageEntry, NetworkPolicy, list[Notice]]:
    warnings: list[Notice] = []
    entry = resolve_entry(catalog, req.runtime, req.version, warnings)
    if entry.runtime not in VM_RUNTIMES:
        raise AtlasError("MODE_INVALID", f"sessions are not available on runtime {entry.runtime!r}")
    return entry, parse_network(req.network), warnings
