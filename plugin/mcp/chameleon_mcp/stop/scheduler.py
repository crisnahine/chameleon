"""The only code allowed to spawn a model: route decision, single-inflight
job slot, and the detached job launch.

Three pieces, each with a narrow contract:

``route(ctx, state, cfg) -> RouteDecision`` is pure decision logic -- digest
freshness (reusing the pre-existing ``.corr_judged.`` marker namespace so a
mid-migration repo's judged history stays congruent), the per-session spawn
cap, captured-intent forcing, and cheap risk facts (security surface,
unarchetyped files, importer blast radius). It never spawns, never claims the
job slot, and never marks a file judged; its only side effect is a
best-effort, fail-open check-event write for its own skip decisions (evidence
for the session attestation, never control flow -- see the STATUSES note on
``route`` itself). SubagentStop never routes: the scheduler refuses outright
(``RouteDecision(spawn=False, reason="subagent_stop")``) rather than reaching
any of the digest/cap/risk logic, because a multi-subagent turn would
otherwise multiply job launches; the parent turn's own Stop re-reviews the
subagent's edits.

``try_acquire_job_slot(repo_id, session_id) -> Path | None`` claims the
single-inflight-per-(session, repo) job slot under the session doc's flock,
atomically with spending the session's spawn budget
(``SessionDoc.review_spawns``). A live (fresh-heartbeat) job already inflight
loses the race; a job whose heartbeat has gone stale (no write for
``JOB_HEARTBEAT_STALE_SECONDS``) is reclaimed rather than left wedging every
later Stop's routing.

``launch_job(request: JobRequest) -> bool`` detaches the job runner
(``python -m chameleon_mcp.stop.job <request-file>``) so it outlives the Stop
hook process: POSIX via ``start_new_session=True`` (setsid, so a
process-group kill at hook exit cannot reap it), Windows via
``creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``. There is no
synchronous fallback on either platform -- a platform or spawn failure
returns False and rolls back the slot claim (releases the heartbeat and
refunds the spent budget) so a mere detach failure never wedges routing for
the rest of the heartbeat-staleness window; the caller is responsible for the
resulting check event (this function itself emits none). The child's env
inherits the caller's real ``CLAUDE_CONFIG_DIR`` unchanged -- an empty
throwaway config dir strips OAuth/subscription auth and makes the reviewer
silently never fire on a non-API-key install (BUG-J1) -- plus
``CHAMELEON_DISABLE=1`` so the spawned reviewer's own hooks never recurse
into chameleon.

Extracted-and-redesigned, not a verbatim port: absorbs the digest-freshness /
session-cap / intent-forced / risk-tier CONDITIONS of the pre-phase-3
``hook_helper._correctness_judge_route``, but not its route dict or the five
booleans that used to coordinate the correctness/multi-lens/duplication gates
against each other (``corr_spawning``, ``spawn_failed``, ``spawn_timed_out``,
``multilens_owns_dup``, ``allow_spawn``) -- those existed to arbitrate three
separate gates sharing one spawn budget; this scheduler IS the one spawn
path, so there is nothing left to arbitrate. Mirrors ``stop/pipeline.py`` and
``stop/gates.py``'s own pattern: top-level imports stay stdlib-only, every
non-stdlib symbol is resolved via a deferred import inside the function that
needs it, so a test that patches ``chameleon_mcp.hook_helper.<name>`` (or
this module's own attributes) stays effective for a call made from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Check-event vocabulary this module records under the "review_job" check
# name, read by ``stop/pipeline.py``'s ``_run_review_job`` and pinned by
# tests: routed_skip_low_risk, skipped_session_cap, skipped_digest_dup are
# emitted by route() below; spawned / any launch-failure status are the
# CALLER's responsibility (see launch_job's docstring) since only the caller
# has the route reason/lens-set detail worth recording. The session
# attestation and the SessionStart health banner still read the pre-cutover
# "correctness_judge" vocabulary (``_build_session_attestation`` /
# ``_judge_spawn_health_banner`` in hook_helper.py) -- retargeting them to
# "review_job" is a follow-up, not yet done.
_CHECK_NAME = "review_job"


@dataclass(frozen=True)
class RouteDecision:
    """The scheduler's spawn decision for one Stop invocation.

    ``files`` is the fresh (not-yet-judged) subset of the turn's edited
    files that a spawned job should review -- empty when ``spawn`` is False.
    ``lens_names`` and ``model`` are likewise only meaningful when spawning.
    """

    spawn: bool
    reason: str
    lens_names: tuple[str, ...] = ()
    model: str | None = None
    intent_tokens: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteContext:
    """Read-only routing-relevant facts for one Stop invocation.

    Deliberately its own small type rather than ``stop.pipeline.RootContext``:
    ``route()`` stays importable and unit-testable without pulling in the
    rest of the pipeline package (``stop/pipeline.py``'s ``_run_review_job``
    builds one from a ``RootContext`` on every Stop). ``files`` is the
    turn's edited absolute paths (pre-freshness-filter) -- the caller's
    equivalent of the pre-phase-3 ``EnforcementState.files`` keys.
    """

    repo_root: Path
    repo_id: str
    session_id: str | None
    repo_data: Path
    is_subagent: bool
    files: tuple[str, ...] = ()
    daemon_state: dict | None = None


@dataclass(frozen=True)
class JobRequest:
    """Everything the detached job runner (``stop/job.py``, Task 4) needs.

    Round-trips through JSON via ``to_dict``/``from_dict`` -- ``launch_job``
    writes this exact shape to the request file the child process is handed
    as its sole argv. Schema (all keys always present):

    ```json
    {
        "repo_root": "<str, absolute path>",
        "repo_id": "<str>",
        "session_id": "<str>",
        "files": ["<str, absolute path>", ...],
        "intent_tokens": ["<str>", ...],
        "lens_names": ["<str>", ...],
        "model": "<str>",
        "heartbeat_path": "<str, absolute path>"
    }
    ```
    """

    repo_root: Path
    repo_id: str
    session_id: str
    files: tuple[str, ...]
    intent_tokens: tuple[str, ...]
    lens_names: tuple[str, ...]
    model: str
    heartbeat_path: Path

    def to_dict(self) -> dict:
        return {
            "repo_root": str(self.repo_root),
            "repo_id": self.repo_id,
            "session_id": self.session_id,
            "files": list(self.files),
            "intent_tokens": list(self.intent_tokens),
            "lens_names": list(self.lens_names),
            "model": self.model,
            "heartbeat_path": str(self.heartbeat_path),
        }

    @classmethod
    def from_dict(cls, data: dict) -> JobRequest:
        return cls(
            repo_root=Path(str(data["repo_root"])),
            repo_id=str(data["repo_id"]),
            session_id=str(data["session_id"]),
            files=tuple(str(p) for p in data.get("files") or []),
            intent_tokens=tuple(str(t) for t in data.get("intent_tokens") or []),
            lens_names=tuple(str(n) for n in data.get("lens_names") or []),
            model=str(data.get("model") or ""),
            heartbeat_path=Path(str(data["heartbeat_path"])),
        )


def _emit(ctx: RouteContext, status: str, *, detail: dict | None = None) -> None:
    try:
        from chameleon_mcp import hook_helper as hh

        hh._emit_check_event(ctx.repo_id, ctx.session_id, _CHECK_NAME, status, detail=detail)
    except Exception:
        pass


def route(ctx: RouteContext, state, cfg) -> RouteDecision:
    """Decide whether this Stop schedules a detached review job.

    ``state`` is the session's ``core.session_state.SessionDoc`` (only
    ``review_spawns`` is read here -- claiming the job slot and spending the
    budget is ``try_acquire_job_slot``'s job, not this function's). ``cfg``
    is the repo's ``profile.config.EnforcementConfig``.
    """
    if ctx.is_subagent:
        return RouteDecision(spawn=False, reason="subagent_stop")
    try:
        if cfg.mode == "off":
            return RouteDecision(spawn=False, reason="mode_off")

        lens_names = tuple(
            name
            for name, enabled in (
                ("correctness", getattr(cfg, "correctness_judge", True)),
                ("duplication", getattr(cfg, "duplication_review", True)),
                ("idiom", getattr(cfg, "idiom_review", True)),
            )
            if enabled
        )
        if not lens_names:
            return RouteDecision(spawn=False, reason="feature_disabled")

        return _route_inner(ctx, state, lens_names)
    except Exception:
        return RouteDecision(spawn=False, reason="route_error")


def _route_inner(ctx: RouteContext, state, lens_names: tuple[str, ...]) -> RouteDecision:
    from chameleon_mcp.violation_class import ignored_rules

    # An edited file that still exists and was not opted out via an inline
    # bare `chameleon-ignore` directive.
    edited: list[str] = []
    for path in ctx.files:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        if "" in (ignored_rules(content, file_path=path) or set()):
            continue
        edited.append(path)
    if not edited:
        return RouteDecision(spawn=False, reason="no_edits")

    from chameleon_mcp import duplication_review as dr
    from chameleon_mcp import hook_helper as hh

    # Freshness: digest over the same first-1MB byte window the duplication
    # gate keys its own markers on, read through the SAME ".corr_judged."
    # namespace the pre-phase-3 correctness gate used, so a repo mid-migration
    # does not lose its judged history. This function only READS the
    # namespace -- marking a file judged happens after a job actually
    # completes (stop/job.py, Task 4), never here.
    fresh: list[str] = []
    fresh_rels: list[str] = []
    for path in edited:
        try:
            raw = Path(path).read_bytes()[:1_000_000]
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()[:16]
        rel = hh._repo_rel(ctx.repo_root, path) or Path(path).name
        if not dr.already_judged(
            ctx.repo_data, ctx.session_id or "", rel, digest, prefix=hh._CORR_JUDGED_PREFIX
        ):
            fresh.append(path)
            fresh_rels.append(rel)
    if not fresh:
        _emit(ctx, "skipped_digest_dup")
        return RouteDecision(spawn=False, reason="digest_dup")

    from chameleon_mcp._thresholds import threshold_int

    if state.review_spawns >= threshold_int("CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION"):
        _emit(ctx, "skipped_session_cap")
        return RouteDecision(spawn=False, reason="session_cap")

    # Intent trigger: checkable tokens or a security-lens hit captured since
    # the last spawn force the review regardless of risk tier.
    intent_tokens: tuple[str, ...] = ()
    security_intent = False
    try:
        from chameleon_mcp import intent_capture
        from chameleon_mcp.exec_log import read_check_events

        entries = intent_capture.read_intent(ctx.repo_data, ctx.session_id)
        since_ts: float | None = None
        try:
            ev = read_check_events(
                ctx.repo_id,
                ctx.session_id or "",
                limit=threshold_int("ATTESTATION_MAX_CHECK_EVENTS"),
            )
            spawn_ts = [
                e.get("ts")
                for e in ev.get("events") or []
                if e.get("check") == _CHECK_NAME
                and e.get("status") == "spawned"
                and isinstance(e.get("ts"), (int, float))
            ]
            since_ts = max(spawn_ts) if spawn_ts else None
        except Exception:
            since_ts = None
        intent_tokens = tuple(intent_capture.checkable_tokens(entries, since_ts))
        security_intent = intent_capture.security_intent_seen(entries, since_ts)
    except Exception:
        intent_tokens = ()
        security_intent = False

    from chameleon_mcp.judge import judge_model_for_route

    if intent_tokens or security_intent:
        return RouteDecision(
            spawn=True,
            reason="intent_forced",
            lens_names=lens_names,
            model=judge_model_for_route("intent_forced"),
            intent_tokens=intent_tokens,
            files=tuple(fresh),
        )

    # Risk facts over the fresh set, every leg fail-open toward spawning.
    try:
        from chameleon_mcp import autopass

        security = bool(autopass.security_surface_categories(fresh_rels))
    except Exception:
        security = True

    resolver = hh._archetype_resolver(ctx.repo_root, ctx.daemon_state or {"available": True})
    unarchetyped = 0
    for path in fresh:
        try:
            if resolver(path) is None:
                unarchetyped += 1
        except Exception:
            unarchetyped += 1

    # Blast radius from the reverse index. UNKNOWN escalates: a missing index
    # or a failed read must route toward review, never read as zero.
    blast = 0
    blast_unknown = False
    try:
        from chameleon_mcp.tools import query_symbol_importers

        for path in fresh:
            envelope = query_symbol_importers(str(ctx.repo_root), path)
            data = (envelope.get("data") or {}) if isinstance(envelope, dict) else {}
            if not data.get("found"):
                blast_unknown = True
                break
            for imp in data.get("importers") or []:
                try:
                    blast += int(imp.get("count") or 0)
                except (TypeError, ValueError):
                    blast_unknown = True
    except Exception:
        blast_unknown = True

    if security or blast_unknown or blast > threshold_int("AUTOPASS_MAX_BLAST_RADIUS"):
        reason = "risk_high"
    elif unarchetyped > 0 or len(fresh) > threshold_int("AUTOPASS_MAX_FILES"):
        reason = "risk_elevated"
    elif state.review_spawns == 0:
        # Low risk: the first routed turn of a session still spawns,
        # preserving at-least-once coverage; later low-risk turns skip.
        reason = "first_low_risk"
    else:
        _emit(ctx, "routed_skip_low_risk")
        return RouteDecision(spawn=False, reason="routed_skip_low_risk")

    return RouteDecision(
        spawn=True,
        reason=reason,
        lens_names=lens_names,
        model=judge_model_for_route(reason),
        intent_tokens=intent_tokens,
        files=tuple(fresh),
    )


def _heartbeat_path(repo_id: str, session_id: str) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker
    from chameleon_mcp.profile.trust import repo_data_dir

    marker = _safe_session_marker(session_id)
    return repo_data_dir(repo_id) / f".job_heartbeat.{marker}"


def try_acquire_job_slot(repo_id: str, session_id: str) -> Path | None:
    """Claim the single-inflight-per-(session, repo) job slot, or None.

    Atomically (under the session doc's flock): if no job is recorded
    inflight, or the recorded one's heartbeat file is missing or has not
    been touched in ``JOB_HEARTBEAT_STALE_SECONDS``, claims the slot --
    records the heartbeat path + claim time on the doc and spends one unit
    of ``review_spawns`` -- and returns the heartbeat path (freshly touched,
    ready for the job runner to keep alive). Otherwise returns None: a live
    job already owns this session's one slot.

    The heartbeat file's mtime, not ``job_started_at``, is the staleness
    clock (spec: "keys on heartbeat staleness ... NOT a multiple of total
    budget -- a dead job never suppresses review for minutes"). The file name
    is stable per (repo_id, session_id), so reclaiming a stale slot re-touches
    the SAME path rather than minting a new one.

    Ordering invariant: the heartbeat file is created INSIDE the mutate
    callback (under the doc's flock), BEFORE the claim fields are set, so a
    committed ``job_inflight`` always implies the heartbeat exists. Touching
    it after ``update_session_doc`` returned left a window where a concurrent
    acquirer took the flock, saw the claim with no heartbeat file, read it as
    a dead job, and double-claimed -- one double billable spawn per hit
    (reproduced at 16-thread contention). A failed touch raises out of the
    callback, aborting the load-mutate-save cycle before anything commits.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.core.session_state import update_session_doc

    heartbeat_path = _heartbeat_path(repo_id, session_id)
    stale_after = threshold_int("JOB_HEARTBEAT_STALE_SECONDS")
    now = time.time()
    acquired = False

    def _mutate(doc) -> None:
        nonlocal acquired
        live = False
        if doc.job_inflight:
            try:
                age = now - Path(doc.job_inflight).stat().st_mtime
                live = age < stale_after
            except OSError:
                live = False  # heartbeat file gone: the job is dead
        if live:
            acquired = False
            return
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        heartbeat_path.touch(exist_ok=True)  # updates mtime when reclaiming a stale slot
        try:
            os.chmod(heartbeat_path, 0o600)
        except OSError:
            pass
        doc.job_inflight = str(heartbeat_path)
        doc.job_started_at = now
        doc.review_spawns += 1
        acquired = True

    try:
        update_session_doc(repo_id, session_id, _mutate)
    except Exception:
        # A heartbeat-touch failure or a lock timeout aborted the cycle before
        # the claim committed, so there is nothing to roll back. Deliberately
        # no heartbeat unlink here: on a lock timeout the existing file may be
        # a LIVE job's heartbeat, and unlinking it would get that job's slot
        # reclaimed out from under it.
        return None
    if not acquired:
        return None
    return heartbeat_path


def _release_job_slot(repo_id: str, session_id: str) -> None:
    """Roll back a claim: clear the inflight marker and refund the spend.

    Used only when a claimed slot never became a real job (the heartbeat file
    could not be created, or the detached launch itself failed) -- a mere
    filesystem or platform hiccup must not cost the session a real review nor
    wedge routing for the rest of the staleness window.
    """
    from chameleon_mcp.core.session_state import update_session_doc

    def _mutate(doc) -> None:
        doc.job_inflight = ""
        doc.job_started_at = 0.0
        doc.review_spawns = max(0, doc.review_spawns - 1)

    try:
        update_session_doc(repo_id, session_id, _mutate)
    except Exception:
        pass


def clear_job_slot(repo_id: str, session_id: str) -> None:
    """Clear a COMPLETED job's inflight marker, WITHOUT refunding its spend.

    Called once by the job runner (``stop/job.py``, Task 4) as it exits --
    successfully or not -- so the single-inflight slot frees for a later
    Stop to claim a new job. This is deliberately NOT ``_release_job_slot``:
    that function is the failed-*launch* rollback (the job never actually
    ran, so refunding its spend is correct -- a mere detach/filesystem
    hiccup must not cost the session a real review). A job that ran DID
    consume its one per-session spawn unit regardless of what it found or
    whether every stage inside it degraded, so ``review_spawns`` stays
    charged here; reusing ``_release_job_slot`` for this path would refund
    that spend on every completion and let a session spawn unbounded jobs,
    defeating ``CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION`` entirely.
    """
    from chameleon_mcp.core.session_state import update_session_doc

    def _mutate(doc) -> None:
        doc.job_inflight = ""
        doc.job_started_at = 0.0

    try:
        update_session_doc(repo_id, session_id, _mutate)
    except Exception:
        pass


def _job_env() -> dict[str, str]:
    """The detached job child's environment.

    Inherits the caller's real environment UNCHANGED (including
    ``CLAUDE_CONFIG_DIR`` if the user's shell set one) so the job's own
    reviewer spawns stay authenticated -- an empty throwaway config dir was
    the pre-phase-3 bug (BUG-J1): it strips OAuth/subscription credentials
    and the reviewer silently never fires on a non-API-key install. The one
    addition is ``CHAMELEON_DISABLE=1``, so the job's own `claude -p`
    reviewer spawns never recurse into chameleon's own hooks.

    Forward contract for the job runner (``stop/job.py``): the flag exists
    for the REVIEWER CHILDREN the job spawns, and the job process itself
    inherits it -- so job.py must never consult the plugin's own optout
    hierarchy (``is_chameleon_suppressed`` / the ``CHAMELEON_DISABLE`` env
    check) as a run/skip gate, or every job would read its own environment
    as "chameleon disabled" and silently self-disable.
    """
    env = dict(os.environ)
    env["CHAMELEON_DISABLE"] = "1"
    return env


def _write_request_file(request: JobRequest) -> Path | None:
    from chameleon_mcp.optouts import _safe_session_marker
    from chameleon_mcp.profile.trust import repo_data_dir

    try:
        repo_data = repo_data_dir(request.repo_id)
        marker = _safe_session_marker(request.session_id)
        path = repo_data / f".job_request.{marker}.json"
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(request.to_dict(), separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def _detach_kwargs(os_name: str) -> dict | None:
    """Platform-specific ``subprocess.Popen`` kwargs that detach the child, or
    None when ``os_name`` is neither POSIX nor Windows (no platform falls back
    to a synchronous spawn -- see ``launch_job``).

    A pure function of ``os_name`` (never reads ``os.name`` itself) so it is
    unit-testable for the Windows branch without mutating the real ``os``
    module's ``name`` attribute -- doing that process-wide would also flip
    which concrete ``Path`` subclass every later ``pathlib.Path(...)`` call
    constructs, breaking any filesystem access for the rest of the test.

    - POSIX: ``start_new_session=True`` (setsid), so a process-group kill at
      hook exit cannot reap the child.
    - Windows: ``creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``.
      Both constants are read via ``getattr(subprocess, name, 0)`` because
      neither exists in the ``subprocess`` module on a POSIX host, even when
      a test passes ``"nt"`` to exercise this branch off-Windows.
    """
    if os_name == "posix":
        return {"start_new_session": True}
    if os_name == "nt":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return None


def launch_job(request: JobRequest) -> bool:
    """Detach the job runner for ``request``. False means the caller must
    treat review as skipped this turn -- there is no synchronous fallback.

    Validates ``request.model`` (falls back to the judge's own base-model
    default on anything unrecognized, never spawning a garbage ``--model``),
    writes the request file, then detaches per ``_detach_kwargs(os.name)``.
    Any other platform, or any exception from the write or the spawn, is a
    hard failure: the request file and the claimed job slot are both cleaned
    up (mirrors the pre-phase-3 async-judge contract: "nothing may be left
    behind that would wedge future routing"), and this function returns
    False. It never emits a check event itself -- only the caller has the
    turn context (route reason, turn key) worth recording.
    """
    from chameleon_mcp.judge import _valid_model

    model = request.model if _valid_model(request.model) else "sonnet"
    if model != request.model:
        from dataclasses import replace as _replace

        request = _replace(request, model=model)

    request_path = _write_request_file(request)
    if request_path is None:
        _cleanup_failed_launch(None, request)
        return False

    detach_kwargs = _detach_kwargs(os.name)
    if detach_kwargs is None:
        _cleanup_failed_launch(request_path, request)
        return False

    argv = [sys.executable, "-m", "chameleon_mcp.stop.job", str(request_path)]
    spawn_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": _job_env(),
        **detach_kwargs,
    }
    try:
        subprocess.Popen(argv, **spawn_kwargs)
    except OSError:
        _cleanup_failed_launch(request_path, request)
        return False
    return True


def _cleanup_failed_launch(request_path: Path | None, request: JobRequest) -> None:
    """Undo everything a failed launch left behind: request file (when it got
    as far as being written), heartbeat file, and the session doc's slot claim
    -- so a failed launch is indistinguishable from never having tried."""
    if request_path is not None:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        Path(request.heartbeat_path).unlink(missing_ok=True)
    except OSError:
        pass
    _release_job_slot(request.repo_id, request.session_id)
