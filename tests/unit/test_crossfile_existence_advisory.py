"""Tests for the Stop-time cross-file existence-break advisory in hook_helper.

These drive ``_crossfile_existence_advisory_lines`` directly with a minimal
config and a real EnforcementState pointing at TypeScript files on disk, plus a
committed reverse index. The builder reuses the persisted index and a regex
presence check (no parse at Stop), so the test only needs files on disk and the
index artifact -- no extractor subprocess, no daemon.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chameleon_mcp.enforcement import EnforcementState, FileState
from chameleon_mcp.hook_helper import _crossfile_existence_advisory_lines
from chameleon_mcp.symbol_index import REVERSE_INDEX_FILENAME, SCHEMA_VERSION


def _cfg(mode="shadow", crossfile_existence_advisory=True):
    return SimpleNamespace(mode=mode, crossfile_existence_advisory=crossfile_existence_advisory)


def _state_for(paths: list[Path]) -> EnforcementState:
    st = EnforcementState()
    for p in paths:
        st.files[str(p)] = FileState()
    return st


def _touch(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _write_reverse_index(root: Path, targets: dict) -> None:
    cham = root / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / REVERSE_INDEX_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "targets": targets}), encoding="utf-8"
    )


def test_flags_removed_export_with_live_importer(tmp_path):
    src = _touch(tmp_path, "src/pricing.ts", "export function editPrice() {}\n")
    _touch(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _write_reverse_index(
        tmp_path,
        {
            "src/pricing.ts": {
                "editPrice": [{"path": "src/cart.ts", "line": 1}],
                "oldName": [{"path": "src/cart.ts", "line": 1}],
            }
        },
    )
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    text = "\n".join(lines)
    assert "oldName" in text
    assert "src/cart.ts:1" in text
    # The still-exported binding is not a break.
    assert "editPrice" not in text


def test_off_mode_emits_nothing(tmp_path):
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    _touch(tmp_path, "c.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(tmp_path, {"p.ts": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg(mode="off")
    )
    assert lines == []


def test_disabled_flag_emits_nothing(tmp_path):
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    _touch(tmp_path, "c.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(tmp_path, {"p.ts": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path,
        state=_state_for([src]),
        cfg=_cfg(crossfile_existence_advisory=False),
    )
    assert lines == []


def test_no_index_fails_open(tmp_path):
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_importer_that_dropped_the_name_is_not_flagged(tmp_path):
    # The export is gone, but the importer no longer references it either (the
    # rename was completed there too), so the presence check fails -> no advisory.
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    _touch(tmp_path, "c.ts", "import { keep } from './p';\nkeep;\n")
    _write_reverse_index(tmp_path, {"p.ts": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_open_export_set_is_skipped(tmp_path):
    # `export * from` makes the set unenumerable; a missing name may be re-exported.
    src = _touch(tmp_path, "barrel.ts", "export * from './other';\n")
    _touch(tmp_path, "c.ts", "import { maybe } from './barrel';\nmaybe();\n")
    _write_reverse_index(tmp_path, {"barrel.ts": {"maybe": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_inline_ignore_directive_opts_out(tmp_path):
    src = _touch(
        tmp_path,
        "p.ts",
        "// chameleon-ignore removed-export-breaks-importers\nexport const keep = 1;\n",
    )
    _touch(tmp_path, "c.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(tmp_path, {"p.ts": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_non_typescript_file_skipped(tmp_path):
    # A Ruby file in state must never be probed against the TS reverse index.
    src = _touch(tmp_path, "p.rb", "class Foo; end\n")
    _touch(tmp_path, "c.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(tmp_path, {"p.rb": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_site_cap_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_CROSSFILE_MAX_SITES_PER_FINDING", "1")
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    _touch(tmp_path, "a.ts", "import { gone } from './p';\ngone();\n")
    _touch(tmp_path, "b.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(
        tmp_path,
        {
            "p.ts": {
                "gone": [
                    {"path": "a.ts", "line": 1},
                    {"path": "b.ts", "line": 1},
                ]
            }
        },
    )
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    text = "\n".join(lines)
    assert "gone" in text
    # One site shown plus the "..." more marker; the other importer is elided.
    assert "..." in text
