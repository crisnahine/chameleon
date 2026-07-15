"""The VERIFY stage: refuter.run_batch over canonical core.finding.Finding.

Replaces ``stop_verify._finding_to_refuter_dict``, which built its refuter
dict from a judge-attribute-or-multi-lens-dict duck type and (a) never set a
``kind`` key at all -- so ``refuter.build_refuter_prompt`` rendered a literal
``kind: None`` on every turn-end spawn -- and (b) hardcoded ``evidence`` to
``""`` regardless of what the finding actually carried. Both are read
directly off the canonical Finding here (``f.kind``, ``f.evidence``), so the
existing, UNCHANGED prompt template renders real values without touching
refuter.py at all. ``intent_tokens`` rides on the dict too (spec section 5.3:
"the full canonical Finding -- intent tokens, kind, and evidence included");
it is not yet interpolated by ``build_refuter_prompt`` (out of this module's
scope -- that template is shared production machinery), but it is no longer
lost between the finding and the adapter. More structurally: the pre-phase-3
over-refutation bug traced to ``id()``-keyed in-place mutation of a finding's
claim text across the VERIFY seam (fold intent in, restore after). A frozen
Finding makes that whole bug class impossible -- this module never mutates a
finding, it only ever derives a new one via ``dataclasses.replace``.

Contract (spec section 5.3): may only DROP a finding the refuter actively
REFUTES. Every other outcome -- confirmed, unverified, disabled, no budget,
CLI absent, no verifiable excerpt, any exception -- passes every finding
through, each tagged ``verified="confirmed"`` or ``"unverified"``. A skipped
run is never silent: ``event_sink`` always receives a status naming why.
Findings are returned in their input order; ranking for display belongs to
the render stage, not here.

Kind gate: only single-file-local kinds -- ``correctness`` and ``idiom``
(an idiom claim cites slug + offending lines in the edited file, so the
excerpt covers the violation) -- are refutable. A ``duplication`` finding's
evidence spans TWO locations ("X in fileA re-implements Y in fileB"); the
refuter sees only fileA's excerpt and its "cannot tell -> refute" rule would
systematically kill every one -- and each already survived an LLM
confirmation (``duplication_review.judge_body_matches``) inside its own
lens, so blind re-refutation is both wrong and a wasted spawn. Duplication
findings pass through ``verified="confirmed"`` (pre-confirmed), every other
non-refutable kind passes through ``verified="unverified"`` (no refutation
target), and the exemption is disclosed via an ``("exempt", ...)`` event.
This mirrors the legacy multi-lens gate, whose VERIFY eligibility was
``lenses == ["correctness"]`` for the same two-location reason.

Every REFUTABLE finding also gets its excerpt attached here
(``dataclasses.replace``, the deferral ``core.finding.Finding
.from_judge_finding`` documented) -- before any disabled/budget/CLI check,
so delivery can detect staleness even on a turn where VERIFY itself never
got to spawn a refuter. Exempt findings keep whatever excerpt they arrived
with (a duplication finding's single-file window would misrepresent its
two-location evidence).

Spawns run ``retry=False``: the Stop/job path is on a hard wall-clock budget,
so one refuter slot costs exactly one timeout window, never two.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.budget import TurnBudget
    from chameleon_mcp.core.finding import Finding

_EXCERPT_CONTEXT_LINES = 25
_EXCERPT_CHAR_CAP = 4000
_HEAD_FALLBACK_LINES = 50
_HIGH_SEVERITIES = ("blocker", "high")
# Kinds whose claim a single-file excerpt window can actually support or
# contradict (see the module docstring's kind-gate section). Everything else
# is exempt from refutation and passes through annotated.
_REFUTABLE_KINDS = ("correctness", "idiom")


def _sink(event_sink, status: str, detail: str | None = None) -> None:
    if event_sink is None:
        return
    try:
        event_sink(status, detail)
    except Exception:
        pass


def _contained_rel(repo_root: Path, rel_or_abs: str) -> str | None:
    """``rel_or_abs`` as a repo-relative path iff it stays inside ``repo_root``.

    A finding's ``file`` field can originate from model output (a reviewer's
    parsed claim), so it is never trusted before a filesystem read: an
    absolute path outside the repo or a ``..`` traversal must resolve to
    None, never be read.
    """
    try:
        root = Path(repo_root).resolve()
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p.resolve().relative_to(root).as_posix()
        return (root / p).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _excerpt_window(repo_root: Path, file: str, line: int | None) -> str:
    """A +/-``_EXCERPT_CONTEXT_LINES``-line window around ``file:line``.

    Containment-checked, fail-open to "". No line number falls back to the
    file's first ``_HEAD_FALLBACK_LINES`` lines, so an anchorless-but-filed
    finding still gets a real excerpt to review against.
    """
    if not file:
        return ""
    rel = _contained_rel(repo_root, file)
    if rel is None:
        return ""
    try:
        from chameleon_mcp.safe_open import safe_read_text

        text = safe_read_text(Path(repo_root).resolve(), rel)
    except Exception:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        lo = max(0, line - 1 - _EXCERPT_CONTEXT_LINES)
        hi = min(len(lines), line - 1 + _EXCERPT_CONTEXT_LINES + 1)
    else:
        lo, hi = 0, _HEAD_FALLBACK_LINES
    return "\n".join(lines[lo:hi])[:_EXCERPT_CHAR_CAP]


def _with_excerpt(repo_root: Path, f: Finding) -> Finding:
    """Attach a real reviewed-excerpt window, unless one is already pinned.

    A finding a lens already pinned (from_judge_finding never does today,
    but a future producer might) is left untouched. An unreadable or
    escaping path leaves the finding as-is -- an excerpt is never
    fabricated, only ever read from disk.
    """
    if f.excerpt:
        return f
    line = f.span[0] if f.span else None
    text = _excerpt_window(repo_root, f.file, line)
    if not text:
        return f
    from chameleon_mcp.judge import _excerpt_digest

    return dataclasses.replace(f, excerpt=text, excerpt_sha=_excerpt_digest(text) or "")


def _affordable_spawns(budget_seconds: float, timeout: int, max_spawns: int) -> int:
    """How many refuter spawns fit ``budget_seconds`` at ``timeout`` each.

    Mirrors ``stop_verify._affordable_spawns``'s arithmetic: the refuter runs
    up to 4 spawns per wave, so ``budget // timeout`` full timeout windows
    buy that many waves, capped at ``max_spawns``. A budget too small for
    even one window yields 0 -- the caller then passes findings through
    unverified rather than risk the wall-clock cap.
    """
    concurrency = 4
    try:
        b = float(budget_seconds)
        t = float(timeout)
    except (TypeError, ValueError):
        return 0
    if b <= 0 or t <= 0:
        return 0
    waves = int(b // t)
    if waves <= 0:
        return 0
    return min(max_spawns, waves * concurrency)


def _to_refuter_dict(idx: int, f: Finding) -> dict:
    """Adapt a canonical Finding to the refuter's finding shape.

    ``id`` is the finding's index into ``_refute``'s refutable list
    (verdicts map back after the batch reorders/caps). ``kind`` and
    ``evidence`` are read straight off the Finding -- the fix for the
    pre-phase-3 ``kind: None`` / always-empty-evidence drift (module
    docstring). ``intent_tokens`` rides along too, so nothing on the
    canonical Finding is lost crossing into the refuter's dict shape, even
    though today's prompt template does not yet render it.
    """
    line = f.span[0] if f.span else None
    return {
        "id": str(idx),
        "kind": f.kind,
        "severity": f.severity,
        "file": f.file,
        "line": line,
        "claim": f.claim,
        "evidence": f.evidence,
        "intent_tokens": list(f.intent_tokens),
    }


def _annotate_exempt(f: Finding) -> Finding:
    """Annotate a kind-exempt finding (see the module docstring's kind gate).

    Duplication arrives pre-confirmed (it already survived
    ``judge_body_matches``'s LLM confirmation inside its own lens), so it
    reads ``confirmed``; any other exempt kind has no refutation target and
    reads ``unverified``.
    """
    verdict = "confirmed" if f.kind == "duplication" else "unverified"
    return dataclasses.replace(f, verified=verdict)


def verify_findings(
    findings: list[Finding],
    *,
    repo_root: Path,
    budget: TurnBudget,
    event_sink=None,
) -> list[Finding]:
    """Independently VERIFY ``findings`` with the refuter. Never drops a
    finding except a refutable-kind one the refuter actively refutes; exempt
    kinds pass through annotated without a spawn; never silent about a skip
    or an exemption. Output preserves input order. See the module docstring
    for the full contract.
    """
    items = list(findings or [])
    if not items:
        return []

    refutable_set = {i for i, f in enumerate(items) if f.kind in _REFUTABLE_KINDS}
    exempt_idx = [i for i in range(len(items)) if i not in refutable_set]

    # None marks a dropped (refuted) finding; exempt slots are final already.
    results: dict[int, Finding | None] = {i: _annotate_exempt(items[i]) for i in exempt_idx}
    if exempt_idx:
        kinds = ",".join(sorted({items[i].kind for i in exempt_idx}))
        _sink(event_sink, "exempt", f"count={len(exempt_idx)} kinds={kinds}")

    ref_idx = sorted(refutable_set)
    if ref_idx:
        ref_items = [_with_excerpt(repo_root, items[i]) for i in ref_idx]
        refuted = _refute(ref_items, repo_root=repo_root, budget=budget, event_sink=event_sink)
        for orig, f in zip(ref_idx, refuted, strict=True):
            results[orig] = f

    return [results[i] for i in range(len(items)) if results[i] is not None]


def _refute(
    ref_items: list[Finding],
    *,
    repo_root: Path,
    budget: TurnBudget,
    event_sink=None,
) -> list[Finding | None]:
    """Run the refuter over the refutable findings.

    Returns a list aligned index-for-index with ``ref_items``: each slot a
    Finding annotated ``confirmed``/``unverified``, or None for one the
    refuter actively refuted -- the only drop. Every skip seam (disabled,
    CLI absent, no budget, no excerpts, exception) passes the whole set
    through ``unverified`` with a ``("skipped", <why>)`` event.
    """
    if os.environ.get("CHAMELEON_STOP_VERIFY") == "0":
        _sink(event_sink, "skipped", "disabled")
        return [dataclasses.replace(f, verified="unverified") for f in ref_items]

    try:
        from chameleon_mcp import refuter
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.judge import _valid_model

        absent = refuter.refuter_cli_absent()
        if absent is not None:
            _sink(event_sink, "skipped", absent)
            return [dataclasses.replace(f, verified="unverified") for f in ref_items]

        remaining = budget.remaining_seconds()
        timeout = max(15, min(threshold_int("REFUTER_TIMEOUT_SECONDS"), int(remaining)))
        max_spawns = threshold_int("REFUTER_MAX_SPAWNS_PER_INVOCATION")
        affordable = _affordable_spawns(remaining, timeout, max_spawns)
        if affordable <= 0:
            _sink(event_sink, "skipped", "no_budget")
            return [dataclasses.replace(f, verified="unverified") for f in ref_items]

        spawnable = [i for i, f in enumerate(ref_items) if f.excerpt]
        if not spawnable:
            _sink(event_sink, "skipped", "no_verifiable_excerpts")
            return [dataclasses.replace(f, verified="unverified") for f in ref_items]

        model = os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
        if not _valid_model(model):
            model = "sonnet"

        # High severity first so a budget that cannot cover every spawnable
        # finding spends on the highest-stakes ones; the rest keep the cap's
        # "unverified" fallback below.
        order = sorted(
            spawnable, key=lambda i: 0 if ref_items[i].severity in _HIGH_SEVERITIES else 1
        )
        ref_findings = [_to_refuter_dict(i, ref_items[i]) for i in order]
        excerpts = [ref_items[i].excerpt for i in order]
        verdicts = refuter.run_batch(
            repo_root,
            ref_findings,
            excerpts,
            model=model,
            timeout=timeout,
            max_spawns=affordable,
            concurrency=min(4, affordable),
            retry=False,
        )
    except Exception:  # noqa: BLE001 -- VERIFY must never drop a finding on failure
        _sink(event_sink, "skipped", "error")
        return [dataclasses.replace(f, verified="unverified") for f in ref_items]

    verdict_by_id: dict = {}
    for v in verdicts or []:
        if isinstance(v, dict):
            verdict_by_id[v.get("id")] = v.get("verdict")

    out: list[Finding | None] = []
    refuted = confirmed = unverified = 0
    for i, f in enumerate(ref_items):
        verdict = verdict_by_id.get(str(i))
        if verdict == "refuted":
            refuted += 1
            out.append(None)
        elif verdict == "confirmed":
            confirmed += 1
            out.append(dataclasses.replace(f, verified="confirmed"))
        else:
            unverified += 1
            out.append(dataclasses.replace(f, verified="unverified"))

    _sink(
        event_sink, "completed", f"refuted={refuted} confirmed={confirmed} unverified={unverified}"
    )
    return out
