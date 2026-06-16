"""Tests for gather_semantic_findings: name/shape-prefiltered duplication
candidates that the body-hash gate is blind to (different body, same intent).

This is the turn-end counterpart to the pr-review semantic-duplication pass:
the body-hash gate only sees byte-identical (or param-renamed) re-implementations,
so a helper that re-implements an existing one with a different body slips through.
"""

from __future__ import annotations

import chameleon_mcp.duplication_review as dr
from chameleon_mcp.function_catalog import (
    CatalogedFunction,
    FunctionCatalog,
    ParsedFn,
    name_tokens,
)


def _catalog(*fns):
    return FunctionCatalog(list(fns))


def _cataloged(name, file, *, arity=1, required=1, body_hash=None, body_hash_pnorm=None):
    return CatalogedFunction(
        name=name,
        kind="method",
        file=file,
        arity=arity,
        required=required,
        tokens=name_tokens(name),
        body_hash=body_hash,
        body_hash_pnorm=body_hash_pnorm,
    )


def test_semantic_finds_different_body_same_intent(monkeypatch, tmp_path):
    existing = _cataloged(
        "strip_attributes",
        "app/models/concerns/sanitizable.rb",
        body_hash="EXISTING_HASH",
        body_hash_pnorm="EXISTING_PNORM",
    )
    catalog = _catalog(existing)
    # New helper: shares domain tokens (strip, attributes), same arity, but a
    # DIFFERENT body hash -> the body-hash gate cannot see it.
    new_fn = ParsedFn(
        name="strip_widget_attributes",
        kind="method",
        arity=1,
        required=1,
        start_line=5,
        body_hash="NEW_HASH",
        body_hash_pnorm="NEW_PNORM",
        excerpt="def strip_widget_attributes(w)\n  w.strip\nend\n",
    )
    monkeypatch.setattr(dr, "_parse", lambda root, path: [new_fn])

    # Body-hash gate is blind: its index holds only EXISTING_HASH, the new
    # function's body hash is different, so it returns nothing.
    idx = dr.CandidateIndex()
    idx.add_function(
        existing.file, existing.name, body_hash="EXISTING_HASH", body_hash_pnorm="EXISTING_PNORM"
    )
    assert dr.gather_body_match_findings(tmp_path, ["app/models/widget.rb"], idx, lang="ruby") == []

    # Semantic gate surfaces the candidate.
    findings = dr.gather_semantic_findings(tmp_path, ["app/models/widget.rb"], catalog, lang="ruby")
    assert len(findings) == 1
    f = findings[0]
    assert f.new_name == "strip_widget_attributes"
    assert f.existing_name == "strip_attributes"
    assert f.existing_file == "app/models/concerns/sanitizable.rb"
    assert f.line == 5
    assert "strip_widget_attributes" in f.excerpt


def test_semantic_drops_single_shared_token_noise(monkeypatch, tmp_path):
    # One shared domain token with a different body is noise on real repos
    # (us_state_code vs create_state). The turn-end pass must NOT surface it.
    # Matching arity so the ONLY discriminator is the shared-token count (1).
    catalog = _catalog(
        _cataloged("create_state", "app/services/qbo/create_oauth_url.rb", arity=1, required=1)
    )
    new_fn = ParsedFn(
        name="map_state_id",  # shares only {state} with create_state
        kind="method",
        arity=1,
        required=1,
        start_line=1,
        body_hash="DIFFERENT",
        body_hash_pnorm="DIFFERENT_P",
        excerpt="def map_state_id(x)\n  x.id\nend\n",
    )
    monkeypatch.setattr(dr, "_parse", lambda root, path: [new_fn])
    assert (
        dr.gather_semantic_findings(tmp_path, ["app/models/address.rb"], catalog, lang="ruby") == []
    )


def test_semantic_keeps_two_shared_tokens(monkeypatch, tmp_path):
    # Two co-occurring domain tokens is a real lead even with a different body.
    catalog = _catalog(_cataloged("sanitize_user_name", "app/services/clean.rb"))
    new_fn = ParsedFn(
        name="sanitize_buyer_name",  # shares {sanitize, name}
        kind="method",
        arity=1,
        required=1,
        start_line=1,
        body_hash="DIFFERENT",
        body_hash_pnorm="DIFFERENT_P",
        excerpt="def sanitize_buyer_name(s)\n  s.strip\nend\n",
    )
    monkeypatch.setattr(dr, "_parse", lambda root, path: [new_fn])
    findings = dr.gather_semantic_findings(tmp_path, ["app/models/buyer.rb"], catalog, lang="ruby")
    assert len(findings) == 1
    assert findings[0].existing_name == "sanitize_user_name"


