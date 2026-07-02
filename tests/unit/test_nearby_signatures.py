"""Nearby collaborator signatures: default-on gating + call-proximity ranking.

The section reads the committed ``symbol_signatures.json`` for sibling contracts
and ranks candidates by whether the edited file actually calls into them (read
from the reverse ``calls_index.json``), falling back to deterministic name order
when no call facts are available. Advisory, offline, bounded, fail-open.
"""

import json
from pathlib import Path

from chameleon_mcp.hook_helper import _nearby_signatures_section


def _write_profile(
    repo: Path,
    *,
    signatures: dict[str, dict],
    calls: dict[str, dict] | None = None,
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
    # Write each sibling with a real def per symbol at its stored line: the
    # signature index is DERIVED from the file, and the per-edit re-verify drops a
    # symbol absent from the current source (a phantom). Fixtures must reflect that.
    for rel, by_name in signatures.items():
        fpath = repo / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        placed: dict[int, str] = {}
        overflow: list[str] = []
        for name, row in by_name.items():
            ln = row.get("start_line")
            defline = f"def {name}(): pass"
            if isinstance(ln, int) and ln >= 1 and ln not in placed:
                placed[ln] = defline
            else:
                overflow.append(defline)
        max_ln = max(placed) if placed else 0
        body = [placed.get(i + 1, "") for i in range(max_ln)] + overflow
        fpath.write_text("\n".join(body) + "\n", encoding="utf-8")


def _make_sources(repo: Path, names: list[str]) -> None:
    for name in names:
        (repo / name).write_text("# source\n", encoding="utf-8")


def _sig_row() -> dict:
    return {"params": [], "start_line": 1, "end_line": 2}


def test_default_on_renders_without_env(tmp_path, monkeypatch):
    """With the flag unset the section now renders (graduated to default-on)."""
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    _make_sources(tmp_path, ["target.py", "sibling.py"])
    _write_profile(tmp_path, signatures={"sibling.py": {"helper": _sig_row()}})

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert section, "default-on: section should render without the env flag set"
    assert "sibling.py" in section


def test_kill_switch_disables(tmp_path, monkeypatch):
    """CHAMELEON_NEARBY_SIGNATURES=0 still fully suppresses the section."""
    monkeypatch.setenv("CHAMELEON_NEARBY_SIGNATURES", "0")
    _make_sources(tmp_path, ["target.py", "sibling.py"])
    _write_profile(tmp_path, signatures={"sibling.py": {"helper": _sig_row()}})

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert section == ""


def test_proximity_ranks_called_sibling_first(tmp_path, monkeypatch):
    """A sibling the edited file calls outranks an alphabetically-earlier one.

    ``aaa.py`` sorts first by name but is uncalled; ``zzz.py`` is called by
    ``target.py`` per the reverse calls index, so it must appear first.
    """
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    _make_sources(tmp_path, ["target.py", "aaa.py", "zzz.py"])
    _write_profile(
        tmp_path,
        signatures={
            "aaa.py": {"helper_a": _sig_row()},
            "zzz.py": {"helper_z": _sig_row()},
        },
        calls={
            "zzz.py": {
                "helper_z": {
                    "callers": [
                        {
                            "path": "target.py",
                            "caller": "main",
                            "line": 5,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert "zzz.py" in section and "aaa.py" in section
    assert section.index("zzz.py") < section.index("aaa.py"), (
        "called sibling zzz.py should rank before uncalled aaa.py"
    )


def test_falls_back_to_name_order_without_calls_index(tmp_path, monkeypatch):
    """No calls_index.json: deterministic name order is preserved (no regression)."""
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    _make_sources(tmp_path, ["target.py", "aaa.py", "zzz.py"])
    _write_profile(
        tmp_path,
        signatures={
            "aaa.py": {"helper_a": _sig_row()},
            "zzz.py": {"helper_z": _sig_row()},
        },
        calls=None,
    )

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert section.index("aaa.py") < section.index("zzz.py")


def test_phantom_symbol_dropped_when_absent_from_checkout(tmp_path, monkeypatch):
    """A stored signature whose symbol is gone from the current file is NOT injected
    — the profile can be production-ref derived or predate a local delete, and the
    model must never be told to call a symbol the checkout no longer has."""
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    (tmp_path / "target.py").write_text("# t\n", encoding="utf-8")
    _write_profile(tmp_path, signatures={"sibling.py": {"gone": _sig_row()}})
    # The checkout no longer defines `gone`.
    (tmp_path / "sibling.py").write_text("def other():\n    pass\n", encoding="utf-8")

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert "gone" not in section
    assert section == ""  # nothing verifiable to render


def test_drifted_line_dropped_but_contract_kept(tmp_path, monkeypatch):
    """Symbol still present but moved off its stored line: keep the contract, drop
    the now-misleading :line."""
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    (tmp_path / "target.py").write_text("# t\n", encoding="utf-8")
    _write_profile(tmp_path, signatures={"sibling.py": {"helper": {"params": [], "start_line": 1}}})
    # helper now lives at line 3, not the stored line 1.
    (tmp_path / "sibling.py").write_text("\n\ndef helper():\n    pass\n", encoding="utf-8")

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert "helper()" in section  # contract retained
    assert "sibling.py" in section
    assert "sibling.py:" not in section  # the stale line is dropped, not shown wrong


def test_fresh_line_retained_when_symbol_on_stored_line(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAMELEON_NEARBY_SIGNATURES", raising=False)
    (tmp_path / "target.py").write_text("# t\n", encoding="utf-8")
    # _write_profile writes `def helper(): pass` at line 1, matching the stored line.
    _write_profile(tmp_path, signatures={"sibling.py": {"helper": {"params": [], "start_line": 1}}})

    section = _nearby_signatures_section(str(tmp_path / "target.py"), tmp_path)

    assert "sibling.py:1" in section  # verified line is retained
