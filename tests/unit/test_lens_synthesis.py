from chameleon_mcp.lens_synthesis import synthesize_lens_findings


def _f(file, line, claim, lens, confidence):
    return {"file": file, "line": line, "claim": claim, "lens": lens, "confidence": confidence}


def test_two_lenses_agreeing_merge_to_one_surfaced_finding():
    findings = [
        _f("a.ts", 10, "Dropped await on async call", "correctness", 0.6),
        _f("a.ts", 10, "dropped await on async call", "security", 0.5),
    ]

    out = synthesize_lens_findings(findings, min_confidence=0.7)

    assert len(out) == 1
    item = out[0]
    assert item["agreement"] == 2
    assert sorted(item["lenses"]) == ["correctness", "security"]
    # Cross-lens agreement surfaces even though neither lens alone cleared the bar.
    assert item["surface"] is True


def test_single_low_confidence_finding_does_not_surface():
    # The anti-raw-union rule: one lens, below the bar, is not trusted enough to surface.
    out = synthesize_lens_findings([_f("a.ts", 5, "maybe off by one", "correctness", 0.4)])

    assert len(out) == 1
    assert out[0]["agreement"] == 1
    assert out[0]["surface"] is False


def test_single_high_confidence_finding_surfaces():
    out = synthesize_lens_findings(
        [_f("a.ts", 5, "null deref", "correctness", 0.95)], min_confidence=0.7
    )

    assert out[0]["surface"] is True


def test_distinct_findings_stay_separate():
    out = synthesize_lens_findings(
        [
            _f("a.ts", 5, "x", "correctness", 0.9),
            _f("b.ts", 9, "y", "security", 0.9),
        ]
    )

    assert len(out) == 2


def test_confidence_is_the_max_across_lenses():
    out = synthesize_lens_findings(
        [
            _f("a.ts", 1, "same", "correctness", 0.3),
            _f("a.ts", 1, "same", "security", 0.8),
        ]
    )

    assert out[0]["confidence"] == 0.8


def test_empty_input():
    assert synthesize_lens_findings([]) == []
