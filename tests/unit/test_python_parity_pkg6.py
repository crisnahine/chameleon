"""PKG-6: Python identifier naming-convention (PEP 8) derive + lint + calibrate.

Functions/methods snake_case, classes PascalCase. Reuses the Ruby casing
classifiers (the rules coincide) so what's measured is what's checked.
"""

from __future__ import annotations

from chameleon_mcp.conventions import (
    extract_declarations_from_content,
    extract_naming_conventions,
)
from chameleon_mcp.lint_engine import lint_conventions
from chameleon_mcp.violation_class import BLOCK_RULE_LANGUAGES


def test_declarations_extracted_for_python():
    src = "def fetch_data():\n    pass\n\n\nclass UserService:\n    def run(self):\n        pass\n"
    decls = extract_declarations_from_content(src, language="python")
    assert "fetch_data" in decls["method"]
    assert "run" in decls["method"]
    assert "UserService" in decls["class"]


def test_naming_convention_derived_snake_methods():
    # 12 snake functions -> a snake_case method convention.
    decls = {"method": [f"do_thing_{i}" for i in range(12)]}
    conv = extract_naming_conventions(declarations=decls)
    assert conv.get("method_casing", {}).get("pattern") == "snake_case"


_SNAKE_NAMING = {"naming": {"method_casing": {"pattern": "snake_case", "consistency": 0.95}}}
_PASCAL_NAMING = {"naming": {"class_casing": {"pattern": "PascalCase", "consistency": 0.95}}}


def test_lint_flags_non_snake_function():
    v = lint_conventions("def FetchData():\n    pass\n", _SNAKE_NAMING, language="python")
    assert any(x.rule == "naming-convention-violation" and x.actual == "FetchData" for x in v)


def test_lint_clean_for_snake_function():
    v = lint_conventions("def fetch_data():\n    pass\n", _SNAKE_NAMING, language="python")
    assert not any(x.rule == "naming-convention-violation" for x in v)


def test_lint_flags_non_pascal_class():
    v = lint_conventions("class user_service:\n    pass\n", _PASCAL_NAMING, language="python")
    assert any(x.rule == "naming-convention-violation" and x.actual == "user_service" for x in v)


def test_naming_block_eligible_for_python():
    assert "python" in (BLOCK_RULE_LANGUAGES.get("naming-convention-violation") or set())
