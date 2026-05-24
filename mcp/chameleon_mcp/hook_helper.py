"""CLI helper for Claude Code hooks.

Hooks invoke this via:
    python -m chameleon_mcp.hook_helper <command>

Where <command> is one of: session-start | preflight-and-advise |
posttool-recorder | posttool-verify | callout-detector.

Reads JSON from stdin, calls the appropriate MCP tool, emits a Claude Code
hook output JSON to stdout.

Phase 4: implements session-start (loads using-chameleon + profile primer)
and preflight-and-advise (calls get_pattern_context). posttool-recorder and
callout-detector remain Phase 4-end stubs.

Per docs/architecture.md "Bootstrap mechanism" + "Hook stack".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


def _emit(output: dict) -> None:
    """Write Claude Code hook output JSON to stdout. Single source of truth."""
    sys.stdout.write(json.dumps(output))
    sys.stdout.write("\n")


def _emit_chameleon_context(block: str) -> None:
    """Wrap a ``<chameleon-context>`` block in the PreToolUse hook envelope.

    Rec 3: one helper for every advisory or degradation block the hook
    surfaces, so the envelope shape stays consistent across the five
    historically-divergent emit sites.
    """
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": block,
            }
        }
    )


def _emit_posttool_context(block: str) -> None:
    """Wrap a ``<chameleon-context>`` block in the PostToolUse hook envelope."""
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": block,
            }
        }
    )


def _degraded_banner(reason: str, detail: str | None = None) -> str:
    """Tiny advisory block naming a degradation cause.

    Rec 3 + 4.1: when chameleon goes silent for a system-degradation
    reason (not a user opt-out), surface a one-line banner so the model
    can mention to the human partner that the advisory was unavailable.
    The model should not assume "no banner == healthy".

    ``reason`` is a short slug rendered inside the bracketed header;
    optional ``detail`` adds a single line of prose so the user knows
    what to investigate.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    safe_reason = sanitize_for_chameleon_context(reason)
    parts = [
        "<chameleon-context>",
        f"[chameleon: degraded — {safe_reason}]",
    ]
    if detail:
        parts.append("")
        parts.append(sanitize_for_chameleon_context(detail))
    parts.append("</chameleon-context>")
    return "\n".join(parts)


