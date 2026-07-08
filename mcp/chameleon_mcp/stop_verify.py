"""VERIFY stage for the turn-end Stop review pipeline.

Chameleon's turn-end review runs SCOPE (route + diff scoping) -> EVIDENCE (caller /
contract facts) -> ATTACK (the correctness judge / review lenses) -> REPORT. The
VERIFY stage was historically absent from this path: the reviewer's findings went
straight to REPORT with no independent verification, so a plausible-but-wrong finding
surfaced unchallenged. The independent refuter (``refuter.py``) already existed but
was wired only to the interactive pr-review / receiving skills. This module wires it
into the automatic turn-end path (both the single-lens correctness gate and the
default multi-lens pass, plus the async-detached child).

Contract (mirrors the skills' round-3 refuter): VERIFY may only ever DROP a finding
the refuter actively REFUTES on real evidence. On any failure -- disabled, no budget,
CLI absent, spawn error -- it passes every finding through labeled ``unverified`` and
never drops one. A finding whose excerpt cannot be safely fetched (no file, no
readable contained path) is NEVER spawned: the refuter prompt commands refutation
when the excerpt cannot support the claim, so a zero-evidence spawn would
systematically kill anchorless findings -- those pass through unverified instead. It
never invents a ``confirmed``. So a broken refuter degrades to today's behavior (raw
findings surfaced), never to silently swallowing real defects.

Budget: the sync Stop path is capped by the 55s hook wrapper, so its caller passes
only the measured remaining budget and this stage spawns a refuter only when a full
timeout window fits; the async-detached child passes its generous remainder and
verifies the full set. Spawns run with ``retry=False`` so one slot is bounded by
exactly one timeout window (the arithmetic a hard-capped hook depends on).
``CHAMELEON_STOP_VERIFY=0`` disables the stage entirely (pass-through).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Mirror hook_helper._FINDING_HIGH_CONFIDENCE: a correctness finding carries only a
# 0..1 confidence; at/above this floor it reads high-severity (which drives both the
# refuter model ladder and REPORT ranking).
_HIGH_CONFIDENCE = 0.7
_EXCERPT_CHAR_CAP = 4000
_HEAD_FALLBACK_LINES = 50


@dataclass
class VerifyResult:
    """Outcome of the VERIFY stage.

    ``kept`` are the surviving findings, ranked for REPORT (confirmed-high first, then
    confidence descending). ``kept_verdicts`` is aligned index-for-index with ``kept``
    (``"confirmed"`` | ``"unverified"``). ``ran`` is False whenever the stage
    passed everything through without an actual refuter batch (disabled / no budget /
    unavailable / error), so the caller can tell a real verification from a fallthrough.
    """

    kept: list
    kept_verdicts: list
    refuted: int
    confirmed: int
    unverified: int
    ran: bool
    skip_reason: str | None = None


def _enabled() -> bool:
    return os.environ.get("CHAMELEON_STOP_VERIFY") != "0"


def _field(f, *names):
    """Read ``names`` off a finding that may be a judge Finding (attrs) or a
    multi-lens synthesis dict (keys); first present non-None wins."""
    for name in names:
        if isinstance(f, dict):
            val = f.get(name)
        else:
            val = getattr(f, name, None)
        if val is not None:
            return val
    return None


def _severity_for(f) -> str:
    """Normalized severity across finding shapes (mirrors hook_helper._finding_severity):
    an explicit ``severity`` string wins; a confidence at/above the high floor reads
    high; two lenses independently agreeing reads high; else medium."""
    sev = _field(f, "severity")
    if isinstance(sev, str) and sev:
        return sev
    conf = _field(f, "confidence")
    try:
        if conf is not None and float(conf) >= _HIGH_CONFIDENCE:
            return "high"
    except (TypeError, ValueError):
        pass
    lenses = _field(f, "lenses")
    if isinstance(lenses, list) and len(lenses) >= 2:
        return "high"
    return "medium"


def _affordable_spawns(budget_seconds, timeout, max_spawns: int, concurrency: int = 4) -> int:
    """How many refuter spawns fit in ``budget_seconds`` at ``timeout`` each.

    ``budget_seconds=None`` means unbounded (the async child's generous budget) and
    yields ``max_spawns``. Otherwise the refuter runs ``concurrency`` spawns per wave,
    so ``(budget // timeout)`` full timeout windows buy that many waves; the result is
    capped at ``max_spawns``. Spawns run retry-free, so a wave is bounded by one
    timeout window. A budget too small for even one window yields 0 (the caller then
    passes findings through unverified rather than risk the wall-clock cap).
    """
    if budget_seconds is None:
        return max(0, max_spawns)
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
    return min(max_spawns, waves * max(1, concurrency))


def _contained_rel(repo_root, rel_or_abs) -> str | None:
    """``rel_or_abs`` as a repo-relative path iff it stays inside ``repo_root``.

    The finding's file field is unvalidated model output, so an absolute path
    outside the repo or a ``..`` traversal must never be read -- the excerpt is
    inlined into a model prompt, and an escape would exfiltrate arbitrary local
    files (the sibling skills path uses safe_read_text for the same reason).
    Returns None when the path escapes or cannot be normalized.
    """
    try:
        root = Path(repo_root).resolve()
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p.resolve().relative_to(root).as_posix()
        # Relative: let resolve() collapse any ../ then require containment.
        return (root / p).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _excerpt_window(repo_root, rel_or_abs, line, *, context: int = 25) -> str:
    """A +/-``context``-line window around ``file:line``, containment-checked. Fail-open "".

    Reads through ``safe_read_text`` (symlink/size/segment checks) after confirming
    the path stays inside ``repo_root``. The turn's edits are already on disk, so no
    base_ref diff is needed (unlike the pr-review refuter, which scopes to
    base...HEAD). A finding with a readable file but no line number falls back to the
    file's first ~50 lines, so an anchorless-but-filed finding still gets real
    evidence. A missing/escaping/unreadable path yields "" -- the CALLER then skips
    the spawn entirely and passes the finding through unverified (never a
    zero-evidence refutation).
    """
    if not rel_or_abs:
        return ""
    rel = _contained_rel(repo_root, rel_or_abs)
    if rel is None:
        return ""
    try:
        from chameleon_mcp.safe_open import safe_read_text

        text = safe_read_text(Path(repo_root).resolve(), rel)
    except Exception:  # UnsafeFileError, OSError, decode -- all fail-open to ""
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    if isinstance(line, int) and line > 0:
        lo = max(0, line - 1 - context)
        hi = min(len(lines), line - 1 + context + 1)
    else:
        lo, hi = 0, _HEAD_FALLBACK_LINES
    return "\n".join(lines[lo:hi])[:_EXCERPT_CHAR_CAP]


def _finding_to_refuter_dict(idx: int, f) -> dict:
    """Adapt a turn-end finding to the refuter's finding shape.

    ``id`` is the original list index so verdicts map back after the batch reorders /
    caps. ``severity`` drives the refuter's per-finding model ladder (a high finding
    can escalate to a stronger refuter model).
    """
    return {
        "id": str(idx),
        "severity": _severity_for(f),
        "file": _field(f, "file") or "",
        "line": _field(f, "line"),
        "claim": _field(f, "message", "claim") or "",
        "evidence": "",
    }


def _rank_key(f, verdict):
    confirmed_high = 0 if (verdict == "confirmed" and _severity_for(f) == "high") else 1
    try:
        conf = -float(_field(f, "confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return (confirmed_high, conf)


def rank_kept(findings, verify_by_id) -> list:
    """Order surviving findings for REPORT: confirmed-high first, then confidence desc.

    ``verify_by_id`` maps ``str(index-into-findings)`` -> verdict. Sort is stable, so
    ties keep input order. Pure helper -- no side effects.
    """
    items = list(findings)
    return [
        f
        for _, f in sorted(
            enumerate(items),
            key=lambda pair: _rank_key(pair[1], verify_by_id.get(str(pair[0]))),
        )
    ]


def _passthrough(items, reason: str) -> VerifyResult:
    return VerifyResult(list(items), ["unverified"] * len(items), 0, 0, len(items), False, reason)


def verify_stop_findings(
    repo_root,
    findings,
    *,
    budget_seconds,
    model: str,
    max_spawns: int,
    timeout: int,
    enabled: bool | None = None,
) -> VerifyResult:
    """Independently VERIFY the turn-end reviewer's findings with the refuter.

    Returns a :class:`VerifyResult`. Fails open at every seam: a refuted finding is the
    ONLY thing that gets dropped; anything else keeps every finding, labeled unverified.
    A finding without a fetchable excerpt is never spawned (it passes through
    unverified), so evidence absence can never masquerade as refutation.
    """
    items = list(findings or [])
    if not items:
        return VerifyResult([], [], 0, 0, 0, False, "no findings")
    if enabled is None:
        enabled = _enabled()
    if not enabled:
        return _passthrough(items, "disabled")

    affordable = _affordable_spawns(budget_seconds, timeout, max_spawns)
    if affordable <= 0:
        return _passthrough(items, "no budget")

    try:
        from chameleon_mcp import refuter

        absent = refuter.refuter_cli_absent()
        if absent is not None:
            return _passthrough(items, absent)

        # Only findings with a real excerpt are spawnable: the refuter prompt
        # commands refutation when the excerpt cannot support the claim, so a
        # zero-evidence spawn would kill anchorless findings on data absence.
        excerpt_by_idx = {
            i: _excerpt_window(repo_root, _field(items[i], "file"), _field(items[i], "line"))
            for i in range(len(items))
        }
        # Pin each finding's reviewed-excerpt hash so a later render (the async
        # next-turn delivery) can flag it "[stale]" if the cited code changed
        # since review, instead of silently dropping it. Only Finding objects
        # carry the field; dict-shaped items are left untouched. Best-effort.
        try:
            from chameleon_mcp.judge import pin_excerpt

            for i, ex in excerpt_by_idx.items():
                if ex and hasattr(items[i], "excerpt_sha"):
                    pin_excerpt(items[i], ex)
        except Exception:
            pass
        spawnable = [i for i in range(len(items)) if excerpt_by_idx[i]]
        if not spawnable:
            return _passthrough(items, "no verifiable excerpts")

        # Rank high-severity first so the bounded spawn budget is spent on the
        # highest-stakes findings; the refuter caps the rest to unverified (kept).
        order = sorted(spawnable, key=lambda i: 0 if _severity_for(items[i]) == "high" else 1)
        ref_findings = [_finding_to_refuter_dict(i, items[i]) for i in order]
        excerpts = [excerpt_by_idx[i] for i in order]
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
        return _passthrough(items, "verify error")

    verdict_by_id: dict = {}
    for v in verdicts or []:
        if isinstance(v, dict):
            verdict_by_id[v.get("id")] = v.get("verdict")

    survivors: list[tuple] = []  # (finding, verdict)
    refuted = confirmed = unverified = 0
    for i, f in enumerate(items):
        verdict = verdict_by_id.get(str(i), "unverified")
        if verdict == "refuted":
            refuted += 1
            continue
        if verdict == "confirmed":
            confirmed += 1
        else:
            verdict = "unverified"
            unverified += 1
        survivors.append((f, verdict))

    survivors.sort(key=lambda pair: _rank_key(pair[0], pair[1]))
    kept = [f for f, _ in survivors]
    kept_verdicts = [v for _, v in survivors]
    return VerifyResult(kept, kept_verdicts, refuted, confirmed, unverified, True, None)
