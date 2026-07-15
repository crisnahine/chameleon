"""Tests for the judge's caller-facts block (committed calls-index grounding).

At turn end the correctness judge's prompt gains a bounded block of caller
facts for the callables the turn actually changed, read from the committed
``calls_index.json`` snapshot. These tests pin the hunk parser, the block
format (sites, [+N more], lower-bound suffix, no-callers wording), the
changed-callable selection (span x hunk intersection, whole-file diffs), the
char cap's line-boundary truncation, the build_prompt insertion, and the
config flag. The real parse is stubbed through the ``_parse_changed_file`` indirection so
no node/ruby toolchain is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp import judge
from chameleon_mcp.calls_index import SCHEMA_VERSION as _CALLS_SCHEMA
from chameleon_mcp.function_catalog import ParsedFn
from chameleon_mcp.judge import FileDiff

HEADER = (
    "Committed callers of the changed functions "
    "(snapshot at profile derivation; deterministic grades only):"
)


def _result_line(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _write_calls_index(repo: Path, callees: dict) -> None:
    d = repo / ".chameleon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calls_index.json").write_text(
        json.dumps({"schema_version": _CALLS_SCHEMA, "callees": callees}), encoding="utf-8"
    )
    # The caller-facts/transitive blocks now re-verify each cited caller against
    # the working tree (a deleted/no-longer-calling caller is dropped), so the
    # synthetic callers must exist on disk and still name the callee at the
    # recorded line.
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


def test_constructor_kind_rows_are_skipped(tmp_path, monkeypatch):
    # The TS index records `new Klass()` under the exported class name, never
    # under "constructor"; rendering a "constructor() ... no committed callers"
    # line for a changed constructor would be a false claim about a row the
    # index keys differently, so constructor-kind rows say nothing at all.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "src/k.ts": {
                "Klass": {
                    "callers": [_caller("src/use.ts", "boot")],
                    "total": 1,
                    "truncated": False,
                },
                "helper": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    ctor = ParsedFn("constructor", "constructor", 0, 0, 2, None, None, "", end_line=4)
    monkeypatch.setattr(
        judge, "_parse_changed_file", lambda root, path: [ctor, _fn("helper", 6, 8)]
    )
    block = judge.caller_facts_for_diffs(repo, [_diff("src/k.ts", "", whole=True)])
    assert "constructor()" not in block
    assert "helper()" in block


def test_only_constructor_changed_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {"src/k.ts": {"Klass": {"callers": [], "total": 0, "truncated": False}}},
    )
    ctor = ParsedFn("constructor", "constructor", 0, 0, 2, None, None, "", end_line=4)
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [ctor])
    assert judge.caller_facts_for_diffs(repo, [_diff("src/k.ts", "", whole=True)]) == ""


def test_function_merely_named_constructor_still_renders(tmp_path, monkeypatch):
    # The skip keys on kind, not name: a plain function someone named
    # "constructor" IS indexed under that name, so its facts still render.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "src/k.ts": {
                "constructor": {
                    "callers": [_caller("src/k.ts", "boot", 7, "same_file")],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("constructor", 1, 3)])
    block = judge.caller_facts_for_diffs(repo, [_diff("src/k.ts", "", whole=True)])
    assert "- constructor() in src/k.ts: 1 committed caller" in block


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


def test_char_cap_truncates_at_line_boundary_with_tail(tmp_path, monkeypatch):
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
    first_line = (
        "- first() in a.ts: no committed callers found (new, unused, or called dynamically)"
    )
    tail = "(+1 more changed callable not shown)"
    # Room for header + first fact + the tail: the tail is reserved within the
    # cap, never appended over it.
    cap = len(HEADER) + 1 + len(first_line) + 1 + len(tail)
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", str(cap))
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert len(block) <= cap
    assert "first()" in block
    assert "second()" not in block
    # Every surviving line is whole (line-boundary cut) and the block says how
    # many callable lines the cap dropped.
    assert block.splitlines() == [HEADER, first_line, tail]


def test_char_cap_tail_counts_all_dropped_lines(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {
                "first": {"callers": [], "total": 0, "truncated": False},
                "second": {"callers": [], "total": 0, "truncated": False},
                "third": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [
            _fn("first", 1, 2),
            _fn("second", 3, 4),
            _fn("third", 5, 6),
        ],
    )
    first_line = (
        "- first() in a.ts: no committed callers found (new, unused, or called dynamically)"
    )
    tail = "(+2 more changed callables not shown)"
    cap = len(HEADER) + 1 + len(first_line) + 1 + len(tail)
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", str(cap))
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert len(block) <= cap
    assert block.splitlines()[-1] == tail
    assert "second()" not in block and "third()" not in block


def test_no_tail_when_nothing_dropped(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {"a.ts": {"first": {"callers": [], "total": 0, "truncated": False}}},
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("first", 1, 2)])
    block = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert "first()" in block
    assert "more changed callable" not in block


def test_tail_singular_when_one_dropped_plural_when_more(tmp_path, monkeypatch):
    # N==1: "callable" (singular); N>1: "callables" (plural). Pin both.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "a.ts": {
                "first": {"callers": [], "total": 0, "truncated": False},
                "second": {"callers": [], "total": 0, "truncated": False},
                "third": {"callers": [], "total": 0, "truncated": False},
            }
        },
    )
    first_line = (
        "- first() in a.ts: no committed callers found (new, unused, or called dynamically)"
    )
    second_line = (
        "- second() in a.ts: no committed callers found (new, unused, or called dynamically)"
    )
    singular_tail = "(+1 more changed callable not shown)"
    plural_tail = "(+2 more changed callables not shown)"

    # singular: cap allows header + first + second + singular_tail (drops third only)
    cap_singular = len(HEADER) + 1 + len(first_line) + 1 + len(second_line) + 1 + len(singular_tail)
    monkeypatch.setattr(
        judge,
        "_parse_changed_file",
        lambda root, path: [
            _fn("first", 1, 2),
            _fn("second", 3, 4),
            _fn("third", 5, 6),
        ],
    )
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", str(cap_singular))
    block_one = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert block_one.splitlines()[-1] == singular_tail, (
        f"N=1 tail must be singular; got {block_one.splitlines()[-1]!r}"
    )

    # plural: cap allows header + first + plural_tail (drops second and third)
    cap_plural = len(HEADER) + 1 + len(first_line) + 1 + len(plural_tail)
    monkeypatch.setenv("CHAMELEON_JUDGE_FACTS_CHAR_CAP", str(cap_plural))
    block_two = judge.caller_facts_for_diffs(repo, [_diff("a.ts", "", whole=True)])
    assert block_two.splitlines()[-1] == plural_tail, (
        f"N=2 tail must be plural; got {block_two.splitlines()[-1]!r}"
    )


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
    assert "Committed callers" not in prompt


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

_NODE_MODULES = (
    Path(__file__).resolve().parents[2] / "plugin" / "mcp" / "node_modules" / "typescript"
)


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


# --- hostile-content sanitization --------------------------------------------


def test_caller_facts_strips_esc_and_newline_from_index_strings(tmp_path, monkeypatch):
    """Artifact-derived strings (path, caller name) are sanitized before entering the facts block.

    ESC (\x1b) is a C0 control stripped by sanitize_for_chameleon_context; a bare
    newline (\n) is explicitly preserved by the sanitizer (it is valid whitespace),
    so the line must remain intact even when the caller path contains one. Both
    characters are picked from what sanitization.py genuinely strips/preserves.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # ESC byte in the caller path; newline in the caller name.
    hostile_path = "src/\x1bevil.ts"
    hostile_caller = "render\ninjected"
    _write_calls_index(
        repo,
        {
            "src/util.ts": {
                "doWork": {
                    "callers": [
                        {
                            "path": hostile_path,
                            "caller": hostile_caller,
                            "line": 5,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("doWork", 1, 3)])

    block = judge.caller_facts_for_diffs(repo, [_diff("src/util.ts", "", whole=True)])

    # ESC byte must be stripped.
    assert "\x1b" not in block, "ESC byte leaked into facts block"
    # The sanitized path still contributes to the line (safe chars preserved).
    assert "src/evil.ts" in block
    # The line is intact (not split mid-output by the injected newline).
    assert "doWork() in src/util.ts" in block


# --- caller-contract directive (C3.2): facts become an active check ----------


def test_build_prompt_adds_caller_contract_directive_with_caller_facts():
    prompt = judge.build_prompt(
        Path("/r"),
        Path("/r/.chameleon"),
        [_diff("src/util.ts", "@@ -1 +1 @@\n+export function f(a) {}\n")],
        caller_facts="Committed callers of the changed functions:\nf -> src/a.ts:3",
    )
    low = prompt.lower()
    assert "listed call site" in low
    assert "signature" in low
    assert "return shape" in low
    assert "throw" in low or "raise" in low


def test_build_prompt_omits_caller_contract_directive_without_caller_facts():
    prompt = judge.build_prompt(
        Path("/r"),
        Path("/r/.chameleon"),
        [_diff("src/util.ts", "@@ -1 +1 @@\n+x\n")],
    )
    assert "listed call site" not in prompt.lower()


# --- stale-caller re-verification (the live-drop path) ------------------------


def _raw_calls_index(repo: Path, callees: dict) -> None:
    # Writes ONLY the index (no caller files), so callers are stale unless the
    # test creates the file itself -- the exact case the live re-verify handles.
    d = repo / ".chameleon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calls_index.json").write_text(
        json.dumps({"schema_version": _CALLS_SCHEMA, "callees": callees}), encoding="utf-8"
    )


def test_caller_facts_drops_deleted_caller_and_recomputes_count(tmp_path, monkeypatch):
    _raw_calls_index(
        tmp_path,
        {
            "util.ts": {
                "helper": {
                    "callers": [
                        {"path": "live.ts", "caller": "a", "line": 1, "grade": "import"},
                        {"path": "gone.ts", "caller": "b", "line": 1, "grade": "import"},
                    ],
                    "total": 2,
                    "truncated": False,
                }
            }
        },
    )
    (tmp_path / "live.ts").write_text("return helper();\n", encoding="utf-8")  # gone.ts absent
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("helper", 1, 1)])
    block = judge.caller_facts_for_diffs(tmp_path, [_diff("util.ts", "", whole=True)])
    assert "live.ts" in block and "gone.ts" not in block
    assert "1 committed caller" in block  # recomputed from snapshot total 2 -> 1 live


def test_caller_facts_omits_callable_when_all_callers_stale(tmp_path, monkeypatch):
    # A renamed/deleted-only caller must NOT produce a false "no callers" line that
    # steers the reviewer to skip a real caller: the callable is omitted entirely.
    _raw_calls_index(
        tmp_path,
        {
            "util.ts": {
                "helper": {
                    "callers": [{"path": "gone.ts", "caller": "b", "line": 1, "grade": "import"}],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("helper", 1, 1)])
    block = judge.caller_facts_for_diffs(tmp_path, [_diff("util.ts", "", whole=True)])
    assert block == ""  # callable omitted -> only the header remains -> ""
    assert "no live callers" not in block and "no committed callers" not in block


def test_caller_facts_drops_caller_that_no_longer_references(tmp_path, monkeypatch):
    _raw_calls_index(
        tmp_path,
        {
            "util.ts": {
                "helper": {
                    "callers": [{"path": "x.ts", "caller": "a", "line": 1, "grade": "import"}],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    (tmp_path / "x.ts").write_text("return somethingElse();\n", encoding="utf-8")  # no 'helper'
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("helper", 1, 1)])
    block = judge.caller_facts_for_diffs(tmp_path, [_diff("util.ts", "", whole=True)])
    assert block == ""  # the only caller no longer references helper -> omitted


def test_caller_facts_truncated_all_stale_no_phantom_more(tmp_path, monkeypatch):
    callers = [{"path": f"g{i}.ts", "caller": "c", "line": 1, "grade": "import"} for i in range(50)]
    _raw_calls_index(
        tmp_path, {"util.ts": {"helper": {"callers": callers, "total": 50, "truncated": True}}}
    )
    # No caller files -> every sampled site is stale; truncated keeps the snapshot
    # total as a lower bound but must NOT print an example-less "[+N more]".
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("helper", 1, 1)])
    block = judge.caller_facts_for_diffs(tmp_path, [_diff("util.ts", "", whole=True)])
    assert "50 committed callers" in block
    assert "[+" not in block  # no phantom doubled count
    assert "e.g." not in block  # no examples shown
    assert "lower bound" in block


def test_caller_needle_handles_ruby_setter_and_operators():
    assert judge._caller_needle("url=").search("record.url = v") is not None  # setter -> base name
    assert judge._caller_needle("foo?").search("obj.foo?") is not None  # predicate keeps suffix
    assert judge._caller_needle("[]=") is None  # operator method -> unverifiable -> keep
    assert judge._caller_needle("[]") is None
