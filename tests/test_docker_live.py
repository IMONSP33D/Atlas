"""Real-Docker smoke test. Skips unless the daemon is up and the image exists.

Image defaults to atlas-ephemeral:dev (scripts/build-ephemeral.sh); override with ATLAS_TEST_IMAGE.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from atlas.backends.base import ExecSpec
from atlas.backends.docker import DockerBackend
from atlas.config import EphemeralConfig, ImageEntry, LimitsConfig
from atlas.validation import NetworkPolicy

IMAGE = os.environ.get("ATLAS_TEST_IMAGE", "atlas-ephemeral:dev")


def _ok(*argv: str) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    not (_ok("docker", "version", "--format", "{{.Server.Version}}") and _ok("docker", "image", "inspect", IMAGE)),
    reason=f"docker daemon or image {IMAGE} unavailable",
)


async def test_real_python_hello():
    entry = ImageEntry(runtime="ephemeral", id="default", build=IMAGE, default=True, languages=["python"])
    b = DockerBackend(EphemeralConfig(image=IMAGE), LimitsConfig())
    assert await b.entry_state(entry) == "ready"
    h = await b.open(entry, NetworkPolicy(enabled=False))
    try:
        res = await b.execute(h, ExecSpec("python", "print('hello')", None, [], {}, None, 30, []))
    finally:
        await b.close(h)
    assert res.status == "ok", (res.stdout, res.stderr, res.errors)
    assert res.stdout == "hello\n" and res.exit_code == 0
