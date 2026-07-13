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
SKILL = REPO_ROOT / "plugin" / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    """Body plus lazily-loaded references — the skill's full procedure text."""
    refs = sorted(SKILL.parent.glob("references/*.md"))
    parts = [SKILL.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in refs]
    return "\n".join(parts)


# --- 1. Review ledger ---------------------------------------------------------


def test_ledger_final_step_calls_record_review_verdict():
    text = _skill_text()
    assert "Step 5: Record the verdict in the review ledger" in text
    # The pinned operation name and its argument shape, called AFTER the verdict.
    assert "record_review_verdict" in text
    assert "After the verdict is rendered" in text
    for arg in ("verdict", "findings_count", "commit_sha"):
        # Direct kwarg form (arg=...) or the chameleon_review dispatcher form
        # ("arg": ... inside params) — either pins the ledger call's shape.
        assert f"{arg}=" in text or f'"{arg}"' in text, f"ledger call omits {arg!r}"


# The chameleon MCP operations the two review skills + reviewer.md are expected
# to use. Under the v3 surface split the first group stays top-level registered
# tools; the second group routes through the chameleon_review dispatcher as
# actions, so the registry check differs per group. Driven from lists so a
# newly-cited operation is added in one place.
_REVIEW_TOP_LEVEL_TOOLS = (
    "get_pattern_context",
    "lint_file",
    "get_duplication_candidates",
    "get_crossfile_context",
    "get_callers",
    "get_contract_breaks",
)

_REVIEW_DISPATCHER_ACTIONS = (
    "scan_dependency_changes",
    "get_autopass_verdict",
    "refute_finding",
    "record_review_verdict",
    "get_review_history",
)

_RECEIVING_SKILL = REPO_ROOT / "plugin" / "skills" / "chameleon-receiving-code-review" / "SKILL.md"
_REVIEWER_MD = REPO_ROOT / "plugin" / "skills" / "chameleon-pr-review" / "reviewer.md"


def _registered_tools_by_name() -> dict[str, set[str]]:
    """name -> set of real parameter names, from the live FastMCP registry.

    Uses the PRIVATE sync `_tool_manager.list_tools()` returning FastMCP Tool
    objects whose `.parameters` is the JSON schema (NOT the async public
    `mcp.list_tools()`, which returns protocol Tools with `.inputSchema`).
    """
    from chameleon_mcp import server

    return {
        t.name: set(t.parameters.get("properties", {}))
        for t in server.mcp._tool_manager.list_tools()
    }


def _real_params(name: str) -> set[str]:
    """Real parameter names of the UNDERLYING tools.py function.

    The dispatcher fold removed the per-operation MCP registrations, but the
    in-process functions (and their signatures, which the dispatchers bind
    `params` against) are unchanged — so they stay the source of truth the
    skills' call shapes must match.
    """
    import inspect

    from chameleon_mcp import tools

    return set(inspect.signature(getattr(tools, name)).parameters)


