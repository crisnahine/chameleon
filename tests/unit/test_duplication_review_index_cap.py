"""build_candidate_index caps the session files it re-parses (P6 compute fix).

On a long execute turn the gate would re-parse every session file unbounded.
The index is the search space the gate checks edits AGAINST, so it is bounded to
CHAMELEON_DUPLICATION_INDEX_MAX_FILES; over the cap only the files the caller
ranked first (most-recently-edited) are parsed, and <=cap files are unchanged.
"""

from __future__ import annotations

import os
import time

import chameleon_mcp.tools as tools
from chameleon_mcp import duplication_review as dr
from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.stop.lenses import duplication as dup_lens


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


def _stagger_mtimes(paths_oldest_first: list[str]) -> None:
    # Insertion order is oldest-first; mtime increases with index, so the
    # most-recently-modified file is last in insertion order but must sort
    # FIRST into the index input.
    base = time.time() - len(paths_oldest_first) - 10
    for i, p in enumerate(paths_oldest_first):
        os.utime(p, (base + i, base + i))


def _run_lens(tmp_path, files, monkeypatch, parsed: list[str], events: list[tuple[str, str]]):
    def _spy(repo_root, file_path):
        parsed.append(file_path)
        return []

    monkeypatch.setattr(tools, "parse_edited_functions", _spy)
    monkeypatch.setattr(dr, "gather_findings", lambda *a, **kw: [])
    return dup_lens.run(
        tmp_path,
        tmp_path,
        files,
        None,
        event_sink=lambda kind, detail=None: events.append((kind, detail or "")),
    )


def test_lens_index_orders_most_recent_first(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap + 5)
    _stagger_mtimes(files)

    parsed: list[str] = []
    _run_lens(tmp_path, files, monkeypatch, parsed, [])

    # Reverse-chronological head survives the cap: the freshest working set.
    assert parsed == list(reversed(files))[:cap]


def test_lens_logs_dropped_over_cap(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    over = 5
    files = _make_repo(tmp_path, cap + over)
    _stagger_mtimes(files)

    events: list[tuple[str, str]] = []
    _run_lens(tmp_path, files, monkeypatch, [], events)

    capped = [e for e in events if e[0] == "index_files_capped"]
    assert capped == [("index_files_capped", f"dropped:{over};cap:{cap}")]


def test_lens_no_event_at_or_below_cap(tmp_path, monkeypatch):
    cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
    files = _make_repo(tmp_path, cap)
    _stagger_mtimes(files)

    events: list[tuple[str, str]] = []
    _run_lens(tmp_path, files, monkeypatch, [], events)

    assert [e for e in events if e[0] == "index_files_capped"] == []


def test_lens_gather_receives_freshest_first(tmp_path, monkeypatch):
    # gather_findings' own DUPLICATION_REVIEW_MAX_FILES slice is order-
    # dependent too: it must see the same most-recent-first view the index
    # gets, so an over-cap turn checks the freshest edits, not the oldest.
    files = _make_repo(tmp_path, 6)
    _stagger_mtimes(files)

    seen: list[list[str]] = []

    def _spy_gather(root, edited, **kw):
        seen.append(list(edited))
        return []

    monkeypatch.setattr(tools, "parse_edited_functions", lambda r, p: [])
    monkeypatch.setattr(dr, "gather_findings", _spy_gather)
    dup_lens.run(tmp_path, tmp_path, files, None)

    assert seen == [list(reversed(files))]
