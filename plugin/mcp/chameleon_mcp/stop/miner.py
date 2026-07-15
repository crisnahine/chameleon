"""The self-learning idiom miner (spec section 7.4): the detached review
job's END-of-run tail stage, run from ``stop/job.py::_run`` after every other
stage. Zero Stop-time cost -- it only ever executes inside the detached job,
never on a hook hot path.

Mines three usage signals out of artifacts the rest of Stop already writes
(the finding-lifecycle ledger and the override audit) into
``.chameleon/idiom-candidates/<slug>.json`` proposals via
``core.idiom_candidates.write_candidate``. NOTHING here auto-adopts: a
candidate is a proposal, never taught, never rendered into a live session's
context, never trust-hashed -- the exact same human approval a hand-taught
idiom needs (``/chameleon-teach`` or ``/chameleon-auto-idiom``) applies to a
mined candidate too. This module never imports or calls
``core.idiom_store.upsert_idiom``/``teach_record``/``regenerate_views``, and
never writes under ``idioms/`` -- only under the separate, unhashed
``idiom-candidates/`` directory.

Three independent, try-wrapped passes, each fail-open (a raising sub-step
records a ``review_job``/``miner_error`` check event and costs only ITS OWN
signal -- the other two, and the job itself, are unaffected):

- Signal 2 (new-idiom candidates): a ``correctness``/``duplication`` finding
  whose match_key has recurred at least ``IDIOM_MINER_MIN_RECURRENCE`` times
  across at least ``IDIOM_MINER_MIN_SESSIONS`` distinct sessions becomes a
  ``source="learned"`` candidate.
- Signal 3 (deprecation/loosening): a rule ``review_ledger.build_override_audit``
  flags (``high_override_rate`` or ``blanket_abuse``) becomes a candidate
  proposing to deprecate or loosen it, naming the rule and its override rate.
- Signal 1 (reinforcement): an idiom-lens finding that reached ``addressed``
  status appends reinforcement evidence onto its OWN idiom's candidate -- but
  only if one already exists (from a prior signal-2/3 mine, or a manually
  seeded proposal); it never mints a bare new candidate for an idiom nobody
  has proposed yet (design ambiguity #5).

Top-level imports stay stdlib-only; every non-stdlib symbol is resolved via a
deferred import inside the function that needs it, mirroring the rest of the
``stop/`` package's pattern of deferring every non-stdlib import to call time.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from chameleon_mcp.core.budget import TurnBudget
    from chameleon_mcp.stop.scheduler import JobRequest

_CHECK_NAME = "review_job"

# The idiom lens's own claim prefix (stop/lenses/idiom.py::_coerce_claim):
# `claim = f"idiom '{slug}' ({rec.title}): {message}"`. The captured group
# mirrors core.idiom_store._SLUG_RE exactly, so a match is always safe to use
# as a filename component.
_IDIOM_SLUG_CLAIM_RE = re.compile(r"idiom '([a-z][a-z0-9-]{2,63})'")


def _checkpoint(request: JobRequest, status: str, *, reason: str | None = None) -> None:
    try:
        from chameleon_mcp import hook_helper as hh

        hh._emit_check_event(
            request.repo_id, request.session_id, _CHECK_NAME, status, reason=reason
        )
    except Exception:
        pass


def _mine_new_candidates(request: JobRequest, profile_dir: Path) -> None:
    """Signal 2: recurring correctness/duplication findings become candidates."""
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.core import idiom_candidates
        from chameleon_mcp.core.idiom_store import slug_for_title
        from chameleon_mcp.review_ledger import _read_findings_rows

        min_recurrence = threshold_int("IDIOM_MINER_MIN_RECURRENCE")
        min_sessions = threshold_int("IDIOM_MINER_MIN_SESSIONS")
        count = 0
        for row in _read_findings_rows(request.repo_id).values():
            if not isinstance(row, dict) or row.get("kind") not in ("correctness", "duplication"):
                continue
            occurrences = int(row.get("recurrence") or 0) + 1
            session_ids = [str(s) for s in (row.get("session_ids") or []) if s]
            if occurrences < min_recurrence or len(session_ids) < min_sessions:
                continue
            claim = str(row.get("claim") or "").strip()
            if not claim:
                continue
            title = claim[:80]
            idiom_candidates.write_candidate(
                profile_dir,
                slug=slug_for_title(title),
                title=title,
                rationale=(
                    f"A {row.get('kind')} finding recurred {occurrences} time(s) across "
                    f"{len(session_ids)} session(s): {claim}"
                )[:1000],
                source="learned",
                evidence=(f"match_key={row.get('match_key', '')} file={row.get('file', '')}")[:500],
                occurrences=occurrences,
                session_ids=session_ids,
            )
            count += 1
        _checkpoint(request, "miner_new_candidates", reason=f"count={count}")
    except Exception as exc:  # noqa: BLE001 -- this signal must never crash the job
        _checkpoint(request, "miner_error", reason=f"new_candidates:{repr(exc)[:200]}")


def _mine_deprecation(request: JobRequest, profile_dir: Path) -> None:
    """Signal 3: over-overridden rules become deprecation/loosening candidates."""
    try:
        from chameleon_mcp.core import idiom_candidates
        from chameleon_mcp.core.idiom_store import slug_for_title
        from chameleon_mcp.review_ledger import build_override_audit

        audit = build_override_audit(request.repo_id)
        rules = audit.get("rules") or {}
        count = 0
        for rule in audit.get("flagged") or []:
            info = rules.get(rule) or {}
            rationale = (
                f"The rule '{rule}' is overridden in {info.get('overrides', 0)} of its "
                f"{int(info.get('overrides', 0)) + int(info.get('would_blocks', 0))} "
                f"triggers (override_rate={info.get('override_rate')}, "
                f"blanket_abuse={info.get('blanket_abuse')}) -- consider deprecating "
                "or loosening it."
            )
            idiom_candidates.write_candidate(
                profile_dir,
                slug=slug_for_title(f"deprecate {rule}"),
                title=f"Deprecate or loosen '{rule}'",
                rationale=rationale[:1000],
                source="learned",
                evidence=(
                    f"rule={rule} overrides={info.get('overrides')} "
                    f"distinct_sessions={info.get('distinct_sessions')}"
                )[:500],
                occurrences=1,
            )
            count += 1
        _checkpoint(request, "miner_deprecation", reason=f"count={count}")
    except Exception as exc:  # noqa: BLE001 -- this signal must never crash the job
        _checkpoint(request, "miner_error", reason=f"deprecation:{repr(exc)[:200]}")


def _mine_reinforcement(request: JobRequest, profile_dir: Path) -> None:
    """Signal 1: an addressed idiom finding reinforces its idiom's EXISTING
    candidate. Never mints a bare new candidate (design ambiguity #5) -- a
    slug with no proposal on disk yet is skipped."""
    try:
        from chameleon_mcp.core import idiom_candidates
        from chameleon_mcp.review_ledger import _read_findings_rows

        count = 0
        for row in _read_findings_rows(request.repo_id).values():
            if not isinstance(row, dict) or row.get("kind") != "idiom":
                continue
            if row.get("status") != "addressed":
                continue
            claim = str(row.get("claim") or "")
            match = _IDIOM_SLUG_CLAIM_RE.search(claim)
            if not match:
                continue
            slug = match.group(1)
            candidate_path = idiom_candidates.candidates_dir(profile_dir) / f"{slug}.json"
            if not candidate_path.is_file():
                continue
            idiom_candidates.write_candidate(
                profile_dir,
                slug=slug,
                title="",
                rationale="",
                source="learned",
                evidence=f"reinforced: idiom finding addressed -- {claim}"[:500],
                occurrences=1,
                session_ids=[str(s) for s in (row.get("session_ids") or []) if s],
            )
            count += 1
        _checkpoint(request, "miner_reinforcement", reason=f"count={count}")
    except Exception as exc:  # noqa: BLE001 -- this signal must never crash the job
        _checkpoint(request, "miner_error", reason=f"reinforcement:{repr(exc)[:200]}")


def run_miner(request: JobRequest, budget: TurnBudget) -> None:
    """End-of-job tail stage. Fail-open and NEVER raises into ``_run``: the
    only code here that can raise (the env read and the budget/profile-dir
    resolution) is trivial and guarded; each of the three mining passes
    already wraps its own body.

    Guarded by ``CHAMELEON_IDIOM_MINER=0`` (default ON -- offline, no repo-code
    execution, no network) and by whatever remains of the job's own budget
    after every earlier stage, capped by ``JOB_MINER_BUDGET_SECONDS``.
    """
    if os.environ.get("CHAMELEON_IDIOM_MINER") == "0":
        return

    try:
        from chameleon_mcp._thresholds import threshold_int

        window = min(float(threshold_int("JOB_MINER_BUDGET_SECONDS")), budget.remaining_seconds())
    except Exception as exc:  # noqa: BLE001 -- budget resolution must never crash the job
        _checkpoint(request, "miner_stage_error", reason=repr(exc)[:200])
        return
    if window <= 0:
        _checkpoint(request, "miner_skipped", reason="no_budget")
        return

    try:
        from chameleon_mcp import hook_helper as hh

        profile_dir = hh._enf_profile_dir(request.repo_root)
    except Exception as exc:  # noqa: BLE001 -- profile resolution must never crash the job
        _checkpoint(request, "miner_stage_error", reason=repr(exc)[:200])
        return

    _mine_new_candidates(request, profile_dir)
    _mine_deprecation(request, profile_dir)
    _mine_reinforcement(request, profile_dir)
