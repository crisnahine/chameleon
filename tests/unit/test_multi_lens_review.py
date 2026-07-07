"""Tests for the opt-in multi-lens turn-end review wiring (_multi_lens_review_lines).

When enforcement.multi_lens_review is on, the Stop path runs a coordinated
correctness + duplication lens pass (no mutual defer) merged through
lens_synthesis, instead of the separate gates. The lens internals are covered by
test_lens_runner; here the gating, the spawn-budget bookkeeping, and the
surfaced-finding formatting are exercised. run_lenses is mocked so no subprocess
spawns.
"""

from __future__ import annotations

from types import SimpleNamespace

from chameleon_mcp import hook_helper, lens_runner
from chameleon_mcp.enforcement import EnforcementState, FileState


def _cfg(*, flag=True, mode="shadow", correctness_judge=True, duplication_review=True):
    return SimpleNamespace(
        multi_lens_review=flag,
        mode=mode,
        correctness_judge=correctness_judge,
        duplication_review=duplication_review,
    )


def _capture_lenses(monkeypatch):
    seen = {}

    def fake(lenses, **k):
        seen["names"] = [lens.name for lens in lenses]
        return []

    monkeypatch.setattr(lens_runner, "run_lenses", fake)
    return seen


def _route(spawn=True, fresh=None):
    return {
        "spawn": spawn,
        "fresh": fresh if fresh is not None else ["/repo/app/x.rb"],
        "intent_tokens": [],
        "digests": {},
        "turn_key": "tk",
        "reason": "edited",
    }


def _surfaced(monkeypatch, findings):
    monkeypatch.setattr(lens_runner, "run_lenses", lambda lenses, **k: findings)


def _call(tmp_path, *, cfg, route, state=None):
    return hook_helper._multi_lens_review_lines(
        repo_root=tmp_path,
        repo_id="rid",
        session_id="sid",
        state=state if state is not None else EnforcementState(),
        cfg=cfg,
        repo_data=tmp_path,
        daemon_state={"available": True},
        route=route,
    )


