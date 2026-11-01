from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from atlas.config import ConfigError, load_atlas_config, load_image_catalog


def test_examples_load(cfg, catalog):
    assert cfg.limits.max_sessions == 2
    assert cfg.limits.exclusive_with("windows") == {"linux"}
    assert catalog.default_for("windows").id == "win11-24h2"
    assert catalog.get("windows", "win10-22h2").warm is False
    assert catalog.get("linux", "ubuntu-24.04").is_vm


def test_shipped_example_rejects_placeholder_hash(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ConfigError) as ei:
        load_atlas_config(root / "config/atlas.example.yaml")
    assert any("REPLACE_ME" in p for p in ei.value.problems)


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


def _mutate_images(example_configs, tmp_path, fn):
    data = yaml.safe_load(example_configs[1].read_text())
    fn(data)
    return _write(tmp_path, "images2.yaml", data)


@pytest.mark.parametrize("mutator,needle", [
    (lambda d: d["images"][1].__setitem__("default", False), "exactly one enabled default"),
    (lambda d: d["images"][2].__setitem__("default", True), "exactly one enabled default"),
    (lambda d: d["images"][2].__setitem__("vmid", 201), "vmid 201 used by both"),
    (lambda d: d["images"][0].__setitem__("warm", True), "may not set"),
    (lambda d: d["images"][1].pop("vmid"), "needs vmid"),
    (lambda d: d["images"][4].pop("cape_machine_tag"), "needs cape_machine_tag"),
    (lambda d: d["images"][1].__setitem__("languages", ["python", "cobol"]), "unknown languages"),
    (lambda d: d["images"][1].__setitem__("bogus", 1), "Extra inputs"),
    (lambda d: d["images"].append(dict(d["images"][1])), "duplicate entry"),
    (lambda d: d["images"].pop(0), "exactly one ephemeral"),
    (lambda d: d["images"][2].update(deprecated=True), "needs removal_date"),
])
def test_images_cross_checks(example_configs, tmp_path, mutator, needle):
    p = _mutate_images(example_configs, tmp_path, mutator)
    with pytest.raises(ConfigError) as ei:
        load_image_catalog(p)
    assert any(needle in msg for msg in ei.value.problems), ei.value.problems


def test_images_require_proxmox_section(example_configs, tmp_path):
    data = yaml.safe_load(example_configs[0].read_text())
    data.pop("proxmox")
    cfg = load_atlas_config(_write(tmp_path, "a.yaml", data))
    with pytest.raises(ConfigError) as ei:
        load_image_catalog(example_configs[1], cfg)
    assert "no proxmox section" in ei.value.problems[0]


@pytest.mark.parametrize("mutator,needle", [
    (lambda d: d["limits"].__setitem__("session_idle_warn_sec", 900), "must be <"),
    (lambda d: d["limits"].__setitem__("default_timeout_sec", 901), "default_timeout_sec >"),
    (lambda d: d["limits"].__setitem__("exclusive_runtimes", [["windows", "cape"]]), "pairs of VM runtimes"),
    (lambda d: d["auth"]["keys"].append(dict(d["auth"]["keys"][0])), "duplicate key name"),
    (lambda d: d["cape"].__setitem__("api_token", None), "requires api_base and api_token"),
    (lambda d: d.__setitem__("typo", {}), "Extra inputs"),
])
def test_atlas_cross_checks(example_configs, tmp_path, mutator, needle):
    data = yaml.safe_load(example_configs[0].read_text())
    mutator(data)
    with pytest.raises(ConfigError) as ei:
        load_atlas_config(_write(tmp_path, "a.yaml", data))
    assert any(needle in msg for msg in ei.value.problems), ei.value.problems


def test_bad_yaml(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text("server: [unclosed")
    with pytest.raises(ConfigError) as ei:
        load_atlas_config(p)
    assert "YAML parse error" in ei.value.problems[0]
