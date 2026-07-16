"""The detached job runner: the child ``stop/scheduler.py`` launches as
``python -m chameleon_mcp.stop.job <request-file>``.

Reads the request file, runs the turn's active lenses (in parallel, per
spec section 5.1: "lenses 150s (parallel where the model allows)") under one
``core.budget.TurnBudget`` anchored at process entry, shadow-logs every RAW
finding for precision sampling (``CHAMELEON_STOP_VERIFY``'s documented
contract: "raw findings are still shadow-logged pre-VERIFY"), runs VERIFY
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
API). That ledger superseded two older stores: the legacy
``.judge_pending.<sid>.json`` queue (whose writer, the old async judge, is
gone -- only pre-upgrade leftover files remain, migrated once into the
ledger via ``review_ledger.migrate_pending_queue``) and the
``judge_findings`` drift.db table the pre-cutover ``stop/gates.py`` gates
read and wrote (now retired -- nothing reads or writes that table anymore).

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
        # Only the idiom lens accepts shown_idiom_slugs, and only the
        # correctness lens accepts intent_contract -- each rides as a
        # conditional kwarg rather than a parameter every lens runner must
        # declare and ignore.
        extra_kwargs: dict = {}
        if name == "idiom":
            extra_kwargs["shown_idiom_slugs"] = list(request.shown_idiom_slugs)
        if name == "correctness" and os.environ.get("CHAMELEON_INTENT_CONTRACT") != "0":
            excerpts = list(request.intent_excerpts)
            scope_lines = list(request.scope_lines)
            if excerpts or scope_lines:
                extra_kwargs["intent_contract"] = {
                    "excerpts": excerpts,
                    "scope_lines": scope_lines,
                }
        result = runner(
            repo_root,
            profile_dir,
            list(request.files),
            resolver,
            intent_tokens=list(request.intent_tokens),
            budget=timeout,
            event_sink=_sink,
            model=model,
            **extra_kwargs,
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


def _shadow_log_raw_findings(request: JobRequest, findings: list[Finding]) -> None:
    """Shadow-log every RAW lens finding before VERIFY runs, for later
    human-labeled precision sampling.

    Mirrors the pre-cutover ``_correctness_judge_gate``'s emit exactly (same
    hook name, rule, and shape: ``stop-correctness-judge`` /
    ``correctness-judge-finding``), so a precision-sampling query written
    against the old metric keeps working unchanged -- a finding VERIFY later
    refutes is exactly the row a precision sample needs, so this must run
    BEFORE ``_run_verify`` drops anything, and it must fire regardless of
    what VERIFY later does. Never blocks (``would_block`` is always False --
    a lens finding is advisory only).
    """
    if not findings:
        return
    try:
        from chameleon_mcp.metrics import emit_hook_metric

        for f in findings:
            line = f.span[0] if f.span else None
            emit_hook_metric(
                "stop-correctness-judge",
                elapsed_ms=0,
                repo_id=request.repo_id,
                advisory_emitted=True,
                would_block=False,
                rule="correctness-judge-finding",
                # The lens reports a repo-relative path already; keep it as
                # given rather than re-resolving against the working directory.
                file_rel=f.file,
                line=line,
            )
    except Exception as exc:  # noqa: BLE001 -- shadow logging must never crash the job
        _checkpoint(request, "shadow_log_error", reason=repr(exc)[:200])


def _resolve_surface_bar(repo_root: Path) -> str:
    """The repo's configured ``review.surface_bar``, fail-open to "medium".

    Reads the same ``config.json`` every other enforcement gate reads
    (``hook_helper._enf_profile_dir`` -- worktree-aware, resolves to the main
    worktree's profile). Any read/parse failure (missing profile, malformed
    config, unrecognized section) falls back to the built-in default rather
    than risking the job on a config read.
    """
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.profile.config import load_config

        return load_config(hh._enf_profile_dir(repo_root)).review.surface_bar
    except Exception:
        return "medium"


def _persist(request: JobRequest, findings: list[Finding]) -> None:
    """Persist surviving findings to the canonical finding-lifecycle ledger.

    See core/finding.py's lifecycle and review_ledger.record_findings's
    surface bar for what happens to each finding from here.
    """
    if not findings:
        return
    try:
        from chameleon_mcp import review_ledger

        review_ledger.record_findings(
            request.repo_id,
            str(request.repo_root),
            findings,
            surface_bar=_resolve_surface_bar(request.repo_root),
            session_id=request.session_id,
        )
    except Exception as exc:  # noqa: BLE001 -- persistence must never crash the job
        _checkpoint(request, "persist_error", reason=repr(exc)[:200])


def _write_delivery_payload(request: JobRequest) -> None:
    """Pre-render this job's repo's undelivered findings into the delivery
    payload cache (spec section 3.5), so a later UserPromptSubmit read under
    the callout-detector wrapper's 3s cap only ever pays for a file read.

    Renders the FULL current ``undelivered_findings`` snapshot for
    ``request.repo_root`` -- not just this run's own new findings -- so the
    cache always reflects everything still pending for this workspace. The
    render's own ``delivered_match_keys`` (the subset that fit under the
    ceiling) is persisted ALONGSIDE the text, so a cache-hit consumer
    (``stop/delivery.py``'s ``deliver_for_root``) marks delivered ONLY what
    the text shows -- an overflow finding the render omitted stays pending
    rather than being silently retired unseen. An empty snapshot
    writes/clears an empty payload rather than leaving a stale render in
    place. Fail-open: any exception here only costs the fast-path cache,
    never the job's own persisted findings.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.profile.trust import repo_data_dir
        from chameleon_mcp.review_ledger import undelivered_findings
        from chameleon_mcp.stop.assemble import render_findings, write_delivery_payload
        from chameleon_mcp.stop.delivery import _annotate_staleness, _delivery_header

        live = undelivered_findings(request.repo_id, ws_roots=[str(request.repo_root)])
        text = ""
        match_keys: tuple[str, ...] = ()
        if live:
            live = _annotate_staleness(request.repo_root, live)
            rendered = render_findings(
                live,
                header=_delivery_header(len(live)),
                ceiling_tokens=threshold_int("REVIEW_RENDER_TOKEN_CEILING"),
            )
            text = rendered.text
            match_keys = rendered.delivered_match_keys
        write_delivery_payload(repo_data_dir(request.repo_id), request.session_id, text, match_keys)
    except Exception as exc:  # noqa: BLE001 -- the payload cache must never crash the job
        _checkpoint(request, "payload_render_error", reason=repr(exc)[:200])


def _run_miner(request: JobRequest, budget: TurnBudget) -> None:
    """Self-learning idiom miner (spec section 7.4): the job's END-of-run tail
    stage, mining candidates from the ledger + override audit into
    ``.chameleon/idiom-candidates/`` -- never the live idiom store. Runs LAST,
    after the delivery payload, so it never competes with the lens/VERIFY/
    persist/render stages that actually gate what the user sees this turn.
    ``stop/miner.py::run_miner`` is already fail-open at every one of its own
    seams; this wrapper only guards the deferred import itself, mirroring
    every other stage in this module.
    """
    try:
        from chameleon_mcp.stop.miner import run_miner

        run_miner(request, budget)
    except Exception as exc:  # noqa: BLE001 -- the miner must never crash the job
        _checkpoint(request, "miner_stage_error", reason=repr(exc)[:200])


def _run(request: JobRequest) -> None:
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.core.budget import TurnBudget

    try:
        # This process IS the detached review child: reviewer spawns that
        # resolve their timeout internally (duplication_review.judge_body_matches
        # via judge._reviewer_timeout_seconds) may take the generous detached
        # budget instead of the short synchronous one -- the job owns its own
        # wall clock, no 55s hook wrapper caps it.
        from chameleon_mcp.judge import mark_detached_run

        mark_detached_run()
    except Exception:
        pass

    budget = TurnBudget.for_hook(
        total_seconds=float(threshold_int("JOB_TOTAL_BUDGET_SECONDS")),
        token_ceiling=threshold_int("JOB_TOKEN_CEILING"),
    )
    findings = _run_lenses(request, budget)
    _shadow_log_raw_findings(request, findings)
    verified = _run_verify(request, findings, budget)
    _persist(request, verified)
    _write_delivery_payload(request)
    _run_miner(request, budget)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m chameleon_mcp.stop.job <request-file>``.

    Always returns 0 (see module docstring). A missing argv, or a request
    file that cannot be loaded, is itself a fail-open no-op: with no
    repo_id/session_id resolved there is nowhere meaningful to record a
    check event against, so this returns 0 silently in both cases.

    Everything from here on that can raise -- resolving the heartbeat
    interval, constructing and starting the heartbeat thread, and running
    the job itself -- lives inside ONE try whose ``finally`` always clears
    the job slot. A failure in any of that setup used to happen BEFORE the
    try/finally existed at all, so an exception there (a bad threshold read,
    a thread the OS refused to start under resource pressure) skipped
    ``clear_job_slot`` entirely and left the session's single-inflight slot
    wedged until the heartbeat staleness window expired on its own.
    ``interval`` is seeded with a safe fallback before the try so the
    ``finally``'s heartbeat-thread join has a sane timeout even when
    ``threshold_int`` itself is what raised.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    request = _load_request(Path(args[0]))
    if request is None:
        return 0
    _unlink_request_file(Path(args[0]))

    stop_event = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    interval = 10.0
    try:
        from chameleon_mcp._thresholds import threshold_int

        interval = float(threshold_int("JOB_HEARTBEAT_INTERVAL_SECONDS"))
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(Path(request.heartbeat_path), stop_event, interval),
            daemon=True,
        )
        heartbeat_thread.start()
        _run(request)
    except Exception as exc:  # noqa: BLE001 -- the job must never exit un-slotted
        _checkpoint(request, "run_error", reason=repr(exc)[:200])
    finally:
        stop_event.set()
        if heartbeat_thread is not None:
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
