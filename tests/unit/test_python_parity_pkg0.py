"""PKG-0: the three P0 wiring bugs that make Python silently wrong.

1. class_shapes base: libcst emits `bases` (list); the class-contract consumer
   read TS's `extends` (string), dropping the Python class base.
2. file-naming: the convention is derived for `.py` but the edit-time gate omitted
   `_PY_EXTENSIONS`, so it never fired.
3. vacuous-active calibration: phantom-import is declared language-independent but
   never fires for `.py`, so calibration certified it active-but-inert for a
   Python profile. It must report inert until a Python phantom-import lands.
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.conventions import extract_class_contract_conventions
from chameleon_mcp.enforcement_calibration import rule_inert_for_language
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.lint_engine import _file_naming_violations


def _pf(path: str, *, extras: dict) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras=extras,
    )


def test_python_class_contract_reads_base_from_bases_key():
    def f(i):
        cls = f"User{i}"
        return _pf(
            f"app/models{i}.py",
            extras={
                "class_shapes": [
                    {"name": cls, "decorators": ["dataclass"], "bases": ["BaseModel"]}
                ],
                "callable_signatures": [{"name": "save", "kind": "method", "enclosing_class": cls}],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="python")
    assert out.get("base") == "BaseModel"


def test_file_naming_fires_for_python_wrong_casing():
    fn = {"casing": "snake_case", "casing_consistency": 0.95, "sample_size": 20}
    v = _file_naming_violations("app/MyModel.py", fn)
    assert any(x.rule == "file-naming-convention-violation" for x in v)


def test_file_naming_clean_for_snake_python():
    fn = {"casing": "snake_case", "casing_consistency": 0.95, "sample_size": 20}
    assert _file_naming_violations("app/my_model.py", fn) == []


def test_file_naming_still_fires_for_typescript():
    # Regression guard: the gate widening must not change TS behavior.
    fn = {"casing": "snake_case", "casing_consistency": 0.95, "sample_size": 20}
    v = _file_naming_violations("src/MyThing.ts", fn)
    assert any(x.rule == "file-naming-convention-violation" for x in v)


def test_phantom_import_active_for_python_profile(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({"language": "python"}), encoding="utf-8")
    # PKG-3 implemented a Python phantom-import (relative-import resolution), so
    # the rule is now genuinely active for a Python profile, not inert.
    assert rule_inert_for_language("phantom-import", tmp_path) is False


def test_phantom_import_still_active_for_typescript_profile(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({"language": "typescript"}), encoding="utf-8")
    assert rule_inert_for_language("phantom-import", tmp_path) is False
