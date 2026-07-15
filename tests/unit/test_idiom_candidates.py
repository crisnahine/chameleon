"""Idiom candidates: the miner's write surface. Per-file atomic writes that
MERGE (never clobber) on a repeat slug, fail-open reads, the not-trust-hashed
guarantee, and the new-slug cap.
"""

from __future__ import annotations

import json
import stat

import pytest

from chameleon_mcp.core.idiom_candidates import (
    CANDIDATES_DIRNAME,
    candidates_dir,
    load_candidates,
    write_candidate,
)


def _profile(tmp_path):
    p = tmp_path / "repo" / ".chameleon"
    p.mkdir(parents=True)
    return p


# --- write_candidate: creation ------------------------------------------------


def test_write_candidate_creates_file_under_idiom_candidates_dirname(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(
        profile,
        slug="prefer-api-client",
        title="Prefer apiClient over fetch",
        rationale="Every HTTP call in this repo goes through apiClient.",
        source="learned",
        evidence="match_key=abc file=src/x.ts",
        occurrences=3,
        session_ids=["s1", "s2"],
    )
    path = candidates_dir(profile) / "prefer-api-client.json"
    assert path.is_file()
    assert candidates_dir(profile).name == CANDIDATES_DIRNAME
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["slug"] == "prefer-api-client"
    assert body["title"] == "Prefer apiClient over fetch"
    assert body["rationale"].startswith("Every HTTP call")
    assert body["source"] == "learned"
    assert body["occurrences"] == 3
    assert set(body["session_ids"]) == {"s1", "s2"}
    assert body["evidence"] == "match_key=abc file=src/x.ts"


def test_write_candidate_dir_created_0700(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(
        profile,
        slug="a-slug",
        title="A slug",
        rationale="r",
        source="auto",
        evidence="e",
    )
    mode = stat.S_IMODE(candidates_dir(profile).stat().st_mode)
    assert mode == 0o700


def test_write_candidate_rejects_invalid_source(tmp_path):
    profile = _profile(tmp_path)
    with pytest.raises(ValueError):
        write_candidate(
            profile, slug="a-slug", title="t", rationale="r", source="taught", evidence="e"
        )


def test_write_candidate_rejects_invalid_slug(tmp_path):
    profile = _profile(tmp_path)
    with pytest.raises(ValueError):
        write_candidate(
            profile, slug="../etc/passwd", title="t", rationale="r", source="auto", evidence="e"
        )


# --- write_candidate: merge on repeat slug -----------------------------------


def test_second_write_of_same_slug_merges_not_clobbers(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(
        profile,
        slug="dup-slug",
        title="First title",
        rationale="First rationale",
        source="learned",
        evidence="first evidence",
        occurrences=2,
        session_ids=["s1"],
    )
    write_candidate(
        profile,
        slug="dup-slug",
        title="First title",
        rationale="First rationale",
        source="learned",
        evidence="second evidence",
        occurrences=1,
        session_ids=["s2"],
    )
    body = json.loads((candidates_dir(profile) / "dup-slug.json").read_text(encoding="utf-8"))
    # occurrences takes the max of prior (2) and the second call's total (1),
    # never their sum -- the stored value is the authoritative running total,
    # not an accumulator.
    assert body["occurrences"] == 2
    # session_ids unions rather than replacing.
    assert set(body["session_ids"]) == {"s1", "s2"}
    # evidence appends -- both the original and the new line survive.
    assert "first evidence" in body["evidence"]
    assert "second evidence" in body["evidence"]


def test_second_write_with_same_evidence_does_not_duplicate_the_line(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(
        profile, slug="dup2", title="t", rationale="r", source="learned", evidence="same line"
    )
    write_candidate(
        profile, slug="dup2", title="t", rationale="r", source="learned", evidence="same line"
    )
    body = json.loads((candidates_dir(profile) / "dup2.json").read_text(encoding="utf-8"))
    assert body["evidence"].count("same line") == 1


def test_occurrences_is_authoritative_max_not_a_sum(tmp_path):
    """The miner re-submits its caller's own current TOTAL sighting count on
    every job run, including re-mining an unchanged ledger -- occurrences
    must be idempotent under a repeat of the same total, and must still track
    a genuine increase, never double-count by summing across calls."""
    profile = _profile(tmp_path)
    write_candidate(
        profile,
        slug="stable-slug",
        title="t",
        rationale="r",
        source="learned",
        evidence="e1",
        occurrences=3,
        session_ids=["s1"],
    )
    # Re-mining the SAME state resubmits the same total: idempotent, not 3+3=6.
    write_candidate(
        profile,
        slug="stable-slug",
        title="t",
        rationale="r",
        source="learned",
        evidence="e2",
        occurrences=3,
        session_ids=["s2"],
    )
    body = json.loads((candidates_dir(profile) / "stable-slug.json").read_text(encoding="utf-8"))
    assert body["occurrences"] == 3
    # session_ids and evidence still merge normally -- only occurrences semantics changed.
    assert set(body["session_ids"]) == {"s1", "s2"}
    assert "e1" in body["evidence"]
    assert "e2" in body["evidence"]

    # A genuine new sighting raises the ledger's total, and the candidate
    # tracks it -- max, not a floor.
    write_candidate(
        profile,
        slug="stable-slug",
        title="t",
        rationale="r",
        source="learned",
        evidence="e3",
        occurrences=5,
        session_ids=["s3"],
    )
    body = json.loads((candidates_dir(profile) / "stable-slug.json").read_text(encoding="utf-8"))
    assert body["occurrences"] == 5


def test_merge_write_with_empty_title_and_rationale_preserves_originals(tmp_path):
    """The reinforcement signal deliberately passes title="" / rationale="" so
    it never clobbers a fuller proposal with a stub."""
    profile = _profile(tmp_path)
    write_candidate(
        profile,
        slug="reinforced-slug",
        title="Real title",
        rationale="Real rationale",
        source="learned",
        evidence="initial",
    )
    write_candidate(
        profile,
        slug="reinforced-slug",
        title="",
        rationale="",
        source="learned",
        evidence="reinforcement evidence",
    )
    body = json.loads(
        (candidates_dir(profile) / "reinforced-slug.json").read_text(encoding="utf-8")
    )
    assert body["title"] == "Real title"
    assert body["rationale"] == "Real rationale"
    assert "reinforcement evidence" in body["evidence"]


# --- load_candidates: fail-open ----------------------------------------------


def test_load_candidates_returns_all_written(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(profile, slug="one", title="One", rationale="r1", source="auto", evidence="e")
    write_candidate(
        profile, slug="two", title="Two", rationale="r2", source="learned", evidence="e"
    )
    rows = load_candidates(profile)
    assert {r["slug"] for r in rows} == {"one", "two"}


def test_load_candidates_empty_when_dir_absent(tmp_path):
    profile = _profile(tmp_path)
    assert load_candidates(profile) == []


def test_load_candidates_skips_corrupt_file_keeps_the_rest(tmp_path):
    profile = _profile(tmp_path)
    write_candidate(
        profile, slug="good-one", title="Good", rationale="r", source="auto", evidence="e"
    )
    cdir = candidates_dir(profile)
    (cdir / "corrupt.json").write_text("not json{{{", encoding="utf-8")
    rows = load_candidates(profile)
    assert {r["slug"] for r in rows} == {"good-one"}


# --- IDIOM_CANDIDATE_MAX: bounds only NEW slugs -------------------------------


def test_new_slug_beyond_cap_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_IDIOM_CANDIDATE_MAX", "2")
    profile = _profile(tmp_path)
    write_candidate(profile, slug="one", title="One", rationale="r", source="auto", evidence="e")
    write_candidate(profile, slug="two", title="Two", rationale="r", source="auto", evidence="e")
    write_candidate(
        profile, slug="three", title="Three", rationale="r", source="auto", evidence="e"
    )
    rows = load_candidates(profile)
    assert {r["slug"] for r in rows} == {"one", "two"}


def test_merge_into_existing_slug_is_unaffected_by_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_IDIOM_CANDIDATE_MAX", "1")
    profile = _profile(tmp_path)
    write_candidate(profile, slug="one", title="One", rationale="r", source="auto", evidence="e1")
    # At the cap already -- but re-writing the SAME slug is a merge, not a new file.
    write_candidate(profile, slug="one", title="One", rationale="r", source="auto", evidence="e2")
    body = json.loads((candidates_dir(profile) / "one.json").read_text(encoding="utf-8"))
    assert "e1" in body["evidence"]
    assert "e2" in body["evidence"]


# --- NOT trust-hashed ----------------------------------------------------------


def test_idiom_candidates_dirname_absent_from_hashed_artifacts():
    """Candidates are unapproved by definition: hashing idiom-candidates/ would
    arm the trust gate on the miner's own unreviewed output."""
    import inspect

    from chameleon_mcp.profile import trust

    assert CANDIDATES_DIRNAME not in trust._HASHED_ARTIFACTS
    source = inspect.getsource(trust)
    assert CANDIDATES_DIRNAME not in source