def _plugin_data_dir() -> Path:
    """Return the per-user chameleon plugin data dir (override-aware).

    Mirrors chameleon_mcp.plugin_paths.plugin_data_dir but keeps the import
    cost off the hook hot path.
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "chameleon"


_TRUST_PROMPT_FILENAME = ".trust_prompted.{session}"
_TRUST_MARKER_TTL_SECONDS = 24 * 3600  # re-prompt after 24h even on resumed session


def _marker_is_fresh(marker_path: Path) -> bool:
    """True if marker exists and was touched within TTL."""
    try:
        st = marker_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    age = time.time() - st.st_mtime
    return age < _TRUST_MARKER_TTL_SECONDS


def _should_emit_untrusted_prompt(repo_id: str, session_id: str | None) -> bool:
    """BUG-024: emit the untrusted trust prompt at most once per session.

    Writes a marker file at ``${PLUGIN_DATA}/<repo_id>/.trust_prompted.<session>``
    the first time and returns False on every subsequent invocation in
    the same session. Sessions are per-Claude-Code-conversation so the
    user gets a fresh prompt in a new conversation.

    Best-effort: any filesystem error short-circuits to "yes, prompt"
    (the prompt is harmless if duplicated; suppressing it on error would
    hide the trust gate completely).
    """
    if not repo_id or not session_id:
        return True
    try:
        from chameleon_mcp.optouts import _safe_session_marker
        marker_dir = _plugin_data_dir() / repo_id
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / _TRUST_PROMPT_FILENAME.format(session=_safe_session_marker(session_id))
        if _marker_is_fresh(marker):
            return False
        marker.touch(exist_ok=True)
        return True
    except (OSError, PermissionError):
        return True


def _emit_session_context(content: str) -> None:
    """Emit SessionStart context per platform's expected JSON shape.

    Cursor: `{ "additional_context": ... }`
    Claude Code: `{ "hookSpecificOutput": { "hookEventName": "SessionStart", "additionalContext": ... } }`
    SDK / Copilot CLI: `{ "additionalContext": ... }`

    Single-format-per-platform: never emit both formats — Claude Code reads
    both `additional_context` and `hookSpecificOutput` without dedup.
    """
    if os.environ.get("CURSOR_PLUGIN_ROOT"):
        _emit({"additional_context": content})
    elif os.environ.get("CLAUDE_PLUGIN_ROOT") and not os.environ.get("COPILOT_CLI"):
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        })
    else:
        _emit({"additionalContext": content})


_DRIFT_BANNER_FILENAME = ".drift_banner.last"


def _drift_banner_for_repo(repo_root: Path, session_id: str | None = None) -> str | None:
    """Return a one-line drift advisory for SessionStart, or None.

    Rec 4: surfaces "profile drift is high — consider /chameleon-refresh"
    at session start so the user sees the signal without having to opt
    in via /chameleon-status. Three gates must all hold:
      1. observation count >= CHAMELEON_DRIFT_BANNER_MIN_OBSERVATIONS
      2. score >= CHAMELEON_DRIFT_BANNER_THRESHOLD
      3. per-repo cooldown marker older than CHAMELEON_DRIFT_BANNER_TTL_SECONDS

    Honors the optouts hierarchy (CHAMELEON_DISABLE, .chameleon/.skip,
    .session_disabled.<sid>, .pause_until) — never fires when chameleon
    has been explicitly silenced.

    Walks up from ``repo_root`` via ``find_repo_root`` so a subdir-launched
    session resolves to the same repo_id the drift.db is keyed under
    (matters for non-git repos where ``_compute_repo_id`` falls back to a
    path hash).

    Marker lives under ``plugin_data_dir`` (per-user state), NOT in-repo,
    so a shared filesystem or git checkout doesn't race the cooldown.
    """
    try:
        from chameleon_mcp._thresholds import threshold_float, threshold_int
        from chameleon_mcp.drift.observations import compute_drift_stats
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        # Walk up to the actual repo root; for non-git repos this is what
        # bootstrap keyed drift.db under.
        resolved_root = find_repo_root(repo_root) or repo_root
        repo_id = _compute_repo_id(resolved_root)

        # Honor opt-outs BEFORE touching the cooldown marker so a user
        # who later re-enables sees the banner on the next eligible
        # session instead of having the cooldown silently consumed
        # during the disabled period.
        if is_chameleon_suppressed(resolved_root, repo_id, session_id) is not None:
            return None

        stats = compute_drift_stats(repo_id)
        if stats is None:
            return None
        if stats["count"] < threshold_int("DRIFT_BANNER_MIN_OBSERVATIONS"):
            return None
        if stats["score"] < threshold_float("DRIFT_BANNER_THRESHOLD"):
            return None

        marker = _plugin_data_dir() / repo_id / _DRIFT_BANNER_FILENAME
        ttl = threshold_int("DRIFT_BANNER_TTL_SECONDS")
        if _marker_path_is_fresh(marker, ttl):
            return None

        # Touch the marker so the banner stays quiet for the cooldown
        # window. mkdir(mode=0o700) is silently ignored when the dir
        # already exists (e.g. the trust-prompt path created it earlier),
        # so chmod explicitly afterwards.
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        marker.write_text(str(int(time.time())), encoding="utf-8")
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass

        score_str = f"{stats['score']:.2f}"
        return (
            "[chameleon: drift]\n"
            f"Observed drift score is {score_str} over the last 14 days "
            f"(N={stats['count']} edits). The profile may not match how "
            "the team actually writes code today. Suggest "
            "**/chameleon-refresh** when you have a moment."
        )
    except Exception as exc:
        # Fail-open: drift telemetry is observability, never blocks
        # the SessionStart hook. But surface to stderr so the bash
        # wrapper's .hook_errors.log captures real bugs in the path
        # (silent fail-opens earlier in chameleon's life produced 70+
        # unattributed events on a single workstation, see rec 3).
        try:
            print(
                f"chameleon: drift banner failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001
            pass
        return None


_AUTO_REFRESH_COOLDOWN_FILENAME = ".auto_refresh_cooldown"


def _maybe_auto_refresh(repo_root: Path) -> None:
    """Fire ``refresh_repo`` in background when v0.6.0 auto-refresh fires.

    Gates (ALL must hold):
      - ``.chameleon/config.json`` has ``auto_refresh.enabled == true``
      - drift stats >= ``drift_threshold`` OR profile mtime older than
        ``max_age_hours``
      - per-repo cooldown marker is stale (don't re-fire every session)

    Best-effort: any exception is swallowed silently — auto-refresh is
    convenience, never blocks the session.
    """
    try:
        from chameleon_mcp.drift.observations import compute_drift_stats
        from chameleon_mcp.profile.config import load_config
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        resolved_root = find_repo_root(repo_root) or repo_root
        profile_dir = resolved_root / ".chameleon"
        if not profile_dir.is_dir():
            return
        cfg = load_config(profile_dir)
        if not cfg.auto_refresh.enabled:
            return

        repo_id = _compute_repo_id(resolved_root)

        # Cooldown: don't re-fire within (max_age_hours / 4) hours so a
        # repo with frequent SessionStart hits doesn't auto-refresh
        # back-to-back.
        cooldown_seconds = max(60, (cfg.auto_refresh.max_age_hours * 3600) // 4)
        cooldown_marker = (
            _plugin_data_dir() / repo_id / _AUTO_REFRESH_COOLDOWN_FILENAME
        )
        if _marker_path_is_fresh(cooldown_marker, cooldown_seconds):
            return

        # Trigger condition: drift is high OR profile is old.
        should_fire = False
        try:
            stats = compute_drift_stats(repo_id)
            if stats and stats.get("score", 0.0) >= cfg.auto_refresh.drift_threshold:
                should_fire = True
        except Exception:  # noqa: BLE001
            pass
        if not should_fire:
            profile_json = profile_dir / "profile.json"
            if profile_json.is_file():
                age_hours = (time.time() - profile_json.stat().st_mtime) / 3600
                if age_hours >= cfg.auto_refresh.max_age_hours:
                    should_fire = True
        if not should_fire:
            return

        # v0.6.1: redirect detached refresh stderr to a per-repo log so
        # post-spawn failures (parse exceptions, OOM, schema rejection)
        # are queryable. v0.6.0 used DEVNULL → silent failures + 42h
        # cooldown burn per crash. The bash hook wrapper can't capture
        # detached subprocess stderr because Popen replaces the fd.
        repo_log_dir = _plugin_data_dir() / _compute_repo_id(resolved_root)
        repo_log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(repo_log_dir, 0o700)
        except OSError:
            pass
        log_path = repo_log_dir / "auto_refresh.log"
        # Bound the log to ~64 KB by truncating before each spawn (cheap
        # rotation — append-mode within a single refresh is fine).
        try:
            if log_path.exists() and log_path.stat().st_size > 65536:
                log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        log_fd = None
        try:
            log_fd = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
        except OSError:
            log_fd = None

        # Fire detached: refresh_repo can take seconds; we don't block
        # the SessionStart hook on it.
        import subprocess as _sp

        try:
            _sp.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; from chameleon_mcp.tools import refresh_repo; "
                        f"refresh_repo({str(resolved_root)!r})"
                    ),
                ],
                stdout=log_fd if log_fd is not None else _sp.DEVNULL,
                stderr=log_fd if log_fd is not None else _sp.DEVNULL,
                stdin=_sp.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass

        # v0.6.1: touch cooldown AFTER Popen returns successfully so a
        # transient spawn failure (OSError / ENOMEM) doesn't burn the
        # 42h cooldown window. Inner refresh_repo flock will catch any
        # racing concurrent SessionStart that slipped past this point.
        cooldown_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(cooldown_marker.parent, 0o700)
        except OSError:
            pass
        cooldown_marker.write_text(str(int(time.time())), encoding="utf-8")
        try:
            os.chmod(cooldown_marker, 0o600)
        except OSError:
            pass
    except Exception as exc:
        try:
            print(
                f"chameleon: auto-refresh check failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001
            pass


def _marker_path_is_fresh(marker: Path, ttl_seconds: int) -> bool:
    """Mirror of _marker_is_fresh with a per-call TTL.

    The trust-prompt marker uses a fixed TTL; the drift-banner marker
    uses a longer one. Kept as a separate helper so neither caller
    accidentally shares a constant.
    """
    try:
        st = marker.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return (time.time() - st.st_mtime) < ttl_seconds


def session_start() -> int:
    """SessionStart: inject using-chameleon SKILL.md + profile primer."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        # Without plugin root we can't locate the skill file. Emit empty context.
        _emit({})
        return 0

    skill_path = Path(plugin_root) / "skills" / "using-chameleon" / "SKILL.md"
    if not skill_path.is_file():
        _emit({})
        return 0

    skill_content = skill_path.read_text(encoding="utf-8", errors="replace")

    # Rec 4: append a drift banner when the repo's profile is drifting
    # (high observed_drift_score). Honors opt-outs (CHAMELEON_DISABLE,
    # .skip, /chameleon-disable, /chameleon-pause-15m); requires a min
    # observation count plus a 7-day cooldown so it doesn't fire on
    # every SessionStart in an active repo. Banner sits AFTER the skill
    # body so it's the freshest signal at the end of the inject,
    # immediately before whatever comes next in the model's context
    # (recency dominates retrieval; the always-on skill rules stay at
    # the top where the model expects them).
    session_id: str | None = None
    try:
        # Best-effort read of the hook payload. Claude Code passes
        # SessionStart payload on stdin; session_id is one of the
        # fields. Do not block on a missing stdin (test harnesses).
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
                sid = payload.get("session_id")
                if isinstance(sid, str):
                    session_id = sid
    except Exception:
        session_id = None
    drift_banner = _drift_banner_for_repo(Path.cwd(), session_id=session_id)

    # v0.6.0: opt-in auto-refresh. Reads .chameleon/config.json; if
    # auto_refresh.enabled AND drift is high enough / profile is stale
    # enough, fires refresh_repo in the background so the user doesn't
    # have to manually /chameleon-refresh. The session_start hook
    # returns immediately — the refresh runs detached.
    _maybe_auto_refresh(Path.cwd())

    wrapped_parts = [
        "<chameleon-context>",
        "You have chameleon, a profile-aware coding assistant.",
        "",
        "Below is the full content of your `using-chameleon` skill. Follow it.",
        "",
        skill_content,
    ]
    if drift_banner:
        wrapped_parts.append("")
        wrapped_parts.append(drift_banner)
    wrapped_parts.append("</chameleon-context>")
    wrapped = "\n".join(wrapped_parts)

    _emit_session_context(wrapped)
    return 0


def preflight_and_advise() -> int:
    """PreToolUse Edit/Write/NotebookEdit: inject canonical context.

    Reads tool_input.file_path, calls chameleon_mcp.tools.get_pattern_context,
    emits the result as additionalContext.

    Fast path (Phase 4.5): try the long-lived daemon at
    ``${PLUGIN_DATA}/.daemon.sock``. The daemon holds the python import
    cache + profile state hot between hook calls, so warm latency drops
    from 200-500 ms (subprocess) to sub-100 ms (socket roundtrip).

    Fallback: if the daemon is unreachable / slow / returned an error,
    fall through to the in-process get_pattern_context call. The two
    paths are wire-equivalent; the daemon is purely a latency
    optimization, not a correctness layer.

    On first call from a session, we kick off a background daemon spawn
    so the SECOND hook call sees the daemon ready. The current call
    proceeds via the in-process path either way.
    """
    _started = time.time()

    def _elapsed() -> int:
        return int((time.time() - _started) * 1000)

    def _metric(
        *,
        advisory_emitted: bool,
        repo_id: str | None = None,
        suppression_reason: str | None = None,
        fail_open: bool = False,
        trust_state: str | None = None,
        archetype: str | None = None,
        confidence: str | None = None,
    ) -> None:
        try:
            from chameleon_mcp.metrics import emit_hook_metric
            emit_hook_metric(
                "preflight-and-advise",
                elapsed_ms=_elapsed(),
                repo_id=repo_id,
                advisory_emitted=advisory_emitted,
                suppression_reason=suppression_reason,
                fail_open=fail_open,
                trust_state=trust_state,
                archetype=archetype,
                confidence=confidence,
            )
        except Exception:
            pass

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        _emit({})
        return 0

    # Opt-out check BEFORE any expensive work. If suppressed, emit empty
    # context so the edit proceeds without injection. Mirrors the docs'
    # "hook stack still fires (safety hard-deny preserved) but no
    # <chameleon-context> content is injected" promise.
    repo_id_hint: str | None = None
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root_path = find_repo_root(Path(file_path).expanduser())
        repo_id_hint = _compute_repo_id(repo_root_path) if repo_root_path else None
        session_id = payload.get("session_id")
        suppressed = is_chameleon_suppressed(
            repo_root=repo_root_path,
            repo_id=repo_id_hint,
            session_id=session_id,
        )
        if suppressed is not None:
            _metric(advisory_emitted=False, repo_id=repo_id_hint, suppression_reason=suppressed)
            _emit({})
            return 0
    except Exception:
        # Suppression check should never block — fail open into normal flow
        pass

    # Fast path: try the daemon. On any failure (None response), kick off
    # a background spawn (no-op if it's already running or in flight) and
    # fall through to the in-process call. The hook's 2s ceiling means we
    # cannot afford to wait for a cold daemon — the warm hits show up on
    # the second invocation onwards.
    result: dict | None = None
    try:
        from chameleon_mcp import daemon_client

        result = daemon_client.call("get_pattern_context", {"file_path": file_path})
    except Exception:
        result = None

    if result is None:
        # Kick off the daemon for next time, then run in-process now.
        try:
            from chameleon_mcp.daemon import ensure_daemon_async

            ensure_daemon_async()
        except Exception:
            pass
        try:
            from chameleon_mcp.tools import get_pattern_context
            result = get_pattern_context(file_path)
        except Exception as exc:
            # Fail-open per docs/architecture.md — never block edits on advisor
            # failure. Rec 3 + 4.1: surface a banner so the model knows the
            # advisory was unavailable. v0.5.15 follow-up: also write the
            # ACTUAL exception to stderr so the bash wrapper's `2>>` redirect
            # captures it in .hook_errors.log — the previous version emitted
            # the banner but silently swallowed the cause, leaving the user
            # with no diagnostic. Real-world surfaced cause: hook bash wrapper
            # falling back to system python3 (Py3.9 on macOS Command Line
            # Tools) when the plugin lacks a bundled venv, which then trips
            # ``@dataclass(slots=True)`` (Py3.10+ feature) inside
            # chameleon_mcp.signatures and ImportErrors out.
            _metric(advisory_emitted=False, repo_id=repo_id_hint, fail_open=True)
            try:
                import sys as _sys
                import time as _time
                import traceback as _tb

                ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                py_ver = ".".join(str(v) for v in _sys.version_info[:3])
                print(
                    f"[{ts}] preflight-and-advise fail-open "
                    f"(python={_sys.executable} {py_ver}): "
                    f"{type(exc).__name__}: {exc}",
                    file=_sys.stderr,
                )
                _tb.print_exc(file=_sys.stderr)
            except Exception:  # noqa: BLE001
                pass
            _emit_chameleon_context(
                _degraded_banner(
                    "advisor_unavailable",
                    "get_pattern_context failed; this edit proceeds without chameleon "
                    "guidance. Run /chameleon-doctor — the underlying "
                    "exception is logged at "
                    "~/.local/share/chameleon/.hook_errors.log.",
                )
            )
            return 0

    data = result.get("data", {})
    archetype_obj = data.get("archetype", {}) or {}
    canonical = data.get("canonical_excerpt", {}) or {}
    repo_info = data.get("repo", {}) or {}
    trust_state = repo_info.get("trust_state")
    # Note: get_archetype returns {archetype: <name>, alternatives, content_signal_match,
    # confidence_band}. The cluster name lives under the "archetype" key (yes, nested).
    archetype_name = archetype_obj.get("archetype")

    if not archetype_name:
        repo_id = repo_info.get("id") or repo_id_hint
        _metric(advisory_emitted=False, repo_id=repo_id, trust_state=trust_state)
        _emit({})
        return 0

    # Record a drift observation BEFORE the trust gate. Drift recording is
    # internal observability and stays useful regardless of whether we
    # inject canonical content for this edit. Failure must not block the
    # edit.
    repo_id = repo_info.get("id")
    confidence_band = archetype_obj.get("confidence_band")
    if repo_id:
        try:
            from chameleon_mcp.drift.observations import record_edit_observation

            record_edit_observation(
                repo_id=repo_id,
                rel_path=str(file_path),
                archetype=archetype_name,
                confidence_band=confidence_band,
                matched_canonical=bool(canonical.get("witness_path")),
            )
        except Exception:
            pass

    # BUG-024: gate canonical injection on trust_state. Pre-v0.5.6 the hook
    # injected the full canonical witness even when the user had not
    # granted trust, contradicting the using-chameleon skill ("if
    # untrusted, surface trust prompt once, proceed without injection
    # until trusted"). For an untrusted profile we now:
    #   1. Emit a one-time trust prompt (per session) suggesting
    #      /chameleon-trust, and
    #   2. Suppress the canonical excerpt and rules until the user trusts.
    # The "once per session" tracking uses a marker file under the per-
    # repo plugin-data dir.
    session_id = payload.get("session_id")
    if trust_state == "untrusted" and repo_id:
        if _should_emit_untrusted_prompt(repo_id, session_id):
            block = (
                "<chameleon-context>\n"
                "[chameleon: profile present, untrusted]\n\n"
                "A `.chameleon/` profile exists in this repo but the user "
                "has not granted trust for it yet. Surface this to your "
                "human partner once and suggest:\n\n"
                "    /chameleon-trust\n\n"
                "Chameleon will not inject canonical excerpts or team "
                "idioms into your context until trust is granted. The "
                "edit may still proceed; this is an advisory gate, not a "
                "hard deny.\n"
                "</chameleon-context>"
            )
            _metric(
                advisory_emitted=True,
                repo_id=repo_id,
                trust_state="untrusted",
                archetype=archetype_name,
                confidence=confidence_band,
            )
            _emit_chameleon_context(block)
            return 0
        # Already prompted this session — stay silent. Rec 4.2: the
        # historical label was "session_disable", which conflated this
        # trust-prompt dedup with the user's explicit /chameleon-disable.
        # Operators reading metrics couldn't distinguish "the user has
        # not run /chameleon-trust yet" from "the user actively disabled
        # chameleon" — both showed up as session_disable.
        _metric(
            advisory_emitted=False,
            repo_id=repo_id,
            suppression_reason="trust_prompt_dedup",
            trust_state="untrusted",
            archetype=archetype_name,
            confidence=confidence_band,
        )
        _emit({})
        return 0

    # Build a short context block; cap at 1500 tokens approx via char limit
    excerpt_content = canonical.get("content") or ""
    rules_count = len(data.get("rules") or [])
    idioms_text = data.get("idioms") or ""
    has_idioms = bool(idioms_text.strip())
    # Rec 1: enrich the header with match_quality (exact|ast|fallback|none)
    # so the model can calibrate how much to trust the canonical excerpt
    # below, and sub_buckets so the model knows whether the archetype is
    # a clean cluster or a heterogenous group (e.g., a controller cluster
    # silently absorbing concerns shows sub_buckets >= 2). Both values
    # come from the get_pattern_context envelope and stay inside the
    # existing bracketed header — pinned substrings ``[chameleon: archetype=``
    # and ``Canonical witness:`` are preserved verbatim.
    match_quality = archetype_obj.get("match_quality") or "unknown"
    sub_buckets_count = archetype_obj.get("sub_buckets_count") or 0
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context
    safe_name = sanitize_for_chameleon_context(archetype_name or "")
    safe_band = sanitize_for_chameleon_context(confidence_band or "unknown")
    safe_match = sanitize_for_chameleon_context(str(match_quality))
    block = (
        "<chameleon-context>\n"
        f"[chameleon: archetype={safe_name}, "
        f"confidence={safe_band}, "
        f"match_quality={safe_match}, "
        f"sub_buckets={int(sub_buckets_count)}]\n\n"
    )
    if trust_state == "stale":
        # BUG-NEW-011 (v0.5.7): explain the "stale" cause. Pre-fix the
        # message just said "Trust is stale" without saying why, which was
        # confusing right after a user trusted then refreshed. Refresh
        # invalidates the trust grant because the profile sha changes.
        block += (
            "**Trust is stale**: a recent /chameleon-refresh (or manual edit) "
            "changed `.chameleon/profile.json` after the trust grant. Trust is "
            "tied to the profile sha, so the grant no longer covers the current "
            "profile. Suggest /chameleon-trust to re-confirm. Do not block the "
            "edit; chameleon advisory is provided below for reference only.\n\n"
        )
    if excerpt_content:
        block += "Canonical witness:\n```\n"
        block += excerpt_content
        block += "\n```\n\n"
    if rules_count:
        block += f"Rules: {rules_count} entries available via get_rules({archetype_name!r}).\n"
    if has_idioms:
        block += "Team idioms captured via /chameleon-teach are available via get_pattern_context.\n"
    block += "</chameleon-context>"

    _metric(
        advisory_emitted=True,
        repo_id=repo_id,
        trust_state=trust_state,
        archetype=archetype_name,
        confidence=confidence_band,
    )
    _emit_chameleon_context(block)
    return 0


def posttool_recorder() -> int:
    """PostToolUse Bash: HMAC-signed exec log."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    # Extract the bits we care about
    tool_input = payload.get("tool_input", {})
    tool_response = payload.get("tool_response", {})
    command = tool_input.get("command", "")
    session_id = payload.get("session_id", "unknown")
    exit_code = tool_response.get("returnCode") if isinstance(tool_response, dict) else None

    # Compute repo_id from cwd if available; else use session_id as the bucket
    cwd = Path(os.environ.get("CLAUDE_CWD") or os.getcwd()).resolve()
    repo_id = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()

    try:
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(
            repo_id=repo_id,
            session_id=session_id,
            command=command,
            exit_code=int(exit_code) if exit_code is not None else -1,
        )
    except Exception:
        # Fail-open per Round 4 — never break the hook chain on logging errors
        pass

    _emit({})
    return 0


_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})
_VERIFY_SEEN_TTL_SECONDS = 30


def posttool_verify() -> int:
    """PostToolUse Edit/Write/NotebookEdit: archetype conformance lint.

    After a successful edit in a profiled repo, lints the written file
    against its archetype and injects violations as additionalContext
    so the model can self-correct.

    Default ON. Set CHAMELEON_VERIFY=0 to disable.
    Honors all opt-out mechanisms.
    Fails open on any error.
    """
    if os.environ.get("CHAMELEON_VERIFY") == "0":
        _emit({})
        return 0

    _started = time.time()

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in _EDIT_TOOLS:
        _emit({})
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        _emit({})
        return 0

    tool_response = payload.get("tool_response", {})
    if isinstance(tool_response, dict):
        if "error" in tool_response or tool_response.get("success") is False:
            _emit({})
            return 0

    session_id = payload.get("session_id")

    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(Path(file_path).expanduser())
        if repo_root is None:
            _emit({})
            return 0

        repo_id = _compute_repo_id(repo_root)

        if is_chameleon_suppressed(repo_root, repo_id, session_id) is not None:
            _emit({})
            return 0

        # Per-file cooldown: skip re-verification within 30s
        file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
        marker = _plugin_data_dir() / repo_id / f".verify_seen.{file_hash}"
        if _marker_path_is_fresh(marker, _VERIFY_SEEN_TTL_SECONDS):
            _emit_posttool_context(
                "<chameleon-context>\n"
                "[chameleon: already verified this file — review previous feedback]\n"
                "</chameleon-context>"
            )
            return 0

        p = Path(file_path).expanduser()
        if not p.is_file():
            _emit({})
            return 0
        content = p.read_bytes().decode("utf-8", errors="replace")

        # Resolve archetype — daemon fast path, then in-process fallback
        archetype_name: str | None = None
        try:
            from chameleon_mcp import daemon_client

            arch_result = daemon_client.call(
                "get_archetype", {"repo": str(repo_root), "file_path": file_path}
            )
            if arch_result:
                archetype_name = (arch_result.get("data") or {}).get("archetype")
        except Exception:
            pass

        if not archetype_name:
            from chameleon_mcp.tools import get_archetype

            arch_result = get_archetype(str(repo_root), file_path)
            archetype_name = (arch_result.get("data") or {}).get("archetype")

        if not archetype_name:
            _emit({})
            return 0

        # Lint — daemon fast path, in-process fallback (skips scan_secrets)
        violations: list[dict] = []
        daemon_responded = False

        try:
            from chameleon_mcp import daemon_client as _dc

            lint_result = _dc.call("lint_file", {
                "repo": str(repo_root),
                "archetype": archetype_name,
                "content": content,
            })
            if lint_result is not None:
                daemon_responded = True
                raw = (lint_result.get("data") or {}).get("violations") or []
                violations = [
                    v for v in raw
                    if v.get("rule") != "secret-detected-in-content"
                ]
        except Exception:
            pass

        if not daemon_responded:
            from chameleon_mcp.lint_engine import (
                detect_language,
                extract_dimensions,
                lint,
            )
            from chameleon_mcp.profile.loader import load_profile_dir

            loaded = load_profile_dir(repo_root / ".chameleon")
            canonicals = (
                (loaded.canonicals.get("canonicals") or {}).get(archetype_name) or []
            )
            ast_query: dict | None = None
            if canonicals:
                first = canonicals[0] or {}
                ast_query = (first.get("normative_shape") or {}).get("ast_query")
                witness_rel = (first.get("witness") or {}).get("path")

                # Recalibrate ast_query from witness using regex heuristic
                # (same fix as tools.lint_file step 3b)
                if ast_query and witness_rel:
                    w_full = repo_root / witness_rel
                    if w_full.is_file():
                        w_raw = w_full.read_bytes()[:100_000].decode(
                            "utf-8", errors="replace"
                        )
                        w_lang = detect_language(witness_rel)
                        w_snap = extract_dimensions(
                            w_raw, language=w_lang, file_path=witness_rel
                        )
                        ast_query = {
                            "default_export_kind": w_snap.default_export_kind,
                            "jsx_present": w_snap.jsx_present,
                            "top_level_node_kinds": sorted(
                                set(w_snap.top_level_node_kinds)
                            ),
                            "named_export_count_bucket": w_snap.named_export_count_bucket,
                            "content_signal": w_snap.content_signal,
                        }

            if ast_query:
                language = detect_language(file_path)
                snapshot = extract_dimensions(
                    content, language=language, file_path=file_path
                )
                violations = [v.to_dict() for v in lint(snapshot, ast_query)]

        # Metrics
        elapsed_ms = int((time.time() - _started) * 1000)
        try:
            from chameleon_mcp.metrics import emit_hook_metric

            emit_hook_metric(
                "posttool-verify",
                elapsed_ms=elapsed_ms,
                repo_id=repo_id,
                advisory_emitted=bool(violations),
                archetype=archetype_name,
            )
        except Exception:
            pass

        # Touch cooldown marker (both clean and violation paths)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(marker.parent, 0o700)
            except OSError:
                pass
            marker.touch(exist_ok=True)
            try:
                os.chmod(marker, 0o600)
            except OSError:
                pass
        except OSError:
            pass

        if not violations:
            _emit({})
            return 0

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        safe_path = sanitize_for_chameleon_context(file_path)
        lines = [
            "<chameleon-context>",
            "[chameleon: post-edit verification]",
            "",
            f"File: {safe_path}",
            f"Archetype violations ({len(violations)}):",
        ]
        for v in violations:
            sev = sanitize_for_chameleon_context(v.get("severity", "info"))
            rule = sanitize_for_chameleon_context(v.get("rule", "unknown"))
            msg = sanitize_for_chameleon_context(v.get("message", ""))
            lines.append(f"- [{sev}] {rule}: {msg}")
        lines.append("")
        lines.append(
            "Fix these to match the archetype."
            " See PreToolUse canonical for the expected pattern."
        )
        lines.append("</chameleon-context>")

        block = "\n".join(lines)

        _emit_posttool_context(block)
        return 0

    except Exception as exc:
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            py_ver = ".".join(str(v) for v in sys.version_info[:3])
            print(
                f"[{ts}] posttool-verify fail-open "
                f"(python={sys.executable} {py_ver}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass
        _emit({})
        return 0


# Frustration phrases that suggest the user is unhappy with chameleon's
# latency or pattern advice. Surfaced as a one-line reminder via
# additionalContext.
#
# BUG-NEW-014 (v0.5.7): expanded coverage. Pre-fix the patterns missed
# the obvious frustration markers ("annoying", "hate", expletives) and
# over-triggered on a bare "stop" in any context ("don't stop now"). The
# expanded set:
#   - keeps interjection markers (ugh, argh, wtf, nope) but drops solo "stop"
#     to avoid false positives;
#   - adds plain-English unhappiness ("hate", "annoying", "frustrating", "useless");
#   - adds common expletives that almost always co-occur with frustration;
#   - keeps chameleon-specific patterns explicit.
_FRUSTRATION_PATTERNS = (
    re.compile(r"\b(ugh|argh|wtf|nope|sucks|useless|dumb)\b", re.IGNORECASE),
    re.compile(r"\b(annoying|annoyed|frustrating|frustrated|hate|hating)\b", re.IGNORECASE),
    re.compile(r"\b(damn|fuck|fucking|shit|crap|bullshit)\b", re.IGNORECASE),
    re.compile(r"this isn'?t right", re.IGNORECASE),
    re.compile(r"don'?t (do|use|inject) (that|this)", re.IGNORECASE),
    re.compile(r"chameleon\s+is\s+(slow|wrong|broken|annoying|useless)", re.IGNORECASE),
    re.compile(r"stop (injecting|using|doing|adding)", re.IGNORECASE),
)


def callout_detector() -> int:
    """UserPromptSubmit: frustration phrase reminder.

    On detected frustration during a chameleon-active session, surface a
    one-line hint about /chameleon-disable, /chameleon-pause-15m, and
    /chameleon-teach as actionable next steps.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    user_prompt = payload.get("user_prompt", "") or payload.get("prompt", "")
    if not user_prompt:
        _emit({})
        return 0

    if not any(pattern.search(user_prompt) for pattern in _FRUSTRATION_PATTERNS):
        _emit({})
        return 0

    # Frustration detected. Emit a brief hint as additionalContext.
    hint = (
        "<chameleon-context>\n"
        "[chameleon: detected frustration phrase]\n"
        "If chameleon is the issue, options:\n"
        "  /chameleon-disable      — suppress for the rest of this session\n"
        "  /chameleon-pause-15m    — pause for 15 minutes (auto-resume)\n"
        "  /chameleon-teach <pattern>  — capture the missed pattern as an idiom\n"
        "If chameleon is unrelated, ignore this note.\n"
        "</chameleon-context>"
    )
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": hint,
        }
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write("hook_helper.py: missing command argument\n")
        return 1
    command = args[0]
    if command == "session-start":
        return session_start()
    if command == "preflight-and-advise":
        return preflight_and_advise()
    if command == "posttool-recorder":
        return posttool_recorder()
    if command == "posttool-verify":
        return posttool_verify()
    if command == "callout-detector":
        return callout_detector()
    sys.stderr.write(f"hook_helper.py: unknown command {command!r}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
