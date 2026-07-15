"""CHAMELEON_JUDGE_WAIT: the harness/eval synchronous-wait path over the
otherwise async-first detached review job (spec section 3.1).

One-shot ``claude -p`` sessions (the effectiveness eval, the journey
harness, CI) have no next UserPromptSubmit to deliver a detached job's
findings into, so under the async-first design a model-reviewed finding
would never reach that session's own transcript. ``CHAMELEON_JUDGE_WAIT=1``
makes Stop, after the scheduler would normally launch-and-return, instead
poll for that SAME job's completion (bounded by the Stop hook's own
remaining budget) and render whatever is ready in-turn -- trading
immediacy for a one-shot session's only chance to observe review output.

Polling ends on whichever comes first: the job's session-doc slot clears
(``core.session_state.SessionDoc.job_inflight`` reset by ``stop/job.py``'s
``finally``, the same clean-exit signal ``scheduler.try_acquire_job_slot``
trusts) or the heartbeat goes stale (the job crashed without clearing --
the identical staleness window ``try_acquire_job_slot`` itself reclaims on),
or ``budget`` expires. Once the job is confirmed done, the job's
pre-rendered payload (``stop.assemble.read_delivery_payload``) is preferred
-- it is written moments earlier by the exact job being waited on, the
freshest possible source -- falling back to a live ``deliver_for_root`` call
when no payload exists (the job ran but had nothing to persist, or exited
before ever reaching its render step).

NOT wired into the live Stop pipeline yet. ``stop/scheduler.py``'s
route/launch and ``stop/job.py``'s runner are not yet called from
``stop/pipeline.py`` or ``hook_helper.stop_backstop`` (that wiring is a
later task). This module is the poll-and-render helper that wiring will
call once it lands; every test here drives a seeded heartbeat file /
session doc / ledger row directly -- never a real launched job.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.budget import TurnBudget


def judge_wait_enabled() -> bool:
    return os.environ.get("CHAMELEON_JUDGE_WAIT") == "1"


def _job_done(repo_id: str, session_id: str, heartbeat_path: Path) -> bool:
    """True once the job owning ``heartbeat_path`` is no longer inflight.

    Primary signal: the session doc's ``job_inflight`` no longer names this
    exact heartbeat path -- either ``stop.job``'s ``finally`` cleared it (a
    clean exit) or a LATER job's claim overwrote it (this one is stale by
    definition either way). Fallback: the heartbeat file itself has gone
    stale (the process died before ever reaching its own cleanup) or
    vanished outright -- the same staleness window
    ``scheduler.try_acquire_job_slot`` reclaims a dead job's slot on, so
    "done" here never disagrees with what a fresh Stop would independently
    conclude.
    """
    try:
        from chameleon_mcp.core.session_state import read_session_doc

        doc = read_session_doc(repo_id, session_id)
        if doc.job_inflight != str(heartbeat_path):
            return True
    except Exception:
        pass
    try:
        from chameleon_mcp._thresholds import threshold_int

        stale_after = threshold_int("JOB_HEARTBEAT_STALE_SECONDS")
        age = time.time() - heartbeat_path.stat().st_mtime
        return age >= stale_after
    except OSError:
        return True  # heartbeat file gone entirely: nothing left to wait for


def wait_for_job(
    repo_id: str,
    session_id: str,
    heartbeat_path: Path,
    budget: TurnBudget,
    *,
    poll_interval: float | None = None,
) -> bool:
    """Block until ``_job_done`` or ``budget`` runs out.

    Never sleeps past the budget's own deadline. Returns whether the job is
    confirmed done (True) or the budget was exhausted while it was still
    live (False) -- the caller treats False as "nothing to show this turn,
    the finding stays pending for the next real delivery point," exactly
    like an ordinary async turn.
    """
    if poll_interval is None:
        from chameleon_mcp._thresholds import threshold_float

        poll_interval = threshold_float("JUDGE_WAIT_POLL_INTERVAL_SECONDS")
    while True:
        if _job_done(repo_id, session_id, heartbeat_path):
            return True
        remaining = budget.remaining_seconds()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval, remaining))


def wait_and_render(
    *,
    repo_id: str,
    repo_data: Path,
    ws_root,
    session_id: str,
    heartbeat_path: Path,
    budget: TurnBudget,
    poll_interval: float | None = None,
) -> str | None:
    """The full CHAMELEON_JUDGE_WAIT path: wait, then render in-turn.

    A no-op (returns None immediately, no polling, no sleeping) unless
    ``CHAMELEON_JUDGE_WAIT=1`` -- ordinary async turns must never pay even
    one poll tick. Marks delivered exactly what it emits (via
    ``stop.delivery``'s shared helpers), so a finding shown here is not
    re-shown at the next real UserPromptSubmit.
    """
    if not judge_wait_enabled():
        return None

    done = wait_for_job(repo_id, session_id, heartbeat_path, budget, poll_interval=poll_interval)
    if not done:
        return None

    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.stop.delivery import deliver_for_root

    try:
        return deliver_for_root(
            repo_id,
            repo_data,
            ws_root,
            session_id,
            ceiling_tokens=threshold_int("REVIEW_RENDER_TOKEN_CEILING"),
        )
    except Exception:
        return None
