"""The detached job runner: the child ``stop/scheduler.py`` launches as
``python -m chameleon_mcp.stop.job <request-file>``.

Reads the request file, runs the turn's active lenses (in parallel, per
spec section 5.1: "lenses 150s (parallel where the model allows)") under one
``core.budget.TurnBudget`` anchored at process entry, runs VERIFY
(``stop/verify.py``) over whatever the lenses found, persists the surviving
findings, and clears its session-doc job slot. It absorbs ``judge_async.py``'s
role as the detached correctness-judge child, generalized to every lens.

Fail-open at every seam (spec section 8): a stage exception is caught,
recorded as a ``review_job`` check event, and the run continues with
whatever the stage produced (usually nothing) -- ``main`` always returns 0.
Nobody reads this process's exit code (the scheduler launches it with
stdout/stderr DEVNULL and does not wait on it), so a nonzero exit would
communicate nothing; the check-event log is the only outcome record.

This module MUST NOT consult the optout hierarchy (``CHAMELEON_DISABLE`` /
``is_chameleon_suppressed``) as a run/skip gate. It inherits
``CHAMELEON_DISABLE=1`` from ``scheduler._job_env`` -- that flag exists so
the reviewer CHILDREN this job spawns (``claude -p``) never recurse into
chameleon's own hooks; reading it here too would make every job read its own
environment as "chameleon disabled" and silently no-op forever.

Findings are persisted through ``review_ledger.record_findings`` -- the
canonical finding-lifecycle ledger (one JSON row per match_key under the
repo's plugin-data dir, keyed for cross-session recurrence; see
core/finding.py's lifecycle and review_ledger.py's surface-bar/resurface
API). That ledger is a NEW store, distinct from the legacy
``.judge_pending.<sid>.json`` judge_async.py still writes and the
``judge_findings`` drift.db table stop/gates.py's still-live gates read and
write -- both keep running unchanged until the Stop pipeline is switched
over to this job runner and the ledger's delivery/resurface API.

Top-level imports stay stdlib-only; every non-stdlib symbol is resolved via
a deferred import inside the function that needs it, mirroring the rest of
the ``stop/`` package, so a test that monkeypatches
``chameleon_mcp.stop.lenses.resolve_runner`` (or any other module attribute)
stays effective for a call made from here.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.budget import TurnBudget
    from chameleon_mcp.core.finding import Finding
    from chameleon_mcp.stop.scheduler import JobRequest

_CHECK_NAME = "review_job"


def _checkpoint(request: JobRequest, status: str, *, reason: str | None = None) -> None:
    try:
        from chameleon_mcp import hook_helper as hh

        hh._emit_check_event(
            request.repo_id, request.session_id, _CHECK_NAME, status, reason=reason
        )
    except Exception:
        pass


def _load_request(path: Path) -> JobRequest | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        from chameleon_mcp.stop.scheduler import JobRequest

        return JobRequest.from_dict(raw)
    except Exception:
        return None


def _unlink_request_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _heartbeat_loop(path: Path, stop_event: threading.Event, interval: float) -> None:
    """Touch ``path``'s mtime immediately, then every ``interval`` seconds
    until ``stop_event`` is set. ``Event.wait`` returns as soon as the event
    fires, so shutdown never waits out a full interval."""
    while True:
        try:
            path.touch(exist_ok=True)
            os.chmod(path, 0o600)
        except OSError:
            pass
        if stop_event.wait(interval):
            return


def _run_lens_one(
    request: JobRequest, name: str, timeout: float, model: str | None
) -> list[Finding]:
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.stop.lenses import resolve_runner

    def _sink(kind, detail=None) -> None:
        _checkpoint(request, "lens_event", reason=f"{name}:{kind}:{detail or ''}"[:300])

    try:
        runner = resolve_runner(name)
    except Exception as exc:
        _checkpoint(request, "lens_error", reason=f"{name}:resolve:{repr(exc)[:200]}")
        return []

    try:
        repo_root = request.repo_root
        profile_dir = hh._enf_profile_dir(repo_root)
        resolver = hh._archetype_resolver(repo_root, {"available": True})
        result = runner(
            repo_root,
            profile_dir,
            list(request.files),
            resolver,
            intent_tokens=list(request.intent_tokens),
            budget=timeout,
            event_sink=_sink,
            model=model,
        )
        return list(result.findings)
    except Exception as exc:
        _checkpoint(request, "lens_error", reason=f"{name}:{repr(exc)[:200]}")
        return []


def _run_lenses(request: JobRequest, budget: TurnBudget) -> list[Finding]:
    """Run every requested lens concurrently under the shared lens-stage
    budget window, mirroring the pre-phase-3 ``lens_runner.run_lenses``'s
    reason for going concurrent: sequential spawns would sum each lens's own
    timeout, and the job's total budget -- generous as it is -- is not
    unbounded. A lens that raises (or fails to resolve) never takes the
    others down with it; each is wrapped independently in ``_run_lens_one``.
    """
    if not request.lens_names:
        return []
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.judge import _valid_model

        lens_window = min(
            float(threshold_int("JOB_LENS_BUDGET_SECONDS")), budget.remaining_seconds()
        )
        if lens_window <= 0:
            _checkpoint(request, "lens_skipped", reason="no_budget")
            return []

        model = request.model if _valid_model(request.model) else None

        names = list(request.lens_names)
        from concurrent.futures import ThreadPoolExecutor

        all_findings: list[Finding] = []
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
            for findings in ex.map(lambda n: _run_lens_one(request, n, lens_window, model), names):
                all_findings.extend(findings)
        return all_findings
    except Exception as exc:  # noqa: BLE001 -- the lens stage must never crash the job
        _checkpoint(request, "lens_stage_error", reason=repr(exc)[:200])
        return []


def _run_verify(request: JobRequest, findings: list[Finding], budget: TurnBudget) -> list[Finding]:
    if not findings:
        return []
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.core.budget import TurnBudget as _TurnBudget
        from chameleon_mcp.stop.verify import verify_findings

        verify_window = max(
            0.0, min(float(threshold_int("JOB_VERIFY_BUDGET_SECONDS")), budget.remaining_seconds())
        )
        verify_budget = _TurnBudget.for_hook(
            total_seconds=verify_window, token_ceiling=budget.tokens_remaining()
        )

        def _sink(status, detail=None) -> None:
            _checkpoint(request, f"verify_{status}", reason=detail)

        return verify_findings(
            findings, repo_root=request.repo_root, budget=verify_budget, event_sink=_sink
        )
    except Exception as exc:  # noqa: BLE001 -- VERIFY must never crash the job
        _checkpoint(request, "verify_stage_error", reason=repr(exc)[:200])
        return list(findings)


def _persist(request: JobRequest, findings: list[Finding]) -> None:
    """Persist surviving findings to the canonical finding-lifecycle ledger.

    See core/finding.py's lifecycle and review_ledger.record_findings's
    surface bar for what happens to each finding from here.
    """
    if not findings:
        return
    try:
        from chameleon_mcp import review_ledger

        review_ledger.record_findings(request.repo_id, str(request.repo_root), findings)
    except Exception as exc:  # noqa: BLE001 -- persistence must never crash the job
        _checkpoint(request, "persist_error", reason=repr(exc)[:200])


def _run(request: JobRequest) -> None:
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.core.budget import TurnBudget

    budget = TurnBudget.for_hook(
        total_seconds=float(threshold_int("JOB_TOTAL_BUDGET_SECONDS")),
        token_ceiling=threshold_int("JOB_TOKEN_CEILING"),
    )
    findings = _run_lenses(request, budget)
    verified = _run_verify(request, findings, budget)
    _persist(request, verified)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m chameleon_mcp.stop.job <request-file>``.

    Always returns 0 (see module docstring). A missing argv, or a request
    file that cannot be loaded, is itself a fail-open no-op: with no
    repo_id/session_id resolved there is nowhere meaningful to record a
    check event against, so this returns 0 silently in both cases.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    request = _load_request(Path(args[0]))
    if request is None:
        return 0
    _unlink_request_file(Path(args[0]))

    from chameleon_mcp._thresholds import threshold_int

    interval = float(threshold_int("JOB_HEARTBEAT_INTERVAL_SECONDS"))
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(Path(request.heartbeat_path), stop_event, interval),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        _run(request)
    except Exception as exc:  # noqa: BLE001 -- the job must never exit un-slotted
        _checkpoint(request, "run_error", reason=repr(exc)[:200])
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(1.0, interval))
        try:
            from chameleon_mcp.stop.scheduler import clear_job_slot

            clear_job_slot(request.repo_id, request.session_id)
        except Exception:
            pass
        # Deliberately does NOT unlink the heartbeat file: its path is
        # deterministic per (repo_id, session_id) and reused by
        # ``scheduler.try_acquire_job_slot``, which always re-touches it on
        # the NEXT claim. Unlinking here races that next claim -- if it slots
        # in between ``clear_job_slot`` above (which frees job_inflight) and
        # an unlink below, the unlink would delete the NEW job's
        # freshly-touched heartbeat file, and its own staleness check would
        # then see a missing file and misread the brand-new job as dead.
    return 0


if __name__ == "__main__":
    sys.exit(main())
