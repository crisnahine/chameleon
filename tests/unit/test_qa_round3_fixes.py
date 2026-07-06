"""Regression pins for the round-3 hostile-QA fixes (v2.53.0).

Each test is red before its fix and green after. Pure-function pins for the
deterministic fixes; the cross-file/index and Ruby-AST fixes are additionally
verified live against real repos during QA.
"""

from __future__ import annotations

from chameleon_mcp.bootstrap.orchestrator import _classify_framework
from chameleon_mcp.enforcement import _coerce_nonneg_int
from chameleon_mcp.hook_helper import _reference_present
from chameleon_mcp.judge import _extract_json_array
from chameleon_mcp.lint_engine import _python_guard_violations
from chameleon_mcp.phantom_imports import _py_imported_names
from chameleon_mcp.signature_diff import diff_file_contracts


class TestReferencePresentMultiline:
    """#1 (BLOCK): a removed export surviving only inside a multi-line comment or
    template literal must NOT read as a live reference (a false cross-file break /
    false deny), while a genuine code reference still does."""

    def test_name_in_multiline_block_comment_is_not_live(self):
        txt = 'import {a} from "x";\n/* provider:\n   uauthService here\n   */\nconst z = 1;\n'
        assert _reference_present(txt, "uauthService", None, "typescript") is False
        # the recorded import line lands inside the block comment
        assert _reference_present(txt, "uauthService", 3, "typescript") is False

    def test_name_in_multiline_template_literal_is_not_live(self):
        txt = "const q = `\n  uses uauthService here\n`;\nexport const y = 2;\n"
        assert _reference_present(txt, "uauthService", 2, "typescript") is False

    def test_genuine_code_reference_is_live(self):
        txt = 'import {uauthService} from "x";\nconst r = uauthService.findAll();\n'
        assert _reference_present(txt, "uauthService", 2, "typescript") is True


class TestFrameworkDjangoMisdetect:
    """#8: a bare `manage.py` (no Django content, no django dep) must not force
    'django'; a real Flask dep wins even when a manage.py is present."""

    def test_flask_dep_wins_over_bare_manage_py(self, tmp_path):
        (tmp_path / "manage.py").write_text("import click\n", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("Flask==3.0\nFlask-Login\n", encoding="utf-8")
        assert _classify_framework(tmp_path, "python") == "flask"

    def test_bare_manage_py_without_django_is_not_django(self, tmp_path):
        (tmp_path / "manage.py").write_text("import click\nprint('run')\n", encoding="utf-8")
        assert _classify_framework(tmp_path, "python") != "django"

    def test_real_django_manage_py_still_detected(self, tmp_path):
        (tmp_path / "manage.py").write_text(
            "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')\n",
            encoding="utf-8",
        )
        assert _classify_framework(tmp_path, "python") == "django"


class TestJudgeArrayDecoy:
    """#9: a valid-JSON object-array decoy emitted BEFORE the real findings array
    must not shadow it -- the parser keeps the LAST object-containing array."""

    def test_last_object_array_wins_over_earlier_decoy(self):
        text = (
            'reasoning: [{"note": "thinking"}] then the real findings: '
            '[{"title": "real bug", "severity": "high"}]'
        )
        out = _extract_json_array(text)
        assert out == [{"title": "real bug", "severity": "high"}]


class TestEnforcementScalarCoerce:
    """#13: a poisoned non-int scalar in the committed state file must fail open to
    the default, not survive raw (which then crashes every save_state)."""

    def test_non_int_coerces_to_default(self):
        assert _coerce_nonneg_int("notanumber") == 0
        assert _coerce_nonneg_int("notanumber", 5) == 5

    def test_negative_coerces_to_default(self):
        assert _coerce_nonneg_int(-3) == 0

    def test_valid_int_passes(self):
        assert _coerce_nonneg_int(7) == 7
        assert _coerce_nonneg_int("7") == 7


class TestKeywordOnlyContractBreak:
    """#7: a Python `def f(*, x)` required keyword-only addition is a breaking
    narrowing (all callers pass by keyword); Ruby/default stay positional-only."""

    OLD = {"f": [{"kind": "keyword", "optional": False}]}
    NEW = {
        "f": [
            {"kind": "keyword", "optional": False},
            {"kind": "keyword", "optional": False},
        ]
    }

    def test_python_keyword_only_addition_is_a_break(self):
        breaks = diff_file_contracts(self.OLD, self.NEW, language="python")
        assert [b.name for b in breaks] == ["f"]

    def test_ruby_keyword_addition_is_not_flagged(self):
        assert diff_file_contracts(self.OLD, self.NEW, language="ruby") == []

    def test_default_language_positional_only(self):
        assert diff_file_contracts(self.OLD, self.NEW) == []


class TestAuthzProjectDecorator:
    """#5: a project authz decorator (`@allow_permission`) satisfies the authz
    convention; a genuinely unguarded view still flags."""

    CONV = {"required_guards": {"authz_required": True}}

    def test_project_decorator_satisfies_authz(self):
        src = (
            "class WorkspaceStickyViewSet(BaseViewSet):\n"
            "    @allow_permission(['admin'])\n"
            "    def list(self, request):\n"
            "        return []\n"
        )
        assert _python_guard_violations(src, self.CONV) == []

    def test_unguarded_view_still_flags(self):
        src = "class OpenViewSet(APIView):\n    def list(self, request):\n        return []\n"
        assert len(_python_guard_violations(src, self.CONV)) == 1


class TestPhantomMultilineImport:
    """#22: names from a multi-line parenthesized Python import must be extracted
    (the dominant multi-name style), including a name after a per-line comment."""

    def test_multiline_paren_import_names(self):
        clause = "(\n    real_one,\n    Hallucinated,  # note\n    other,\n)"
        assert _py_imported_names(clause) == ["real_one", "Hallucinated", "other"]

    def test_single_line_paren_still_works(self):
        assert _py_imported_names("(a, b as c)") == ["a", "b"]

    def test_star_import_yields_nothing(self):
        assert _py_imported_names("*") == []