def _call_site_kwargs(name: str, text: str) -> list[set[str]]:
    """Every kwarg-set the text shows for operation `name`, in either form.

    Direct form:      name(repo=..., base_ref=...)
    Dispatcher form:  chameleon_review(action="name", params={"repo": ..., ...})
                      (top-level `k=` kwargs besides action/params count too)
    """
    import re

    sites: list[set[str]] = []
    # Direct call form.
    for m in re.finditer(rf"\b{re.escape(name)}\(([^)]*)\)", text):
        kwargs = {
            part.split("=", 1)[0].strip().lstrip("*")
            for part in m.group(1).split(",")
            if "=" in part
        }
        kwargs = {k for k in kwargs if k.isidentifier()}
        if kwargs:
            sites.append(kwargs)
    # Dispatcher call form: find each action="name" inside a chameleon_* call
    # and read the params dict's quoted keys (brace-balanced window).
    for m in re.finditer(
        rf"chameleon_(?:lifecycle|review|telemetry)\(\s*action\s*=\s*[\"']{re.escape(name)}[\"']",
        text,
    ):
        window = text[m.end() : m.end() + 800]
        brace_at = window.find("params")
        if brace_at == -1:
            continue
        open_at = window.find("{", brace_at)
        if open_at == -1:
            continue
        depth = 0
        end_at = None
        for i, ch in enumerate(window[open_at:], start=open_at):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_at = i
                    break
        if end_at is None:
            continue
        params_src = window[open_at : end_at + 1]
        keys = set(re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*:", params_src))
        if keys:
            sites.append(keys)
    return sites


def test_all_skill_cited_tool_calls_match_real_signatures():
    """Every chameleon tool CALL in either review skill or reviewer.md must hit a
    real operation with real kwargs.

    Parses every call site out of BOTH SKILL.md files AND reviewer.md — the
    direct `tool(...)` form AND the dispatcher `chameleon_review(action=...,
    params={...})` form — and asserts each kwarg shown is a real parameter of
    the UNDERLYING tools.py function (the dispatcher binds `params` against
    exactly that signature). A drifted arg name (``base_ref`` vs ``ref``,
    ``repo`` vs ``repo_id``) or a phantom operation fails here instead of
    silently at model runtime.
    """
    tools_by_name = _registered_tools_by_name()
    texts = {
        "pr-review/SKILL.md": _skill_text(),
        "receiving/SKILL.md": _RECEIVING_SKILL.read_text(encoding="utf-8"),
        "pr-review/reviewer.md": _REVIEWER_MD.read_text(encoding="utf-8"),
    }

    # (a) The top-level review tools stay registered by name; the folded
    # operations must be routable actions of the registered chameleon_review
    # dispatcher (its docstring is the model-facing action list).
    for name in _REVIEW_TOP_LEVEL_TOOLS:
        assert name in tools_by_name, (
            f"review skills cite {name!r} but it is not a registered MCP tool"
        )
    assert "chameleon_review" in tools_by_name, "chameleon_review dispatcher not registered"
    from chameleon_mcp import server

    review_doc = server.chameleon_review.__doc__ or ""
    for name in _REVIEW_DISPATCHER_ACTIONS:
        assert name in server._REVIEW_ACTIONS, f"{name!r} is not a routable chameleon_review action"
        assert name in review_doc, f"chameleon_review docstring omits action {name!r}"

    # (b) Every call-shape's kwargs map to real parameters of that operation.
    problems: list[str] = []
    for name in _REVIEW_TOP_LEVEL_TOOLS + _REVIEW_DISPATCHER_ACTIONS:
        real = _real_params(name)
        for label, text in texts.items():
            for kwargs in _call_site_kwargs(name, text):
                unknown = kwargs - real
                if unknown:
                    problems.append(
                        f"{label}: {name}(...) passes unknown kwarg(s) "
                        f"{sorted(unknown)} (real params: {sorted(real)})"
                    )
    assert not problems, "skill tool-call kwargs drifted from real signatures:\n" + "\n".join(
        problems
    )


def test_record_review_verdict_call_kwargs_are_real():
    """Keep the focused ledger-call check (subset of the generalized test)."""
    text = _skill_text()
    assert "record_review_verdict" in text, "skill no longer records the verdict"
    sites = _call_site_kwargs("record_review_verdict", text)
    params = _real_params("record_review_verdict")
    for skill_kwargs in sites:
        unknown = skill_kwargs - params
        assert not unknown, (
            f"skill passes unknown kwargs to record_review_verdict: {sorted(unknown)}"
        )


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

    # get_review_history is trust-gated (the ledger's verdict/findings text is
    # derived from this repo_id's own reviewed commits); this fixture has no
    # language signal for a real bootstrap, so grant trust directly against a
    # minimal profile dir instead.
    from chameleon_mcp.profile.trust import grant_trust

    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)
    repo_id = tools._compute_repo_id(repo)
    grant_trust(repo_id, repo / ".chameleon")

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


def test_get_review_history_untrusted_repo_withholds_records(tmp_path, monkeypatch):
    # Regression: get_review_history disclosed another checkout's PR-review
    # ledger (verdict text, commit_sha, profile hash, reviewer) to ANY caller
    # with zero trust gating. An UNGRANTED repo_id must get an untrusted
    # envelope, not the real records.
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

    # Record a real verdict but deliberately never grant trust for this repo_id.
    written = tools.record_review_verdict(
        str(repo), "BLOCK", findings_count=3, commit_sha=sha, pr_id="42"
    )["data"]
    assert written["status"] == "ok"

    history = tools.get_review_history(str(repo))["data"]
    assert history["status"] == "untrusted"
    assert history["records"] == []
    assert history["total"] == 0


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
    # The cross-file witnessed FIXes are the existence break AND the contract break.
    assert "high-confidence existence break" in text
    assert "caller-contract signature break" in text


# --- 11. Contract-break (2.9e) is wired into the grounding loop ---------------


