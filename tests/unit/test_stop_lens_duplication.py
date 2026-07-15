"""stop/lenses/duplication.py: wraps duplication_review into canonical Findings.

Mirrors test_duplication_gate_stop.py's mocking shape (build_candidate_index /
gather_body_match_findings / gather_semantic_findings mocked, judge._spawn_reviewer
returns a stream-json verdict) but drives the lens's ``run()`` directly instead of
the whole Stop hook, and asserts on canonical ``core.finding.Finding`` objects
instead of rendered advisory lines.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from chameleon_mcp.core.finding import Finding
from chameleon_mcp.duplication_review import Finding as DupFinding
from chameleon_mcp.stop.lenses import duplication


def _result_line(payload) -> str:
    return json.dumps({"type": "result", "result": json.dumps(payload)}) + "\n"


def _dup_finding(**over):
    base = dict(
        new_name="toDisplayDate",
        new_file="src/widget.ts",
        line=7,
        excerpt="return d.toISOString()",
        existing_name="formatDate",
        existing_file="src/dates.ts",
    )
    base.update(over)
    return DupFinding(**base)


def _write_source(repo, rel="src/widget.ts", body="export function toDisplayDate() {}\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_run_no_edited_files_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    result = duplication.run(repo, profile, [str(repo / "ghost.ts")], lambda _p: None)
    assert result.findings == []
    assert result.check_events == []


def test_run_no_findings_never_spawns(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch("chameleon_mcp.duplication_review.gather_body_match_findings", return_value=[]),
        patch("chameleon_mcp.duplication_review.gather_semantic_findings", return_value=[]),
        patch("chameleon_mcp.judge._spawn_reviewer") as spawn,
    ):
        result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_not_called()
    assert result.findings == []


def test_run_conftest_guard_blocks_real_spawn(tmp_path):
    # No explicit mock of judge._spawn_reviewer here: exercises the autouse
    # conftest guard directly. judge_body_matches degrades to [] when the
    # (neutralized) spawn returns no stdout, so the lens must not crash and
    # must not surface a phantom finding.
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    finding = _dup_finding()
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=[finding],
        ),
        patch("chameleon_mcp.duplication_review.gather_semantic_findings", return_value=[]),
    ):
        result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []


def test_run_produces_canonical_re_implements_finding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    finding = _dup_finding()
    verdict = [{"new_name": finding.new_name, "is_duplicate": True}]
    events = []
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=[finding],
        ),
        patch("chameleon_mcp.duplication_review.gather_semantic_findings", return_value=[]),
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)) as spawn,
    ):
        result = duplication.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append((kind, detail)),
        )

    spawn.assert_called_once()
    assert len(result.findings) == 1
    f = result.findings[0]
    assert isinstance(f, Finding)
    assert f.kind == "duplication"
    assert f.source_lens == "duplication"
    assert f.status == "pending"
    assert f.confidence == 1.0
    assert f.severity == "high"
    assert f.file == finding.new_file
    assert f.span == (finding.line, finding.line)
    assert f.claim == (
        "toDisplayDate (src/widget.ts:7) re-implements formatDate (src/dates.ts) — reuse it."
    )
    # event_sink is threaded the same events collected in check_events.
    assert [k for k, _ in events] == [k for k, _ in result.check_events]


def test_run_called_from_n_sites_folds_into_claim(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    finding = _dup_finding(called_from_n_sites=3)
    verdict = [{"new_name": finding.new_name, "is_duplicate": True}]
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=[finding],
        ),
        patch("chameleon_mcp.duplication_review.gather_semantic_findings", return_value=[]),
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)),
    ):
        result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    assert "already called from 3 sites" in result.findings[0].claim


def test_run_semantic_only_finding_still_confirmed(tmp_path):
    # Body-hash gather blind (different body), semantic gather surfaces the
    # same-intent candidate -- the lens must still confirm and surface it.
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    finding = _dup_finding(new_name="formatDateForDisplay")
    verdict = [{"new_name": finding.new_name, "is_duplicate": True}]
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch("chameleon_mcp.duplication_review.gather_body_match_findings", return_value=[]),
        patch(
            "chameleon_mcp.duplication_review.gather_semantic_findings",
            return_value=[finding],
        ),
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)),
    ):
        result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    assert len(result.findings) == 1
    assert result.findings[0].claim.startswith("formatDateForDisplay")


def test_run_unconfirmed_finding_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)
    finding = _dup_finding()
    verdict = [{"new_name": finding.new_name, "is_duplicate": False}]
    with (
        patch("chameleon_mcp.duplication_review.build_candidate_index", return_value=MagicMock()),
        patch(
            "chameleon_mcp.duplication_review.gather_body_match_findings",
            return_value=[finding],
        ),
        patch("chameleon_mcp.duplication_review.gather_semantic_findings", return_value=[]),
        patch("chameleon_mcp.judge._spawn_reviewer", return_value=_result_line(verdict)),
    ):
        result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []


def test_run_pipeline_error_is_caught(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = _write_source(repo)

    def _boom(*_a, **_k):
        raise RuntimeError("index build exploded")

    monkeypatch.setattr(
        "chameleon_mcp.duplication_review.build_candidate_index", _boom, raising=True
    )
    result = duplication.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert any(kind == "pipeline_error" for kind, _detail in result.check_events)
