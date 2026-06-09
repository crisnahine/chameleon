"""The chameleon-pr-review skill must wire the Phase 1-2 engine data into review.

Phases 1-2 derived new profile artifacts and tools that the review flow has to
consume so each finding cites real data, never bare model intuition. Ten finding
classes hang off that data:

- the review ledger (``record_review_verdict`` final step, queryable via
  ``get_review_history``, tamper-evident not forgery-proof),
- the co-change advisory for newly-added files (``cochange.py`` curated pairs),
- the stale paired-test check (a removed export still named in the paired test,
  ``test_pairing`` in conventions.json),
- the error-handling finding citing the ``error_handling`` convention entry,
- the required-guard authz finding citing ``required_guards`` per archetype,
- the callable-signature drift finding citing ``callable_signatures``,
- the layering / cycle finding citing ``conventions.layering``,
- the semantic-duplication finding gated on ``get_duplication_candidates``,
- the cross-file existence break gated on ``get_crossfile_context`` high
  confidence,
- the stale-comment NIT.

Every one of these must keep the integrity rule: a finding cites a chameleon
artifact, a tool result, or a diff line, never model intuition. If any of the
load-bearing instructions or severity caps is lost in an edit, the skill
regresses to either no signal or an ungrounded one, so these tests pin the
instructions in place. The skill is an LLM-driven procedure, so the tests assert
on the procedure text the same way the other pr-review tests do.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


# --- 1. Review ledger ---------------------------------------------------------


def test_ledger_final_step_calls_record_review_verdict():
    text = _skill_text()
    assert "Step 5: Record the verdict in the review ledger" in text
    # The pinned tool name and its argument shape, called AFTER the verdict.
    assert "record_review_verdict" in text
    assert "After the verdict is rendered" in text
    for arg in ("verdict=", "findings_count=", "commit_sha="):
        assert arg in text, f"ledger call omits {arg!r}"


def test_skill_cited_tools_are_actually_registered_with_matching_params():
    """The tool names the skill calls must be real registered MCP tools.

    The text-only assertions above pin the instruction, but a model following
    the skill calls these as MCP tools. If a name in the skill is not registered
    (or a kwarg the skill passes is not a real parameter), the call fails at run
    time. Pin the seam: every tool the skill names exists in the server, and
    every kwarg the skill passes to the ledger tools maps to a real parameter.
    """
    import re

    from chameleon_mcp import server

    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    text = _skill_text()
    for name in (
        "get_duplication_candidates",
        "get_crossfile_context",
        "record_review_verdict",
        "get_review_history",
        "get_autopass_verdict",
    ):
        assert name in text, f"skill stopped citing {name!r}"
        assert name in registered, f"{name!r} is cited by the skill but not a registered MCP tool"

    # The kwargs the skill passes to record_review_verdict must be real params.
    tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "record_review_verdict"
    )
    params = set(tool.parameters.get("properties", {}))
    call = re.search(r"record_review_verdict\(([^)]*)\)", text)
    assert call is not None, "skill no longer shows a record_review_verdict call"
    skill_kwargs = {a.split("=", 1)[0].strip() for a in call.group(1).split(",") if "=" in a}
    unknown = skill_kwargs - params
    assert not unknown, f"skill passes unknown kwargs to record_review_verdict: {sorted(unknown)}"


def test_record_review_verdict_tool_roundtrips_to_get_review_history(tmp_path, monkeypatch):
    """The skill's final step must write a record get_review_history reads back.

    Exercises the tools-layer wrapper the MCP tool delegates to end to end:
    record_review_verdict writes the verdict, provenance, and findings count;
    get_review_history reads the same record back, HMAC-verified.
    """
    import subprocess

    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")

    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    written = tools.record_review_verdict(
        str(repo), "BLOCK", findings_count=3, commit_sha=sha, pr_id="42"
    )["data"]
    assert written["status"] == "ok"
    assert written["recorded"] is True
    assert written["signed"] is True

    history = tools.get_review_history(str(repo))["data"]
    assert history["total"] == 1
    rec = history["records"][0]
    assert rec["verdict"] == "BLOCK"
    assert rec["commit_sha"] == sha
    assert rec["findings"] == {"total": 3}
    assert rec["pr_id"] == "42"
    assert rec["verified"] is True


def test_record_review_verdict_fails_open_on_unresolvable_repo():
    """A ledger write to a repo that cannot be resolved degrades, never raises."""
    from chameleon_mcp import tools

    out = tools.record_review_verdict("not-a-real-repo-id", "APPROVE")["data"]
    assert out.get("recorded") is False
    assert out.get("status") in ("no_repo", "failed")


def test_ledger_is_queryable_and_honest_about_scope():
    text = _skill_text()
    # Verdicts are queryable via the read tool.
    assert "get_review_history" in text
    # The honest scope: tamper-evident, not forgery-proof.
    assert "tamper-evident" in text
    assert "not forgery-proof" in text or "NOT forgery-proof" in text
    # Best-effort: a ledger failure never blocks the review.
    assert "best-effort" in text.lower()


# --- 2. Co-change advisory ----------------------------------------------------


def test_cochange_step_is_new_files_only_and_advisory():
    text = _skill_text()
    assert "Step 2.8: Co-change advisory" in text
    # Triggers only on added files; a modified file must not demand a companion.
    assert "files the diff ADDS" in text
    assert "A modified existing file does NOT trigger" in text
    # Curated pairs from the engine, not a learned statistic.
    assert "cochange.py" in text
    assert "curated co-change pairs" in text
    # Capped at FIX, never BLOCK.
    assert "Cap this at FIX, never BLOCK" in text


def test_cochange_cites_the_curated_rule_ids():
    text = _skill_text()
    for rule_id in (
        "cochange-model-migration",
        "cochange-controller-route",
        "cochange-prisma-migration",
        "cochange-slice-store",
    ):
        assert rule_id in text, f"co-change step omits {rule_id!r}"


# --- 3. Stale paired-test check ----------------------------------------------


def test_stale_test_check_uses_removed_export_and_paired_test():
    text = _skill_text()
    assert "Step 3f-i: Stale paired-test check" in text
    # The data source: the test_pairing convention entry.
    assert "test_pairing" in text
    # The mechanism: a removed/renamed export still named in the paired test.
    assert "the diff REMOVES or renames" in text
    assert "still appears as a string token in the paired test" in text
    # The worked example (renamed symbol still in the spec) and FIX severity.
    assert "getUserById" in text
    assert "raise a **FIX**" in text


# --- 4. Error-handling cites the convention entry ----------------------------


def test_error_handling_cites_convention_entry_with_witness_fallback():
    text = _skill_text()
    # The error-handling check now cites the error_handling convention entry.
    assert "error_handling" in text
    assert "conventions.error_handling[<archetype>]" in text
    # The recorded shape fields and the project error target.
    assert "error_shape" in text
    # The free-text witness line survives only as the fallback when no entry.
    assert "fall back to comparing against the canonical witness" in text


# --- 5. Authz cites required_guards ------------------------------------------


def test_authz_cites_required_guards_and_keeps_honesty_label():
    text = _skill_text()
    assert "required_guards" in text
    assert "conventions.required_guards[<archetype>]" in text
    # The specific expected guard is named, with the known_guards variant check.
    assert "before_action :authorize!" in text
    assert "known_guards" in text
    # The honesty label is preserved: cannot confirm the action is covered.
    assert "cannot confirm the new action is covered" in text


# --- 6. Callable signatures ---------------------------------------------------


def test_callable_signature_drift_advisory_fix_at_most():
    text = _skill_text()
    assert "Callable signature drift" in text
    assert "callable_signatures" in text
    assert "conventions.callable_signatures[<archetype>]" in text
    # Advisory FIX at most, never BLOCK; framework bases are not asserted.
    assert "**FIX** (never BLOCK)" in text
    assert "overrides_base" in text
    assert "Framework base contracts" in text


# --- 7. Layering / cycles -----------------------------------------------------


def test_layering_surfaces_upward_edge_and_cites_cycle_report():
    text = _skill_text()
    assert "Layering / cycle violations" in text
    assert "conventions.layering" in text
    assert "forbidden_upward_edges" in text
    # The bootstrap cycle report is referenced.
    assert "import_cycles" in text
    # NIT/FIX advisory, never BLOCK.
    assert "never BLOCK" in text.split("Layering / cycle")[1].split("####")[0]


# --- 8. Duplication -----------------------------------------------------------


def test_duplication_gated_on_returned_candidate_only():
    text = _skill_text()
    assert "get_duplication_candidates" in text
    assert "Semantic duplication of NEW functions" in text
    # The tool only prefilters; the model is the semantic judge.
    assert "tool only PREFILTERS" in text
    # Never claim duplication without a returned candidate.
    assert "Never claim duplication without a candidate" in text
    # Advisory only, never BLOCK.
    assert "Advisory only, never BLOCK" in text


# --- 9. Crossfile -------------------------------------------------------------


def test_crossfile_relays_only_high_confidence_existence_breaks():
    text = _skill_text()
    assert "get_crossfile_context" in text
    assert "Cross-file existence breaks" in text
    # Only existence-break findings, only when high_confidence is true.
    assert "ONLY existence-break findings" in text
    assert "high_confidence" in text
    assert "Drop every finding without `high_confidence=true`" in text
    # Relayed as FIX.
    assert "Relay a finding as a **FIX**" in text


# --- 10. Stale-comment judge line --------------------------------------------


def test_stale_comment_nit_is_one_checklist_line_capped_at_nit():
    text = _skill_text()
    assert "Step 3f-ii: Stale-comment check" in text
    # The one question: did the change make an adjacent comment lie?
    assert "adjacent comment now lies" in text
    # Capped at NIT and hunk-gated.
    assert "caps at NIT" in text
    assert "Raise a **NIT**" in text


# --- Cross-cutting: integrity rule preserved for every new class -------------


def test_new_classes_keep_the_no_intuition_integrity_rule():
    text = _skill_text()
    # The added integrity bullet ties every new finding to a tool/artifact.
    assert "Cross-file findings cite their tool or artifact, not intuition" in text
    # The 2-round loop names the new data sources and tool results.
    assert "get_duplication_candidates` candidate" in text
    assert "get_crossfile_context` finding with `high_confidence=true`" in text


def test_output_template_and_severity_table_cover_new_classes():
    text = _skill_text()
    # The output template gained a Cross-file findings section.
    assert "### Cross-file findings" in text
    # The severity table gained a Cross-file examples column.
    assert "Cross-file examples" in text
    # The cross-file caps are stated: FIX max for existence break, advisory rest.
    assert "Only a high-confidence existence break" in text