def test_contract_break_is_grounding_loop_exempt_and_in_summaries():
    """get_contract_breaks (Step 2.9e) is deterministic + cross-file, so it must be
    exempt from BOTH the hunk gate (its callers live in non-diff files) and the
    round-3 refuter (which cannot re-derive cross-file evidence). It must also
    appear in the severity/verdict/integrity/output surfaces, not just be defined.
    """
    text = _skill_text()
    assert "get_contract_breaks" in text
    # Step 4a hunk-gate exemption names contract-break.
    hunk_gate = text.split("#### 4a.")[1].split("#### 4b.")[0]
    assert "contract-break" in hunk_gate, (
        "Step 4a does not exempt contract-break from the hunk gate"
    )
    # Step 4b refuter-exempt verify-inline list names contract-break.
    refuter = text.split("#### 4b.")[1].split("Format the review")[0]
    assert "contract-break" in refuter, "Step 4b does not list contract-break as refuter-exempt"
    # It appears in the severity table cross-file FIX cell and the output template.
    assert "caller-contract signature break" in text
    assert "Caller-contract signature break" in text  # output-template example line


# --- 12. Step 2.6d deterministic lint-sink routing ---------------------------


def test_2_6d_routes_lint_sinks_with_correct_caps():
    """lint_file already returns deterministic sinks + test-quality rules; Step
    2.6d routes them with the approved severity: ONLY error-severity eval-call
    BLOCKs, the other sinks (command-injection included) FIX, test-quality NIT,
    refuter-exempt, line parsed from actual. The BLOCK set must track the engine's
    BLOCK_ELIGIBLE_RULES, not a hand-list that drifts.
    """
    from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

    text = _skill_text()
    assert "2.6d" in text and "Deterministic lint-sink" in text
    block = text.split("#### 2.6d.")[1].split("### Step 2.7")[0]
    # Only error-severity eval-call BLOCKs. command-injection is emitted at
    # `warning` and is NOT block-eligible in the engine, so the skill must cap it
    # at FIX (matching the receiving skill), never escalate by rule name.
    assert "eval-call" in BLOCK_ELIGIBLE_RULES
    assert "command-injection" not in BLOCK_ELIGIBLE_RULES
    assert "`eval-call` (only the `severity: error` forms) → **BLOCK**" in block
    assert "`command-injection` is NOT block-eligible" in block
    assert "class_eval" in block and "warning" in block
    assert "RESPECT the returned `severity`" in block
    # The witnessed FIX sinks (command-injection routes here, at warning severity).
    for rule in (
        "command-injection",
        "sql-string-interpolation",
        "insecure-deserialization",
        "weak-hash",
        "insecure-random",
    ):
        assert rule in block, f"2.6d omits FIX sink {rule!r}"
    assert "**FIX**" in block
    # Test-quality NIT bucket.
    for rule in ("then-without-catch", "skipped-test", "tautological-assertion"):
        assert rule in block, f"2.6d omits NIT rule {rule!r}"
    assert "**NIT**" in block
    # Witnessed -> refuter-exempt, and the line is parsed from `actual`.
    assert "refuter-EXEMPT" in block or "refuter-exempt" in block
    assert "at line N" in block
    # It supersedes the hand-rolled taint pass on overlap.
    assert "2.6c" in block and "WINS" in block


def test_2_6d_block_drives_verdict_and_is_in_severity_table():
    text = _skill_text()
    # The verdict rule escalates an error-severity eval-call to a BLOCK verdict;
    # command-injection (warning, not block-eligible) caps at FIX, matching the
    # engine's BLOCK_ELIGIBLE_RULES and the receiving skill.
    assert "error-severity `eval-call` sink on an added/changed line (Step 2.6d" in text
    # command-injection must NOT be co-listed as a BLOCK-verdict driver.
    assert "`eval-call` or `command-injection` sink on an added/changed line" not in text
    # Severity table security cells carry the 2.6d rules.
    assert "Step 2.6d)" in text


# --- 13. New dependency is an ACK, not a verdict-driving BLOCK ----------------


def test_new_dependency_is_ack_not_block():
    """A new direct dependency must NOT raise a BLOCK (which would drive a BLOCK
    verdict written to the ledger). It is a human provenance ACK that does not
    affect the verdict, matching the engine's NIT classification of new-dependency.
    """
    text = _skill_text()
    s = text.split("#### 2.5a.")[1].split("#### 2.5b.")[0]
    assert "ACK" in s, "Step 2.5a no longer uses the ACK channel"
    assert "does NOT drive the verdict" in s or "does not drive the verdict" in s.lower()
    # It must NOT instruct a BLOCK for a new dependency.
    assert "raise a **BLOCK**" not in s, "Step 2.5a still raises a BLOCK for a new dependency"
    # The output template has the dedicated ACK section.
    assert "Acknowledge before merge" in text
    # The verdict rules note the ACK never affects the verdict.
    assert "new-dependency ACK" in text
