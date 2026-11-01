"""`atlas` command line.

    atlas config check  [--config atlas.yaml] [--images images.yaml]
    atlas serve         [--config ...] [--images ...] [--fake]
    atlas keygen --name perseus-main
    atlas images validate [--config ...] [--images ...]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import AtlasConfig, ConfigError, ImageCatalog, load_atlas_config, load_image_catalog


def _load(args: argparse.Namespace) -> tuple[AtlasConfig | None, ImageCatalog | None, ConfigError | None]:
    try:
        cfg = load_atlas_config(args.config)
        cat = load_image_catalog(args.images, cfg)
        return cfg, cat, None
    except ConfigError as exc:
        return None, None, exc


def cmd_check(args: argparse.Namespace) -> int:
    cfg, cat, err = _load(args)
    if err:
        print(f"CONFIG_INVALID: {err.path}", file=sys.stderr)
        for p in err.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    assert cfg and cat
    print(f"atlas.yaml ok: {len(cfg.auth.keys)} key(s), max_sessions={cfg.limits.max_sessions}")
    print(f"images.yaml ok: {len(cat.images)} entries")
    for e in cat.images:
        flags = ("default " if e.default else "") + ("warm " if e.warm else "") + ("" if e.enabled else "DISABLED ")
        print(f"  {e.runtime:9} {e.id:14} build={e.build} {flags}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import create_app

    logging.basicConfig(level=logging.INFO)
    cfg, cat, err = _load(args)
    backends = {}
    if args.fake:
        from .backends.fake import FakeBackend
        fb = FakeBackend()
        backends = {rt: fb for rt in fb.runtimes}
    else:
        # Only the ephemeral (Docker) backend exists so far; VM runtimes stay unregistered.
        if cfg is None:
            print("real backends need a valid config; fix it or run with --fake", file=sys.stderr)
            return 2
        from .backends.docker import DockerBackend
        backends = {"ephemeral": DockerBackend(cfg.ephemeral, cfg.limits)}
    if err:
        print(f"CONFIG_INVALID: {err}; serving /v1/health only", file=sys.stderr)
    app = create_app(cfg, cat, backends, config_error=err)
    bind, port = ("127.0.0.1", 8443) if cfg is None else (cfg.server.bind, cfg.server.port)
    ssl = {}
    if cfg and cfg.server.tls and not args.no_tls:
        ssl = {"ssl_certfile": str(cfg.server.tls.cert), "ssl_keyfile": str(cfg.server.tls.key)}
    uvicorn.run(app, host=bind, port=port, **ssl)
    return 0


def cmd_images_validate(args: argparse.Namespace, backend=None) -> int:
    """Run ProxmoxBackend.validate_entry for every enabled VM entry; exit 1 on any problem."""
    import asyncio

    cfg, cat, err = _load(args)
    if err:
        print(f"CONFIG_INVALID: {err.path}", file=sys.stderr)
        for p in err.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    assert cfg and cat
    entries = [e for e in cat.images if e.is_vm and e.enabled]
    if not entries:
        print("no enabled linux/windows entries")
        return 0
    if backend is None:
        from .backends.proxmox import ProxmoxBackend
        backend = ProxmoxBackend(cfg.proxmox, cfg.limits, cfg.screenshots, entries,
                                 host_reserve_mb=cfg.network.host_reserve_mb)

    async def run() -> list[tuple]:
        return [(e, await backend.validate_entry(e), await backend.entry_state(e)) for e in entries]

    from .errors import AtlasError
    try:
        rows = asyncio.run(run())
    except AtlasError as exc:
        print(f"{exc.code}: {exc.notice.message}", file=sys.stderr)
        return 1
    print(f"{'RUNTIME':8} {'ID':14} {'VMID':>5} {'STATE':12} RESULT")
    bad = 0
    for e, problems, state in rows:
        print(f"{e.runtime:8} {e.id:14} {e.vmid:>5} {state:12} {'ok' if not problems else 'FAIL'}")
        for p in problems:
            print(f"{'':42}- {p}")
        bad += bool(problems)
    print(f"\n{len(rows) - bad}/{len(rows)} entries ready")
    return 1 if bad else 0


def cmd_keygen(args: argparse.Namespace) -> int:
    import secrets
    from .auth import hash_key
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import importlib
        kg = importlib.import_module("atlas-keygen")  # type: ignore
        words = kg.load_wordlist(args.wordlist)
    except Exception:
        words = None
    if not words:
        print("wordlist unavailable; pass --wordlist", file=sys.stderr)
        return 1
    key = "-".join(secrets.choice(words) for _ in range(args.words))
    print(f"\n=== Atlas API key (shown once) ===\n\n  {key}\n")
    print("=== Paste into atlas.yaml under auth.keys ===\n")
    print(f"  - name: {args.name}\n    hash: \"{hash_key(key)}\"\n    revoked: false\n    rate_limit_per_min: 120\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default="/etc/atlas/atlas.yaml")
        p.add_argument("--images", default="/etc/atlas/images.yaml")

    p = sub.add_parser("config"); s2 = p.add_subparsers(dest="sub", required=True)
    pc = s2.add_parser("check"); common(pc); pc.set_defaults(fn=cmd_check)

    ps = sub.add_parser("serve"); common(ps)
    ps.add_argument("--fake", action="store_true", help="scripted backend, no infra")
    ps.add_argument("--no-tls", action="store_true")
    ps.set_defaults(fn=cmd_serve)

    pi = sub.add_parser("images"); s3 = pi.add_subparsers(dest="sub", required=True)
    piv = s3.add_parser("validate"); common(piv); piv.set_defaults(fn=cmd_images_validate)

    pk = sub.add_parser("keygen")
    pk.add_argument("--name", required=True)
    pk.add_argument("--words", type=int, default=5, choices=range(3, 6))
    pk.add_argument("--wordlist")
    pk.set_defaults(fn=cmd_keygen)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
