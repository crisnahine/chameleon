# tests/unit/test_prewrite_lint.py
from chameleon_mcp.prewrite_lint import banned_imports_in_content

CONV = {"imports": {"component": {"competing": [{"over": "lodash", "preferred": "lodash-es"}]}}}


def test_banned_import_detected():
    out = banned_imports_in_content(
        "import _ from 'lodash'\n",
        language="typescript",
        archetype="component",
        conventions=CONV,
    )
    assert any(v["rule"] == "import-preference-violation" for v in out)


def test_preferred_already_present_no_violation():
    src = "import _ from 'lodash-es'\nimport x from 'lodash'\n"
    out = banned_imports_in_content(
        src, language="typescript", archetype="component", conventions=CONV
    )
    assert out == []


def test_import_inside_string_ignored():
    src = "const code = \"import _ from 'lodash'\"\n"
    out = banned_imports_in_content(
        src, language="typescript", archetype="component", conventions=CONV
    )
    assert out == []


def test_no_rules_no_violation():
    out = banned_imports_in_content(
        "import _ from 'lodash'\n",
        language="typescript",
        archetype="component",
        conventions={},
    )
    assert out == []
