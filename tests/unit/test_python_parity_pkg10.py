"""PKG-10: block-eligible rule-set parity audit for Python.

The audit conclusion is that prior packages already scoped the block rules
correctly for Python; these tests lock that scoping so a future edit can't
silently (a) drop Python from a rule it can block on, or (b) make a rule
block-eligible for Python that would false-fire on idiomatic code.
"""

from __future__ import annotations

import json

from chameleon_mcp.enforcement_calibration import rule_inert_for_language
from chameleon_mcp.lint_engine import _file_naming_violations
from chameleon_mcp.violation_class import (
    BLOCK_ELIGIBLE_RULES,
    BLOCK_RULE_LANGUAGES,
    is_hard_class,
)


def test_block_rule_languages_python_scoping():
    # Rules Python can genuinely block on.
    assert "python" in BLOCK_RULE_LANGUAGES["phantom-import"]
    assert "python" in BLOCK_RULE_LANGUAGES["naming-convention-violation"]
    # Language-independent rules (None) cover Python by definition.
    assert BLOCK_RULE_LANGUAGES["file-naming-convention-violation"] is None
    assert BLOCK_RULE_LANGUAGES["import-preference-violation"] is None
    assert BLOCK_RULE_LANGUAGES["secret-detected-in-content"] is None
    assert BLOCK_RULE_LANGUAGES["eval-call"] is None
    # Rules that must NOT block for Python: no Python JSX, and Python
    # inheritance is advisory only (kept off the block path deliberately).
    assert "python" not in BLOCK_RULE_LANGUAGES["jsx-presence-mismatch"]
    assert "python" not in (BLOCK_RULE_LANGUAGES["inheritance-convention-violation"] or frozenset())


def _python_profile(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({"language": "python"}), encoding="utf-8")
    return tmp_path


def test_rule_inert_for_python_profile(tmp_path):
    p = _python_profile(tmp_path)
    # Active (not inert) for a Python profile.
    assert rule_inert_for_language("phantom-import", p) is False
    assert rule_inert_for_language("naming-convention-violation", p) is False
    assert rule_inert_for_language("file-naming-convention-violation", p) is False
    assert rule_inert_for_language("eval-call", p) is False
    # Inert (cannot block) for a Python profile.
    assert rule_inert_for_language("jsx-presence-mismatch", p) is True
    assert rule_inert_for_language("inheritance-convention-violation", p) is True


def test_file_naming_dunder_files_exempt():
    # The landmine: file-naming is block-eligible, so a real BLOCK on a dunder
    # file would be a trust-killer. __init__.py / __main__.py / conftest.py carry
    # no casing signal and must never be flagged, under any convention.
    for casing in ("snake_case", "kebab", "PascalCase"):
        fn = {"casing": casing, "casing_consistency": 0.95, "sample_size": 20}
        for special in ("pkg/__init__.py", "pkg/__main__.py", "pkg/conftest.py", "pkg/_private.py"):
            assert _file_naming_violations(special, fn) == [], (special, casing)


def test_file_naming_real_violation_is_block_eligible():
    # A genuine wrong-cased Python file IS a hard-class (block-eligible) violation,
    # so the rule isn't merely advisory for Python.
    fn = {"casing": "snake_case", "casing_consistency": 0.95, "sample_size": 20}
    v = _file_naming_violations("app/MyModel.py", fn)
    assert v and v[0].rule == "file-naming-convention-violation"
    assert "file-naming-convention-violation" in BLOCK_ELIGIBLE_RULES
    assert is_hard_class(v[0].to_dict()) is True
