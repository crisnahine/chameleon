"""Tests for the multi-lens runner (G3 core): run N lenses, normalize, synthesize.

The runner is the first real caller of lens_synthesis.synthesize_lens_findings:
it tags each lens's raw findings with the lens name, flattens, and merges so the
surfaced set is deduped and agreement-gated rather than a raw union. Pure core;
spawning lives in the lens callables. Fails open per lens.
"""

from __future__ import annotations

from types import SimpleNamespace

from chameleon_mcp.lens_runner import (
    Lens,
    correctness_lens,
    duplication_lens,
    run_lenses,
)


def _lens(name, findings):
    return Lens(name=name, run=lambda: findings)


def test_correctness_lens_adapts_finding_shape():
    raw = [SimpleNamespace(message="missing nil guard", confidence=0.8, file="x.rb", line=3)]
    lens = correctness_lens(lambda: raw)
    assert lens.name == "correctness"
    out = lens.run()
    assert out == [{"file": "x.rb", "line": 3, "claim": "missing nil guard", "confidence": 0.8}]


def test_duplication_lens_adapts_finding_shape():
    raw = [
        SimpleNamespace(
            new_name="strip_widget_attributes",
            new_file="app/models/widget.rb",
            line=5,
            existing_name="strip_attributes",
            existing_file="app/models/concerns/sanitizable.rb",
        )
    ]
    lens = duplication_lens(lambda: raw)
    assert lens.name == "duplication"
    out = lens.run()
    assert len(out) == 1
    f = out[0]
    assert f["file"] == "app/models/widget.rb"
    assert f["line"] == 5
    assert "strip_widget_attributes" in f["claim"] and "strip_attributes" in f["claim"]
    assert f["confidence"] == 1.0


def test_adapter_thunk_raising_is_handled_by_run_lenses():
    def boom():
        raise RuntimeError("spawn failed")

    # The adapter's run() propagates; run_lenses is the fail-open boundary.
    out = run_lenses([correctness_lens(boom)])
    assert out == []


def test_two_lenses_agreeing_surface_with_agreement_2():
    a = _lens(
        "correctness",
        [{"file": "x.rb", "line": 10, "claim": "missing nil guard", "confidence": 0.5}],
    )
    b = _lens(
        "security", [{"file": "x.rb", "line": 10, "claim": "Missing nil guard", "confidence": 0.4}]
    )
    out = run_lenses([a, b])
    assert len(out) == 1
    f = out[0]
    assert f["agreement"] == 2
    assert f["surface"] is True
    assert sorted(f["lenses"]) == ["correctness", "security"]


def test_single_low_confidence_lens_does_not_surface():
    a = _lens(
        "correctness", [{"file": "x.rb", "line": 5, "claim": "maybe off by one", "confidence": 0.3}]
    )
    out = run_lenses([a])
    assert len(out) == 1
    assert out[0]["surface"] is False


def test_single_high_confidence_lens_surfaces():
    a = _lens(
        "correctness", [{"file": "x.rb", "line": 5, "claim": "dropped await", "confidence": 0.9}]
    )
    out = run_lenses([a])
    assert out[0]["surface"] is True


def test_distinct_findings_not_merged():
    a = _lens("correctness", [{"file": "x.rb", "line": 5, "claim": "a", "confidence": 0.9}])
    b = _lens("security", [{"file": "y.rb", "line": 9, "claim": "b", "confidence": 0.9}])
    out = run_lenses([a, b])
    assert len(out) == 2


def test_raising_lens_fails_open():
    def boom():
        raise RuntimeError("lens exploded")

    good = _lens("correctness", [{"file": "x.rb", "line": 1, "claim": "c", "confidence": 0.9}])
    out = run_lenses([Lens(name="bad", run=boom), good])
    assert len(out) == 1
    assert out[0]["claim"] == "c"


def test_max_lenses_cap_bounds_spawns():
    calls = []

    def mk(name):
        def run():
            calls.append(name)
            return []

        return Lens(name=name, run=run)

    run_lenses([mk("a"), mk("b"), mk("c"), mk("d"), mk("e")], max_lenses=2)
    assert calls == ["a", "b"]


def test_empty_lenses_returns_empty():
    assert run_lenses([]) == []
