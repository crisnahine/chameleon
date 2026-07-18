"""Class I: a test file mis-routed to a SOURCE archetype must not get the
production structural-shape lint (a test file legitimately has no default export,
no production top-level constructs, etc.). jsx-presence-mismatch is block-eligible
and is deliberately NOT suppressed, so the hard partition + Stop arming stay
byte-identical.
"""

from __future__ import annotations

from chameleon_mcp.hook_helper import (
    _TEST_SUPPRESSED_STRUCTURAL_RULES,
    _drop_test_structural_findings,
)
from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES


def _v(rule):
    return {"rule": rule, "severity": "info", "message": "m", "expected": "", "actual": ""}


def test_structural_findings_dropped_on_ts_test_file():
    viols = [
        _v("named-export-count-bucket-mismatch"),
        _v("top-level-node-kinds-mismatch"),
        _v("default-export-kind-mismatch"),
        _v("content-signal-mismatch"),
        _v("jsx-presence-mismatch"),  # block-eligible -> kept
        _v("secret-detected-in-content"),  # security -> kept
        _v("naming-convention-violation"),  # not structural -> kept
    ]
    got = {
        v["rule"]
        for v in _drop_test_structural_findings(
            viols, "src/components/x/__tests__/foo.test.tsx", "typescript"
        )
    }
    assert got == {
        "jsx-presence-mismatch",
        "secret-detected-in-content",
        "naming-convention-violation",
    }


def test_structural_findings_kept_on_source_file():
    viols = [_v("named-export-count-bucket-mismatch"), _v("default-export-kind-mismatch")]
    got = _drop_test_structural_findings(viols, "src/components/x/foo.tsx", "typescript")
    assert len(got) == 2  # a non-test file is untouched


def test_python_test_file_structural_dropped():
    viols = [_v("top-level-node-kinds-mismatch"), _v("named-export-count-bucket-mismatch")]
    got = _drop_test_structural_findings(
        viols, "readthedocs/projects/tests/test_models.py", "python"
    )
    assert got == []


def test_enforcement_invariant_no_block_eligible_suppressed():
    # The suppress set must never include a block-eligible rule; jsx-presence-
    # mismatch (the only block-eligible structural rule) must stay enforced so the
    # hard partition and Stop arming are byte-identical on test files.
    assert _TEST_SUPPRESSED_STRUCTURAL_RULES.isdisjoint(BLOCK_ELIGIBLE_RULES)
    assert "jsx-presence-mismatch" not in _TEST_SUPPRESSED_STRUCTURAL_RULES


def test_fail_safe_on_empty_and_bad_input():
    assert _drop_test_structural_findings([], "a.test.ts", "typescript") == []
    # an unrecognizable path is treated as non-test -> findings kept (fail-safe)
    v = [_v("named-export-count-bucket-mismatch")]
    assert len(_drop_test_structural_findings(v, "src/x/foo.ts", "typescript")) == 1
