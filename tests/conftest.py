from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atlas.api import create_app  # noqa: E402
from atlas.auth import hash_key  # noqa: E402
from atlas.backends.fake import FakeBackend  # noqa: E402
from atlas.config import load_atlas_config, load_image_catalog  # noqa: E402

TEST_KEY = "apple-pie-red-car-purple"
# Hashing is slow by design; do it once per test session.
_TEST_HASH = hash_key(TEST_KEY)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.base = datetime(2026, 11, 1, 18, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return self.base + timedelta(seconds=self.t - 1000.0)

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def example_configs(tmp_path: Path) -> tuple[Path, Path]:
    atlas = yaml.safe_load((ROOT / "config/atlas.example.yaml").read_text())
    atlas["auth"]["keys"][0]["hash"] = _TEST_HASH
    atlas["proxmox"]["token_secret"] = "x"
    atlas["cape"]["api_token"] = "x"
    a = tmp_path / "atlas.yaml"
    a.write_text(yaml.safe_dump(atlas))
    i = tmp_path / "images.yaml"
    i.write_text((ROOT / "config/images.example.yaml").read_text())
    return a, i


@pytest.fixture
def cfg(example_configs):
    return load_atlas_config(example_configs[0])


@pytest.fixture
def catalog(example_configs, cfg):
    return load_image_catalog(example_configs[1], cfg)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def app(cfg, catalog, backend, clock):
    backends = {rt: backend for rt in backend.runtimes}
    return create_app(cfg, catalog, backends, clock=clock, wall=clock.wall)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_KEY}"}
