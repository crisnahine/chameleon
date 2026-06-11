"""Tests for the judge's caller-facts block (committed calls-index grounding).

At turn end the correctness judge's prompt gains a bounded block of caller
facts for the callables the turn actually changed, read from the committed
``calls_index.json`` snapshot. These tests pin the hunk parser, the block
format (sites, [+N more], lower-bound suffix, no-callers wording), the
changed-callable selection (span x hunk intersection, whole-file diffs), the
char cap's line-boundary truncation, the build_prompt insertion, the config
flag, and the run_correctness_judge wiring with its judge_facts_* sink kinds.
The real parse is stubbed through the ``_parse_changed_file`` indirection so
no node/ruby toolchain is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp import judge
from chameleon_mcp.function_catalog import ParsedFn
from chameleon_mcp.judge import FileDiff

HEADER = (
    "Cross-file callers of the changed functions "
    "(snapshot at profile derivation; deterministic grades only):"
)


def _result_line(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _write_calls_index(repo: Path, callees: dict) -> None:
    d = repo / ".chameleon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calls_index.json").write_text(
        json.dumps({"schema_version": 1, "callees": callees}), encoding="utf-8"
    )


def _caller(path: str, caller: str, line: int | None = 3, grade: str = "import") -> dict:
    return {"path": path, "caller": caller, "line": line, "grade": grade}


def _fn(name: str, start: int | None, end: int | None) -> ParsedFn:
    return ParsedFn(name, "function", 0, 0, start, None, None, "", end_line=end)


def _diff(rel: str, hunks: str, *, whole: bool = False) -> FileDiff:
    return FileDiff(rel_path=rel, archetype=None, diff_text=hunks, is_whole_file=whole)


# --- _changed_lines ----------------------------------------------------------


def test_changed_lines_single_hunk():
    assert judge._changed_lines("@@ -1,3 +10,4 @@ ctx\n+x\n") == [(10, 13)]


def test_changed_lines_multi_hunk():
    text = "@@ -1,2 +5,2 @@\n+a\n@@ -9,1 +20,3 @@\n+b\n"
    assert judge._changed_lines(text) == [(5, 6), (20, 22)]


def test_changed_lines_count_omitted_means_one_line():
    assert judge._changed_lines("@@ -5 +7 @@\n+x\n") == [(7, 7)]


def test_changed_lines_zero_count_is_empty_range_at_start():
    # A pure deletion hunk (+N,0) still anchors at N without going negative.
    assert judge._changed_lines("@@ -3,2 +2,0 @@\n-x\n") == [(2, 2)]


def test_changed_lines_empty_and_non_hunk_text():
    assert judge._changed_lines("") == []
    assert judge._changed_lines("no hunks here") == []


# --- caller_facts_for_diffs: block format ------------------------------------


def test_block_lists_sites_more_count_and_lower_bound(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    callers = [
        _caller("src/a.ts", "render", 3),
        _caller("src/b.ts", "useThing", 14),
        _caller("src/c.ts", "<module>", None),
        _caller("src/d.ts", "main", 7),
        _caller("src/e.ts", "init", 1),
        _caller("src/f.ts", "boot", 2),
    ]
    _write_calls_index(
        repo,
        {"src/util.ts": {"formatDate": {"callers": callers, "total": 7, "truncated": True}}},
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("formatDate", 8, 14)])

    block = judge.caller_facts_for_diffs(repo, [_diff("src/util.ts", "@@ -8,3 +10,2 @@\n+x\n")])

    assert block.splitlines()[0] == HEADER
    assert "- formatDate() in src/util.ts: 7 committed callers, e.g. " in block
    assert "src/a.ts:3 (render)" in block
    assert "src/b.ts:14 (useThing)" in block
    # A line-less caller renders without a :line suffix.
    assert "src/c.ts (<module>)" in block
    # 7 total, 5 shown (JUDGE_FACTS_MAX_SITES) -> 2 more; truncated -> lower bound.
    assert "src/f.ts" not in block
    assert "[+2 more]" in block
    assert "(count is a lower bound)" in block


def test_block_no_callers_wording_for_unrecorded_and_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {"src/util.ts": {"unused": {"callers": [], "total": 0, "truncated": False}}},
    )
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [_fn("unused", 1, 3), _fn("brandNew", 5, 9)],
    )

    block = judge.caller_facts_for_diffs(repo, [_diff("src/util.ts", "", whole=True)])

    expected = "no committed callers found (new, unused, or called dynamically)"
    assert f"- unused() in src/util.ts: {expected}" in block
    assert f"- brandNew() in src/util.ts: {expected}" in block


def test_absent_index_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f", 1, 2)])
    assert judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)]) == ""


def test_corrupt_index_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "calls_index.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f", 1, 2)])
    assert judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)]) == ""


def test_no_changed_callables_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(repo, {"a.ts": {"f": {"callers": [], "total": 0, "truncated": False}}})
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [])
    assert judge.caller_facts_for_diffs(repo, [_diff("a.ts", "@@ -1 +1 @@\n+x\n")]) == ""


def test_parse_exception_skips_file_never_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {"b.ts": {"g": {"callers": [_caller("c.ts", "h")], "total": 1, "truncated": False}}},
    )

    def boom_then_fn(root, path):
        if str(path).endswith("a.ts"):
            raise RuntimeError("parser exploded")
        return [_fn("g", 1, 2)]

    monkeypatch.setattr(judge, "_parse_changed_file", boom_then_fn)
    block = judge.caller_facts_for_diffs(
        repo, [_diff("a.ts", "", whole=True), _diff("b.ts", "", whole=True)]
    )
    # The raising file is skipped; the healthy file's facts still render.
    assert "- g() in b.ts: 1 committed caller" in block


# --- changed-callable selection ----------------------------------------------


def test_function_outside_hunks_is_excluded(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {
                "untouched": {"callers": [], "total": 0, "truncated": False},
                "edited": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [_fn("untouched", 1, 5), _fn("edited", 10, 12)],
    )
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "@@ -10,2 +10,2 @@\n+x\n")])
    assert "edited()" in block
    assert "untouched()" not in block


def test_whole_file_diff_includes_all_functions(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {
                "one": {"callers": [], "total": 0, "truncated": False},
                "two": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        # ``two`` has no recorded span: a whole-file diff still includes it.
        lambda root, path: [_fn("one", 1, 5), _fn("two", None, None)],
    )
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert "one()" in block and "two()" in block


def test_spanless_function_excluded_from_hunk_diff(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(repo, {"a.ts": {"two": {"callers": [], "total": 0, "truncated": False}}})
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("two", None, None)])
    # No span -> cannot intersect -> not claimed as changed.
    assert judge.caller_facts_for_diffs(repo, [_diff("a.ts", "@@ -1 +1 @@\n+x\n")]) == ""


def test_callable_cap_spans_all_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {"fa": {"callers": [], "total": 0, "truncated": False}},
            "b.ts": {"fb": {"callers": [], "total": 0, "truncated": False}},
        },
    )
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_MAX_CALLABLES", "1")
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [_fn("fa" if str(path).endswith("a.ts") else "fb", 1, 2)],
    )
    block = judge.caller_facts_for_diffs(
        repo, [_diff("a.ts", "", whole=True), _diff("b.ts", "", whole=True)]
    )
    assert "fa()" in block
    assert "fb()" not in block


# --- char cap ----------------------------------------------------------------


def test_char_cap_truncates_at_line_boundary(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {
                "first": {"callers": [], "total": 0, "truncated": False},
                "second": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [_fn("first", 1, 2), _fn("second", 3, 4)],
    )
    cap = (
        len(HEADER)
        + 1
        + len("- first() in a.ts: no committed callers found (new, unused, or called dynamically)")
    )
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", str(cap))
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert len(block) <= cap
    assert "first()" in block
    assert "second()" not in block
    # Every surviving line is whole: the cut happened at a line boundary.
    assert block.splitlines()[-1].endswith("(new, unused, or called dynamically)")


def test_char_cap_too_small_for_any_fact_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(repo, {"a.ts": {"f": {"callers": [], "total": 0, "truncated": False}}})
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f", 1, 2)])
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", "10")
    assert judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)]) == ""


# --- build_prompt insertion ---------------------------------------------------


def test_build_prompt_includes_caller_facts_block(tmp_path):
    profile = tmp_path / ".chameleon"
    profile.mkdir()
    diffs = [_diff("a.ts", "+x\n")]
    prompt = judge.build_prompt(
        tmp_path, profile, diffs, caller_facts="CALLER_FACTS_SENTINEL_BLOCK"
    )
    assert "CALLER_FACTS_SENTINEL_BLOCK" in prompt
    # The facts ride above the diffs so the reviewer reads consumers first.
    assert prompt.index("CALLER_FACTS_SENTINEL_BLOCK") < prompt.index("=== a.ts")


def test_build_prompt_omits_facts_when_none(tmp_path):
    profile = tmp_path / ".chameleon"
    profile.mkdir()
    prompt = judge.build_prompt(tmp_path, profile, [_diff("a.ts", "+x\n")], caller_facts=None)
    assert "Cross-file callers" not in prompt


# --- config flag ---------------------------------------------------------------


def test_config_judge_crossfile_facts_default_on():
    from chameleon_mcp.profile.config import EnforcementConfig, _coerce_enforcement

    assert EnforcementConfig().judge_crossfile_facts is True
    assert _coerce_enforcement(None).judge_crossfile_facts is True
    # The correctness_judge off-by-default trap must NOT be repeated.
    assert _coerce_enforcement({"mode": "shadow"}).judge_crossfile_facts is True


def test_config_judge_crossfile_facts_explicit_off():
    from chameleon_mcp.profile.config import _coerce_enforcement

    assert _coerce_enforcement({"judge_crossfile_facts": False}).judge_crossfile_facts is False


def test_config_judge_crossfile_facts_type_validated():
    from chameleon_mcp.profile.config import ChameleonConfigError, _coerce_enforcement

    with pytest.raises(ChameleonConfigError):
        _coerce_enforcement({"judge_crossfile_facts": "yes"})


# --- real extractor end-to-end -------------------------------------------------

_NODE_MODULES = Path(__file__).resolve().parents[2] / "mcp" / "node_modules" / "typescript"


def _have_ts() -> bool:
    import shutil

    return shutil.which("node") is not None and _NODE_MODULES.is_dir()


@pytest.mark.skipif(not _have_ts(), reason="node + typescript node_modules not available")
def test_real_parse_spans_intersect_hunks(tmp_path):
    # No parse stub: the real ts_dump extraction supplies the spans, proving
    # end_line flows through parse_edited_functions into the intersection.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "src" / "util.ts").write_text(
        "export function untouched() {\n"
        "  return 1\n"
        "}\n"
        "\n"
        "export function formatDate(d: Date) {\n"
        "  return d.toISOString()\n"
        "}\n",
        encoding="utf-8",
    )
    _write_calls_index(
        repo,
        {
            "src/util.ts": {
                "formatDate": {
                    "callers": [_caller("src/a.ts", "render", 3)],
                    "total": 1,
                    "truncated": False,
                },
                "untouched": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    # Hunk covers only formatDate's body (lines 5-7).
    block = judge.caller_facts_for_diffs(repo, [_diff("src/util.ts", "@@ -5,3 +5,3 @@\n+x\n")])
    assert "- formatDate() in src/util.ts: 1 committed caller, e.g. src/a.ts:3 (render)" in block
    assert "untouched()" not in block


# --- run_correctness_judge wiring ----------------------------------------------


def _wired_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    src = repo / "util.ts"
    src.write_text("export function fmt() { return 1 }\n", encoding="utf-8")
    return repo, profile, src


def test_run_judge_passes_facts_to_prompt_and_sinks_included(tmp_path, monkeypatch):
    repo, profile, src = _wired_repo(tmp_path)
    _write_calls_index(
        repo,
        {"util.ts": {"fmt": {"callers": [_caller("b.ts", "use")], "total": 1, "truncated": False}}},
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fmt", 1, 1)])
    captured = {}

    def fake_build(repo_root, profile_dir, diffs, intent_tokens=None, caller_facts=None):
        captured["caller_facts"] = caller_facts
        return "prompt"

    events = []
    with (
        patch.object(judge, "build_prompt", side_effect=fake_build),
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
    ):
        judge.run_correctness_judge(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert captured["caller_facts"] is not None
    assert "- fmt() in util.ts: 1 committed caller" in captured["caller_facts"]
    assert "judge_facts_included" in events


def test_run_judge_config_off_sinks_skipped_disabled(tmp_path, monkeypatch):
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

    def fake_build(repo_root, profile_dir, diffs, intent_tokens=None, caller_facts=None):
        captured["caller_facts"] = caller_facts
        return "prompt"

    events = []
    with (
        patch.object(judge, "build_prompt", side_effect=fake_build),
        patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line("[]"), None)),
    ):
        judge.run_correctness_judge(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    assert captured["caller_facts"] is None
    assert "judge_facts_skipped_disabled" in events
    assert "judge_facts_included" not in events


def test_run_judge_no_index_sinks_skipped_and_still_reviews(tmp_path, monkeypatch):
    repo, profile, src = _wired_repo(tmp_path)
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fmt", 1, 1)])
    stream = _result_line(json.dumps([{"message": "bug", "confidence": 0.8}]))
    events = []
    with patch.object(judge, "_spawn_reviewer_status", return_value=(stream, None)):
        findings = judge.run_correctness_judge(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append(kind),
        )
    # Facts absent, but the judge itself still ran and returned findings.
    assert [f.message for f in findings] == ["bug"]
    assert "judge_facts_skipped_no_calls_index" in events


def test_run_judge_facts_failure_never_blocks_review(tmp_path, monkeypatch):
    repo, profile, src = _wired_repo(tmp_path)
    _write_calls_index(
        repo,
        {"util.ts": {"fmt": {"callers": [_caller("b.ts", "use")], "total": 1, "truncated": False}}},
    )

    def boom(root, path):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(judge, "_parse_changed_file", boom)
    stream = _result_line(json.dumps([{"message": "bug", "confidence": 0.8}]))
    with patch.object(judge, "_spawn_reviewer_status", return_value=(stream, None)):
        findings = judge.run_correctness_judge(repo, profile, [str(src)], lambda _p: None)
    assert [f.message for f in findings] == ["bug"]
