"""build_candidate_index caps the session files it re-parses (P6 compute fix).

On a long execute turn the gate would re-parse every session file unbounded.
The index is the search space the gate checks edits AGAINST, so it is bounded to
CHAMELEON_DUPLICATION_INDEX_MAX_FILES; over the cap only the files the caller
ranked first (most-recently-edited) are parsed, and <=cap files are unchanged.
"""

from __future__ import annotations

import chameleon_mcp.exec_log as exec_log
import chameleon_mcp.tools as tools
from chameleon_mcp import duplication_review as dr
from chameleon_mcp import hook_helper
from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.enforcement import EnforcementState, FileState


def _make_repo(tmp_path, n: int) -> list[str]:
    # No .chameleon/ here, so load_function_catalog returns None and the index is
    # populated only from the parsed session files -- keeps the parse-call count
    # the sole source of indexed entries.
    paths = []
    for i in range(n):
        p = tmp_path / f"mod_{i:03d}.ts"
        p.write_text(f"export function fn{i}() {{ return {i}; }}\n", encoding="utf-8")
        paths.append(str(p))
    return paths


def test_index_over_cap_parses_exactly_cap_files(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap + 8)

    parsed: list[str] = []

    def _spy(repo_root, file_path):
        parsed.append(file_path)
        return []

    # build_candidate_index parses via the direct tools import, not dr._parse.
    monkeypatch.setattr(tools, "parse_edited_functions", _spy)

    dr.build_candidate_index(tmp_path, files)

    assert len(parsed) == cap
    # The caller passes files most-recent-first; the cap keeps that head.
    assert parsed == files[:cap]


def test_index_at_or_below_cap_parses_all(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap)

    parsed: list[str] = []

    def _spy(repo_root, file_path):
        parsed.append(file_path)
        return []

    monkeypatch.setattr(tools, "parse_edited_functions", _spy)

    dr.build_candidate_index(tmp_path, files)

    assert len(parsed) == cap
    assert parsed == files


def _state_with_recency(paths_oldest_first: list[str]) -> EnforcementState:
    state = EnforcementState()
    # Insertion order is oldest-first; last_verified_at increases with index, so
    # the most-recently-edited file is last in insertion order but should sort
    # FIRST out of the helper.
    for i, p in enumerate(paths_oldest_first):
        state.files[p] = FileState(last_verified_at=float(i))
    return state


def test_index_files_orders_most_recent_first(tmp_path):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap + 5)
    state = _state_with_recency(files)

    ordered = hook_helper._duplication_index_files(files, state, repo_id="r", session_id="s")

    # Reverse-chronological: the highest last_verified_at comes first.
    assert ordered == list(reversed(files))


def test_index_files_logs_dropped_over_cap(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    over = 5
    files = _make_repo(tmp_path, cap + over)
    state = _state_with_recency(files)

    events: list[dict] = []
    monkeypatch.setattr(
        exec_log,
        "append_check_event",
        lambda repo_id, **kw: events.append({"repo_id": repo_id, **kw}),
    )

    hook_helper._duplication_index_files(files, state, repo_id="r", session_id="s")

    trunc = [e for e in events if e.get("status") == "truncated"]
    assert len(trunc) == 1
    assert trunc[0]["reason"] == "index_files_capped"
    assert trunc[0]["detail"] == {"dropped": over, "cap": cap, "total": cap + over}


def test_index_files_no_log_at_or_below_cap(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap)
    state = _state_with_recency(files)

    events: list[dict] = []
    monkeypatch.setattr(
        exec_log,
        "append_check_event",
        lambda repo_id, **kw: events.append({"repo_id": repo_id, **kw}),
    )

    hook_helper._duplication_index_files(files, state, repo_id="r", session_id="s")

    assert [e for e in events if e.get("status") == "truncated"] == []
