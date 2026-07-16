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
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
    (cham / REVERSE_INDEX_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "targets": targets}),
        encoding="utf-8",
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


def test_out_breaks_collects_raw_record(tmp_path):
    # The Stop block branch consumes the raw (unsanitized) structured breaks via
    # out_breaks: name, target_key, kind, lang, ws_root, and raw importer sites.
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
    collected: list = []
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg(), out_breaks=collected
    )
    # Advisory output is unchanged by passing the collector.
    assert "oldName" in "\n".join(lines)
    assert len(collected) == 1
    rec = collected[0]
    assert rec["name"] == "oldName"
    assert rec["target_key"] == "src/pricing.ts"
    assert rec["kind"] == "export"
    assert rec["lang"] == "typescript"
    assert rec["importers"] == [("src/cart.ts", 1)]


def test_out_breaks_stays_empty_when_no_break(tmp_path):
    src = _touch(tmp_path, "p.ts", "export const keep = 1;\n")
    _touch(tmp_path, "c.ts", "import { keep } from './p';\nkeep();\n")
    _write_reverse_index(tmp_path, {"p.ts": {"keep": [{"path": "c.ts", "line": 1}]}})
    collected: list = []
    _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg(), out_breaks=collected
    )
    assert collected == []


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


def test_renamed_importer_whose_path_contains_the_name_is_not_flagged(tmp_path):
    # Regression: the removed export's name (`api`) is a bounded substring of its
    # own module path (`./api-client`). The importer fully renamed its reference
    # (import + usage), so it no longer uses `api` as code -- only inside the
    # import path string. The presence check must blank string literals, or a
    # clean rename refactor produces a phantom "you broke this call site".
    src = _touch(tmp_path, "src/api-client.ts", "export const apiClient = {};\n")
    _touch(
        tmp_path,
        "src/cart.ts",
        "import { apiClient } from './api-client';\napiClient.get();\n",
    )
    _write_reverse_index(
        tmp_path, {"src/api-client.ts": {"api": [{"path": "src/cart.ts", "line": 1}]}}
    )
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_genuine_break_from_module_whose_path_contains_the_name_still_flagged(tmp_path):
    # Complement: the importer STILL imports `api` from the module whose path
    # contains `api`. Blanking string literals must not suppress the real `{ api }`
    # binding -- it is code, not inside the path string -- so the break still fires.
    src = _touch(tmp_path, "src/api-client.ts", "export const other = 1;\n")
    _touch(tmp_path, "src/cart.ts", "import { api } from './api-client';\napi.get();\n")
    _write_reverse_index(
        tmp_path, {"src/api-client.ts": {"api": [{"path": "src/cart.ts", "line": 1}]}}
    )
    text = "\n".join(
        _crossfile_existence_advisory_lines(repo_root=tmp_path, state=_state_for([src]), cfg=_cfg())
    )
    assert "api" in text and "src/cart.ts:1" in text


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
    # A Ruby file in state must never be probed against the reverse index -- Ruby
    # has no reverse index, so it stays excluded even now that Python is allowed.
    src = _touch(tmp_path, "p.rb", "class Foo; end\n")
    _touch(tmp_path, "c.ts", "import { gone } from './p';\ngone();\n")
    _write_reverse_index(tmp_path, {"p.rb": {"gone": [{"path": "c.ts", "line": 1}]}})
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_flags_removed_python_export_with_live_importer(tmp_path):
    # A removed Python export with a live importer is flagged: the reverse index
    # covers Python, and the Python reader (not the TS regex) reads the live set.
    src = _touch(tmp_path, "pkg/pricing.py", "def edit_price():\n    pass\n")
    _touch(
        tmp_path,
        "pkg/cart.py",
        "from pkg.pricing import old_name\n\nold_name()\n",
    )
    _write_reverse_index(
        tmp_path,
        {
            "pkg/pricing.py": {
                "edit_price": [{"path": "pkg/cart.py", "line": 1}],
                "old_name": [{"path": "pkg/cart.py", "line": 1}],
            }
        },
    )
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    text = "\n".join(lines)
    assert "old_name" in text
    assert "pkg/cart.py:1" in text
    # The still-defined function is not a break.
    assert "edit_price" not in text


def test_python_still_exported_name_not_flagged(tmp_path):
    # When the Python module still defines the imported name, the Python reader
    # sees it and stays silent -- no false break. Guards against the TS regex
    # being used on a Python module (it would find zero exports and false-positive).
    src = _touch(tmp_path, "pkg/pricing.py", "def edit_price():\n    pass\n")
    _touch(
        tmp_path,
        "pkg/cart.py",
        "from pkg.pricing import edit_price\n\nedit_price()\n",
    )
    _write_reverse_index(
        tmp_path,
        {"pkg/pricing.py": {"edit_price": [{"path": "pkg/cart.py", "line": 1}]}},
    )
    lines = _crossfile_existence_advisory_lines(
        repo_root=tmp_path, state=_state_for([src]), cfg=_cfg()
    )
    assert lines == []


def test_python_star_import_open_set_skipped(tmp_path):
    # `from x import *` makes the Python export set unenumerable; skip rather than
    # claim a break, matching the TS `export *` stance.
    src = _touch(tmp_path, "pkg/barrel.py", "from pkg.other import *\n")
    _touch(
        tmp_path,
        "pkg/cart.py",
        "from pkg.barrel import maybe\n\nmaybe()\n",
    )
    _write_reverse_index(
        tmp_path,
        {"pkg/barrel.py": {"maybe": [{"path": "pkg/cart.py", "line": 1}]}},
    )
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
