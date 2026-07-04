"""Inbound caller-contract section (#2): pre-edit "who breaks if you change this".

The counterpart to nearby-signatures: it reads the edited file's OWN exports
(symbol_signatures.json) and the reverse calls index (calls_index.json) and
renders the recorded call sites for each export, so cross-file staleness is
prevented at the edit instead of only detected at turn end. Default-on, bounded,
carries an honesty note, fails open.
"""

import json
from pathlib import Path

from chameleon_mcp.hook_helper import _inbound_contracts_section


def _write_profile(
    repo: Path, *, signatures: dict[str, dict], calls: dict[str, dict] | None
) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "symbol_signatures.json").write_text(
        json.dumps({"schema_version": 1, "files": signatures}), encoding="utf-8"
    )
    if calls is not None:
        (cham / "calls_index.json").write_text(
            json.dumps({"schema_version": 1, "callees": calls}), encoding="utf-8"
        )


def _sig(rel: str) -> dict:
    return {rel: {"getUser": {"params": [], "start_line": 1, "end_line": 2}}}


def _callers(*sites, total=None):
    # "import" is a real grade the calls-index loader keeps (VALID_GRADES).
    rows = [{"path": p, "caller": "c", "line": ln, "grade": "import"} for p, ln in sites]
    return {"callers": rows, "total": total if total is not None else len(rows), "truncated": False}


def test_default_on_renders_inbound_callers(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_INBOUND_CALLERS", raising=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "user.ts").write_text("export function getUser(){}\n", encoding="utf-8")
    _write_profile(
        tmp_path,
        signatures=_sig("src/user.ts"),
        calls={"src/user.ts": {"getUser": _callers(("src/order.ts", 3))}},
    )
    section = _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path)
    assert "Inbound callers of this file" in section
    assert "getUser() <- src/order.ts:3" in section
    assert "/chameleon-refresh" in section  # honesty note present


def test_kill_switch_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_INBOUND_CALLERS", "0")
    (tmp_path / "src").mkdir()
    _write_profile(
        tmp_path,
        signatures=_sig("src/user.ts"),
        calls={"src/user.ts": {"getUser": _callers(("src/order.ts", 3))}},
    )
    assert _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path) == ""


def test_no_callers_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_INBOUND_CALLERS", raising=False)
    (tmp_path / "src").mkdir()
    _write_profile(tmp_path, signatures=_sig("src/user.ts"), calls={"src/user.ts": {}})
    assert _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path) == ""


def test_no_calls_index_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_INBOUND_CALLERS", raising=False)
    (tmp_path / "src").mkdir()
    _write_profile(tmp_path, signatures=_sig("src/user.ts"), calls=None)
    assert _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path) == ""


def test_caller_sites_bounded_with_overflow_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_INBOUND_CALLERS", raising=False)
    monkeypatch.setenv("CHAMELEON_INBOUND_CALLERS_MAX_SITES", "2")
    (tmp_path / "src").mkdir()
    sites = [(f"src/c{i}.ts", i) for i in range(1, 6)]
    _write_profile(
        tmp_path,
        signatures=_sig("src/user.ts"),
        calls={"src/user.ts": {"getUser": _callers(*sites, total=5)}},
    )
    section = _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path)
    # Only 2 sites shown, with a "+3 more" overflow marker off the recorded total.
    assert section.count("src/c") == 2
    assert "(+3 more)" in section


def test_control_chars_in_path_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_INBOUND_CALLERS", raising=False)
    (tmp_path / "src").mkdir()
    _write_profile(
        tmp_path,
        signatures=_sig("src/user.ts"),
        calls={"src/user.ts": {"getUser": _callers(("src/or\nder.ts", 3))}},
    )
    section = _inbound_contracts_section(str(tmp_path / "src" / "user.ts"), tmp_path)
    # The newline must not split the listing (display sanitization).
    assert "\nder.ts" not in section
    assert "getUser()" in section
