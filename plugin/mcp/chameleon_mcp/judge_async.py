"""Detached post-Stop correctness-judge runner.

Two routes lead here: the operator opt-in (``CHAMELEON_JUDGE_ASYNC=1``) and
the automatic preference when a prior spawn proved ``claude --bare`` loses
credentials on this install -- the plain fallback spawn pays the full session
primer and cannot fit the synchronous Stop budget, so the route detaches even
without the variable. An explicit ``CHAMELEON_JUDGE_ASYNC=0`` forces sync
regardless (accepting the likely spawn timeout, which the SessionStart
judge-health banner and /chameleon-doctor surface).

The synchronous judge spawn pays its wall-clock budget inside the Stop hook.
This module moves that cost off the turn: the gate writes a request file plus
an in-flight marker and detaches a ``python -m chameleon_mcp.judge_async``
child (``start_new_session=True`` so a process-group kill at hook exit cannot
reap it), then returns immediately. The child runs the same judge pipeline
(under the generous ``CORRECTNESS_JUDGE_FALLBACK_TIMEOUT_SECONDS`` spawn
budget when bare auth is known failed), writes its findings to a per-session
pending file, and the next UserPromptSubmit delivers them -- dropping any
finding whose file was edited again in between (digest mismatch). POSIX-only:
``launch_async_judge`` returns False elsewhere and the caller falls back to
the synchronous spawn -- so on Windows a bare-auth-failed install keeps the
sync spawn with the short budget, and the resulting timeout stays visible
through the judge-health banner and doctor.

Failure modes and their mitigations:

- Orphan child (host killed it before completion): the in-flight marker goes
  stale; ``is_inflight_fresh`` unlinks markers older than twice the child's
  spawn budget, and the child itself is bounded by the spawn's own wall clock.
- Partial writes: every file here is written tmp + ``os.replace``, so a reader
  never sees partial JSON.
- Stale findings: the pending file records each reviewed file's content
  digest; the delivery path drops findings whose file no longer matches.
- Session ends before delivery: the pending and in-flight markers remain on
  disk where the Stop attestation can read an unfinished spawn as a SKIPPED
  check, and the SessionStart retention sweep removes leftovers.
- Double spawn: the routing gate skips while a fresh in-flight marker exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.optouts import _safe_session_marker

# Sink kinds that mean the reviewer never produced a usable verdict; reviewed
# files stay unmarked so the next Stop can retry under the session cap.
_FAILURE_KINDS = frozenset(
    {"spawn_timeout", "spawn_exec_error", "spawn_nonzero_exit", "pipeline_error"}
)


def _request_path(repo_data: Path, session_id: str | None) -> Path:
    return Path(repo_data) / f".judge_request.{_safe_session_marker(session_id)}.json"


def _inflight_path(repo_data: Path, session_id: str | None) -> Path:
    return Path(repo_data) / f".judge_inflight.{_safe_session_marker(session_id)}.json"


def _pending_path(repo_data: Path, session_id: str | None) -> Path:
    return Path(repo_data) / f".judge_pending.{_safe_session_marker(session_id)}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def launch_async_judge(
    *,
    repo_root: Path,
    repo_data: Path,
    repo_id: str,
    session_id: str,
    fresh_abs_paths: list[str],
    digests: dict[str, str],
    turn_key: str | None,
    intent_tokens: list[str] | None,
    route_reason: str | None = None,
) -> bool:
    """Detach a judge child for this turn. False means "fall back to sync".

    The request file and the in-flight marker are both written (atomically)
    BEFORE the child spawns, so the routing gate and the Stop attestation see a
    consistent in-flight state from the first instant the child could run. A
    failed launch removes both files: nothing may be left behind that would
    wedge future routing into in-flight skips.
    """
    if os.name != "posix":
        return False
    req_path = _request_path(repo_data, session_id)
    marker_path = _inflight_path(repo_data, session_id)
    try:
        Path(repo_data).mkdir(parents=True, exist_ok=True, mode=0o700)
        started_ts = time.time()
        _atomic_write_json(
            req_path,
            {
                "repo_root": str(repo_root),
                "repo_id": repo_id,
                "session_id": session_id,
                "abs_paths": [str(p) for p in fresh_abs_paths],
                "digests": dict(digests or {}),
                "turn_key": turn_key,
                "intent_tokens": list(intent_tokens or []),
                "route_reason": route_reason,
                "started_ts": started_ts,
            },
        )
        marker = {"turn_key": turn_key, "started_ts": started_ts, "pid": None}
        _atomic_write_json(marker_path, marker)
        # The child's own claude -p spawn sets CHAMELEON_DISABLE=1 (see
        # judge._spawn_reviewer_status); the python child itself inherits the
        # caller's environment unchanged.
        proc = subprocess.Popen(
            [sys.executable, "-m", "chameleon_mcp.judge_async", str(req_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        marker["pid"] = proc.pid
        _atomic_write_json(marker_path, marker)
        return True
    except Exception:
        for p in (req_path, marker_path):
            try:
                p.unlink()
            except OSError:
                pass
        return False


def _child_spawn_budget_seconds() -> int:
    """Spawn budget the detached child is running under, read from the parent.

    The orphan-sweep window below is twice this, so a legitimately slow
    fallback child (bare auth failed, generous budget) is not swept mid-run
    and then double-spawned. Falls back to the short sync budget if the judge
    module cannot answer.
    """
    try:
        from chameleon_mcp.judge import detached_spawn_budget_seconds

        return detached_spawn_budget_seconds()
    except Exception:
        return threshold_int("CORRECTNESS_JUDGE_TIMEOUT_SECONDS")


def _run_verify_stage(repo_root: Path, findings, judge_started: float):
    """VERIFY the async judge's findings with the refuter under the child budget.

    Computes the budget left after the judge spawn, clamps the refuter's per-spawn
    timeout to fit, and refutes as many findings as the remainder affords. Fails open
    to a pass-through result (every finding kept, unverified) on any error so a broken
    verify never drops a real finding or crashes the child.
    """
    from chameleon_mcp import stop_verify

    n = len(findings or [])
    try:
        from chameleon_mcp.judge import _valid_model

        elapsed = max(0.0, time.time() - judge_started)
        # Leave a safety margin under the child budget so the refuter batch and its
        # cleanup finish before the parent's orphan-sweep window (2x the budget).
        remaining = _child_spawn_budget_seconds() - elapsed - 10
        model = os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
        if not _valid_model(model):
            model = "sonnet"
        # Clamp the per-spawn timeout so at least one full window fits the remainder.
        timeout = max(15, min(threshold_int("REFUTER_TIMEOUT_SECONDS"), int(remaining)))
        return stop_verify.verify_stop_findings(
            repo_root,
            findings,
            budget_seconds=remaining,
            model=model,
            max_spawns=threshold_int("REFUTER_MAX_SPAWNS_PER_INVOCATION"),
            timeout=timeout,
        )
    except Exception:
        return stop_verify.VerifyResult(
            list(findings or []), ["unverified"] * n, 0, 0, n, False, "verify stage error"
        )


def is_inflight_fresh(repo_data: Path, session_id: str) -> bool:
    """True while a detached judge for this session is plausibly still running.

    A marker older than twice the child's spawn budget is an orphan (the child
    was killed before its finally-block cleanup) and is unlinked on read, as
    is a corrupt marker, so one dead child can never suppress reviews for the
    rest of the session.
    """
    path = _inflight_path(repo_data, session_id)
    try:
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        started = float(data.get("started_ts"))
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    if time.time() - started < 2 * _child_spawn_budget_seconds():
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def main(argv: list[str] | None = None) -> int:
    """Detached-child entry point: consume a request file, run the judge.

    Loads and deletes the request, runs the judge pipeline with degradations
    recorded to the session's check-event sidecar, writes the pending-findings
    file for next-turn delivery, marks the reviewed files judged at their
    captured digests, and clears the in-flight marker in a finally block so
    even a failing run cannot leave the session looking permanently in-flight.
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 1
    req_path = Path(args[0])
    try:
        raw = json.loads(req_path.read_text(encoding="utf-8"))
    except Exception:
        return 1
    try:
        req_path.unlink()
    except OSError:
        pass
    if not isinstance(raw, dict):
        return 1

    repo_data = req_path.parent
    repo_root = Path(str(raw.get("repo_root") or "."))
    repo_id = str(raw.get("repo_id") or "")
    session_id = str(raw.get("session_id") or "")
    turn_key = raw.get("turn_key")
    digests = raw.get("digests") if isinstance(raw.get("digests"), dict) else {}
    abs_paths = [str(p) for p in raw.get("abs_paths") or []]
    intent_tokens = [str(t) for t in raw.get("intent_tokens") or []]
    # The reviewer model ladder route reason, carried from the launching gate so
    # the detached child escalates the same way the sync path would. The child
    # inherits the caller's env, so it resolves the model itself. The detached
    # budget is generous (fallback timeout), so this is the IDEAL place to run
    # the escalated model without the sync path's timeout risk.
    route_reason = raw.get("route_reason")
    route_reason = route_reason if isinstance(route_reason, str) else None

    def _event(
        status: str,
        reason: str | None = None,
        detail: dict | None = None,
        check: str = "correctness_judge",
    ) -> None:
        try:
            from chameleon_mcp.exec_log import append_check_event

            append_check_event(
                repo_id,
                session_id=session_id,
                check=check,
                status=status,
                reason=reason,
                detail=detail,
            )
        except Exception:
            pass

    def _resolver(abs_path: str):
        try:
            from chameleon_mcp.tools import get_archetype

            return (get_archetype(str(repo_root), abs_path).get("data") or {}).get("archetype")
        except Exception:
            return None

    try:
        from chameleon_mcp import judge

        # This process is the detached child: its reviewer spawn may take the
        # generous fallback budget when bare auth is known failed.
        judge.mark_detached_run()

        failures: list[str] = []

        def _sink(kind: str, detail: str | None = None) -> None:
            # A grounding-context outcome is its own check event, not a spawn
            # degradation -- mirroring the sync gate's translation. All THREE
            # grounding families (facts / imported defs / transitive chains) must
            # route here; handling only judge_facts_ misfiled judge_defs_ and
            # judge_transitive_ as degraded_spawn, so a healthy detached reviewer
            # on a repo without those indexes wrote phantom degradation rows into
            # the attestation and lost the "grounded vs blind" defs/transitive
            # rows the raise-only replay depends on.
            fam = judge.grounding_family(kind)
            if fam is not None:
                _event(
                    kind[len(fam) :],
                    detail={"turn_key": turn_key},
                    check=fam.rstrip("_"),
                )
                return
            if kind in _FAILURE_KINDS:
                failures.append(kind)
            _event("degraded_spawn", kind, {"turn_key": turn_key, "detail": detail})

        _judge_started = time.time()
        findings = judge.run_correctness_judge(
            repo_root,
            repo_root / ".chameleon",
            abs_paths,
            _resolver,
            intent_tokens=intent_tokens,
            event_sink=_sink,
            model=judge.judge_model_for_route(route_reason),
        )

        # VERIFY stage: independently refute each finding before delivery. The
        # detached child runs under the generous fallback budget, so unlike the
        # 55s-capped sync gate it can afford the full refuter batch. A refuted
        # finding is dropped; everything else is delivered labeled. Fails open to
        # the raw findings on any error (verify_stop_findings' own contract).
        verify = _run_verify_stage(repo_root, findings, _judge_started)
        findings = verify.kept
        if verify.ran:
            _event(
                "verified",
                "completed",
                {
                    "turn_key": turn_key,
                    "refuted": verify.refuted,
                    "confirmed": verify.confirmed,
                    "unverified": verify.unverified,
                },
            )

        if not failures:
            _atomic_write_json(
                _pending_path(repo_data, session_id),
                {
                    "turn_key": turn_key,
                    "completed_ts": time.time(),
                    "digests": digests,
                    "verify": {
                        "ran": verify.ran,
                        "refuted": verify.refuted,
                        "confirmed": verify.confirmed,
                        "unverified": verify.unverified,
                    },
                    "findings": [
                        {
                            "file": f.file,
                            "line": f.line,
                            "message": f.message,
                            "confidence": f.confidence,
                            "verify": v,
                            # G1' pinned-evidence layer: carried so next-turn
                            # delivery can flag excerpt staleness and surface a
                            # fix / pinned check output. All optional.
                            "excerpt_sha": getattr(f, "excerpt_sha", None),
                            "suggested_fix": getattr(f, "suggested_fix", None),
                            "evidence_cmds": getattr(f, "evidence_cmds", None),
                        }
                        for f, v in zip(findings, verify.kept_verdicts, strict=False)
                    ],
                },
            )
            _event("spawned", "completed", {"turn_key": turn_key, "findings": len(findings)})
            from chameleon_mcp import duplication_review as dr

            for p in abs_paths:
                rel = dr._repo_rel(repo_root, p)
                dr.mark_judged(
                    repo_data, session_id, rel, digests.get(rel, ""), prefix=".corr_judged."
                )
        return 0
    except Exception as exc:
        _event(
            "degraded_spawn", "pipeline_error", {"turn_key": turn_key, "detail": repr(exc)[:200]}
        )
        return 1
    finally:
        try:
            _inflight_path(repo_data, session_id).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
