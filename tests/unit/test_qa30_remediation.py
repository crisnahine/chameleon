"""Regression tests for the QA-30 remediation pass (v2.14.x).

Each test pins a concrete defect found during the QA-30 campaign so it cannot
silently regress. All seven are toolchain-free (they exercise the pure-Python
regex lint path and orchestration core, never the subprocess AST dumpers). The
idioms.md-deletion warning fix is verified end-to-end against a real bootstrap
rather than here, since it requires a language toolchain.
"""

from __future__ import annotations

import dataclasses
import threading

from chameleon_mcp import lens_runner
from chameleon_mcp.exec_log import classify_test_command
from chameleon_mcp.lint_engine import (
    _blank_ruby_percent_literals,
    _extract_ruby,
    _extract_typescript,
    lint_conventions,
)
from chameleon_mcp.profile.summary import count_config_rules

# --- Fix #1: multi-lens lenses run concurrently, not sequentially -----------


def test_run_lenses_executes_lenses_concurrently():
    """Two lenses must run in parallel so their spawn budgets do not sum past
    the Stop hook's wall-clock cap. A Barrier(2) only releases when BOTH lenses
    are in-flight at once; sequential execution would deadlock the barrier, its
    TimeoutError would be swallowed per-lens, and nothing would surface."""
    barrier = threading.Barrier(2, timeout=5)

    def make(name: str) -> lens_runner.Lens:
        def run() -> list[dict]:
            barrier.wait()  # completes only if the other lens is also running
            return [{"file": f"{name}.py", "line": 1, "claim": name, "confidence": 0.95}]

        return lens_runner.Lens(name=name, run=run)

    out = lens_runner.run_lenses([make("a"), make("b")], max_lenses=2, min_confidence=0.7)
    # Both lenses cleared the barrier and surfaced their high-confidence finding.
    assert len([f for f in out if f.get("surface")]) == 2


def test_run_lenses_still_fails_open_per_lens():
    """A raising lens contributes nothing; the others still surface."""

    def boom() -> list[dict]:
        raise RuntimeError("lens blew up")

    def good() -> list[dict]:
        return [{"file": "g.py", "line": 2, "claim": "g", "confidence": 0.95}]

    out = lens_runner.run_lenses(
        [lens_runner.Lens("bad", boom), lens_runner.Lens("good", good)],
        max_lenses=2,
        min_confidence=0.7,
    )
    assert any(f.get("file") == "g.py" for f in out)


# --- Fix #7: bare/standalone minitest is a test command ---------------------


def test_minitest_classified_as_test_command():
    assert classify_test_command("bundle exec minitest test/foo_test.rb") is True
    assert classify_test_command("minitest test/foo_test.rb") is True
    assert classify_test_command("/usr/local/bin/minitest test/x_test.rb") is True
    # Still-correct neighbours: not a runner when it is an argument.
    assert classify_test_command("cat minitest.txt") is False
    assert classify_test_command("pip install minitest") is False


# --- Fix #8: count_config_rules counts flat top-level tsconfig settings ------


def test_count_config_rules_counts_flat_typescript_block():
    """The typescript tool block stores settings at the top level (no `rules`
    sub-key); the count must reflect them, not read 0."""
    ts_block = {
        "strict": True,
        "noImplicitAny": False,
        "strictNullChecks": True,
        "target": "ESNext",
        "paths": {"@/*": ["src/*"]},
        "source": "tsconfig.json",  # wrapper key, must not count
        "extends_chain": ["./base.json"],  # wrapper key, must not count
    }
    assert count_config_rules(ts_block) == 5


def test_count_config_rules_eslint_and_empty_unchanged():
    eslint_block = {"rules": {"no-var": "error", "eqeqeq": "warn"}, "source": "x"}
    assert count_config_rules(eslint_block) == 2
    assert count_config_rules({}) == 0
    assert count_config_rules({"parse_warning": "x", "source": "y"}) == 0