def test_flag_off_silent(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    assert _call(tmp_path, cfg=_cfg(flag=False), route=_route()) == []


def test_mode_off_silent(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    assert _call(tmp_path, cfg=_cfg(mode="off"), route=_route()) == []


def test_no_spawn_route_silent(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    assert _call(tmp_path, cfg=_cfg(), route=_route(spawn=False)) == []


def test_surfaces_only_surface_true_findings(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [
            {
                "file": "app/x.rb",
                "line": 3,
                "claim": "missing guard",
                "lenses": ["correctness"],
                "surface": True,
            },
            {
                "file": "app/a.rb",
                "line": 5,
                "claim": "foo re-implements bar",
                "lenses": ["duplication"],
                "surface": True,
            },
            {
                "file": "app/y.rb",
                "line": 1,
                "claim": "weak",
                "lenses": ["correctness"],
                "surface": False,
            },
        ],
    )
    lines = _call(tmp_path, cfg=_cfg(), route=_route())
    assert lines
    header = lines[0]
    assert "multi-lens" in header and "2" in header  # only the 2 surfaced
    body = "\n".join(lines)
    assert "missing guard" in body and "foo re-implements bar" in body
    assert "weak" not in body


def test_no_surfaced_findings_returns_empty(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [
            {
                "file": "app/y.rb",
                "line": 1,
                "claim": "weak",
                "lenses": ["correctness"],
                "surface": False,
            }
        ],
    )
    assert _call(tmp_path, cfg=_cfg(), route=_route()) == []


def test_spawn_budget_incremented(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    state = EnforcementState()
    before = state.correctness_spawns
    _call(tmp_path, cfg=_cfg(), route=_route(), state=state)
    assert state.correctness_spawns == before + 1


def test_empty_fresh_silent(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    assert _call(tmp_path, cfg=_cfg(), route=_route(fresh=[])) == []


# --- VERIFY stage on the multi-lens (default-config) path -----------------------


def _fresh_budget(monkeypatch):
    """Anchor the sync VERIFY budget clock to 'now' (the pytest process has been
    alive far longer than a one-shot hook process would be)."""
    import time

    monkeypatch.setattr(hook_helper, "_PROCESS_START_MONOTONIC", time.monotonic())


def _stub_refuter(monkeypatch, verdicts_by_id):
    calls = {"batches": []}

    def fake_run_batch(repo_root, findings, excerpts, *, model, timeout, max_spawns, **kw):
        calls["batches"].append([f.get("id") for f in findings])
        return [
            {"id": f.get("id"), "verdict": verdicts_by_id.get(f.get("id"), "unverified")}
            for f in findings[:max_spawns]
        ]

    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None)
    monkeypatch.setattr(refuter, "run_batch", fake_run_batch)
    return calls


def test_verify_drops_refuted_lone_correctness_finding(tmp_path, monkeypatch):
    """The default-config VERIFY: lone-correctness findings are refuted/confirmed,
    duplication findings are exempt (their evidence spans two locations a one-file
    excerpt cannot show) and pass through untouched."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.rb").write_text(
        "\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8"
    )
    _fresh_budget(monkeypatch)
    calls = _stub_refuter(monkeypatch, {"0": "refuted", "1": "confirmed"})
    _surfaced(
        monkeypatch,
        [
            {
                "file": "app/x.rb",
                "line": 2,
                "claim": "false alarm",
                "lenses": ["correctness"],
                "confidence": 0.9,
                "surface": True,
            },
            {
                "file": "app/x.rb",
                "line": 5,
                "claim": "real missing guard",
                "lenses": ["correctness"],
                "confidence": 0.9,
                "surface": True,
            },
            {
                "file": "app/x.rb",
                "line": 7,
                "claim": "foo re-implements bar",
                "lenses": ["duplication"],
                "confidence": 0.9,
                "surface": True,
            },
        ],
    )
    lines = _call(tmp_path, cfg=_cfg(), route=_route())
    body = "\n".join(lines)
    # Only the two lone-correctness findings were sent to the refuter.
    assert calls["batches"] == [["0", "1"]]
    assert "false alarm" not in body  # refuted -> dropped
    assert "[correctness] [confirmed]: real missing guard" in body
    assert "foo re-implements bar" in body  # duplication exempt, untouched
    assert "[duplication] [confirmed]" not in body
    assert "1 refuted and dropped, 1 confirmed" in body
    assert "flagged 2 possible issue" in lines[0]  # header counts survivors


def test_verify_exempts_cross_lens_agreement(tmp_path, monkeypatch):
    """A finding two lenses independently raised is already independently
    verified; it must not be spent refuter budget or risk a drop."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.rb").write_text("line1\n", encoding="utf-8")
    _fresh_budget(monkeypatch)
    calls = _stub_refuter(monkeypatch, {"0": "refuted"})
    _surfaced(
        monkeypatch,
        [
            {
                "file": "app/x.rb",
                "line": 1,
                "claim": "agreed finding",
                "lenses": ["correctness", "duplication"],
                "confidence": 0.9,
                "surface": True,
            }
        ],
    )
    lines = _call(tmp_path, cfg=_cfg(), route=_route())
    assert calls["batches"] == []  # nothing eligible -> no refuter spawn
    assert "agreed finding" in "\n".join(lines)


def test_verify_failure_keeps_all_multilens_findings(tmp_path, monkeypatch):
    """A broken refuter must never drop a multi-lens finding (fail-open)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.rb").write_text("line1\nline2\n", encoding="utf-8")
    _fresh_budget(monkeypatch)
    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("refuter exploded")

    monkeypatch.setattr(refuter, "run_batch", boom)
    _surfaced(
        monkeypatch,
        [
            {
                "file": "app/x.rb",
                "line": 1,
                "claim": "kept on failure",
                "lenses": ["correctness"],
                "confidence": 0.9,
                "surface": True,
            }
        ],
    )
    body = "\n".join(_call(tmp_path, cfg=_cfg(), route=_route()))
    assert "kept on failure" in body
    assert "refuted and dropped" not in body  # no banner when VERIFY did not run


# --- route gating: multi_lens must not depend on the correctness_judge flag ---


def _route_cfg(*, correctness_judge, multi_lens_review, mode="shadow"):
    return SimpleNamespace(
        mode=mode,
        correctness_judge=correctness_judge,
        multi_lens_review=multi_lens_review,
    )


def _route_state_with_edit(tmp_path):
    src = tmp_path / "app" / "x.rb"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("def compute\n  work\nend\n", encoding="utf-8")
    st = EnforcementState()
    st.files[str(src)] = FileState()
    return st, src


def _run_route(tmp_path, cfg):
    st, _src = _route_state_with_edit(tmp_path)
    return hook_helper._correctness_judge_route(
        repo_root=tmp_path,
        repo_id="rid",
        session_id="sid",
        state=st,
        cfg=cfg,
        repo_data=tmp_path,
        daemon_state={"available": True},
        is_subagent=False,
    )


def test_route_disabled_when_both_off(tmp_path):
    route = _run_route(tmp_path, _route_cfg(correctness_judge=False, multi_lens_review=False))
    assert route["skip_reason"] == "feature_disabled"


def test_route_proceeds_when_only_multi_lens_on(tmp_path):
    # multi_lens replaces the correctness gate, so it must drive the route even
    # with the legacy correctness_judge flag off.
    route = _run_route(tmp_path, _route_cfg(correctness_judge=False, multi_lens_review=True))
    assert route["skip_reason"] != "feature_disabled"


# --- multi-lens must respect the per-lens enforcement flags ---


def test_both_lenses_when_both_flags_on(tmp_path, monkeypatch):
    seen = _capture_lenses(monkeypatch)
    monkeypatch.setattr(hook_helper, "_judge_async_mode", lambda: None)
    _call(tmp_path, cfg=_cfg(correctness_judge=True, duplication_review=True), route=_route())
    assert seen["names"] == ["correctness", "duplication"]


def test_duplication_flag_off_excludes_duplication_lens(tmp_path, monkeypatch):
    seen = _capture_lenses(monkeypatch)
    monkeypatch.setattr(hook_helper, "_judge_async_mode", lambda: None)
    _call(tmp_path, cfg=_cfg(correctness_judge=True, duplication_review=False), route=_route())
    assert seen["names"] == ["correctness"]


def test_correctness_flag_off_excludes_correctness_lens(tmp_path, monkeypatch):
    seen = _capture_lenses(monkeypatch)
    _call(tmp_path, cfg=_cfg(correctness_judge=False, duplication_review=True), route=_route())
    assert seen["names"] == ["duplication"]


def test_both_sub_flags_off_runs_no_lenses(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["correctness"], "surface": True}],
    )
    lines = _call(
        tmp_path, cfg=_cfg(correctness_judge=False, duplication_review=False), route=_route()
    )
    assert lines == []


def test_duplication_spawn_counter_incremented_when_dup_lens_runs(tmp_path, monkeypatch):
    _surfaced(
        monkeypatch,
        [{"file": "x", "line": 1, "claim": "c", "lenses": ["duplication"], "surface": True}],
    )
    state = EnforcementState()
    dup_before = state.duplication_spawns
    corr_before = state.correctness_spawns
    _call(tmp_path, cfg=_cfg(), route=_route(), state=state)
    assert state.duplication_spawns == dup_before + 1
    assert state.correctness_spawns == corr_before + 1