def test_semantic_keeps_body_identical_even_with_weak_name(monkeypatch, tmp_path):
    # A body-identical clone qualifies regardless of shared tokens: that is the
    # renamed-copy case the body-token bar must not suppress.
    catalog = _catalog(
        _cataloged("run", "app/services/a.rb", body_hash="SAME", body_hash_pnorm="SAMEP")
    )
    new_fn = ParsedFn(
        name="execute",  # zero shared tokens with "run"
        kind="method",
        arity=0,
        required=0,
        start_line=1,
        body_hash="SAME",  # identical body
        body_hash_pnorm="SAMEP",
        excerpt="def execute\n  do_thing\nend\n",
    )
    monkeypatch.setattr(dr, "_parse", lambda root, path: [new_fn])
    findings = dr.gather_semantic_findings(tmp_path, ["app/services/b.rb"], catalog, lang="ruby")
    assert len(findings) == 1
    assert findings[0].existing_name == "run"


def test_semantic_none_catalog_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("x", "method", 0, 0, 1, "H", "P", "b")],
    )
    assert dr.gather_semantic_findings(tmp_path, ["a.rb"], None, lang="ruby") == []


def test_semantic_lang_filter(monkeypatch, tmp_path):
    catalog = _catalog(_cataloged("strip_attributes", "a.rb"))
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("strip_attrs", "method", 1, 1, 1, "H", "P", "b")],
    )
    # lang=typescript but file is .rb -> filtered out before parse
    assert dr.gather_semantic_findings(tmp_path, ["a.rb"], catalog, lang="typescript") == []


def test_semantic_excludes_exact_name(monkeypatch, tmp_path):
    # An exact-name match in another file is the name-collision check's job, not
    # the near-duplicate prefilter's -> the semantic gate must not surface it.
    catalog = _catalog(_cataloged("strip_attributes", "other.rb"))
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("strip_attributes", "method", 1, 1, 1, "H2", "P2", "b")],
    )
    assert dr.gather_semantic_findings(tmp_path, ["widget.rb"], catalog, lang="ruby") == []


def _f(new_name, existing_name, *, new_file="new.rb", existing_file="old.rb"):
    return dr.Finding(
        new_name=new_name,
        new_file=new_file,
        line=1,
        excerpt="x",
        existing_name=existing_name,
        existing_file=existing_file,
    )


def test_gather_findings_merges_body_and_semantic(monkeypatch, tmp_path):
    body = [_f("a", "a_orig")]
    semantic = [_f("b", "b_orig")]
    monkeypatch.setattr(dr, "gather_body_match_findings", lambda *a, **k: list(body))
    monkeypatch.setattr(dr, "gather_semantic_findings", lambda *a, **k: list(semantic))
    merged = dr.gather_findings(tmp_path, ["x.rb"], index=object(), catalog=object(), lang="ruby")
    names = {f.new_name for f in merged}
    assert names == {"a", "b"}


def test_gather_findings_dedups_same_pair(monkeypatch, tmp_path):
    # Same (new_name, new_file, existing_name, existing_file) from both sources
    # collapses to one finding (body-hash provenance kept first).
    shared = _f("a", "a_orig")
    monkeypatch.setattr(dr, "gather_body_match_findings", lambda *a, **k: [shared])
    monkeypatch.setattr(dr, "gather_semantic_findings", lambda *a, **k: [_f("a", "a_orig")])
    merged = dr.gather_findings(tmp_path, ["x.rb"], index=object(), catalog=object(), lang="ruby")
    assert len(merged) == 1
    assert merged[0].new_name == "a"


def test_gather_findings_caps_total(monkeypatch, tmp_path):
    from chameleon_mcp._thresholds import threshold_int

    cap = threshold_int("DUPLICATION_REVIEW_MAX_FINDINGS")
    many = [_f(f"fn{i}", f"orig{i}") for i in range(cap + 5)]
    monkeypatch.setattr(dr, "gather_body_match_findings", lambda *a, **k: list(many))
    monkeypatch.setattr(dr, "gather_semantic_findings", lambda *a, **k: [])
    merged = dr.gather_findings(tmp_path, ["x.rb"], index=object(), catalog=object(), lang="ruby")
    assert len(merged) == cap


def test_prompt_semantic_framing_differs_from_body_match():
    f = _f("strip_widget_attributes", "strip_attributes")
    body_prompt = dr.build_duplication_prompt([f])
    semantic_prompt = dr.build_duplication_prompt([f], semantic=True)
    # The semantic header must not claim a body match (the bodies differ) and
    # must ask the judge for intent equivalence, while keeping the JSON contract.
    assert semantic_prompt != body_prompt
    assert "body-matched" not in semantic_prompt
    assert "intent" in semantic_prompt.lower()
    assert '"is_duplicate"' in semantic_prompt


def test_judge_body_matches_passes_semantic_flag(monkeypatch, tmp_path):
    seen = {}

    def fake_prompt(findings, semantic=False):
        seen["semantic"] = semantic
        return "PROMPT"

    monkeypatch.setattr(dr, "build_duplication_prompt", fake_prompt)
    monkeypatch.setattr(dr, "_stream_texts", lambda stdout: [])
    from chameleon_mcp import judge

    monkeypatch.setattr(judge, "_spawn_reviewer", lambda prompt, root: "{}")
    dr.judge_body_matches(tmp_path, [_f("a", "b")], semantic=True)
    assert seen["semantic"] is True
