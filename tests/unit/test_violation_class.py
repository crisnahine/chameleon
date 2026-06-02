from chameleon_mcp.violation_class import (
    BLOCK_ELIGIBLE_RULES,
    hard_class_violations,
    is_archetype_independent,
    is_hard_class,
)


def v(rule, severity="warning"):
    return {"rule": rule, "severity": severity, "message": "m", "expected": "", "actual": ""}


def test_block_eligible_rules_contents():
    assert BLOCK_ELIGIBLE_RULES == frozenset(
        {"phantom-import", "import-preference-violation", "jsx-presence-mismatch"}
    )


def test_phantom_is_hard_and_independent():
    assert is_hard_class(v("phantom-import"))
    assert is_archetype_independent("phantom-import")


def test_banned_import_is_hard_but_dependent():
    assert is_hard_class(v("import-preference-violation"))
    assert not is_archetype_independent("import-preference-violation")


def test_jsx_error_is_hard_warning_is_not():
    assert is_hard_class(v("jsx-presence-mismatch", "error"))
    assert not is_hard_class(v("jsx-presence-mismatch", "warning"))


def test_soft_rules_never_hard():
    for r in (
        "default-export-kind-mismatch",
        "naming-convention-violation",
        "inheritance-convention-violation",
        "top-level-node-kinds-mismatch",
        "content-signal-mismatch",
        "named-export-count-bucket-mismatch",
    ):
        assert not is_hard_class(v(r))


def test_hard_class_violations_filters_by_active_set():
    vs = [
        v("phantom-import"),
        v("naming-convention-violation"),
        v("import-preference-violation"),
    ]
    out = hard_class_violations(vs, active_rules={"phantom-import"})
    assert [x["rule"] for x in out] == ["phantom-import"]
