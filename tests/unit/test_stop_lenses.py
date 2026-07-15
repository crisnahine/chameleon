"""stop/lenses: the lens contract (LensResult, LENSES registry,
active_lenses) and the correctness lens's judge-Finding -> canonical-Finding
adapter.

The correctness lens's spawn seam (``judge._spawn_reviewer_status``) is
neutralized by the autouse ``_no_real_judge_spawn`` fixture in conftest.py
the same way every other judge-adjacent test suite is; one test in this file
(``test_correctness_lens_conftest_guard_blocks_real_spawn``) asserts that
default-stubbed behavior explicitly, so a future rename of the patch target
fails loudly here instead of silently degrading to a real, billable
``claude -p`` spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp import judge
from chameleon_mcp.calls_index import SCHEMA_VERSION as _CALLS_SCHEMA
from chameleon_mcp.core.finding import Finding, compute_match_key
from chameleon_mcp.function_catalog import ParsedFn
from chameleon_mcp.profile.config import EnforcementConfig
from chameleon_mcp.stop import lenses
from chameleon_mcp.stop.lenses import LENSES, LensResult, active_lenses, resolve_runner
from chameleon_mcp.stop.lenses import correctness as correctness_lens


def _result_line(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _write_source(repo: Path, rel: str = "src/widget.ts", body: str = "export const x = 1;\n"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _fn(name: str, start: int | None, end: int | None) -> ParsedFn:
    return ParsedFn(name, "function", 0, 0, start, None, None, "", end_line=end)


def _caller(path: str, caller: str, line: int | None = 3, grade: str = "import") -> dict:
    return {"path": path, "caller": caller, "line": line, "grade": grade}


def _write_calls_index(repo: Path, callees: dict) -> None:
    # Mirrors test_judge_caller_facts.py's fixture: a caller_facts_for_diffs
    # block requires a real calls_index.json AND a real on-disk caller file
    # that still references the callee at the recorded line (the block
    # live-re-verifies every cited caller).
    d = repo / ".chameleon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calls_index.json").write_text(
        json.dumps({"schema_version": _CALLS_SCHEMA, "callees": callees}), encoding="utf-8"
    )
    by_file: dict[str, dict[int, str]] = {}
    for _callee_rel, fns in callees.items():
        for fn_name, entry in fns.items():
            for c in entry.get("callers", []):
                path = c.get("path")
                if not isinstance(path, str):
                    continue
                line = c.get("line")
                ln = (
                    line
                    if isinstance(line, int) and not isinstance(line, bool) and line >= 1
                    else 1
                )
                by_file.setdefault(path, {})[ln] = fn_name
    for path, line_map in by_file.items():
        fp = repo / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        last = max(line_map)
        out = [
            f"  return {line_map[i]}();" if i in line_map else "  // x" for i in range(1, last + 1)
        ]
        fp.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- LENSES registry + active_lenses ----------------------------------------


def test_lenses_registry_has_all_three_names():
    assert set(LENSES) == {"correctness", "duplication", "idiom"}
    for name, (config_key, path) in LENSES.items():
        assert isinstance(config_key, str) and config_key
        assert ":" in path


def test_lenses_registry_config_keys_match_enforcement_fields():
    cfg = EnforcementConfig()
    for _name, (config_key, _path) in LENSES.items():
        assert hasattr(cfg, config_key)


def test_resolve_runner_correctness_returns_real_callable():
    runner = resolve_runner("correctness")
    assert runner is correctness_lens.run


def test_resolve_runner_unregistered_name_raises_keyerror():
    try:
        resolve_runner("not-a-real-lens")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unregistered lens name")


def test_resolve_runner_duplication_not_yet_implemented_raises_import_error():
    # Task 3 lands stop/lenses/duplication.py; until then the registry entry
    # is wired but the module doesn't exist -- resolve_runner surfaces that as
    # an ordinary ModuleNotFoundError rather than swallowing it, so the job
    # runner (Task 4) decides how to handle a lens that isn't ready.
    try:
        resolve_runner("duplication")
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("expected ModuleNotFoundError for the not-yet-built lens")


def test_active_lenses_default_config_has_all_three():
    cfg = EnforcementConfig()
    assert active_lenses(cfg) == ["correctness", "duplication", "idiom"]


def test_active_lenses_idiom_review_off_drops_idiom():
    cfg = EnforcementConfig(idiom_review=False)
    result = active_lenses(cfg)
    assert "idiom" not in result
    assert set(result) == {"correctness", "duplication"}


def test_active_lenses_duplication_review_off_drops_duplication():
    cfg = EnforcementConfig(duplication_review=False)
    assert "duplication" not in active_lenses(cfg)


def test_active_lenses_correctness_judge_off_drops_correctness():
    cfg = EnforcementConfig(correctness_judge=False)
    assert "correctness" not in active_lenses(cfg)


def test_active_lenses_all_off_returns_empty():
    cfg = EnforcementConfig(correctness_judge=False, duplication_review=False, idiom_review=False)
    assert active_lenses(cfg) == []


def test_active_lenses_missing_attr_fails_open_to_enabled():
    class _Bare:
        pass

    assert active_lenses(_Bare()) == ["correctness", "duplication", "idiom"]


def test_lens_result_defaults_are_empty():
    result = LensResult()
    assert result.findings == []
    assert result.check_events == []


# --- Finding.from_judge_finding adapter -------------------------------------


def _jf(**over):
    base = dict(message="dropped await on save()", confidence=0.85, file="src/a.ts", line=12)
    base.update(over)
    return judge.Finding(**base)


def test_from_judge_finding_maps_core_fields():
    jf = _jf()
    f = Finding.from_judge_finding(
        jf,
        kind="correctness",
        source_lens="correctness",
        intent_tokens=("retry-count",),
        created_at="2026-07-15T00:00:00Z",
    )
    assert f.claim == "dropped await on save()"
    assert f.confidence == 0.85
    assert f.file == "src/a.ts"
    assert f.span == (12, 12)
    assert f.kind == "correctness"
    assert f.source_lens == "correctness"
    assert f.status == "pending"
    assert f.created_at == "2026-07-15T00:00:00Z"
    assert f.intent_tokens == ("retry-count",)


def test_from_judge_finding_severity_high_at_or_above_threshold():
    f = Finding.from_judge_finding(
        _jf(confidence=0.7), kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.severity == "high"


def test_from_judge_finding_severity_medium_below_threshold():
    f = Finding.from_judge_finding(
        _jf(confidence=0.69), kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.severity == "medium"


def test_from_judge_finding_confidence_clamped_both_directions():
    over = Finding.from_judge_finding(
        _jf(confidence=1.5), kind="correctness", source_lens="correctness", created_at="t"
    )
    under = Finding.from_judge_finding(
        _jf(confidence=-0.3), kind="correctness", source_lens="correctness", created_at="t"
    )
    assert over.confidence == 1.0
    assert under.confidence == 0.0


def test_from_judge_finding_no_file_or_line_defaults_empty_span():
    jf = _jf(file=None, line=None)
    f = Finding.from_judge_finding(
        jf, kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.file == ""
    assert f.span == (0, 0)


def test_from_judge_finding_evidence_empty_when_no_evidence_cmds():
    f = Finding.from_judge_finding(
        _jf(evidence_cmds=None), kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.evidence == ""


def test_from_judge_finding_evidence_renders_pinned_commands():
    jf = _jf(
        evidence_cmds=[
            {"cmd": "grep -n foo src/a.ts", "output_sha256": "deadbeef"},
            {"cmd": "wc -l src/a.ts", "output_sha256": "cafef00d"},
        ]
    )
    f = Finding.from_judge_finding(
        jf, kind="correctness", source_lens="correctness", created_at="t"
    )
    assert "grep -n foo src/a.ts" in f.evidence
    assert "deadbeef" in f.evidence
    assert "wc -l src/a.ts" in f.evidence
    assert "cafef00d" in f.evidence


def test_from_judge_finding_excerpt_sha_carries_over_but_excerpt_text_is_unfetched():
    jf = _jf(excerpt_sha="ab" * 8)
    f = Finding.from_judge_finding(
        jf, kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.excerpt_sha == "ab" * 8
    assert f.excerpt == ""


def test_from_judge_finding_no_excerpt_sha_defaults_empty_string():
    f = Finding.from_judge_finding(
        _jf(excerpt_sha=None), kind="correctness", source_lens="correctness", created_at="t"
    )
    assert f.excerpt_sha == ""


def test_from_judge_finding_id_and_match_key_are_stable_and_equal():
    jf = _jf()
    f = Finding.from_judge_finding(
        jf, kind="correctness", source_lens="correctness", created_at="t"
    )
    expected = compute_match_key("dropped await on save()", "src/a.ts", "correctness")
    assert f.id == expected
    assert f.match_key == expected


def test_from_judge_finding_match_key_ignores_confidence_and_line():
    # Same claim/file/kind, different confidence/line -- identity must not
    # fork on data that isn't part of the exact-match key.
    a = Finding.from_judge_finding(
        _jf(confidence=0.2, line=1), kind="correctness", source_lens="correctness", created_at="t"
    )
    b = Finding.from_judge_finding(
        _jf(confidence=0.9, line=99),
        kind="correctness",
        source_lens="correctness",
        created_at="t2",
    )
    assert a.match_key == b.match_key
    assert a.id == b.id


# --- correctness lens run() --------------------------------------------------


def test_correctness_lens_run_no_diffs_returns_empty(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    # Nonexistent path -> reconstruct_diff returns None -> no diffs -> [].
    result = correctness_lens.run(repo, profile, [str(repo / "ghost.ts")], lambda _p: None)
    assert result.findings == []
    assert result.check_events == []


def test_correctness_lens_conftest_guard_blocks_real_spawn(tmp_path):
    # No explicit patch of judge._spawn_reviewer_status here: this exercises
    # the autouse conftest guard directly. If a future rename moves the spawn
    # seam without updating conftest.py, this test starts making a real,
    # authenticated, billable `claude -p` call and either hangs or fails
    # loudly -- which is exactly the point.
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    _write_source(repo)
    result = correctness_lens.run(repo, profile, [str(repo / "src/widget.ts")], lambda _p: None)
    assert result.findings == []
    assert ("spawn_exec_error", "") in result.check_events


def test_correctness_lens_run_produces_canonical_findings(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)

    arr = [
        {"file": "src/widget.ts", "line": 3, "message": "dropped await", "confidence": 0.9},
        {"file": "src/widget.ts", "line": 7, "message": "off by one", "confidence": 0.4},
    ]
    stream = _result_line(json.dumps(arr))
    events = []
    with (
        patch.object(judge, "_spawn_reviewer_status", return_value=(stream, None)),
        patch.object(judge, "_witness_for", return_value=""),
    ):
        result = correctness_lens.run(
            repo,
            profile,
            [str(p)],
            lambda _p: "controller",
            intent_tokens=["retry-count"],
            event_sink=lambda kind, detail: events.append((kind, detail)),
        )

    assert len(result.findings) == 2
    for f in result.findings:
        assert isinstance(f, Finding)
        assert f.kind == "correctness"
        assert f.source_lens == "correctness"
        assert f.intent_tokens == ("retry-count",)
        assert f.evidence == ""  # no evidence_cmds pinned yet at this stage
    claims = {f.claim for f in result.findings}
    assert claims == {"dropped await", "off by one"}
    high = next(f for f in result.findings if f.claim == "dropped await")
    assert high.severity == "high"
    low = next(f for f in result.findings if f.claim == "off by one")
    assert low.severity == "medium"
    # The event_sink passed in sees the same events as check_events (threaded
    # through, not just collected); check_events normalizes a None detail to
    # "" to satisfy its tuple[str, str] shape, so compare kinds and the
    # None-vs-"" normalized details separately.
    assert [kind for kind, _ in events] == [kind for kind, _ in result.check_events]
    assert [(d or "") for _, d in events] == [d for _, d in result.check_events]


def test_correctness_lens_run_spawn_failure_events_and_empty_findings(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)
    with patch.object(judge, "_spawn_reviewer_status", return_value=(None, "spawn_timeout")):
        result = correctness_lens.run(repo, profile, [str(p)], lambda _p: None)
    assert result.findings == []
    assert ("spawn_timeout", "") in result.check_events


def test_correctness_lens_run_unparseable_output_event(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)
    stream = _result_line("looks fine to me, no array here")
    with (
        patch.object(judge, "_spawn_reviewer_status", return_value=(stream, None)),
        patch.object(judge, "_witness_for", return_value=""),
    ):
        result = correctness_lens.run(repo, profile, [str(p)], lambda _p: None)
    assert result.findings == []
    assert ("unparseable_output", "") in result.check_events


def test_correctness_lens_run_budget_becomes_spawn_timeout(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)
    captured = {}

    def _fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        captured["timeout_s"] = timeout_s
        captured["model"] = model
        return _result_line("[]"), None

    with (
        patch.object(judge, "_spawn_reviewer_status", side_effect=_fake_spawn),
        patch.object(judge, "_witness_for", return_value=""),
    ):
        result = correctness_lens.run(
            repo, profile, [str(p)], lambda _p: None, budget=30, model="sonnet"
        )
    assert captured["timeout_s"] == 30
    assert captured["model"] == "sonnet"
    assert result.findings == []


def test_correctness_lens_run_budget_non_positive_or_non_numeric_falls_back_to_default(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)
    for bad_budget in (0, -5, "30", None):
        captured = {}

        def _fake_spawn(prompt, cwd, *, model=None, timeout_s=None, _captured=captured):
            _captured["timeout_s"] = timeout_s
            return _result_line("[]"), None

        with (
            patch.object(judge, "_spawn_reviewer_status", side_effect=_fake_spawn),
            patch.object(judge, "_witness_for", return_value=""),
        ):
            correctness_lens.run(repo, profile, [str(p)], lambda _p: None, budget=bad_budget)
        assert captured["timeout_s"] is None, f"budget={bad_budget!r} leaked into timeout_s"


def _wired_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = repo / "util.ts"
    src.write_text("export function fmt() { return 1 }\n", encoding="utf-8")
    return repo, profile, src


def test_correctness_lens_run_facts_included_reaches_build_prompt(tmp_path, monkeypatch):
    repo, profile, src = _wired_repo(tmp_path)
    _write_calls_index(
        repo,
        {"util.ts": {"fmt": {"callers": [_caller("b.ts", "use")], "total": 1, "truncated": False}}},
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fmt", 1, 1)])
    captured = {}

    def fake_build(
        repo_root,
        profile_dir,
        diffs,
        intent_tokens=None,
        caller_facts=None,
        transitive_facts=None,
        imported_defs=None,
    ):
        captured["caller_facts"] = caller_facts
        return "prompt"

    events = []
    with (
        patch.object(judge, "build_prompt", side_effect=fake_build),
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
    ):
        result = correctness_lens.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert captured["caller_facts"] is not None
    assert "- fmt() in util.ts: 1 committed caller" in captured["caller_facts"]
    assert "judge_facts_included" in events
    assert "judge_facts_included" in [k for k, _d in result.check_events]
    assert result.findings == []  # spawn returned "[]"


def test_correctness_lens_run_facts_disabled_via_config(tmp_path, monkeypatch):
    repo, profile, src = _wired_repo(tmp_path)
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"judge_crossfile_facts": False}}), encoding="utf-8"
    )
    _write_calls_index(
        repo,
        {"util.ts": {"fmt": {"callers": [_caller("b.ts", "use")], "total": 1, "truncated": False}}},
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fmt", 1, 1)])
    captured = {}

    def fake_build(
        repo_root,
        profile_dir,
        diffs,
        intent_tokens=None,
        caller_facts=None,
        transitive_facts=None,
        imported_defs=None,
    ):
        captured["caller_facts"] = caller_facts
        return "prompt"

    events = []
    with (
        patch.object(judge, "build_prompt", side_effect=fake_build),
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
    ):
        correctness_lens.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert captured["caller_facts"] is None
    assert "judge_facts_skipped_disabled" in events
    assert "judge_facts_included" not in events


def test_correctness_lens_run_imported_defs_disabled_via_config(tmp_path):
    repo, profile, src = _wired_repo(tmp_path)
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"judge_imported_definitions": False}}), encoding="utf-8"
    )
    events = []
    with (
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
        patch.object(judge, "_witness_for", return_value=""),
    ):
        correctness_lens.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert "judge_defs_skipped_disabled" in events
    assert "judge_defs_included" not in events


def test_correctness_lens_run_transitive_disabled_via_config(tmp_path):
    repo, profile, src = _wired_repo(tmp_path)
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"judge_transitive_impact": False}}), encoding="utf-8"
    )
    events = []
    with (
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
        patch.object(judge, "_witness_for", return_value=""),
    ):
        correctness_lens.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert "judge_transitive_skipped_disabled" in events
    assert "judge_transitive_included" not in events


def test_correctness_lens_run_pipeline_error_is_caught(tmp_path, monkeypatch):
    repo = tmp_path / "plain"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    p = _write_source(repo)

    def _boom(*_a, **_k):
        raise RuntimeError("evidence builder exploded")

    monkeypatch.setattr(judge, "caller_facts_for_diffs", _boom)
    result = correctness_lens.run(repo, profile, [str(p)], lambda _p: None)
    assert result.findings == []
    assert any(kind == "pipeline_error" for kind, _detail in result.check_events)


def test_module_import_does_not_require_duplication_or_idiom_modules():
    # stop/lenses/__init__.py must import cleanly even though the
    # duplication/idiom runner modules referenced in LENSES don't exist yet
    # (Task 3) -- active_lenses() never imports them.
    import importlib

    importlib.reload(lenses)
    assert callable(lenses.active_lenses)
