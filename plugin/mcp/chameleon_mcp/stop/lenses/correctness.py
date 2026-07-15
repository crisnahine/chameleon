"""The correctness lens: the surviving half of ``judge.py``'s
``run_correctness_judge`` (evidence building, prompt assembly, spawn, parse),
wrapped to return canonical ``core.finding.Finding`` objects instead of
``judge.py``'s own bespoke ``Finding`` dataclass.

This module does not replace ``judge.run_correctness_judge`` yet -- that
function stays a live, callable thin wrapper until Task 7 deletes its last
caller -- it is the NEW consumer of the same evidence builders
(``collect_file_diffs``, ``caller_facts_for_diffs``,
``imported_definition_facts``, ``caller_facts_transitive_for_diffs``,
``build_prompt``, ``_spawn_reviewer_status``, ``_parse_findings_status``),
run in the identical order and with the identical fail-open contract, so a
future caller (the Task-4 job runner) gets canonical Findings out of the
correctness review instead of the ad hoc dict/attribute shapes VERIFY and the
ledger used to each translate on their own.

Top-level imports stay stdlib-only; ``judge``, ``core.finding.Finding``, and
``profile.config``/``calls_index`` are resolved via a deferred import inside
``run`` -- mirroring the rest of the ``stop/`` package's pattern of deferring
every non-stdlib import to call time, so a test that patches
``chameleon_mcp.judge.<name>`` stays effective for a call made from here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.stop.lenses import LensResult


def _kind_for(jf) -> str:
    """Map a judge finding's own ``claim_type`` to a canonical Finding kind.

    Returns ``"intent"`` when the reviewer tagged this claim ``"type":
    "intent"`` (an unmet-ask / unrequested-scope violation against the
    intent contract); a missing, blank, or otherwise unrecognized
    ``claim_type`` always reads as the lens's ordinary ``"correctness"``
    kind instead -- this never raises on unexpected reviewer output.
    """
    return "intent" if getattr(jf, "claim_type", None) == "intent" else "correctness"


def run(
    repo_root: Path,
    profile_dir: Path,
    files: list[str],
    archetype_for,
    *,
    intent_tokens: list[str] | None = None,
    intent_contract: dict | None = None,
    budget: float | None = None,
    event_sink=None,
    model: str | None = None,
) -> LensResult:
    """Run the correctness review for one turn's edited ``files``.

    Mirrors ``judge.run_correctness_judge``'s internal sequence exactly
    (diff reconstruction -> grounding facts -> prompt -> spawn -> parse),
    then adapts every parsed judge finding into a canonical
    ``core.finding.Finding`` via ``Finding.from_judge_finding``. Every stage
    still fails open to an empty finding list -- an empty file set, a spawn
    failure, a timeout, or unparseable output all return
    ``LensResult(findings=[], check_events=[...])`` so a caller never has to
    special-case a degraded run.

    ``event_sink``, when given, receives every ``(kind, detail)`` event
    exactly as ``judge.run_correctness_judge``'s own ``event_sink`` would
    (same kind vocabulary: the ``judge_facts_*``/``judge_defs_*``/
    ``judge_transitive_*`` grounding outcomes, the spawn failure reason, or
    ``unparseable_output``/``pipeline_error``). The identical events are also
    collected into the returned ``LensResult.check_events``, so a caller that
    only wants the return value (no live sink) still sees every event, and a
    caller that wants to react to events as they happen may still pass one.

    ``budget``, when given, becomes the spawn's wall-clock timeout in
    seconds. Threaded through loosely (a single spawn call, no accounting for
    the evidence-building stages that precede it); the job runner is where a
    per-stage hard budget is enforced.

    ``intent_contract``, when given a ``{"excerpts": [...], "scope_lines":
    [...]}`` mapping with real content, is forwarded to ``judge.build_prompt``
    unchanged (see that function for the prompt section it adds). Every raw
    finding the reviewer returns is then routed by its own ``claim_type``: a
    claim the reviewer tagged ``"type": "intent"`` becomes
    ``Finding(kind="intent", ...)``; every other claim -- including every
    claim when ``intent_contract`` is ``None`` -- keeps the lens's ordinary
    ``kind="correctness"``.
    """
    from chameleon_mcp import judge
    from chameleon_mcp.core.finding import Finding
    from chameleon_mcp.stop.lenses import LensResult

    events: list[tuple[str, str]] = []

    def _sink(kind: str, detail: str | None = None) -> None:
        events.append((kind, detail or ""))
        if event_sink is None:
            return
        try:
            event_sink(kind, detail)
        except Exception:
            pass

    try:
        diffs = judge.collect_file_diffs(repo_root, files, archetype_for)
        if not diffs:
            return LensResult(findings=[], check_events=events)

        # One config read for all three grounding flags (each default on; an
        # unreadable config fails open to on), mirroring run_correctness_judge.
        try:
            from chameleon_mcp.profile.config import load_config

            _enf = load_config(profile_dir).enforcement
            facts_enabled = _enf.judge_crossfile_facts
            defs_enabled = _enf.judge_imported_definitions
            trans_enabled = _enf.judge_transitive_impact
        except Exception:
            facts_enabled = defs_enabled = trans_enabled = True

        caller_facts: str | None = None
        if not facts_enabled:
            _sink("judge_facts_skipped_disabled")
        else:
            block = judge.caller_facts_for_diffs(repo_root, diffs)
            caller_facts = block or None
            _sink("judge_facts_included" if block else "judge_facts_skipped_no_calls_index")

        imported_defs: str | None = None
        if not defs_enabled:
            _sink("judge_defs_skipped_disabled")
        else:
            defs_block = judge.imported_definition_facts(repo_root, diffs)
            imported_defs = defs_block or None
            _sink("judge_defs_included" if defs_block else "judge_defs_skipped_no_index")

        transitive_facts: str | None = None
        if not trans_enabled:
            _sink("judge_transitive_skipped_disabled")
        else:
            try:
                from chameleon_mcp.calls_index import load_calls_index

                trans_index = load_calls_index(repo_root)
            except Exception:
                trans_index = None
            if trans_index is None:
                _sink("judge_transitive_skipped_no_index")
            else:
                trans_block = judge.caller_facts_transitive_for_diffs(repo_root, diffs, trans_index)
                transitive_facts = trans_block or None
                _sink(
                    "judge_transitive_included"
                    if trans_block
                    else "judge_transitive_skipped_no_chains"
                )

        prompt = judge.build_prompt(
            repo_root,
            profile_dir,
            diffs,
            intent_tokens=intent_tokens,
            caller_facts=caller_facts,
            transitive_facts=transitive_facts,
            imported_defs=imported_defs,
            intent_contract=intent_contract,
        )

        timeout_s = int(budget) if isinstance(budget, (int, float)) and budget > 0 else None
        stdout, fail_reason = judge._spawn_reviewer_status(
            prompt, repo_root, model=model, timeout_s=timeout_s
        )
        if stdout is None:
            _sink(fail_reason or "spawn_exec_error")
            return LensResult(findings=[], check_events=events)

        raw_findings, parsed_ok = judge._parse_findings_status(stdout)
        if not parsed_ok:
            _sink("unparseable_output")

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        canonical = [
            Finding.from_judge_finding(
                jf,
                kind=_kind_for(jf),
                source_lens="correctness",
                intent_tokens=tuple(intent_tokens or ()),
                created_at=created_at,
            )
            for jf in raw_findings
        ]
        return LensResult(findings=canonical, check_events=events)
    except Exception as exc:
        _sink("pipeline_error", repr(exc)[:200])
        return LensResult(findings=[], check_events=events)
