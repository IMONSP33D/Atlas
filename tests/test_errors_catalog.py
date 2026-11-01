"""docs/errors.md is the source of truth. The registry must match it exactly."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.errors import CATALOG, Action, AtlasError, Notice, warning

DOC = Path(__file__).resolve().parents[1] / "docs/errors.md"
_ROW = re.compile(r"^\|\s*`([A-Z0-9_]+)`\s*\|(.*)\|\s*$")
_ACTIONS = {a.value for a in Action}


def parse_doc() -> dict[str, dict]:
    """code -> {cls, http, action, warning}. Tolerates the differing column layouts per class."""
    out: dict[str, dict] = {}
    cls = None
    for line in DOC.read_text().splitlines():
        m = re.match(r"^## Class ([A-K])", line)
        if m:
            cls = m.group(1)
            continue
        if line.startswith("## Warnings"):
            cls = "W"
            continue
        m = _ROW.match(line)
        if not m or cls is None:
            continue
        code, rest = m.group(1), [c.strip() for c in m.group(2).split("|")]
        http = next((int(c) for c in rest if re.fullmatch(r"\d{3}", c)), None)
        action = next((c for c in rest if c in _ACTIONS), None)
        is_warning = cls == "W" or any("(warning)" in c for c in rest)
        if code in out:  # warnings table re-lists class E/I codes
            out[code]["warning"] = out[code]["warning"] or is_warning
            continue
        out[code] = {"cls": cls, "http": http, "action": action, "warning": is_warning}
    return out


DOC_CODES = parse_doc()


def test_doc_parsed_something():
    assert len(DOC_CODES) > 60


def test_every_doc_code_in_registry():
    missing = sorted(set(DOC_CODES) - set(CATALOG))
    assert not missing, f"in errors.md but not errors.py: {missing}"


def test_every_registry_code_in_doc():
    extra = sorted(set(CATALOG) - set(DOC_CODES))
    assert not extra, f"in errors.py but not errors.md: {extra}"


@pytest.mark.parametrize("code", sorted(DOC_CODES))
def test_fields_match(code: str):
    doc, reg = DOC_CODES[code], CATALOG[code]
    if doc["http"] is not None:
        assert reg.http == doc["http"], f"{code}: http {reg.http} != doc {doc['http']}"
    if doc["action"] is not None:
        assert reg.action.value == doc["action"], f"{code}: action {reg.action.value} != doc {doc['action']}"
    assert reg.warning == doc["warning"], f"{code}: warning flag"
    if doc["cls"] != "W":
        assert reg.cls == doc["cls"]


def test_warning_codes_are_200():
    for d in CATALOG.values():
        if d.warning:
            assert d.http == 200 and d.action == Action.NONE


def test_class_e_status_values():
    for d in CATALOG.values():
        if d.cls == "E" and not d.warning:
            assert d.exec_status in {"error", "timeout", "killed"}


def test_notice_shape():
    n = AtlasError("RATE_LIMITED", "slow down", retry_after_sec=3).notice.to_dict()
    assert n == {"code": "RATE_LIMITED", "message": "slow down", "data": {"retry_after_sec": 3},
                 "retryable": True, "suggested_action": "retry"}


def test_unknown_code_rejected():
    with pytest.raises(KeyError):
        AtlasError("MADE_UP_CODE")


def test_warning_helper_rejects_errors():
    with pytest.raises(ValueError):
        warning("AUTH_MISSING", "x")
    assert isinstance(warning("COLD_START", "x", boot_ms=1), Notice)