# --- Fix #4: a leading UTF-8 BOM does not skew the runtime dimension snapshot -


def test_bom_does_not_change_typescript_snapshot():
    ts = "export default function Foo() { return 1; }\nexport const bar = 2;"
    plain = dataclasses.asdict(_extract_typescript(ts))
    bom = dataclasses.asdict(_extract_typescript("﻿" + ts))
    assert plain == bom
    assert bom["default_export_kind"] == "FunctionDeclaration"


def test_bom_does_not_change_ruby_snapshot():
    rb = "class Foo < ApplicationController\n  def x\n    1\n  end\nend"
    plain = dataclasses.asdict(_extract_ruby(rb))
    bom = dataclasses.asdict(_extract_ruby("﻿" + rb))
    assert plain == bom


# --- Fix #6: %(...) percent-literal stripper handles one level of nesting ----


def test_percent_literal_strips_nested_parens():
    # The bare paren delimiter with a nested pair must be blanked so an embedded
    # `eval(` cannot trip the dangerous-sink scan.
    assert "eval(" not in _blank_ruby_percent_literals("%(eval(x))")
    assert "eval(" not in _blank_ruby_percent_literals("%(a(b)c)")
    # A real modulo expression (space after %) is NOT a literal and is preserved.
    assert _blank_ruby_percent_literals("x % (a + b)") == "x % (a + b)"
    # Existing forms keep working.
    assert "eval(" not in _blank_ruby_percent_literals("%q{eval(x)}")
    assert "eval(" not in _blank_ruby_percent_literals("%w[eval( one]")


# --- Fix #2: ignore gates accept the long emitted rule name -----------------


def _naming_conventions() -> dict:
    return {"naming": {"interface_prefix": {"pattern": "I", "consistency": 0.9}}}


def test_naming_violation_fires_without_directive():
    viols = lint_conventions(
        "export interface Foo {}", _naming_conventions(), language="typescript"
    )
    assert any(v.rule == "naming-convention-violation" for v in viols)


def test_long_form_ignore_directive_suppresses_naming():
    content = "// chameleon-ignore naming-convention-violation\nexport interface Foo {}"
    viols = lint_conventions(content, _naming_conventions(), language="typescript")
    assert not any(v.rule == "naming-convention-violation" for v in viols)


def test_short_form_ignore_directive_still_suppresses_naming():
    content = "// chameleon-ignore naming-convention\nexport interface Foo {}"
    viols = lint_conventions(content, _naming_conventions(), language="typescript")
    assert not any(v.rule == "naming-convention-violation" for v in viols)


# --- Fix #3: inheritance lint does not flag a class that IS a base ----------


def _inheritance_conventions() -> dict:
    return {
        "inheritance": {
            "dominant_base": "Api::V1::BaseController",
            "frequency": 0.95,
            "known_bases": ["Api::V1::BaseController"],
        }
    }


def test_inheritance_does_not_flag_a_known_base_itself():
    # `BaseController` IS the established base (matched on its tail): telling it
    # to inherit Api::V1::BaseController would be telling it to inherit itself.
    viols = lint_conventions(
        "class BaseController < ActionController::API\nend",
        _inheritance_conventions(),
        language="ruby",
    )
    assert not any(v.rule == "inheritance-convention-violation" for v in viols)


def test_inheritance_does_not_flag_rails_application_root():
    viols = lint_conventions(
        "class ApplicationController < ActionController::API\nend",
        _inheritance_conventions(),
        language="ruby",
    )
    assert not any(v.rule == "inheritance-convention-violation" for v in viols)


def test_inheritance_still_flags_a_genuinely_wrong_base():
    viols = lint_conventions(
        "class WidgetsController < SomeRandomBase\nend",
        _inheritance_conventions(),
        language="ruby",
    )
    assert any(v.rule == "inheritance-convention-violation" for v in viols)
