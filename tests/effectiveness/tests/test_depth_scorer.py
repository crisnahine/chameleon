"""The depth scorer decides whether the turn-depth claim is measurable at all.

Its failure mode is silent: a bug that mis-orders events, or drops the late
half, yields a plausible number rather than an error, and the run publishes a
decay slope that describes the parser instead of the model. So the cases below
pin ordering, the unscored floors, and the direction of the metric.
"""

from __future__ import annotations

import json
import types

import pytest
from tests.effectiveness.scorers import depth


def _transcript(tmp_path, writes, *, other_tools_between=False):
    """writes: list of (path, content). One assistant message per write."""
    lines = []
    for i, (path, content) in enumerate(writes):
        if other_tools_between and i:
            lines.append(
                json.dumps(
                    {
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                            ]
                        }
                    }
                )
            )
        lines.append(
            json.dumps(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": path, "content": content},
                            }
                        ]
                    }
                }
            )
        )
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines))
    return p


def _ctx(tmp_path, transcript):
    return types.SimpleNamespace(transcript_path=transcript, worktree=tmp_path)


def _patch_lint(monkeypatch, per_path):
    monkeypatch.setattr(depth, "_pattern_context", lambda p: {"data": {"archetype": "c"}})
    monkeypatch.setattr(
        depth,
        "_lint",
        lambda *, repo, archetype, content, file_path: {
            "data": {"violations": [{"rule": "r"}] * per_path[file_path]}
        },
    )


def test_reports_decay_when_late_files_are_worse(tmp_path, monkeypatch):
    writes = [(f"/r/a{i}.ts", "x") for i in range(6)]
    _patch_lint(monkeypatch, {f"/r/a{i}.ts": (0 if i < 3 else 4) for i in range(6)})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["depth_files_scored"] == 6
    assert out["depth_early_viol"] == 0.0
    assert out["depth_late_viol"] == 4.0
    assert out["depth_decay"] == 4.0  # positive = degraded with depth


def test_flat_session_reports_zero_decay(tmp_path, monkeypatch):
    writes = [(f"/r/a{i}.ts", "x") for i in range(6)]
    _patch_lint(monkeypatch, {f"/r/a{i}.ts": 2 for i in range(6)})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["depth_decay"] == 0.0


def test_improvement_reports_negative_decay(tmp_path, monkeypatch):
    """A session that gets BETTER with depth must not read as degradation."""
    writes = [(f"/r/a{i}.ts", "x") for i in range(6)]
    _patch_lint(monkeypatch, {f"/r/a{i}.ts": (5 if i < 3 else 1) for i in range(6)})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["depth_decay"] == -4.0


def test_authoring_order_is_transcript_order_not_path_order(tmp_path, monkeypatch):
    """Ordering by filename would invert this case and report the wrong sign."""
    writes = [("/r/zzz.ts", "x"), ("/r/mmm.ts", "x"), ("/r/aaa.ts", "x"), ("/r/bbb.ts", "x")]
    _patch_lint(monkeypatch, {"/r/zzz.ts": 0, "/r/mmm.ts": 0, "/r/aaa.ts": 6, "/r/bbb.ts": 6})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["depth_decay"] == 6.0


def test_rewritten_file_counts_at_its_last_authoring(tmp_path, monkeypatch):
    """A superseded first draft must not charge the early half for work the
    session itself replaced."""
    writes = [
        ("/r/a.ts", "v1"),
        ("/r/b.ts", "x"),
        ("/r/c.ts", "x"),
        ("/r/d.ts", "x"),
        ("/r/a.ts", "v2"),
    ]
    _patch_lint(monkeypatch, {p: 0 for p in ("/r/a.ts", "/r/b.ts", "/r/c.ts", "/r/d.ts")})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["depth_files_scored"] == 4  # a.ts once, not twice


def test_turn_index_advances_only_on_tool_turns(tmp_path, monkeypatch):
    writes = [(f"/r/a{i}.ts", "x") for i in range(4)]
    _patch_lint(monkeypatch, {f"/r/a{i}.ts": 0 for i in range(4)})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes, other_tools_between=True)))
    assert out["depth_last_turn"] >= 3  # interleaved Bash turns push it out


@pytest.mark.parametrize("n", [0, 1, 3])
def test_too_few_files_is_unscored_not_zero(tmp_path, monkeypatch, n):
    """A fabricated 0 decay would read as 'perfectly flat' -- the exact claim
    this scorer exists to test."""
    writes = [(f"/r/a{i}.ts", "x") for i in range(n)]
    _patch_lint(monkeypatch, {f"/r/a{i}.ts": 0 for i in range(n)})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert "unscored" in out and "depth_decay" not in out


def test_non_source_writes_are_ignored(tmp_path, monkeypatch):
    writes = [("/r/README.md", "x"), ("/r/notes.txt", "x")]
    _patch_lint(monkeypatch, {})
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert out["unscored"] == "session authored no lintable source files"


def test_unreadable_transcript_is_unscored(tmp_path):
    out = depth.score(_ctx(tmp_path, tmp_path / "missing.jsonl"))
    assert "unscored" in out


def test_lint_exception_does_not_crash_the_run(tmp_path, monkeypatch):
    writes = [(f"/r/a{i}.ts", "x") for i in range(6)]
    monkeypatch.setattr(depth, "_pattern_context", lambda p: {"data": {"archetype": "c"}})
    monkeypatch.setattr(depth, "_lint", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = depth.score(_ctx(tmp_path, _transcript(tmp_path, writes)))
    assert "unscored" in out  # degrades to unscored, never a fabricated number
