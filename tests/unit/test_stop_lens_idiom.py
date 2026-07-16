"""stop/lenses/idiom.py: the NEW idiom lens (spec 2026-07-14 section 5.2).

Unlike the legacy ``_idiom_review_gate`` (test_idiom_review.py, untouched by
this task), the idiom lens scopes the taught-idiom STORE to the turn's diff
(languages / archetypes / paths, empty dimension = wildcard --
``core.idiom_store.idioms_for_scope``) and spawns a reviewer only when at
least one idiom is in scope. Every surviving claim must cite the violated
idiom's slug AND the offending diff line numbers; a claim missing either is
dropped.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from chameleon_mcp import judge
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.core.idiom_store import IdiomRecord, upsert_idiom
from chameleon_mcp.stop.lenses import idiom


def _result_line(payload) -> str:
    return json.dumps({"type": "result", "result": json.dumps(payload)})


def _rec(**over):
    base = dict(
        slug="wrap-fetches",
        title="wrap-fetches",
        rationale="Always wrap fetches in the apiClient helper.",
        languages=["typescript"],
        archetypes=[],
        paths=[],
        status="active",
        added_date="2026-07-15",
        rank=1,
    )
    base.update(over)
    return IdiomRecord(**base)


def _write_ts(
    repo, rel="src/widget.ts", body="export function fetchThing() {\n  return fetch('/x')\n}\n"
):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = repo / ".chameleon"
    profile.mkdir()
    return repo, profile


# --- empty-scope silence -----------------------------------------------------


def test_run_no_idioms_taught_no_scope_no_spawn(tmp_path):
    repo, profile = _repo(tmp_path)
    src = _write_ts(repo)
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_not_called()
    assert result.findings == []
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


def test_run_docs_only_turn_no_scope_no_spawn(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(languages=[]))  # wildcard: would apply to any source
    md = repo / "notes.md"
    md.write_text("scratch\n", encoding="utf-8")
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(md)], lambda _p: None)
    spawn.assert_not_called()
    assert result.findings == []
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


def test_run_language_scoped_idiom_excluded_for_unedited_language(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(slug="ruby-only", title="ruby-only", languages=["ruby"]))
    src = _write_ts(repo)  # typescript, not ruby
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_not_called()
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


def test_run_archetype_scoped_idiom_excluded_when_archetype_mismatches(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="svc-only", title="svc-only", languages=[], archetypes=["service"]),
    )
    src = _write_ts(repo)
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: "controller")
    spawn.assert_not_called()
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


def test_run_path_scoped_idiom_excluded_when_path_mismatches(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="models-only", title="models-only", languages=[], paths=["app/models/**"]),
    )
    src = _write_ts(repo, rel="app/other/thing.ts")
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_not_called()
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


# --- scoping matrix: in-scope cases spawn a reviewer -------------------------


def test_run_wildcard_language_idiom_stays_in_scope(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(languages=[]))  # wildcard on every dimension
    src = _write_ts(repo)
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_called_once()
    assert result.findings == []


def test_run_notebook_only_turn_governed_as_python(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(slug="py-thresholds", title="py-thresholds", languages=["python"]))
    nb = repo / "analysis.ipynb"
    nb.write_text('{"cells": []}\n', encoding="utf-8")
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        idiom.run(repo, profile, [str(nb)], lambda _p: None)
    spawn.assert_called_once()


def test_run_archetype_scoped_idiom_included_when_archetype_matches(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="svc-only", title="svc-only", languages=[], archetypes=["service"]),
    )
    src = _write_ts(repo)
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        idiom.run(repo, profile, [str(src)], lambda _p: "service")
    spawn.assert_called_once()


def test_run_archetype_scoped_idiom_excluded_when_no_file_resolves_an_archetype(tmp_path):
    # idioms_for_scope reads an empty CALLER archetype set as a wildcard too,
    # so without the lens's own post-filter an archetype-TAGGED idiom would
    # leak into scope (and spawn a reviewer) on a turn whose governed files
    # all resolve archetype None -- ordinary for utility/script files the
    # detector doesn't classify. Spec section 5.2's intersection semantics:
    # a declared archetype must be matched by a touched file.
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="svc-only", title="svc-only", languages=[], archetypes=["service"]),
    )
    src = _write_ts(repo)
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_not_called()
    assert result.findings == []
    assert result.check_events == [("idiom_lens", "no_scoped_idioms")]


def test_run_archetype_scoped_idiom_kept_when_one_of_mixed_files_matches(tmp_path):
    # One file resolves the matching archetype, another resolves None: the
    # caller set is non-empty and intersects the record, so the idiom stays
    # in scope (the post-filter only fires on an ALL-None turn).
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="svc-only", title="svc-only", languages=[], archetypes=["service"]),
    )
    src_a = _write_ts(repo, rel="src/widget.ts")
    src_b = _write_ts(repo, rel="src/service.ts")

    def _resolver(path):
        return "service" if path.endswith("service.ts") else None

    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        idiom.run(repo, profile, [str(src_a), str(src_b)], _resolver)
    spawn.assert_called_once()


def test_run_wildcard_archetype_idiom_survives_all_none_archetype_turn(tmp_path):
    # A record declaring NO archetypes is a genuine wildcard: it must stay in
    # scope even when no touched file resolves an archetype -- the post-filter
    # drops only archetype-SPECIFIC records.
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(languages=[], archetypes=[]))
    src = _write_ts(repo)
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_called_once()
    assert result.findings == []


def test_run_path_scoped_idiom_included_when_glob_matches(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(
        profile,
        _rec(slug="models-only", title="models-only", languages=[], paths=["app/models/**"]),
    )
    src = _write_ts(repo, rel="app/models/user.ts")
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        idiom.run(repo, profile, [str(src)], lambda _p: None)
    spawn.assert_called_once()


# --- citation requirement ----------------------------------------------------


def test_run_scoped_violation_produces_canonical_finding(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [
        {
            "slug": "wrap-fetches",
            "file": "src/widget.ts",
            "lines": [2],
            "message": "raw fetch call, not wrapped in apiClient",
            "confidence": 0.9,
        }
    ]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            intent_tokens=["retry-count"],
        )
    assert len(result.findings) == 1
    f = result.findings[0]
    assert isinstance(f, Finding)
    assert f.kind == "idiom"
    assert f.source_lens == "idiom"
    assert f.status == "pending"
    assert f.file == "src/widget.ts"
    assert f.span == (2, 2)
    assert "wrap-fetches" in f.claim
    assert f.severity == "high"
    assert f.intent_tokens == ("retry-count",)


def test_run_claim_missing_slug_dropped(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [{"file": "src/widget.ts", "lines": [2], "message": "no slug here"}]
    events = []
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(
            repo,
            profile,
            [str(src)],
            lambda _p: None,
            event_sink=lambda kind, detail: events.append((kind, detail)),
        )
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events
    assert [k for k, _ in events] == [k for k, _ in result.check_events]


def test_run_claim_missing_lines_dropped(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [{"slug": "wrap-fetches", "file": "src/widget.ts", "message": "no lines here"}]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events


def test_run_claim_unknown_slug_dropped(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [{"slug": "not-a-real-idiom", "file": "src/widget.ts", "lines": [2], "message": "x"}]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events


def test_run_claim_non_list_lines_dropped(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [{"slug": "wrap-fetches", "file": "src/widget.ts", "lines": "2", "message": "x"}]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events


def test_run_claim_non_positive_lines_filtered_and_dropped_if_none_survive(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [
        {"slug": "wrap-fetches", "file": "src/widget.ts", "lines": [0, -1, True], "message": "x"}
    ]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events


def test_run_claim_mixed_valid_and_invalid_lines_keeps_only_valid(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = [
        {"slug": "wrap-fetches", "file": "src/widget.ts", "lines": [0, 2, -1, 5], "message": "x"}
    ]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert len(result.findings) == 1
    assert result.findings[0].span == (2, 5)


def test_run_non_dict_array_element_dropped(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    arr = ["not-a-dict"]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "claim_missing_citation") in result.check_events


def test_run_empty_array_verdict_is_parsed_ok_no_findings(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "unparseable_output") not in result.check_events


# --- spawn failure / parse / crash handling ----------------------------------


def test_run_unparseable_output_event(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    stream = json.dumps({"type": "result", "result": "no json array here"})
    with patch.object(judge, "_spawn_reviewer_status", return_value=(stream, None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "unparseable_output") in result.check_events


def test_run_conftest_guard_blocks_real_spawn(tmp_path):
    # No explicit patch of judge._spawn_reviewer_status: exercises the autouse
    # conftest guard directly, same discipline as the correctness lens's own
    # guard test -- a future rename of the spawn seam must fail loudly here.
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert ("idiom_lens", "spawn_exec_error") in result.check_events


def test_run_pipeline_error_is_caught(tmp_path, monkeypatch):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)

    def _boom(*_a, **_k):
        raise RuntimeError("diff collection exploded")

    monkeypatch.setattr(judge, "collect_file_diffs", _boom)
    result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert result.findings == []
    assert any(
        kind == "idiom_lens" and detail.startswith("pipeline_error")
        for kind, detail in result.check_events
    )


# --- already-shown slug dedup (spec section 10.1 must-keep) -----------------
#
# Ports the intent of the deleted test_mirror_carried_idiom_renders_gist_not_
# full_text (the old per-edit-hook idiom gate never re-dumped an idiom the
# memory channel already delivered): the async idiom LENS must never spawn a
# reviewer over an idiom the model already saw this session.


def test_run_shown_slug_excludes_only_scoped_idiom_no_spawn(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())  # slug "wrap-fetches"
    src = _write_ts(repo)
    with patch.object(judge, "_spawn_reviewer_status") as spawn:
        result = idiom.run(
            repo, profile, [str(src)], lambda _p: None, shown_idiom_slugs=["wrap-fetches"]
        )
    spawn.assert_not_called()
    assert result.findings == []
    assert ("idiom_lens", "no_scoped_idioms") in result.check_events
    assert any(
        k == "idiom_lens" and d.startswith("deduped_shown_slugs:") for k, d in result.check_events
    )


def test_run_shown_slug_mix_only_unshown_idiom_reviewed(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(slug="wrap-fetches", title="wrap-fetches"))
    upsert_idiom(profile, _rec(slug="use-logger", title="use-logger"))
    src = _write_ts(repo)
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        result = idiom.run(
            repo, profile, [str(src)], lambda _p: None, shown_idiom_slugs=["wrap-fetches"]
        )
    spawn.assert_called_once()
    prompt = spawn.call_args[0][0]
    assert "use-logger" in prompt
    assert "wrap-fetches" not in prompt
    assert result.findings == []


def test_run_no_shown_slugs_reviews_everything_as_before(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())
    src = _write_ts(repo)
    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        result = idiom.run(repo, profile, [str(src)], lambda _p: None, shown_idiom_slugs=None)
    spawn.assert_called_once()
    assert not any(
        k == "idiom_lens" and d.startswith("deduped_shown_slugs:") for k, d in result.check_events
    )


def test_run_archetype_resolver_raising_fails_open(tmp_path):
    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec(languages=[]))  # wildcard, so scope survives regardless
    src = _write_ts(repo)

    def _boom(_p):
        raise RuntimeError("resolver down")

    with patch.object(
        judge, "_spawn_reviewer_status", return_value=(_result_line([]), None)
    ) as spawn:
        result = idiom.run(repo, profile, [str(src)], _boom)
    spawn.assert_called_once()
    assert result.findings == []


# --- flood caps (IDIOM_LENS_MAX_IDIOMS / _MAX_PROMPT_BYTES / _MAX_FINDINGS) -


def test_build_prompt_caps_idiom_count_at_threshold():
    # A turn whose diff scopes MORE idioms than IDIOM_LENS_MAX_IDIOMS must
    # not dump the whole store into the reviewer prompt -- only the leading
    # slice up to the real threshold survives.
    from chameleon_mcp._thresholds import threshold_int

    max_idioms = threshold_int("IDIOM_LENS_MAX_IDIOMS")
    scoped = [
        _rec(slug=f"idiom-{i:02d}", title=f"idiom-{i:02d}", rationale=f"Rule number {i}.")
        for i in range(max_idioms + 5)
    ]
    prompt = idiom._build_prompt(scoped, [])
    included = [rec.slug for rec in scoped if f"### {rec.slug}:" in prompt]
    assert len(included) == max_idioms
    assert included == [rec.slug for rec in scoped[:max_idioms]]


def test_build_prompt_caps_prompt_bytes_at_threshold():
    # Even under IDIOM_LENS_MAX_IDIOMS, a handful of idioms with large
    # rationale/example text can blow past IDIOM_LENS_MAX_PROMPT_BYTES; the
    # byte budget must bound the built prompt independently of the count cap.
    from chameleon_mcp._thresholds import threshold_int

    budget = threshold_int("IDIOM_LENS_MAX_PROMPT_BYTES")
    huge_rationale = "x" * 20_000
    scoped = [_rec(slug=f"huge-{i}", title=f"huge-{i}", rationale=huge_rationale) for i in range(6)]
    # 6 * 20,000 char rationales comfortably exceed the 60,000-byte budget, so
    # the loop must break before appending every idiom.
    prompt = idiom._build_prompt(scoped, [])
    assert len(prompt) <= budget
    included = [rec.slug for rec in scoped if f"### {rec.slug}:" in prompt]
    assert 0 < len(included) < len(scoped)


def test_run_findings_truncated_to_max_findings(tmp_path):
    # A reviewer spawn that returns more valid, correctly-cited claims than
    # IDIOM_LENS_MAX_FINDINGS must still surface only the capped count -- one
    # over-eager reviewer response cannot flood a turn's findings.
    from chameleon_mcp._thresholds import threshold_int

    repo, profile = _repo(tmp_path)
    upsert_idiom(profile, _rec())  # slug "wrap-fetches"
    src = _write_ts(repo)
    max_findings = threshold_int("IDIOM_LENS_MAX_FINDINGS")
    arr = [
        {
            "slug": "wrap-fetches",
            "file": "src/widget.ts",
            "lines": [i],
            "message": f"violation {i}",
        }
        for i in range(1, max_findings + 5)
    ]
    with patch.object(judge, "_spawn_reviewer_status", return_value=(_result_line(arr), None)):
        result = idiom.run(repo, profile, [str(src)], lambda _p: None)
    assert len(result.findings) == max_findings
    assert [f.span for f in result.findings] == [(i, i) for i in range(1, max_findings + 1)]
