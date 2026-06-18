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


def _absorb_broken_stdout() -> None:
    """Point stdout at devnull after EPIPE so the exit flush cannot raise again.

    Without this, the interpreter's shutdown flush of the still-buffered stream
    raises a second BrokenPipeError that lands on stderr, pollutes
    ``.hook_errors.log``, and trips the doctor's error-log warning — all for a
    consumer that already hung up.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError):
        pass


def _emit(output: dict) -> None:
    """Write Claude Code hook output JSON to stdout. Single source of truth.

    A consumer that closed the pipe (harness timeout-kill, mid-write teardown)
    must not turn into a noisy crash: nobody is left to read the JSON, so
    EPIPE is absorbed and stdout is neutralized for the rest of the process.
    """
    try:
        sys.stdout.write(json.dumps(output))
        sys.stdout.write("\n")
        # Flush HERE so a closed pipe surfaces now, inside the guard — a
        # buffered write succeeds silently and the EPIPE would otherwise fire
        # in the interpreter's exit flush, outside any handler (exit code 120).
        sys.stdout.flush()
    except BrokenPipeError:
        _absorb_broken_stdout()
    except OSError as exc:
        import errno

        if exc.errno != errno.EPIPE:
            raise
        _absorb_broken_stdout()


def _read_payload_dict() -> dict | None:
    """Read+parse hook stdin JSON, returning a dict or None on malformed input.

    json.loads succeeds on valid-but-non-object JSON (``[1,2,3]``, ``null``,
    ``42``, ``"x"``), which would then crash ``payload.get(...)`` with an
    AttributeError that the (JSONDecodeError, ValueError) guard does NOT catch.
    Returning None lets every entry point fail open with ``_emit({})`` instead
    of depending on the bash wrapper's ``|| printf '{}'`` to mask the traceback.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError, RecursionError):
        # RecursionError (deeply nested JSON) subclasses RuntimeError, not
        # ValueError, so it would otherwise escape and write a traceback to the
        # error log even though the bash wrapper masks the exit code -- a false
        # /chameleon-doctor "degraded" warning from one malformed payload.
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _as_dict(value: object) -> dict:
    """Coerce a payload sub-field to a dict; non-dict (str/list/None) -> {}.

    Hook inputs like ``tool_input`` / ``tool_response`` are dicts in the
    contract, but a malformed payload can carry a string or list there, which
    would crash the subsequent ``.get(...)``.
    """
    return value if isinstance(value, dict) else {}


def _repo_rel(repo_root: Path | None, file_path: str | None) -> str | None:
    """Repo-relative path for a would_block metric row, or None if unresolvable.

    The shadow report samples ``file_rel:line`` for human spot-check, so a
    stable repo-relative form keeps the sample readable and avoids leaking the
    user's absolute home path into the metrics log. Falls back to the file's
    basename when the path is outside the repo root, and to None when either
    input is missing.
    """
    if not file_path:
        return None
    try:
        p = Path(file_path)
        if repo_root is not None:
            try:
                return p.resolve().relative_to(repo_root.resolve()).as_posix()
            except (ValueError, OSError):
                return p.name
        return p.name
    except (OSError, ValueError):
        return None


def _content_digest_16(content: str) -> str:
    """16-hex-char sha256 digest of an edit's analyzed content window.

    Pinned definition shared by the verify cooldown marker, the decision log's
    replay key, and the per-turn judge routing: sha256 over the utf-8
    re-encoding of the first 100,000 file bytes decoded with
    ``errors="replace"``, hex-truncated to 16 chars. Every consumer must derive
    the digest from that same window or the keys stop joining.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _emit_check_event(
    repo_id: str | None,
    session_id: str | None,
    check: str,
    status: str,
    reason: str | None = None,
    file_rel: str | None = None,
    detail: dict | None = None,
) -> None:
    """One guarded check-event write for the session attestation. Never raises.

    The sidecar records that a turn-end check ran / was skipped / degraded so
    the Stop attestation can attest it. The write is evidence, never control
    flow: any failure is swallowed and the hook outcome is unchanged.
    """
    try:
        from chameleon_mcp.exec_log import append_check_event

        append_check_event(
            repo_id or "",
            session_id=session_id or "",
            check=check,
            status=status,
            reason=reason,
            file_rel=file_rel,
            detail=detail,
        )
    except Exception:
        pass


def _record_overrides(
    repo_id: str | None,
    overridden: list[dict],
    *,
    archetype: str | None,
    file_rel: str | None,
    session_id: str | None,
    blanket: bool,
) -> None:
    """Record each inline-ignored block-eligible rule as an auditable override.

    Emits one metric counter per rule (paired with the would_block stream the
    shadow report already reads) and one durable drift.db row, so a bypass is
    visible after the turn. Best-effort: a logging failure must never break the
    hook, so every step is guarded.
    """
    if not overridden:
        return
    rules = sorted({v.get("rule") for v in overridden if v.get("rule")})
    if not rules:
        return
    try:
        from chameleon_mcp.metrics import emit_hook_metric

        for rule in rules:
            emit_hook_metric(
                "override",
                elapsed_ms=0,
                repo_id=repo_id,
                advisory_emitted=False,
                archetype=archetype,
                rule=rule,
                file_rel=file_rel,
                override=True,
            )
    except Exception:
        pass
    if not repo_id:
        return
    try:
        from chameleon_mcp.drift.observations import record_override

        for rule in rules:
            record_override(
                repo_id,
                rule,
                rel_path=file_rel,
                archetype=archetype,
                session_id=session_id,
                blanket=blanket,
            )
    except Exception:
        pass


def _record_edit_decision(
    repo_id: str | None,
    repo_root: Path | None,
    file_path: str | None,
    *,
    archetype: str | None,
    match_quality: str | None,
    confidence_band: str | None,
    violations_raised: int,
    blockable_rules: list[str] | None,
    outcome: str,
    session_id: str | None,
    content_digest: str | None = None,
) -> None:
    """Persist one decision_log row capturing what chameleon knew and did here.

    Written once per governed edit, after the outcome is resolved, so a
    postmortem can replay 'last time this file was edited, chameleon matched X at
    quality Q and the gate did Y'. Keyed by a true repo-relative path plus the
    content digest of the verified window, so a later replay resolves the row
    that governed these exact bytes. Best-effort only: a logging failure must
    never break the hook.
    """
    if not repo_id:
        return
    try:
        from chameleon_mcp.drift.observations import record_decision

        record_decision(
            repo_id,
            _repo_rel(repo_root, file_path) or "",
            archetype=archetype,
            match_quality=match_quality,
            confidence_band=confidence_band,
            violations_raised=violations_raised,
            blockable_rules=blockable_rules,
            outcome=outcome,
            session_id=session_id,
            content_digest=content_digest,
        )
    except Exception:
        pass


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


def _ignore_hint(paths: object, rule: str = "<rule>") -> str:
    """Render the inline-override directive in the offending file's comment syntax.

    A block message that hands a Ruby developer ``//`` describes a directive
    that is a syntax error in their file; the parser only honors the
    language's own comment token. Accepts a single path or a list (the Stop
    backstop can hold files in both languages at once — then both forms are
    shown).
    """
    from chameleon_mcp.lint_engine import detect_language

    if isinstance(paths, str) or paths is None:
        paths = [paths] if paths else []
    langs = {detect_language(str(p)) for p in paths if p}
    # Unknown extensions never carry violations, so they don't shape the hint.
    langs.discard(None)
    if langs == {"ruby"}:
        return f"`# chameleon-ignore {rule}`"
    if "ruby" in langs:
        return f"`// chameleon-ignore {rule}` (`# chameleon-ignore {rule}` in Ruby)"
    return f"`// chameleon-ignore {rule}`"


def _emit_posttool_block(reason: str, additional_context: str) -> None:
    """PostToolUse hard block: stops the loop and feeds ``reason`` back to Claude."""
    _emit(
        {
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context,
            },
        }
    )


def _emit_pretool_deny(reason: str) -> None:
    """PreToolUse hard deny: blocks the tool call before it runs."""
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
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
        f"[🦎 chameleon: degraded — {safe_reason}]",
    ]
    if detail:
        parts.append("")
        parts.append(sanitize_for_chameleon_context(detail))
    parts.append("</chameleon-context>")
    return "\n".join(parts)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text via a tmp file + os.replace so a reader never sees torn JSON."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _update_statusline(
    activity: str,
    repo_name: str | None = None,
    trust_state: str | None = None,
    repo_root: Path | None = None,
) -> None:
    """Update the statusline cache with live activity + trust state. Fail-open.

    ``repo_root`` places the cache under the repo root's ``.claude/`` so a
    subdir-launched session updates the same file SessionStart wrote and the
    statusline script reads. Falls back to cwd when the root is unknown.
    """
    try:
        base = repo_root if repo_root else Path.cwd()
        cache = base / ".claude" / ".chameleon-statusline-cache"
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            data["activity"] = activity
            if repo_name and trust_state:
                for p in data.get("profiles", []):
                    if p.get("name") == repo_name:
                        p["trust"] = trust_state
                        break
            _atomic_write_text(cache, json.dumps(data))
    except Exception:
        pass


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
_TRUST_MARKER_TTL_SECONDS = 24 * 3600


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
        marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker_dir, 0o700)
        except OSError:
            pass
        marker = marker_dir / _TRUST_PROMPT_FILENAME.format(
            session=_safe_session_marker(session_id)
        )
        if _marker_is_fresh(marker):
            return False
        marker.touch(exist_ok=True)
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        return True
    except (OSError, PermissionError):
        return True


def _emit_session_context(content: str) -> None:
    """Emit SessionStart context in Claude Code's expected JSON shape:
    `{ "hookSpecificOutput": { "hookEventName": "SessionStart", "additionalContext": ... } }`
    """
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        }
    )


_DRIFT_BANNER_FILENAME = ".drift_banner.last"
_ENGINE_BANNER_FILENAME = ".engine_banner.last"
_PRODUCTION_BANNER_FILENAME = ".production_banner.last"
_JUDGE_HEALTH_BANNER_FILENAME = ".judge_health_banner.last"
_INTERPRETER_BANNER_FILENAME = ".interpreter_banner.last"

# Degradation reasons the judge paths write into check events. The banner
# echoes the reason into injected context, so anything outside this set reads
# as "unknown" -- the injection surface stays allowlisted even if the ledger
# text was tampered with.
_JUDGE_DEGRADED_REASONS = frozenset(
    {
        "spawn_timeout",
        "spawn_exec_error",
        "spawn_nonzero_exit",
        "pipeline_error",
        "unparseable_output",
    }
)


def _production_tip_banner(repo_root: Path, session_id: str | None = None) -> str | None:
    """One-line SessionStart advisory when the locked production ref's tip
    moved past the profile's recorded derivation SHA.

    For a production-pinned repo this is the real freshness signal — the
    profile describes a commit the production branch has since left
    behind. Same optout + TTL-marker discipline as the drift banner; tip
    comparison reads the LOCAL ref (current as of the user's last fetch),
    never the network. Best-effort: any failure returns None.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.production_ref import resolve_production_ref
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import (
            _compute_repo_id,
            _persisted_production_ref,
            _recorded_derivation_sha,
        )

        resolved_root = find_repo_root(repo_root) or repo_root
        profile_dir = resolved_root / ".chameleon"
        if not profile_dir.is_dir():
            return None
        branch = _persisted_production_ref(resolved_root)
        if not branch:
            return None
        recorded = _recorded_derivation_sha(profile_dir)
        if not recorded:
            return None
        # Optout check BEFORE the git subprocess: a disabled/paused repo must
        # not pay resolution cost on session start.
        repo_id = _compute_repo_id(resolved_root)
        if is_chameleon_suppressed(resolved_root, repo_id, session_id) is not None:
            return None
        # Short per-call timeout: this runs inside the session-start `timeout 3`
        # wrapper; memoized so the auto-refresh trigger reuses the answer.
        resolved = resolve_production_ref(resolved_root, branch, timeout_seconds=1, use_memo=True)
        if resolved is None or resolved.sha == recorded:
            return None

        marker = _plugin_data_dir() / repo_id / _PRODUCTION_BANNER_FILENAME
        if _marker_path_is_fresh(marker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
            return None
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(marker, str(int(time.time())))
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        return (
            f"[🦎 chameleon: production drift] The locked production branch "
            f"({resolved.ref}) has moved to {resolved.sha[:12]} since this profile "
            f"was derived ({recorded[:12]}). Suggest /chameleon-refresh to re-derive "
            "from the current production tree."
        )
    except Exception:  # noqa: BLE001
        return None


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

        resolved_root = find_repo_root(repo_root) or repo_root
        repo_id = _compute_repo_id(resolved_root)

        if is_chameleon_suppressed(resolved_root, repo_id, session_id) is not None:
            return None

        # Engine-version upgrade is a stronger staleness signal than
        # edit-observation drift: the analysis logic changed, so re-derive
        # regardless of recorded edits. Its own cooldown marker keeps it from
        # firing every session until the user refreshes (which clears the
        # mismatch). Falls through to the edit-drift logic when versions match.
        try:
            from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION
            from chameleon_mcp.tools import _engine_version_changed

            if _engine_version_changed(resolved_root / ".chameleon", ENGINE_MIN_VERSION):
                emarker = _plugin_data_dir() / repo_id / _ENGINE_BANNER_FILENAME
                if _marker_path_is_fresh(emarker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
                    return None
                emarker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.chmod(emarker.parent, 0o700)
                except OSError:
                    pass
                _atomic_write_text(emarker, str(int(time.time())))
                try:
                    os.chmod(emarker, 0o600)
                except OSError:
                    pass
                # When auto-refresh is on, this same SessionStart triggers the
                # re-derive itself — suggesting a manual /chameleon-refresh
                # right after it already ran is stale advice.
                auto_on = True
                try:
                    from chameleon_mcp.profile.config import load_config

                    auto_on = load_config(resolved_root / ".chameleon").auto_refresh.enabled
                except Exception:  # noqa: BLE001
                    auto_on = True
                if auto_on:
                    return (
                        "[🦎 chameleon: drift]\n"
                        "The chameleon engine was upgraded since this profile was "
                        "built; the profile auto-refresh has been triggered for "
                        "this session. If guidance still looks stale next "
                        "session, run **/chameleon-refresh**."
                    )
                return (
                    "[🦎 chameleon: drift]\n"
                    "The chameleon engine was upgraded since this profile was "
                    "built, so its clustering may be out of date. Suggest "
                    "**/chameleon-refresh** to re-derive the profile."
                )
        except Exception:
            pass

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

        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(marker, str(int(time.time())))
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass

        score_str = f"{stats['score']:.2f}"
        # The score is structural mimicry, not correctness. Lead with the
        # blind-spots disclaimer so a reader never reads "low drift" as a
        # quality bar, then state the metric and the refresh suggestion.
        from chameleon_mcp.shadow_report import CONFORMANCE_DISCLAIMER

        return (
            "[🦎 chameleon: structural conformance]\n"
            f"{CONFORMANCE_DISCLAIMER}\n"
            f"Structural-conformance drift is {score_str} over the last 14 days "
            f"(N={stats['count']} edits): recent edits diverge from the "
            "profile's archetype shapes. The profile may not match how the team "
            "writes code today. Suggest **/chameleon-refresh** when you have a "
            "moment."
        )
    except Exception as exc:
        try:
            print(
                f"chameleon: drift banner failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001
            pass
        return None


def _judge_spawn_health_banner(repo_root: Path, session_id: str | None = None) -> str | None:
    """One-line SessionStart advisory when the previous session's attestation
    recorded a degraded correctness-judge spawn.

    A failed reviewer spawn otherwise lives only in the attestation ledger:
    the turn-end review layer can be silently dead (broken auth, missing
    binary) for weeks with no user-visible signal. Reads the NEWEST ledger
    row -- the last session that attested -- and skips a row from the current
    session so a resumed session never warns about its own in-progress state.
    Same optout + TTL-marker discipline as the drift banner. Best-effort: any
    failure returns None.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.review_ledger import read_session_attestations
        from chameleon_mcp.tools import _compute_repo_id

        resolved_root = find_repo_root(repo_root) or repo_root
        repo_id = _compute_repo_id(resolved_root)
        if is_chameleon_suppressed(resolved_root, repo_id, session_id) is not None:
            return None

        # No data dir means no prior session attested here; bail before the
        # ledger read, whose path resolution would CREATE the dir (session
        # start for an unprofiled repo must stay side-effect free).
        if not (_plugin_data_dir() / repo_id).is_dir():
            return None

        records = read_session_attestations(repo_id, limit=1).get("records") or []
        if not records:
            return None
        latest = records[0]
        if session_id and latest.get("session_id") == session_id:
            return None
        reason: str | None = None
        for entry in latest.get("checks") or []:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("check") == "correctness_judge"
                and entry.get("status") == "degraded_spawn"
            ):
                raw = entry.get("reason")
                reason = raw if raw in _JUDGE_DEGRADED_REASONS else "unknown"
                break
        if reason is None:
            return None

        marker = _plugin_data_dir() / repo_id / _JUDGE_HEALTH_BANNER_FILENAME
        if _marker_path_is_fresh(marker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
            return None
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(marker, str(int(time.time())))
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        return (
            f"[🦎 chameleon: turn-end reviewer failed to spawn last session "
            f"({reason}); run /chameleon-doctor]"
        )
    except Exception:  # noqa: BLE001
        return None


def _hook_error_log_path() -> Path:
    """Path the hook scripts append fail-open lines to (override-aware).

    Mirrors the shell hooks' LOG_FILE resolution so the SessionStart banner
    reads the same file the hooks write.
    """
    override = os.environ.get("CHAMELEON_HOOK_ERROR_LOG")
    if override:
        return Path(override).expanduser()
    return _plugin_data_dir() / ".hook_errors.log"


def _interpreter_degraded_banner(repo_root: Path, session_id: str | None = None) -> str | None:
    """One-line SessionStart advisory when recent hook fires fail-opened.

    The hook scripts log a line when they cannot resolve a Python >=3.11 (the
    ``no-interpreter`` case) or when the spawned helper exits non-zero
    (``failed (python=...)``). Either means enforcement and guidance went silent
    for that fire — invisible to the user, since the hook still exits 0 with
    ``{}``. Read the tail of that log over a 24h window and surface a count so a
    degraded session is not mistaken for a healthy one. ``no-interpreter`` is
    definitive (raise on the first); spawn failures need a few to clear one-off
    timeout noise. Same optout discipline as the other SessionStart banners, and
    the same re-show cooldown (DRIFT_BANNER_TTL_SECONDS, 7d default), so a
    persistently-broken machine warns at most once per cooldown window rather
    than every session. Best-effort: any failure returns None.
    """
    try:
        import calendar
        import re as _re

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        resolved_root = find_repo_root(repo_root) or repo_root
        # Profile-presence gate FIRST: an unprofiled repo has no enforcement to
        # degrade, and returning here keeps session start side-effect free (the
        # opt-out probe and marker write below would otherwise create the repo
        # data dir). The hooks share one resolver, so a machine-level
        # interpreter problem still surfaces on the next profiled repo.
        if not (resolved_root / ".chameleon").is_dir():
            return None
        repo_id = _compute_repo_id(resolved_root)
        if is_chameleon_suppressed(resolved_root, repo_id, session_id) is not None:
            return None

        log_path = _hook_error_log_path()
        try:
            size = log_path.stat().st_size
        except OSError:
            return None
        with log_path.open("rb") as fh:
            if size > 16384:
                fh.seek(size - 16384)
            tail = fh.read().decode("utf-8", errors="replace")

        cutoff = time.time() - 86400
        ts_re = _re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]")
        no_interp = 0
        spawn_fail = 0
        for line in tail.splitlines():
            m = ts_re.match(line)
            if not m:
                continue
            try:
                when = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if when < cutoff:
                continue
            if "no-interpreter" in line:
                no_interp += 1
            elif "failed (python=" in line:
                spawn_fail += 1

        if not (no_interp >= 1 or spawn_fail >= 3):
            return None
        # Count the triggering reason, not the sum: when no-interpreter fires,
        # below-threshold one-off spawn-fails must not inflate the headline.
        count = no_interp if no_interp else spawn_fail

        marker = _plugin_data_dir() / repo_id / _INTERPRETER_BANNER_FILENAME
        if _marker_path_is_fresh(marker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
            return None
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(marker, str(int(time.time())))
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        detail = "no Python >=3.11 resolved" if no_interp else "hook spawn failed"
        return (
            f"[🦎 chameleon: {count} hook fail-open(s) in the last 24h "
            f"({detail}); enforcement was degraded — run /chameleon-doctor]"
        )
    except Exception:  # noqa: BLE001
        return None


_AUTO_REFRESH_COOLDOWN_FILENAME = ".auto_refresh_cooldown"


def _refresh_interpreter_cmd() -> list[str] | None:
    """Argv prefix for an interpreter that can import chameleon's runtime deps.

    The hook scripts resolve a python via a fallback ladder (bundled ``.venv``,
    then a system ``python3.x``). That winner can lack chameleon's third-party
    deps (xxhash et al.). The hot-path hooks are stdlib-only and survive, but the
    refresh/bootstrap path imports the extractors at module load, so a depless
    interpreter aborts the spawned refresh with ModuleNotFoundError — surfaced
    only in auto_refresh.log. Prefer the current interpreter when it already
    imports the deps; otherwise fall back to ``uv run`` against the bundled mcp
    project (the same resolver the MCP server uses via ``uvx``), which
    materializes them. Returns None when neither is viable so the caller can log
    an actionable line instead of spawning a child doomed to fail.
    """
    try:
        # Probe the third-party deps the refresh/bootstrap path actually imports
        # — xxhash (extractors), pyyaml (tool-config / workspace scan),
        # detect-secrets (secret scanner) — not xxhash alone, so an interpreter
        # carrying one but not the others is not mistaken for deps-complete.
        import detect_secrets  # noqa: F401
        import xxhash  # noqa: F401
        import yaml  # noqa: F401

        return [sys.executable]
    except Exception:
        pass

    import shutil

    uv = shutil.which("uv")
    if uv:
        mcp_dir = Path(__file__).resolve().parent.parent
        return [uv, "run", "--project", str(mcp_dir), "python"]
    return None


def _maybe_auto_refresh(repo_root: Path) -> None:
    """Fire ``refresh_repo`` in background when auto-refresh fires.

    Gates (ALL must hold):
      - ``auto_refresh.enabled`` is not explicitly ``false`` in
        ``.chameleon/config.json`` (default: on)
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

        cooldown_seconds = max(60, (cfg.auto_refresh.max_age_hours * 3600) // 4)
        cooldown_marker = _plugin_data_dir() / repo_id / _AUTO_REFRESH_COOLDOWN_FILENAME
        if _marker_path_is_fresh(cooldown_marker, cooldown_seconds):
            return

        should_fire = False
        # Migration trigger: an engine upgrade or a profile missing enforcement.json
        # (an existing user's pre-upgrade profile, built before calibration existed)
        # must auto-upgrade on the next session rather than waiting for drift or age
        # to accumulate. The refresh re-derives the profile, regenerates the
        # calibration, and re-stamps the engine version, so the trigger self-clears
        # and the cooldown marker below prevents a re-fire while it runs.
        try:
            from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION
            from chameleon_mcp.tools import _engine_version_changed

            if (
                _engine_version_changed(profile_dir, ENGINE_MIN_VERSION)
                or not (profile_dir / "enforcement.json").is_file()
            ):
                should_fire = True
        except Exception:  # noqa: BLE001
            pass
        if not should_fire:
            # Production-pinned staleness: the locked ref's tip moved past the
            # profile's recorded derivation SHA (or a locked repo's profile
            # predates ref-pinned provenance entirely). This is THE freshness
            # signal for pinned repos — working-tree drift below says nothing
            # about the production tree. Tip comparison is against the local
            # ref (origin/<branch> as of the user's last fetch); no network.
            try:
                from chameleon_mcp.production_ref import resolve_production_ref
                from chameleon_mcp.tools import (
                    _persisted_production_ref,
                    _recorded_derivation_sha,
                )

                prod_branch = _persisted_production_ref(resolved_root)
                if prod_branch:
                    # Memoized: the tip banner usually resolved this same lock
                    # moments earlier in this process; short timeout keeps the
                    # session-start budget intact on a cold lookup.
                    resolved_ref = resolve_production_ref(
                        resolved_root, prod_branch, timeout_seconds=1, use_memo=True
                    )
                    if resolved_ref is not None:
                        recorded_sha = _recorded_derivation_sha(profile_dir)
                        if recorded_sha != resolved_ref.sha:
                            should_fire = True
            except Exception:  # noqa: BLE001
                pass
        if not should_fire:
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

        repo_log_dir = _plugin_data_dir() / _compute_repo_id(resolved_root)
        repo_log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(repo_log_dir, 0o700)
        except OSError:
            pass
        log_path = repo_log_dir / "auto_refresh.log"
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

        import subprocess as _sp

        refresh_cmd = _refresh_interpreter_cmd()
        try:
            if refresh_cmd is None:
                # No deps-complete interpreter resolves: the hook landed on a
                # system python without chameleon's deps and `uv` is not on PATH.
                # Spawning the child would just abort with ModuleNotFoundError, so
                # log an actionable line and skip rather than fail silently.
                if log_fd is not None:
                    try:
                        os.write(
                            log_fd,
                            (
                                f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                                "auto-refresh skipped: the hook interpreter "
                                f"({sys.executable}) cannot import chameleon's deps "
                                "(e.g. xxhash) and `uv` is not on PATH. Install uv, or "
                                "create the mcp/.venv, then run /chameleon-refresh. "
                                "See /chameleon-doctor.\n"
                            ).encode(),
                        )
                    except OSError:
                        pass
                else:
                    # The log file could not be opened either (broken data dir);
                    # the hook's stderr is captured to .hook_errors.log, so a line
                    # there keeps the skip diagnosable instead of fully silent.
                    try:
                        print(
                            "chameleon: auto-refresh skipped (no deps-complete "
                            "interpreter; run /chameleon-doctor)",
                            file=sys.stderr,
                        )
                    except Exception:
                        pass
            else:
                _sp.Popen(
                    [
                        *refresh_cmd,
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

        cooldown_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(cooldown_marker.parent, 0o700)
        except OSError:
            pass
        _atomic_write_text(cooldown_marker, str(int(time.time())))
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


def _marker_digest_matches(marker: Path, content_digest: str) -> bool:
    """True when the verify marker records this exact content digest.

    The marker's mtime is the cooldown clock; its body pins WHICH content was
    verified. Suppressing re-analysis on mtime alone hid defects introduced by
    an edit landing inside the cooldown window — the common iterate-then-break
    flow — so the dedup only holds for unchanged content. A legacy empty
    marker (pre-digest format) never matches, forcing one fresh verification
    that rewrites it in the new format.
    """
    try:
        return marker.read_text(encoding="utf-8").strip() == content_digest
    except OSError:
        return False


def _write_verify_marker(marker: Path, content_digest: str) -> None:
    """Stamp a verification pass: cooldown mtime plus the verified content digest."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        marker.write_text(content_digest, encoding="utf-8")
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        _prune_stale_verify_markers(marker.parent)
    except OSError:
        pass


def _prune_stale_verify_markers(repo_data_dir: Path, max_age_seconds: int = 86_400) -> None:
    """Drop verify markers long past any cooldown TTL.

    Markers are keyed per session and per file, so they accumulate one tiny
    file per (session, path) pair. Their useful life is the cooldown window
    (seconds); anything a day old is dead weight. Best-effort — a racing
    unlink or permission error never disturbs the verify path.
    """
    try:
        cutoff = time.time() - max_age_seconds
        for stale in repo_data_dir.glob(".verify_seen.*"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def _sanitize_profile_obj(obj: object) -> object:
    """Recursively sanitize every string in a JSON-loaded profile structure.

    Neutralizes tag-boundary tokens in attacker-controllable committed data
    (conventions.json values) BEFORE it is formatted into a block. We sanitize
    the INPUTS, not the assembled block: ``format_conventions_for_session`` adds
    its own legitimate ``<chameleon-conventions>`` wrapper, and ``<chameleon``
    is itself a dangerous token, so sanitizing the output would corrupt the
    wrapper.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    if isinstance(obj, str):
        return sanitize_for_chameleon_context(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_profile_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_profile_obj(v) for v in obj]
    return obj


def _wire_statusline_settings(project_dir: Path, plugin_root: str | None) -> None:
    """Point the project's settings.local.json at the chameleon statusline script.

    Skips the write when the project already declares a statusLine (in
    settings.json) or when the user pinned a global statusLine in
    ~/.claude/settings.json, since settings.local.json would silently override
    that choice. Fails open: any error leaves the project's settings untouched.
    """
    try:
        if not plugin_root:
            return
        script_path = Path(plugin_root) / "bin" / "chameleon-statusline.sh"
        if not script_path.is_file():
            return

        local_settings = project_dir / ".claude" / "settings.local.json"
        project_settings = project_dir / ".claude" / "settings.json"
        current_cmd: str | None = str(script_path)
        needs_write = False

        if project_settings.is_file():
            try:
                d = json.loads(project_settings.read_text(encoding="utf-8"))
                if "statusLine" in d:
                    current_cmd = None
            except Exception:
                pass

        if current_cmd is not None:
            user_settings = Path.home() / ".claude" / "settings.json"
            if user_settings.is_file():
                try:
                    ud = json.loads(user_settings.read_text(encoding="utf-8"))
                    if "statusLine" in ud:
                        current_cmd = None
                except Exception:
                    pass

        existing: dict = {}
        if current_cmd is not None:
            if local_settings.is_file():
                try:
                    existing = json.loads(local_settings.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            old_cmd = (existing.get("statusLine") or {}).get("command", "")
            if "statusLine" not in existing:
                needs_write = True
            elif old_cmd != current_cmd and "chameleon" in old_cmd:
                needs_write = True

        if needs_write and current_cmd is not None:
            existing["statusLine"] = {
                "type": "command",
                "command": current_cmd,
            }
            local_settings.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write_text(local_settings, json.dumps(existing, indent=2) + "\n")
    except Exception:
        pass


def _seed_archetype_seen(repo_id: str | None, session_id: str | None, archetype: str) -> None:
    """Record an archetype in the enforcement state without other side effects.

    The PreToolUse deny path returns before the normal advisory flow seeds
    ``archetypes_seen``, so a denied edit would leave the archetype unseen and a
    later successful edit to it would re-trigger the verbose first-in-archetype
    advisory. Seeding here keeps the seen-set accurate across a deny. Fails open.
    """
    try:
        if not repo_id or not session_id or not archetype:
            return
        from chameleon_mcp import enforcement

        repo_data = _plugin_data_dir() / repo_id
        state = enforcement.load_state(repo_data, session_id)
        if archetype in state.archetypes_seen:
            return
        state.archetypes_seen.add(archetype)
        enforcement.save_state(state, repo_data, session_id)
    except Exception:
        pass


def session_start() -> int:
    """SessionStart: inject using-chameleon SKILL.md + profile primer."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        _emit({})
        return 0

    skill_path = Path(plugin_root) / "skills" / "using-chameleon" / "SKILL.md"
    if not skill_path.is_file():
        _emit({})
        return 0

    skill_content = skill_path.read_text(encoding="utf-8", errors="replace")

    session_id: str | None = None
    try:
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

    repo_root = None
    try:
        from chameleon_mcp.plugin_paths import plugin_data_dir
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(Path.cwd())
        if repo_root:
            repo_id = _compute_repo_id(repo_root)
            repo_data = plugin_data_dir() / repo_id
            if repo_data.is_dir():
                cutoff = time.time() - 86400
                for pattern in (".enforcement.*.json", ".enforcement.*.lock"):
                    for p in repo_data.glob(pattern):
                        try:
                            if p.stat().st_mtime < cutoff:
                                p.unlink()
                        except OSError:
                            pass
                # Reap stale per-session opt-out markers (no SessionEnd hook
                # exists to clean them up; safe since each only matches its own
                # session_id).
                try:
                    from chameleon_mcp.optouts import reap_stale_session_markers

                    reap_stale_session_markers(repo_id)
                except Exception:
                    pass
                # Same lifecycle for the judge/intent session files, including
                # the retired once-per-session .correctness_judged. markers.
                try:
                    from chameleon_mcp._thresholds import threshold_int
                    from chameleon_mcp.intent_capture import reap_stale_prefixed

                    reap_stale_prefixed(
                        repo_data,
                        SESSION_REAP_PREFIXES,
                        max_age_seconds=threshold_int("INTENT_RETENTION_DAYS") * 86400,
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Opt-out gate: honor .skip / session-disable / pause at SessionStart too,
    # matching PreToolUse/PostToolUse. When suppressed, inject nothing and skip
    # the statusLine write + auto-refresh side effects (a .skip repo opted out).
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.tools import _compute_repo_id

        sup_repo_id = _compute_repo_id(repo_root) if repo_root else None
        if is_chameleon_suppressed(repo_root, sup_repo_id, session_id) is not None:
            _emit({})
            return 0
    except Exception:
        pass

    # Detect a code upgrade (the running package path differs from the installed
    # one) and stop the stale daemon. This runs unconditionally: a workspace whose
    # root has no profile still launched the old daemon, and leaving it alive lets
    # the new hooks connect back to stale linting/clustering logic for up to the
    # idle timeout. The detected version is reused for the statusline "update"
    # badge below when a profile exists.
    upgrade_badge: str | None = None
    try:
        import chameleon_mcp as _cm

        running_pkg = Path(_cm.__file__).resolve().parent
        installed_pkg = (Path(plugin_root) / "mcp" / "chameleon_mcp").resolve()
        if running_pkg != installed_pkg:
            installed_init = installed_pkg / "__init__.py"
            installed_version = ""
            if installed_init.is_file():
                for line in installed_init.read_text(encoding="utf-8").splitlines():
                    if line.startswith("__version__"):
                        installed_version = line.split("=", 1)[1].strip().strip("\"'")
                        break
            upgrade_badge = installed_version or "new"
            try:
                from chameleon_mcp.daemon import stop_daemon

                stop_daemon(timeout=2.0)
            except Exception:
                pass
    except Exception:
        pass

    try:
        from chameleon_mcp.profile.trust import hash_profile, trust_state_for

        # Place the statusline cache under the repo root, not the launch cwd. The
        # statusline script reads it at the repo root, so a subdir-launched session
        # (e.g. claude started from repo/tests) must still write there.
        sl_base = repo_root if repo_root else Path.cwd()
        cache_dir = sl_base / ".claude"
        sl_cache = cache_dir / ".chameleon-statusline-cache"
        profiles: list[dict] = []

        def _trust_for(root: Path) -> str:
            rid = _compute_repo_id(root)
            ts = trust_state_for(rid)
            if ts is None or not ts.grants_root(root):
                # ungranted workspace under a monorepo-shared repo_id
                return "untrusted"
            pdir = root / ".chameleon"
            if pdir.is_dir():
                cur = hash_profile(pdir)
                expected = ts.hash_for_root(root)
                if cur and expected != cur:
                    return "stale"
            return "trusted"

        has_own_profile = repo_root and (repo_root / ".chameleon" / "profile.json").is_file()
        if has_own_profile:
            profiles.append({"name": repo_root.name, "trust": _trust_for(repo_root)})
        else:
            # Scan from the repo root, not the launch cwd, so sibling-workspace
            # profiles are discovered when Claude is launched from a subdirectory.
            scan_dir = repo_root if repo_root else Path.cwd()
            try:
                children = sorted(scan_dir.iterdir())
            except OSError:
                children = []
            for child in children:
                try:
                    if child.is_dir() and (child / ".chameleon" / "profile.json").is_file():
                        child_root = find_repo_root(child)
                        if child_root:
                            profiles.append(
                                {
                                    "name": child_root.name,
                                    "trust": _trust_for(child_root),
                                }
                            )
                except Exception:
                    pass

        if profiles:
            cache_data: dict = {"profiles": profiles}
            if upgrade_badge:
                cache_data["update"] = upgrade_badge
            cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write_text(sl_cache, json.dumps(cache_data))
    except Exception:
        pass

    # Wire the statusline command into the repo root's settings, matching the
    # cache placement above: Claude resolves project settings at the workspace
    # root, so a subdir-launched session must not split them into the subdir.
    _wire_statusline_settings(repo_root if repo_root else Path.cwd(), plugin_root)

    conventions_block = ""
    try:
        from chameleon_mcp.conventions import format_conventions_for_session

        if repo_root and (repo_root / ".chameleon" / "conventions.json").is_file():
            # Trust gate: conventions.json + principles.md are attacker-controllable
            # committed content, so don't inject an untrusted profile's conventions
            # into trusted system context (stale still injects, matching the
            # canonical path and the documented contract). And route the assembled
            # block through the context sanitizer — every other injection site does,
            # this one historically did not, leaving a tag-boundary injection vector
            # through principles.md prose / convention values.
            from chameleon_mcp.profile.trust import trust_state_for
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context
            from chameleon_mcp.tools import _compute_repo_id

            _ss_rec = trust_state_for(_compute_repo_id(repo_root))
            if _ss_rec is not None and _ss_rec.grants_root(repo_root):
                import json as _conv_json

                conv_text = (repo_root / ".chameleon" / "conventions.json").read_text(
                    encoding="utf-8"
                )
                conv_data = _conv_json.loads(conv_text)
                pr_text = ""
                pr_path = repo_root / ".chameleon" / "principles.md"
                if pr_path.is_file():
                    pr_text = pr_path.read_text(encoding="utf-8")
                # Sanitize the attacker-controllable inputs at the boundary (see
                # _sanitize_profile_obj — sanitizing the assembled block would
                # mangle its <chameleon-conventions> wrapper).
                conventions_block = format_conventions_for_session(
                    _sanitize_profile_obj(conv_data),
                    principles_text=sanitize_for_chameleon_context(pr_text),
                )
    except Exception:
        pass

    wrapped_parts = [
        "<chameleon-context>",
        "You have chameleon, a profile-aware coding assistant.",
        "",
        "Below is the full content of your `using-chameleon` skill. Follow it.",
        "",
        skill_content,
    ]
    if conventions_block:
        wrapped_parts.append("")
        wrapped_parts.append(conventions_block)
    if drift_banner:
        wrapped_parts.append("")
        wrapped_parts.append(drift_banner)
    production_banner = _production_tip_banner(repo_root or Path.cwd(), session_id=session_id)
    if production_banner:
        wrapped_parts.append("")
        wrapped_parts.append(production_banner)
    judge_health_banner = _judge_spawn_health_banner(repo_root or Path.cwd(), session_id=session_id)
    if judge_health_banner:
        wrapped_parts.append("")
        wrapped_parts.append(judge_health_banner)
    interpreter_banner = _interpreter_degraded_banner(
        repo_root or Path.cwd(), session_id=session_id
    )
    if interpreter_banner:
        wrapped_parts.append("")
        wrapped_parts.append(interpreter_banner)
    wrapped_parts.append("</chameleon-context>")
    wrapped = "\n".join(wrapped_parts)

    _emit_session_context(wrapped)

    # Reuse the repo root already resolved above instead of re-deriving it from
    # cwd inside the helper.
    _maybe_auto_refresh(repo_root or Path.cwd())

    return 0


def preflight_and_advise() -> int:
    """PreToolUse Edit/Write/NotebookEdit: inject canonical context.

    Reads tool_input.file_path, calls chameleon_mcp.tools.get_pattern_context,
    emits the result as additionalContext.

    Fast path (Phase 4.5): try the long-lived daemon at
    ``${PLUGIN_DATA}/.daemon-<version>.sock``. The daemon holds the python import
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
        would_block: bool = False,
        rule: str | None = None,
        file_rel: str | None = None,
        line: int | None = None,
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
                would_block=would_block,
                rule=rule,
                file_rel=file_rel,
                line=line,
            )
        except Exception:
            pass

    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0

    tool_input = _as_dict(payload.get("tool_input"))
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    # A non-string path (malformed payload) must fail open silently here, not
    # surface as a TypeError in the error log doctor reads.
    if not isinstance(file_path, str) or not file_path:
        _emit({})
        return 0

    repo_id_hint: str | None = None
    repo_root_path: Path | None = None
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
        pass

    result: dict | None = None
    try:
        from chameleon_mcp import daemon_client

        result = daemon_client.call("get_pattern_context", {"file_path": file_path})
    except Exception:
        result = None

    if result is not None:
        _profile_status = (result.get("data") or {}).get("repo", {}).get("profile_status")
        if _profile_status in (
            "profile_corrupted",
            "profile_unsupported_schema_version",
            "no_profile",
        ):
            result = None

    if result is None:
        try:
            from chameleon_mcp.daemon import ensure_daemon_async

            ensure_daemon_async()
        except Exception:
            pass
        try:
            from chameleon_mcp.tools import get_pattern_context

            result = get_pattern_context(file_path)
        except Exception as exc:
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
    archetype_name = archetype_obj.get("archetype")

    # PreToolUse deny, credential class: a deterministic hard-kind secret in
    # the PROPOSED content (Write content / Edit new_string / NotebookEdit
    # new_source) is denied before it reaches disk. This runs before the
    # no-archetype early-return and carries no match-quality/confidence gate —
    # a hardcoded credential is a credential no matter which archetype (if
    # any) the file resolves to, and a brand-new unarchetyped config file is
    # the most common leak target. The enforcement spine still applies: only a
    # trusted profile may block, the rule must be calibration-active, enforce
    # denies while shadow records the would-block row and falls through, and
    # mode=off / CHAMELEON_ENFORCE=0 disable. would_block rows are emitted in
    # BOTH shadow and enforce so the shadow report and /chameleon-explain see
    # deny frequency. Fail-open: any error leaves the advisory path untouched.
    # The active block set computed here is reused by the import deny below so
    # the enforcement.json read is not doubled per edit.
    active_rules: set[str] | None = None
    try:
        if (
            os.environ.get("CHAMELEON_ENFORCE") != "0"
            and trust_state == "trusted"
            and repo_root_path is not None
        ):
            proposed = (
                tool_input.get("new_string")
                or tool_input.get("content")
                or tool_input.get("new_source")
                or ""
            )
            if proposed and isinstance(proposed, str):
                from chameleon_mcp.enforcement_calibration import active_block_rules

                profile_dir = repo_root_path / ".chameleon"
                active_rules = active_block_rules(profile_dir)
                if "secret-detected-in-content" in active_rules:
                    session_id = payload.get("session_id")
                    repo_id = repo_info.get("id") or repo_id_hint
                    hard, named_suppressed = _proposed_hard_secret_violations(
                        proposed,
                        file_path,
                        tool_name=str(payload.get("tool_name") or ""),
                    )
                    if named_suppressed:
                        _record_overrides(
                            repo_id,
                            [{"rule": "secret-detected-in-content"}],
                            archetype=archetype_name,
                            file_rel=_repo_rel(repo_root_path, file_path),
                            session_id=session_id,
                            blanket=False,
                        )
                    if hard:
                        from chameleon_mcp.profile.config import load_config
                        from chameleon_mcp.violation_class import violation_line

                        mode = load_config(profile_dir).enforcement.mode
                        if mode in ("shadow", "enforce"):
                            for v in hard[:3]:
                                _metric(
                                    advisory_emitted=(mode == "shadow"),
                                    repo_id=repo_id,
                                    archetype=archetype_name,
                                    would_block=True,
                                    rule="secret-detected-in-content",
                                    file_rel=_repo_rel(repo_root_path, file_path),
                                    line=violation_line(v),
                                )
                        if mode == "enforce":
                            if archetype_name:
                                # Same re-show rationale as the import deny: the
                                # normal seen-set seeding below is skipped by this
                                # early return.
                                _seed_archetype_seen(repo_id, session_id, archetype_name)
                            parts = []
                            for v in hard[:3]:
                                kind = v.get("secret_kind") or "credential"
                                line = violation_line(v)
                                parts.append(f"{kind} at line {line}" if line else kind)
                            summary = "; ".join(parts)
                            if len(hard) > 3:
                                summary += f" (+{len(hard) - 3} more)"
                            from chameleon_mcp.sanitization import (
                                sanitize_for_chameleon_context,
                            )

                            # The summary carries only the secret kind and line —
                            # scanner hits redact the matched token, so the deny
                            # reason can never echo the credential back.
                            _emit_pretool_deny(
                                sanitize_for_chameleon_context(
                                    "chameleon: hardcoded credential in the proposed "
                                    f"content: {summary}. Rotate any real credential "
                                    "and load it from an environment variable or "
                                    "secret manager. If this is a known-fake fixture "
                                    "value, add "
                                    f"{_ignore_hint(file_path, 'secret-detected-in-content')} "
                                    "on the offending line; a bare chameleon-ignore "
                                    "does not cover credentials."
                                )
                            )
                            return 0
    except Exception:
        pass

    if not archetype_name:
        repo_id = repo_info.get("id") or repo_id_hint
        _metric(advisory_emitted=False, repo_id=repo_id, trust_state=trust_state)
        _emit({})
        return 0

    repo_id = repo_info.get("id")
    confidence_band = archetype_obj.get("confidence_band")
    # The drift observation for this edit is recorded ONCE, by posttool_verify:
    # the post-edit hook sees the file as actually written (this hook fires
    # even when the edit is subsequently denied or fails) and also covers the
    # no-archetype branch. Recording here too doubled every drift statistic.

    session_id = payload.get("session_id")
    if trust_state == "untrusted" and repo_id:
        if _should_emit_untrusted_prompt(repo_id, session_id):
            block = (
                "<chameleon-context>\n"
                "[🦎 chameleon: profile present, untrusted]\n\n"
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

    excerpt_content = canonical.get("content") or ""
    rules_count = len(data.get("rules") or [])
    idioms_text = data.get("idioms") or ""
    has_idioms = bool(idioms_text.strip())
    match_quality = archetype_obj.get("match_quality") or "unknown"
    sub_buckets_count = archetype_obj.get("sub_buckets_count") or 0
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    safe_name = sanitize_for_chameleon_context(archetype_name or "")
    safe_band = sanitize_for_chameleon_context(confidence_band or "unknown")
    safe_match = sanitize_for_chameleon_context(str(match_quality))

    # PreToolUse deny: a banned import in the PROPOSED content blocks the write
    # before it lands, but only when the repo opted into enforcement, calibration
    # marked the rule safe to block here, and the archetype was AST-confirmed at
    # high or medium confidence. Only a "trusted" grant denies; a "stale" profile (its hash
    # drifted from the granted one) stays advisory, since the conventions it would
    # enforce were never re-reviewed. Untrusted repos already returned above.
    # Shadow mode records a would_block metric and falls through to the advisory.
    # Fail-open: any error leaves the advisory path untouched.
    try:
        if (
            os.environ.get("CHAMELEON_ENFORCE") != "0"
            and trust_state == "trusted"
            and repo_root_path is not None
        ):
            from chameleon_mcp.lint_engine import detect_language
            from chameleon_mcp.prewrite_lint import banned_imports_in_content
            from chameleon_mcp.profile.config import load_config

            profile_dir = repo_root_path / ".chameleon"
            if active_rules is None:
                # The secret deny above skipped its read (empty proposed
                # content, or it raised); resolve the calibrated set here.
                from chameleon_mcp.enforcement_calibration import active_block_rules

                active_rules = active_block_rules(profile_dir)
            if "import-preference-violation" in active_rules:
                proposed = tool_input.get("new_string") or tool_input.get("content") or ""
                # Gate on a confident archetype match. "ast" is the structural
                # match on existing content; "exact" is the path-based match a
                # brand-new file (Write target, no content on disk yet) resolves
                # to -- a stronger signal, not a weaker one. Excluding "exact"
                # let every new file slip the deny, which is exactly where the
                # model most often introduces a banned import.
                if (
                    proposed
                    and match_quality in ("ast", "exact")
                    and confidence_band in ("high", "medium")
                ):
                    conv_path = profile_dir / "conventions.json"
                    conv = (
                        json.loads(conv_path.read_text(encoding="utf-8")).get("conventions", {})
                        if conv_path.is_file()
                        else {}
                    )
                    banned = banned_imports_in_content(
                        proposed,
                        language=detect_language(file_path),
                        archetype=archetype_name,
                        conventions=conv,
                    )
                    from chameleon_mcp.violation_class import ignored_rules

                    ign = ignored_rules(proposed, file_path=file_path) or set()
                    if banned and ("" in ign or "import-preference-violation" in ign):
                        # The directive bypasses the deny. Record the override so
                        # the audit sees a bypass at the deny gate too, not only
                        # at the PostToolUse verifier. A bare directive (empty
                        # string in the set) is the blanket form.
                        _record_overrides(
                            repo_id,
                            [{"rule": "import-preference-violation"}],
                            archetype=archetype_name,
                            file_rel=_repo_rel(repo_root_path, file_path),
                            session_id=session_id,
                            blanket="" in ign,
                        )
                    if banned and not ("" in ign or "import-preference-violation" in ign):
                        mode = load_config(profile_dir).enforcement.mode
                        if mode == "enforce":
                            # The message is built from attacker-controllable
                            # conventions.json values, so sanitize it before it
                            # lands in the deny reason fed back to the model
                            # (parity with the advisory additionalContext path).
                            msg = sanitize_for_chameleon_context(
                                banned[0].get("message", "banned import")
                            )
                            # Record the archetype as seen even though we deny:
                            # the normal seen-set seeding below is skipped by this
                            # early return, and without it a later successful edit
                            # to the same archetype would re-show the verbose
                            # first-in-archetype advisory.
                            _seed_archetype_seen(repo_id, session_id, archetype_name)
                            _emit_pretool_deny(
                                f"chameleon: {msg}. Use the preferred import, or add "
                                f"{_ignore_hint(file_path, 'import-preference-violation')} "
                                "if intentional."
                            )
                            return 0
                        if mode == "shadow":
                            # Record the would-block counter, then fall through to
                            # the advisory path. The advisory emits its own metric;
                            # this would_block row is a distinct counter, so a shadow
                            # would-block edit produces two rows by design. The rule
                            # and file:line attribute the row so the shadow report
                            # can sample the off-pattern import for spot-check.
                            _metric(
                                advisory_emitted=True,
                                repo_id=repo_id,
                                archetype=archetype_name,
                                would_block=True,
                                rule="import-preference-violation",
                                file_rel=_repo_rel(repo_root_path, file_path),
                                line=banned[0].get("line"),
                            )
    except Exception:
        pass

    enforcement_state = None
    try:
        from chameleon_mcp import enforcement

        repo_data = _plugin_data_dir() / repo_id if repo_id else None
        if repo_data and session_id:
            enforcement_state = enforcement.load_state(repo_data, session_id)
    except Exception:
        pass

    first_in_archetype = True
    has_violations = False
    if enforcement_state is not None:
        first_in_archetype = archetype_name not in enforcement_state.archetypes_seen
        has_violations = archetype_name in enforcement_state.archetypes_with_violations
        enforcement_state.archetypes_seen.add(archetype_name)
        try:
            enforcement.save_state(enforcement_state, repo_data, session_id)
        except Exception:
            pass

    summary = archetype_obj.get("summary", "")
    use_tier2 = first_in_archetype or has_violations or not summary

    if not use_tier2:
        block = f"<chameleon-context>\n[🦎 chameleon: {safe_name} ({safe_band})]\n"
        if summary:
            block += f"{sanitize_for_chameleon_context(summary)}\n"
        conv_echo = ""
        try:
            from chameleon_mcp.conventions import format_conventions_echo

            conventions_path = (
                repo_root_path / ".chameleon" / "conventions.json" if repo_root_path else None
            )
            if conventions_path and conventions_path.is_file():
                conv_data = json.loads(conventions_path.read_text(encoding="utf-8"))
                pr_text = ""
                pr_path = repo_root_path / ".chameleon" / "principles.md"
                if pr_path.is_file():
                    try:
                        pr_text = pr_path.read_text(encoding="utf-8")
                    except OSError:
                        pass
                # Sanitize attacker-controllable inputs at the boundary, for
                # parity with the SessionStart path (the assembled echo carries a
                # <chameleon-conventions> wrapper the output-sanitizer would mangle).
                conv_echo = format_conventions_echo(
                    _sanitize_profile_obj(conv_data),
                    archetype=archetype_name,
                    principles_text=sanitize_for_chameleon_context(pr_text),
                )
        except Exception:
            pass
        if conv_echo:
            block += f"{conv_echo}\n"
        block += "</chameleon-context>"
        _metric(
            advisory_emitted=True,
            repo_id=repo_id,
            trust_state=trust_state,
            archetype=archetype_name,
            confidence=confidence_band,
        )
        _emit_chameleon_context(block)
        _update_statusline(
            f"{safe_name} ({safe_band})",
            repo_name=repo_root_path.name if repo_root_path else None,
            trust_state=trust_state,
            repo_root=repo_root_path,
        )
        return 0

    block = (
        "<chameleon-context>\n"
        f"[🦎 chameleon: archetype={safe_name}, "
        f"confidence={safe_band}, "
        f"match_quality={safe_match}, "
        f"sub_buckets={int(sub_buckets_count)}]\n\n"
    )
    if trust_state == "stale":
        block += (
            "**Trust is stale**: a recent /chameleon-refresh, /chameleon-teach, "
            "or manual edit changed the committed profile after the trust grant. "
            "Trust is tied to the profile sha, so the grant no longer covers the "
            "current profile. Suggest /chameleon-trust to re-confirm. Do not block "
            "the edit; chameleon advisory is provided below for reference only.\n\n"
        )
    if excerpt_content:
        block += "Canonical witness:\n```\n"
        block += excerpt_content
        block += "\n```\n\n"
    if canonical.get("missing"):
        block += (
            f"(canonical witness {sanitize_for_chameleon_context(str(canonical.get('witness_path')))} is "
            "missing on disk; run /chameleon-refresh to re-select)\n\n"
        )
    if has_idioms:
        # Inline the team idioms (already loaded + sanitized by get_pattern_context)
        # instead of pointing at another tool call — they are the highest-signal,
        # repo-specific guidance and were previously discarded to a boolean.
        block += "Team idioms (captured via /chameleon-teach):\n"
        block += idioms_text.rstrip() + "\n\n"
    if rules_count:
        # Rules are verbose lint/formatter config; keep the pointer rather than
        # flooding the block, but inline the idioms above. Rules are repo-global
        # (scoped by source, not by archetype), so the pointer names the repo,
        # not the archetype, to avoid a failed lookup.
        block += (
            f"Rules: {rules_count} repo-wide lint/format rules apply — "
            "call get_rules with this repo's path or id for the config.\n"
        )
    try:
        from chameleon_mcp.conventions import format_directory_listing

        dir_listing = format_directory_listing(file_path)
        if dir_listing:
            block += f"\n{dir_listing}\n"
    except Exception:
        pass
    block += "</chameleon-context>"

    _metric(
        advisory_emitted=True,
        repo_id=repo_id,
        trust_state=trust_state,
        archetype=archetype_name,
        confidence=confidence_band,
    )
    _emit_chameleon_context(block)
    _update_statusline(
        f"{safe_name} ({safe_band})",
        repo_name=repo_root_path.name if repo_root_path else None,
        trust_state=trust_state,
        repo_root=repo_root_path,
    )
    return 0


# A Bash command can write a file the same way an Edit tool does, but only
# Edit/Write/NotebookEdit reach posttool_verify. The shapes below are the ones a
# single literal target can be read straight off the command line:
#   foo > path        foo >> path        foo | tee path        sed -i ... path
# The target must be one unquoted (or simply-quoted) literal word — no globs, no
# variables, no command substitution. Anything ambiguous yields no target and the
# recorder stays as cheap as it is for a write-free command.
#
# Out of scope, by construction (the target is not a literal command-line word):
#   - git apply / git am: the paths live inside the patch body, not the argv.
#   - heredoc piped onward (cat <<EOF | ...): the sink is another command, not a
#     literal file word.
#   - codegen / scaffolding tools: the written paths are computed at runtime.
# These produce no extractable target and are simply skipped.
_REDIRECT_TARGET_RE = re.compile(
    r"""
    (?:^|\s|;|&|\|)          # start, whitespace, or a shell separator
    (?:\d*)                  # optional leading fd number (e.g. 2>)
    >>?                      # > or >>
    \s*
    (?:&\d+)?               # not a file: a >&N fd dup — captured to be rejected
    (?P<target>
        "(?:[^"\\]|\\.)*"    # double-quoted literal
        | '[^']*'           # single-quoted literal
        # bare word: a run of non-metachar chars, with `\<char>` escapes kept
        # inline so a backslash-escaped space (`Testing\ Apps`) is part of one
        # word and not a word boundary. _unquote_target un-escapes them.
        | (?:\\.|[^\s;&|<>()`$*?\[\]{}~\\])+
    )
    # A bare word immediately followed by an expansion/glob metachar (e.g.
    # `out.$EXT`) is not a literal path; require a boundary after it so a
    # partial prefix is never extracted as a target.
    (?![^\s;&|<>()`])
    """,
    re.VERBOSE,
)

# `tee FILE...` and `tee -a FILE...`: capture the first literal file operand.
_TEE_TARGET_RE = re.compile(
    r"""
    (?:^|\||;|&|\s)\s*tee\b
    (?P<flags>(?:\s+-{1,2}[A-Za-z-]+)*)   # optional flags (-a, --append, ...)
    \s+
    (?P<target>
        "(?:[^"\\]|\\.)*"
        | '[^']*'
        # bare word: first char is neither a metachar nor a leading `-` (a flag);
        # `\<char>` escapes are kept inline so an escaped space is part of the word.
        | (?:\\.|[^\s;&|<>()`$*?\[\]{}~\\-])(?:\\.|[^\s;&|<>()`$*?\[\]{}~\\])*
    )
    (?![^\s;&|<>()`])
    """,
    re.VERBOSE,
)

# `sed -i ... FILE` / `sed -i.bak ... FILE`: the in-place flag means the trailing
# operand is the file mutated on disk. GNU and BSD spell the suffix differently
# (`-i` vs `-i.bak`/`-i ''`), so accept either and take the LAST literal operand
# as the target — sed's file argument is always last.
_SED_INPLACE_RE = re.compile(r"(?:^|\||;|&|\s)\s*sed\b[^|;&]*?\s-i")
_SED_OPERAND_RE = re.compile(
    r"""
    (?P<operand>
        "(?:[^"\\]|\\.)*"
        | '[^']*'
        | (?:\\.|[^\s;&|<>()`$*?\[\]{}~\\])+
    )
    """,
    re.VERBOSE,
)


def _unquote_target(raw: str) -> str | None:
    """Strip one matching pair of surrounding quotes from an extracted operand.

    Returns None for an operand that still carries a shell metachar after
    unquoting (a variable, a substitution, a glob that slipped past the bare-word
    class), so an ambiguous target never reaches path resolution.
    """
    s = raw.strip()
    if not s:
        return None
    quoted = (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")
    if quoted:
        s = s[1:-1]
    else:
        # An unquoted word carries shell escapes (`Testing\ Apps`, `a\$b`).
        # Collapse each `\<char>` to its literal char so the on-disk path is
        # recovered. A trailing lone backslash is unparseable; bail.
        if s.endswith("\\") and not s.endswith("\\\\"):
            return None
        s = re.sub(r"\\(.)", r"\1", s)
    if not s:
        return None
    # A literal must not contain expansion or glob metachars after unquoting; if
    # it does, treat it as unparseable rather than guessing the on-disk path.
    if any(c in s for c in "$`*?[]{}~\n"):
        return None
    return s


def _extract_bash_write_targets(command: str) -> list[str]:
    """Pure-regex pre-filter: extract single-literal-target file-write paths.

    Returns the de-duplicated literal targets of ``>``/``>>`` redirects, the
    first operand of ``tee``, and the file operand of ``sed -i``. Returns an
    empty list whenever no clear single target is present — the common case for a
    write-free Bash command — so the caller can bail before any profile load.

    This never resolves, stats, or opens a path; it only reads the command
    string. Path resolution and the trust gate happen in the caller.
    """
    if not command or not isinstance(command, str):
        return []
    if len(command) > 8192:
        # An unusually long command is almost never a single-target write; cap
        # the regex work so a pathological input can't stall the hook.
        return []

    targets: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if raw is None:
            return
        resolved = _unquote_target(raw)
        if resolved and resolved not in seen:
            seen.add(resolved)
            targets.append(resolved)

    # A `>&N` fd duplication (2>&1) is a redirect to a file descriptor, not a
    # file; the optional fd group inside the pattern consumes the `&N` so no
    # literal target matches, and those forms are skipped without a separate
    # guard here.
    for m in _REDIRECT_TARGET_RE.finditer(command):
        _add(m.group("target"))

    for m in _TEE_TARGET_RE.finditer(command):
        _add(m.group("target"))

    if _SED_INPLACE_RE.search(command):
        # sed's mutated file is its last operand. Scan only the sed segment so a
        # later piped command's argument is not mistaken for the file. The split
        # must ignore separators inside quotes: the most common sed scripts put
        # `|` (alternate delimiter), `;` (chained commands), and `&` (matched-text
        # backreference) INSIDE the quoted script, and splitting there used to
        # hand back a script fragment instead of the file — leaving the written
        # file invisible to the Stop backstop.
        seg = _split_outside_quotes(command)
        for part in seg:
            if re.search(r"\bsed\b.*\s-i", part):
                operands = _SED_OPERAND_RE.findall(part)
                if operands:
                    _add(operands[-1])

    return targets


def _split_outside_quotes(command: str) -> list[str]:
    """Split a shell command on top-level ``;``/``&``/``|`` only.

    Quoted spans (single or double) and backslash-escaped characters stay
    intact, mirroring how the shell itself tokenizes — a separator inside a
    quoted sed script is script content, not a command boundary.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in ";&|":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _record_bash_write_mutations(
    command: str,
    cwd: Path,
    session_id: str,
) -> None:
    """Mark in-repo TS/Ruby files written by a Bash command into enforcement state.

    Mirrors the recording half of posttool_verify: resolve the target's
    archetype, run the same in-process lint, and write the result into the
    session's EnforcementState.files so the existing Stop backstop re-lints and
    can block on an unresolved hard violation — exactly as it does for an edited
    file. This path is advisory-by-default: it never emits a block itself; it only
    arms the same state the calibrated Stop gate consumes.

    Fails open throughout. Any unresolved target, untrusted profile, or sub-lint
    error simply contributes nothing rather than aborting the recorder.
    """
    targets = _extract_bash_write_targets(command)
    if not targets:
        return

    from chameleon_mcp.lint_engine import detect_language
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for

    now = time.time()

    for target in targets:
        try:
            p = Path(target)
            if not p.is_absolute():
                p = cwd / p
            p = p.expanduser()
        except (OSError, ValueError):
            continue

        # Only TS/Ruby files can resolve to an archetype; skip the rest before
        # any stat so a write to a log/data file costs nothing.
        if detect_language(p.name) is None:
            continue
        try:
            if not p.is_file():
                continue
        except OSError:
            continue

        try:
            repo_root = find_repo_root(p)
        except Exception:
            repo_root = None
        if repo_root is None:
            continue

        # The trust gate is the security boundary, same as posttool_verify: a
        # never-trusted profile is attacker-controllable, so its conventions must
        # not drive enforcement state. The written file must also resolve under
        # the repo whose profile we are about to apply.
        try:
            from chameleon_mcp.tools import _compute_repo_id

            target_repo_id = _compute_repo_id(repo_root)
            rec = trust_state_for(target_repo_id)
            if rec is None or not rec.grants_root(repo_root):
                continue
            # The file must resolve under its own repo root. relative_to raises
            # ValueError when the target escaped the boundary (a symlink out, a
            # `..` traversal), so refuse rather than enforce across repos.
            p.resolve().relative_to(repo_root.resolve())
        except Exception:
            continue

        file_path = str(p)
        try:
            content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
        except OSError:
            continue

        try:
            from chameleon_mcp.tools import get_archetype

            arch_result = get_archetype(str(repo_root), file_path)
            archetype_name = (arch_result.get("data") or {}).get("archetype")
        except Exception:
            archetype_name = None
        if not archetype_name:
            # No archetype: the convention/AST lints have nothing to compare
            # against, but a secret or eval in a bash-written file is a content
            # fact that still needs the Stop backstop. Mirror the Edit path's
            # no-archetype handling and record any deterministic-hard hit under the
            # synthetic label so the backstop re-lints and blocks it.
            try:
                indep = _scan_archetype_independent(content, file_path)
            except Exception:
                indep = []
            if not indep:
                continue
            violations = indep
            record_archetype = _NO_ARCHETYPE_LABEL
        else:
            try:
                violations = _lint_file_in_process(repo_root, archetype_name, content, file_path)
            except Exception:
                violations = []
            if not violations:
                continue
            record_archetype = archetype_name

        # Partition the hard (block-eligible) subset exactly as posttool_verify
        # does, so the cached blockable_unresolved flag the Stop backstop reads is
        # set only when a calibrated block-eligible rule actually fired here.
        try:
            from chameleon_mcp.enforcement_calibration import active_block_rules
            from chameleon_mcp.violation_class import (
                build_ignore_index,
                hard_class_violations,
                is_archetype_independent,
                is_violation_ignored,
            )

            active = active_block_rules(repo_root / ".chameleon")
            hard = hard_class_violations(violations, active)
            # Without an archetype only the archetype-independent hard rules (a
            # deterministic secret) can be enforced at Stop, so record only those
            # as blockable here -- matching the backstop's no-archetype re-lint.
            if record_archetype == _NO_ARCHETYPE_LABEL:
                hard = [v for v in hard if is_archetype_independent(v.get("rule"))]
            idx = build_ignore_index(content, file_path=file_path)
            if idx is not None:
                hard = [v for v in hard if not is_violation_ignored(v, idx)]
        except Exception:
            hard = []

        try:
            from chameleon_mcp.enforcement import (
                FileState,
                load_state,
                record_violation,
                save_state,
            )

            # State is keyed by the WRITTEN file's repo, not the command's cwd
            # repo, so the Stop backstop (which loads by the file's repo_id)
            # finds and re-lints it. A Bash write can target a repo other than
            # the one the command ran in.
            repo_data_dir = _plugin_data_dir() / target_repo_id
            state = load_state(repo_data_dir, session_id or "")
            fs = state.files.get(file_path)
            if fs is None:
                fs = FileState()
                state.files[file_path] = fs
            record_violation(
                fs,
                now=now,
                archetype=record_archetype,
                hard_class=bool(hard),
            )
            state.archetypes_with_violations.add(record_archetype)
            save_state(state, repo_data_dir, session_id or "")
        except Exception:
            pass


def posttool_recorder() -> int:
    """PostToolUse Bash: HMAC-signed exec log."""
    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0

    tool_input = _as_dict(payload.get("tool_input"))
    tool_response = _as_dict(payload.get("tool_response"))
    command = tool_input.get("command", "")
    session_id = payload.get("session_id", "unknown")
    exit_code = tool_response.get("returnCode") if isinstance(tool_response, dict) else None

    cwd_raw = payload.get("cwd")
    cwd_str = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else os.getcwd()
    try:
        cwd = Path(cwd_str).resolve()
    except (OSError, ValueError):
        cwd = Path(os.getcwd())
    try:
        from chameleon_mcp.tools import _compute_repo_id

        repo_id = _compute_repo_id(cwd)
    except Exception:
        repo_id = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()

    try:
        from chameleon_mcp.exec_log import append_exec_log, classify_test_command

        # Classify against the test-runner allow-list while the body is still in
        # hand; only the boolean is persisted, never the command itself.
        test_seen = classify_test_command(command)
        append_exec_log(
            repo_id=repo_id,
            session_id=session_id,
            command=command,
            exit_code=int(exit_code) if exit_code is not None else -1,
            test_command_seen=test_seen,
        )
    except Exception:
        pass

    # A file written via `cat > foo.ts`, `tee`, or `sed -i` never reaches
    # posttool_verify (it matches only Edit/Write/NotebookEdit), so the same
    # convention/phantom/secret lint that gates an edited file would silently
    # skip it. Mark single-literal-target TS/Ruby writes into the same
    # enforcement state the Stop backstop re-lints. The pre-filter inside
    # bails before any profile load when there is no clear target, so a
    # write-free Bash command stays as cheap as the HMAC append above. Honors
    # CHAMELEON_VERIFY=0 like the Edit verifier, and fails open on any error.
    if os.environ.get("CHAMELEON_VERIFY") != "0":
        try:
            _record_bash_write_mutations(command, cwd, session_id)
        except Exception:
            pass

    _emit({})
    return 0


_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})
_VERIFY_SEEN_TTL_SECONDS = 30


def _lint_file_in_process(
    repo_root: Path,
    archetype_name: str,
    content: str,
    file_path: str,
    loaded=None,
) -> list[dict]:
    """Run the archetype AST-shape, convention, and phantom-import lints against
    ``content`` in-process and return the merged violation dicts.

    This is the same orchestration posttool_verify uses on its non-daemon path,
    factored out so the Stop backstop can re-lint a file live without the daemon
    dependency. Each sub-lint fails open: a sub-lint that raises contributes no
    violations rather than aborting the whole re-check.

    ``loaded`` lets a caller that re-checks many files in one pass (the Stop
    backstop) load the profile once and reuse it, avoiding a per-file profile
    re-read for every candidate.
    """
    from chameleon_mcp.lint_engine import (
        detect_language,
        extract_dimensions,
        lint,
        lint_conventions,
        recalibrate_ast_query,
    )

    if loaded is None:
        from chameleon_mcp.profile.loader import load_profile_dir
        from chameleon_mcp.tools import _effective_profile_dir

        loaded = load_profile_dir(_effective_profile_dir(repo_root))
    canonicals = (loaded.canonicals.get("canonicals") or {}).get(archetype_name) or []
    ast_query: dict | None = None
    # Witness content, captured from the single read below, so the test-quality
    # pass can self-calibrate its assertion-helper / stub / freeze checks to the
    # team's own test style without a second disk read on the hot path.
    witness_content: str | None = None
    if canonicals:
        first = canonicals[0] or {}
        ast_query = (first.get("normative_shape") or {}).get("ast_query")
        witness_rel = (first.get("witness") or {}).get("path")
        if witness_rel:
            w_full = repo_root / witness_rel
            if w_full.is_file():
                w_raw = w_full.read_bytes()[:100_000].decode("utf-8", errors="replace")
                witness_content = w_raw
                if ast_query:
                    w_lang = detect_language(witness_rel)
                    w_snap = extract_dimensions(w_raw, language=w_lang, file_path=witness_rel)
                    ast_query = recalibrate_ast_query(w_snap)
    language = detect_language(file_path)
    violations: list[dict] = []
    if ast_query:
        snapshot = extract_dimensions(content, language=language, file_path=file_path)
        violations = [v.to_dict() for v in lint(snapshot, ast_query)]

    try:
        conv_data = (
            loaded.conventions.get("conventions", {}) if hasattr(loaded, "conventions") else {}
        )
        arch_conv: dict = {}
        if conv_data.get("imports", {}).get(archetype_name):
            arch_conv["imports"] = conv_data["imports"][archetype_name]
        if conv_data.get("naming", {}).get(archetype_name):
            arch_conv["naming"] = conv_data["naming"][archetype_name]
        if conv_data.get("inheritance", {}).get(archetype_name):
            arch_conv["inheritance"] = conv_data["inheritance"][archetype_name]
        # required_guards drives the advisory authz hint. The lint reads it from
        # the per-archetype slice, so it must be copied in alongside the others or
        # the rule can never fire on this path.
        if conv_data.get("required_guards", {}).get(archetype_name):
            arch_conv["required_guards"] = conv_data["required_guards"][archetype_name]
        # The test-quality pass gates on the archetype name but reads no convention
        # keys; lint_conventions early-returns on an empty conventions dict. A
        # test/spec archetype usually has no import/naming/inheritance conventions,
        # so force a non-empty dict for those so the pass fires. Matches lint_file.
        _is_test_arch = isinstance(archetype_name, str) and archetype_name.startswith(
            ("test", "spec")
        )
        if _is_test_arch and not arch_conv:
            arch_conv = {"_test_quality_only": True}
        if arch_conv:
            # Thread archetype_name (enables the test-quality pass on test/spec
            # archetypes), file_path (enables the file-naming check), and the
            # witness content (self-calibrates the test-quality stub/freeze/helper
            # gates), matching the lint_file tool. Keyword-only with None defaults,
            # so guard for an older engine build that lacks them and fall back.
            try:
                conv_violations = lint_conventions(
                    content,
                    arch_conv,
                    language=language,
                    file_path=file_path,
                    archetype_name=archetype_name,
                    witness_content=witness_content,
                )
            except TypeError:
                conv_violations = lint_conventions(content, arch_conv, language=language)
            violations.extend(
                v.to_dict() for v in conv_violations if v.rule != "secret-detected-in-content"
            )
    except Exception:
        pass

    try:
        from chameleon_mcp.phantom_imports import lint_phantom_imports

        violations.extend(
            v.to_dict()
            for v in lint_phantom_imports(
                content,
                file_path=file_path,
                repo_root=repo_root,
                language=language,
                rules=loaded.rules,
            )
        )
    except Exception:
        pass

    # Secrets are archetype-independent and the highest-value deterministic stop
    # there is, but scan_secrets was never run on the hook path (only the
    # lint_file MCP tool called it), so a committed credential reached neither the
    # PostToolUse advisory nor the Stop backstop. Run it here, the single in-process
    # lint orchestrator both paths share, and tag each hit so only the precise
    # deterministic kinds can ever block; entropy/broad-fallback hits stay advisory.
    try:
        from chameleon_mcp.lint_engine import scan_secrets
        from chameleon_mcp.violation_class import tag_secret_hardness

        secret_violations = [v.to_dict() for v in scan_secrets(content)]
        tag_secret_hardness(secret_violations)
        violations.extend(secret_violations)
    except Exception:
        pass

    # Dangerous code sinks (dynamic eval, weak hash, insecure random, SQL string
    # interpolation) are content facts independent of the archetype, like the
    # secret scan above. Only eval-call is block-eligible; the rest stay advisory.
    # Run it here so the deterministic eval() stop reaches both the PostToolUse
    # advisory and the Stop backstop, not only the lint_file MCP tool.
    try:
        from chameleon_mcp.lint_engine import scan_dangerous_sinks

        violations.extend(v.to_dict() for v in scan_dangerous_sinks(content, language=language))
    except Exception:
        pass

    # Style baseline: indent / quote / line-length checked against the repo's own
    # declared formatter config (prettier/rubocop/.editorconfig in rules.json).
    # Archetype-independent like the secret and sink scans, so it gives a sparse
    # repo a maintainability floor even where no archetype resolved. Advisory only.
    try:
        from chameleon_mcp.lint_engine import scan_style_rules

        violations.extend(
            v.to_dict()
            for v in scan_style_rules(
                content,
                language=language,
                rules=loaded.rules,
                file_path=file_path,
                repo_root=repo_root,
            )
        )
    except Exception:
        pass

    return violations


def _load_rules_for_style(repo_root: Path) -> dict | None:
    """Load the profile's rules.json mapping for the style baseline, or None.

    Used on the no-archetype path where the profile is not already loaded. The
    style scan reads only declared formatter-config values and emits its own
    messages (no profile strings reach the model surface), so a plain load is
    enough here; trust gating on this path is handled by the caller. Fails open
    to None so a missing / corrupt profile just skips the style check.
    """
    try:
        from chameleon_mcp.profile.loader import load_profile_dir
        from chameleon_mcp.tools import _effective_profile_dir

        return load_profile_dir(_effective_profile_dir(repo_root)).rules
    except Exception:
        return None


def _scan_archetype_independent(
    content: str, file_path: str, rules: dict | None = None, repo_root: Path | str | None = None
) -> list[dict]:
    """Run only the archetype-independent content lints (secrets, sinks, style).

    A secret is a fact about the content itself, true no matter which archetype
    the file resolved to (or whether it resolved to one at all). When archetype
    resolution fails the convention/AST lints have nothing to compare against
    and are correctly skipped, but the credential scan must still run so a
    leaked token in an unarchetyped file is not invisible. Sinks and style are
    scanned here too, but they surface as advisories on this path: eval-call
    enforcement stays gated on an archetype match. Each sub-scan is wrapped so a
    raising scanner contributes nothing rather than aborting the whole check;
    the caller treats an empty list as "clean".

    ``rules`` is the loaded rules.json mapping. When supplied, the style baseline
    (indent / quote / line-length against the repo's declared formatter config)
    runs too, so a sparse repo with no resolvable archetype still gets style
    feedback. Callers without the profile loaded pass None and skip that check.
    ``repo_root`` lets the style scan honor rubocop's path Exclude globs (it needs
    the repo-relative path); without it an absolute path can't match a glob.
    """
    from chameleon_mcp.lint_engine import detect_language

    language = detect_language(file_path)
    out: list[dict] = []
    try:
        from chameleon_mcp.lint_engine import scan_secrets
        from chameleon_mcp.violation_class import tag_secret_hardness

        secret_violations = [v.to_dict() for v in scan_secrets(content)]
        tag_secret_hardness(secret_violations)
        out.extend(secret_violations)
    except Exception:
        pass
    try:
        from chameleon_mcp.lint_engine import scan_dangerous_sinks

        out.extend(v.to_dict() for v in scan_dangerous_sinks(content, language=language))
    except Exception:
        pass
    if rules is not None:
        try:
            from chameleon_mcp.lint_engine import scan_style_rules

            out.extend(
                v.to_dict()
                for v in scan_style_rules(
                    content,
                    language=language,
                    rules=rules,
                    file_path=file_path,
                    repo_root=repo_root,
                )
            )
        except Exception:
            pass
    return out


# The synthetic archetype label recorded in enforcement state for a file that
# carries an archetype-independent hard violation (a deterministic secret or
# eval) but resolved to no archetype. The Stop backstop reads it back and runs
# the archetype-independent re-lint, so the credential still blocks the turn.
_NO_ARCHETYPE_LABEL = "<no-archetype>"


def _posttool_no_archetype_advisory(
    *,
    repo_root: Path,
    repo_id: str,
    file_path: str,
    violations: list[dict],
    session_id,
    now: float,
) -> bool:
    """Surface archetype-independent violations on a file with no archetype.

    Returns True if it wrote a hook-output object to stdout, so the caller can
    skip its own ``_emit`` and keep the one-object-per-invocation contract. A
    False return (the emit raised) lets the caller fall back to ``_emit({})``.

    The convention/AST escalation ladder needs an archetype, so this path does
    not block inline; it emits the advisory and, when a deterministic-hard secret
    or eval fired, records the file into enforcement state so the Stop backstop
    re-lints and blocks it the same way an archetyped file's secret does. The
    state is keyed by the written file's repo_id (the Stop backstop loads by it).
    Fails open: any error leaves the advisory un-surfaced rather than crashing.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    hard: list[dict] = []
    try:
        from chameleon_mcp.violation_class import is_archetype_independent, is_hard_class

        # Only an archetype-INDEPENDENT hard rule (a deterministic secret) can be
        # enforced without an archetype: the Stop backstop's no-archetype re-lint
        # filters to the same set, since the confidence/match-quality gate an
        # archetype-dependent rule (eval) needs can never pass here. Recording only
        # what the backstop will actually block keeps the two paths consistent.
        hard = [
            v for v in violations if is_hard_class(v) and is_archetype_independent(v.get("rule"))
        ]
        try:
            from chameleon_mcp.violation_class import build_ignore_index, is_violation_ignored

            idx = build_ignore_index(_read_file_for_ignore(file_path), file_path=file_path)
            if idx is not None:
                hard = [v for v in hard if not is_violation_ignored(v, idx)]
        except Exception:
            pass
    except Exception:
        hard = []

    if hard:
        try:
            from chameleon_mcp.enforcement import (
                FileState,
                load_state,
                record_violation,
                save_state,
            )

            repo_data_dir = _plugin_data_dir() / repo_id
            state = load_state(repo_data_dir, session_id or "")
            fs = state.files.get(file_path)
            if fs is None:
                fs = FileState()
                state.files[file_path] = fs
            record_violation(fs, now=now, archetype=_NO_ARCHETYPE_LABEL, hard_class=True)
            state.archetypes_with_violations.add(_NO_ARCHETYPE_LABEL)
            save_state(state, repo_data_dir, session_id or "")
        except Exception:
            pass

    try:
        displayed = _displayable_violations(violations, _read_file_for_ignore(file_path), file_path)
        if not displayed:
            return False
        lines = []
        for i, v in enumerate(displayed):
            msg = sanitize_for_chameleon_context(v.get("message", ""))
            lines.append(f"{i + 1}. {msg}")
        block = (
            f"[🦎 chameleon: {len(displayed)} "
            f"violation{'s' if len(displayed) != 1 else ''}]\n" + "\n".join(lines)
        )
        _emit_posttool_context(f"<chameleon-context>\n{block}\n</chameleon-context>")
        return True
    except Exception:
        return False


def _content_has_hard_secret(content: str, file_path: str | None = None) -> bool:
    """True when a deterministic-hard secret kind fires on this content.

    Only the hard kinds count — the same set the Stop backstop blocks — so the
    scan runs the regex-only fast path (hard kinds only ever originate from
    the deterministic fallback patterns, never from detect-secrets, so
    skipping the full scanner loses nothing and avoids its per-line cost). A
    rule-NAMED chameleon-ignore directive is honored, matching the filtering
    the full lint path applies; the bare blanket form does not cover the hard
    class.
    """
    from chameleon_mcp.lint_engine import scan_hard_secrets
    from chameleon_mcp.violation_class import (
        build_ignore_index,
        is_hard_class,
        is_violation_ignored,
        tag_secret_hardness,
    )

    violations = [v.to_dict() for v in scan_hard_secrets(content)]
    if not violations:
        return False
    tag_secret_hardness(violations)
    hard = [v for v in violations if is_hard_class(v)]
    if not hard:
        return False
    idx = build_ignore_index(content, file_path=file_path)
    if idx is not None:
        hard = [v for v in hard if not is_violation_ignored(v, idx)]
    return bool(hard)


def _proposed_hard_secret_violations(
    proposed: str, file_path: str, *, tool_name: str
) -> tuple[list[dict], bool]:
    """Hard-kind secret violations in proposed content, after ignore filtering.

    Returns ``(violations, named_suppressed)``: the surviving deny-candidate
    rows, plus whether a rule-NAMED directive suppressed at least one
    otherwise-denying hit (the caller records that bypass as an auditable
    override). The scan is capped at PREWRITE_SECRET_SCAN_MAX_CHARS — the same
    100KB ceiling the on-disk lint reads use; content past the cap is left to
    the PostToolUse/Stop scans of the written file. Only NAMED directives can
    clear a hit: the deterministic hard class is blanket-immune (see
    ``violation_class.is_violation_ignored``).

    An Edit/NotebookEdit fragment is not the whole file, so a NAMED file-scope
    directive in the on-disk target is honored too — a fixture file annotated
    once must not deny every later fragment edit. The disk read is lazy
    (deny-candidate path only, never on clean edits) and on-disk line-scoped
    directives are not consulted: fragment line numbers do not map truthfully
    onto file lines.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.lint_engine import scan_hard_secrets
    from chameleon_mcp.violation_class import (
        IgnoreIndex,
        build_ignore_index,
        is_hard_class,
        is_violation_ignored,
        tag_secret_hardness,
    )

    clipped = proposed[: threshold_int("PREWRITE_SECRET_SCAN_MAX_CHARS")]
    violations = [v.to_dict() for v in scan_hard_secrets(clipped)]
    if not violations:
        return [], False
    tag_secret_hardness(violations)
    hard = [v for v in violations if is_hard_class(v)]
    if not hard:
        return [], False
    named_suppressed = False
    idx = build_ignore_index(clipped, file_path=file_path)
    if idx is not None:
        kept = [v for v in hard if not is_violation_ignored(v, idx)]
        named_suppressed = len(kept) < len(hard)
        hard = kept
    if hard and tool_name in ("Edit", "NotebookEdit"):
        disk_idx = build_ignore_index(_read_file_for_ignore(file_path), file_path=file_path)
        if disk_idx is not None and (disk_idx.file_rules or disk_idx.named_anywhere):
            file_scope = IgnoreIndex(
                file_rules=disk_idx.file_rules,
                named_anywhere=disk_idx.named_anywhere,
            )
            kept = [v for v in hard if not is_violation_ignored(v, file_scope)]
            named_suppressed = named_suppressed or len(kept) < len(hard)
            hard = kept
    return hard, named_suppressed


def _read_file_for_ignore(file_path: str) -> str:
    """Read the file's bytes to scan for an inline chameleon-ignore directive.

    Bounded to the same 100 KB the lint paths read; returns an empty string on
    any read error so the ignore scan simply finds no directive.
    """
    try:
        return Path(file_path).read_bytes()[:100_000].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _displayable_violations(
    violations: list[dict], content: str, file_path: str | None
) -> list[dict]:
    """Drop violations an inline chameleon-ignore directive covers, for display.

    The block decision already filters its hard set through the same index, so a
    directive that suppresses a rule must also stop chameleon re-surfacing it in
    the advisory — otherwise the escape hatch reads as inert in shadow mode,
    where nothing blocks. Convention rules are filtered upstream by the lint
    engine; this additionally covers the content scans (eval-call, deterministic
    secret) whose violations reach the advisory unfiltered. Fails open to the
    full list so a parser error never hides real feedback.
    """
    try:
        from chameleon_mcp.violation_class import build_ignore_index, is_violation_ignored

        idx = build_ignore_index(content or "", file_path=file_path)
        if idx is None:
            return list(violations)
        return [v for v in violations if not is_violation_ignored(v, idx)]
    except Exception:
        return list(violations)


def posttool_verify() -> int:
    """PostToolUse Edit/Write/NotebookEdit: archetype conformance lint.

    Violations are surfaced through the PostToolUse hookSpecificOutput
    ``additionalContext`` channel (the documented feedback path).
    """
    if os.environ.get("CHAMELEON_VERIFY") == "0":
        _emit({})
        return 0

    _started = time.time()

    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in _EDIT_TOOLS:
        _emit({})
        return 0

    tool_input = _as_dict(payload.get("tool_input"))
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    # A non-string path (malformed payload) must fail open silently here, not
    # surface as a TypeError in the error log doctor reads. Same for an
    # embedded NUL, which raises ValueError from Path() before any guard.
    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        _emit({})
        return 0

    tool_response = _as_dict(payload.get("tool_response"))
    if isinstance(tool_response, dict):
        if "error" in tool_response or tool_response.get("success") is False:
            _emit({})
            return 0

    session_id = payload.get("session_id")

    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.profile.trust import trust_state_for
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(Path(file_path).expanduser())
        if repo_root is None:
            _emit({})
            return 0

        repo_id = _compute_repo_id(repo_root)

        if is_chameleon_suppressed(repo_root, repo_id, session_id) is not None:
            _emit({})
            return 0

        # Trust gate: PreToolUse already returns early for an untrusted profile;
        # mirror it here so PostToolUse does not feed violation messages derived
        # from an untrusted (attacker-controllable) profile — conventions.json
        # values and witness content — back to the model. Stale still verifies
        # (the profile was trusted once); only never-trusted is skipped. An
        # ungranted workspace under a monorepo-shared repo_id is untrusted too.
        _gate_rec = trust_state_for(repo_id)
        if _gate_rec is None or not _gate_rec.grants_root(repo_root):
            _emit({})
            return 0

        p = Path(file_path).expanduser()
        if not p.is_file():
            _emit({})
            return 0
        content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")

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
            # No archetype: the convention/AST lints have nothing to compare
            # against, but a secret or a dynamic eval is a content fact that does
            # not depend on an archetype. Scan for those so a credential in an
            # unarchetyped file is not invisible; record any deterministic-hard hit
            # into enforcement state under a synthetic label so the Stop backstop
            # re-lints and blocks it consistently with the archetyped path. Fail
            # open: any error here still ends with a clean emit. Load the rules so
            # the style baseline runs against the repo's declared formatter config
            # even though no archetype resolved; a failed load just skips it.
            _indep_rules = _load_rules_for_style(repo_root)
            try:
                indep = _scan_archetype_independent(
                    content, file_path, _indep_rules, repo_root=repo_root
                )
            except Exception:
                indep = []
            # Absence of coverage is never evidence of cleanliness: the
            # turn-end attestation classifies every touched file as governed or
            # ungoverned, so an archetype-less edit must still be recorded as
            # touched -- an observation row, a decision row keyed by content
            # digest, and a FileState entry so the Stop universe includes it.
            # Each step is individually fail-open.
            if repo_id:
                try:
                    from chameleon_mcp.drift.observations import record_edit_observation

                    record_edit_observation(
                        repo_id,
                        rel_path=str(file_path),
                        archetype=None,
                        confidence_band=None,
                        matched_canonical=False,
                    )
                except Exception:
                    pass
                try:
                    _record_edit_decision(
                        repo_id,
                        repo_root,
                        file_path,
                        archetype=None,
                        match_quality="none",
                        confidence_band=None,
                        violations_raised=len(indep),
                        blockable_rules=None,
                        outcome="advised" if indep else "clean",
                        session_id=session_id,
                        content_digest=_content_digest_16(content),
                    )
                except Exception:
                    pass
                try:
                    from chameleon_mcp.enforcement import FileState, load_state, save_state

                    _na_dir = _plugin_data_dir() / repo_id
                    _na_state = load_state(_na_dir, session_id or "")
                    if file_path not in _na_state.files:
                        # last_verified_at stamps the entry so the recency-based
                        # eviction ordering holds; an existing entry (e.g. one
                        # already armed for the backstop) is never clobbered.
                        _na_state.files[file_path] = FileState(last_verified_at=_started)
                        save_state(_na_state, _na_dir, session_id or "")
                except Exception:
                    pass
            if indep:
                # The advisory emits its own PostToolUse context object. Emitting
                # _emit({}) afterward would write a second JSON object to stdout,
                # making the hook output unparseable. Each hook invocation writes
                # exactly one object, so emit the fallback only when the advisory
                # did not (its emit raised).
                if _posttool_no_archetype_advisory(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    file_path=file_path,
                    violations=indep,
                    session_id=session_id,
                    now=_started,
                ):
                    return 0
            _emit({})
            return 0

        # Match-quality and confidence are resolved once here so every terminal
        # outcome branch below can record the same decision_log row. match_quality
        # none/fallback marks a coverage gap; ast/exact with no violation is an
        # in-scope miss — the distinction a postmortem reads from this row.
        _decision_data = arch_result.get("data") or {}
        decision_match_quality = _decision_data.get("match_quality")
        decision_confidence_band = _decision_data.get("confidence_band")

        if repo_id:
            try:
                from chameleon_mcp.drift.observations import record_edit_observation

                record_edit_observation(
                    repo_id=repo_id,
                    rel_path=str(file_path),
                    archetype=archetype_name,
                    confidence_band=decision_confidence_band,
                    matched_canonical=True,
                )
            except Exception:
                pass

        repo_data_dir = _plugin_data_dir() / repo_id
        enforcement_state = None
        file_state = None
        try:
            from chameleon_mcp.enforcement import (
                LEVEL_NONE,
                MAX_CORRECTIONS_PER_FILE,
                FileState,
                cooldown_for_level,
                is_self_correction,
                load_state,
                maybe_reset_correction_count,
                record_clean,
                record_violation,
                save_state,
                should_surface_to_user,
                tone_for_level,
            )

            enforcement_state = load_state(repo_data_dir, session_id or "")
            file_state = enforcement_state.files.get(file_path)
            if file_state is None:
                file_state = FileState()
                enforcement_state.files[file_path] = file_state
        except Exception:
            enforcement_state = None
            file_state = None

        if enforcement_state is not None and file_state is not None:
            try:
                maybe_reset_correction_count(file_state, _started)
                if file_state.correction_count >= MAX_CORRECTIONS_PER_FILE:
                    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

                    # The breaker suppresses advisory feedback, not security
                    # tracking: an edit landing after corrections ran out could
                    # otherwise introduce a credential that neither blocks
                    # inline (no lint runs) nor at turn end (nothing armed the
                    # backstop). The deterministic secret scan is cheap and
                    # pure, so run just that and arm the backstop on a hit.
                    secret_note = ""
                    try:
                        if _content_has_hard_secret(content, file_path):
                            record_violation(
                                file_state,
                                now=_started,
                                archetype=archetype_name,
                                hard_class=True,
                            )
                            secret_note = (
                                "A hardcoded credential was detected in this "
                                "edit — remove it before ending the turn.\n"
                            )
                    except Exception:
                        pass

                    safe_path = sanitize_for_chameleon_context(file_path)
                    _emit_posttool_context(
                        "<chameleon-context>\n"
                        f"[🦎 chameleon: corrections exhausted for {safe_path}]\n"
                        "Chameleon has verified this file 10 times recently. "
                        "Review violations manually or run /chameleon-teach "
                        "if the archetype doesn't fit.\n"
                        f"{secret_note}"
                        "</chameleon-context>"
                    )
                    try:
                        save_state(enforcement_state, repo_data_dir, session_id or "")
                    except Exception:
                        pass
                    return 0
            except Exception:
                pass

        from chameleon_mcp.optouts import _safe_session_marker

        # Keyed by session as well as path: the cooldown means "this session
        # already verified this content", not "some session somewhere did".
        # A shared marker let an advisory pass in one session swallow the
        # lint — including the enforce-mode block path — for every other
        # session editing the same bytes inside the TTL.
        file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
        marker = repo_data_dir / (f".verify_seen.{_safe_session_marker(session_id)}.{file_hash}")
        content_digest = _content_digest_16(content)

        cooldown_ttl = _VERIFY_SEEN_TTL_SECONDS
        if enforcement_state is not None and file_state is not None:
            try:
                if is_self_correction(file_state, _started):
                    cooldown_ttl = 0
                else:
                    cooldown_ttl = cooldown_for_level(file_state.level)
            except Exception:
                pass

        # Dedup only when the content is byte-identical to what was already
        # verified. A path+mtime-only cooldown suppressed analysis of EDITED
        # files too, so a defect introduced while iterating inside the window
        # slipped through silently.
        if (
            cooldown_ttl > 0
            and _marker_path_is_fresh(marker, cooldown_ttl)
            and _marker_digest_matches(marker, content_digest)
        ):
            # The skip itself is attestable evidence: record that this content
            # was deliberately not re-verified (cooldown) so the turn-end
            # attestation can distinguish "skipped" from "never observed".
            _emit_check_event(
                repo_id,
                session_id,
                "posttool_verify",
                "skipped",
                reason="cooldown",
                file_rel=_repo_rel(repo_root, file_path),
            )
            _emit_posttool_context(
                "<chameleon-context>\n"
                "[🦎 chameleon: already verified this file — review previous feedback]\n"
                "</chameleon-context>"
            )
            return 0

        violations: list[dict] = []
        daemon_responded = False

        try:
            from chameleon_mcp import daemon_client as _dc

            lint_result = _dc.call(
                "lint_file",
                {
                    "repo": str(repo_root),
                    "archetype": archetype_name,
                    "content": content,
                    "file_path": file_path,
                },
            )
            if lint_result is not None:
                daemon_responded = True
                raw = (lint_result.get("data") or {}).get("violations") or []
                # The lint_file tool already runs scan_secrets, so the daemon
                # response carries secret hits. Keep them (parity with the
                # in-process path) and tag each so only the deterministic kinds
                # can hard-block; the rest fall through to the advisory below.
                violations = list(raw)
                try:
                    from chameleon_mcp.violation_class import tag_secret_hardness

                    tag_secret_hardness(violations)
                except Exception:
                    pass
        except Exception:
            pass

        if not daemon_responded:
            violations = _lint_file_in_process(repo_root, archetype_name, content, file_path)

        # Archetype-SHAPE rules presume the archetype actually fits the file.
        # On a fallback/none-quality match (new directory, no structural
        # sibling) a shape mismatch says "the guess was wrong", not "the file
        # is wrong" — flagging it escalates files that were never wrong and
        # invites restructuring working code. Content rules (secrets, sinks,
        # conventions, imports) are archetype-independent and stay.
        if decision_match_quality in ("fallback", "none"):
            _SHAPE_RULES = {
                "top-level-node-kinds-mismatch",
                "default-export-kind-mismatch",
                "named-export-count-bucket-mismatch",
                "jsx-presence-mismatch",
                "content-signal-mismatch",
            }
            violations = [v for v in violations if v.get("rule") not in _SHAPE_RULES]

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

        if violations:
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context

            # Partition the violations into the hard class (block-eligible rules
            # in this repo's calibrated active set) before recording, so the
            # per-file escalation knows whether a blockable rule is unresolved.
            hard: list[dict] = []
            blockable_now: list[dict] = []
            # True when an inline chameleon-ignore dropped a block-eligible rule
            # on this edit, so the decision_log records the outcome as an
            # overridden bypass rather than a plain advisory.
            inline_overridden_hard = False
            # Resolved decision_log outcome for the advisory path: "advised" by
            # default, "would-block" when shadow mode logged a would-block then
            # fell through, "overridden" when an inline ignore dropped a hard rule.
            decision_outcome = "advised"
            try:
                from chameleon_mcp.enforcement_calibration import active_block_rules
                from chameleon_mcp.violation_class import (
                    build_ignore_index,
                    hard_class_violations,
                    is_deferred_to_turn_end,
                    is_violation_ignored,
                )

                active = active_block_rules(repo_root / ".chameleon")
                hard = hard_class_violations(violations, active)
                # An inline `chameleon-ignore <rule>` directive (or a bare one)
                # downgrades the matching rule to advisory on the annotated
                # line. The lint layer already suppresses some rules on the
                # directive, but the AST-query rules (e.g.
                # jsx-presence-mismatch) reach here intact. Filter the hard
                # set itself (not only blockable_now) so the cached
                # blockable_unresolved flag the Stop backstop reads is cleared
                # too; otherwise an inline-ignored rule still arms the backstop.
                idx = build_ignore_index(content, file_path=file_path)
                if idx is not None:
                    overridden = [v for v in hard if is_violation_ignored(v, idx)]
                    inline_overridden_hard = bool(overridden)
                    hard = [v for v in hard if not is_violation_ignored(v, idx)]
                    # An inline directive dropping a block-eligible rule is
                    # otherwise invisible after the turn. Record each bypass so
                    # the override rate is auditable: a metric counter (paired
                    # with the would_block stream) and a durable drift.db row.
                    # A bare directive (the empty-string rule) is flagged
                    # separately because it downgrades every rule at once
                    # rather than annotating one intentional deviation.
                    blanket = "" in idx.all_rules()
                    _record_overrides(
                        repo_id,
                        overridden,
                        archetype=archetype_name,
                        file_rel=_repo_rel(repo_root, file_path),
                        session_id=session_id,
                        blanket=blanket,
                    )
                # phantom imports are a filesystem fact handled at turn end
                # (Stop backstop), never blocked inline here: a later same-turn
                # edit can create the import target. A deterministic secret does
                # NOT defer -- nothing a later edit does makes a hardcoded
                # credential safe -- so it stays in the inline block set as the
                # documented "only security BLOCK". Strip only the deferred rules,
                # not every archetype-independent one.
                blockable_now = [v for v in hard if not is_deferred_to_turn_end(v.get("rule"))]
            except Exception:
                hard = []
                blockable_now = []

            if enforcement_state is not None and file_state is not None:
                try:
                    record_violation(
                        file_state,
                        now=_started,
                        archetype=archetype_name,
                        hard_class=bool(hard),
                    )
                    enforcement_state.archetypes_with_violations.add(archetype_name)
                except Exception:
                    pass

            # Enforcement decision: a blockable hard-class violation, with the
            # archetype gates satisfied, on a file at L2, blocks the edit in
            # "enforce" mode. "shadow" only logs a would_block metric; everything
            # else falls through to the advisory below. CHAMELEON_ENFORCE=0 and
            # mode "off" force advisory regardless. A "stale" grant (the profile
            # hash drifted from the one the user trusted) only verifies; it never
            # blocks, because the conventions it would enforce were not re-reviewed.
            try:
                from chameleon_mcp.profile.config import load_config
                from chameleon_mcp.profile.trust import hash_profile

                enforce_off = os.environ.get("CHAMELEON_ENFORCE") == "0"
                mode = load_config(repo_root / ".chameleon").enforcement.mode
                data = arch_result.get("data") or {}
                match_quality = data.get("match_quality")
                gate_band = data.get("confidence_band")
                gate_ok = (match_quality == "ast") and (gate_band in ("high", "medium"))
                at_l2 = file_state is not None and file_state.level >= 2
                trusted_not_stale = _gate_rec.hash_for_root(repo_root) == hash_profile(
                    repo_root / ".chameleon"
                )
                if (
                    not enforce_off
                    and mode != "off"
                    and blockable_now
                    and gate_ok
                    and at_l2
                    and trusted_not_stale
                ):
                    try:
                        from chameleon_mcp.metrics import emit_hook_metric

                        # One would_block row per blockable rule on this file, so
                        # the shadow report attributes counts to the specific rule
                        # and can sample the off-pattern file for spot-check.
                        file_rel = _repo_rel(repo_root, file_path)
                        for v in blockable_now:
                            emit_hook_metric(
                                "posttool-verify",
                                elapsed_ms=0,
                                repo_id=repo_id,
                                advisory_emitted=True,
                                archetype=archetype_name,
                                would_block=True,
                                rule=v.get("rule"),
                                file_rel=file_rel,
                                line=v.get("line"),
                            )
                    except Exception:
                        pass
                    if mode == "enforce":
                        rules = ", ".join(sorted({v.get("rule") for v in blockable_now}))
                        msgs = "; ".join(v.get("message", "") for v in blockable_now[:3])
                        if enforcement_state is not None:
                            try:
                                save_state(enforcement_state, repo_data_dir, session_id or "")
                            except Exception:
                                pass
                        # Rule names and messages derive from attacker-controllable
                        # conventions.json, so sanitize them before they reach the
                        # block reason fed back to the model, not just the advisory
                        # additionalContext channel.
                        safe_rules = sanitize_for_chameleon_context(rules)
                        safe_msgs = sanitize_for_chameleon_context(msgs)
                        _record_edit_decision(
                            repo_id,
                            repo_root,
                            file_path,
                            archetype=archetype_name,
                            match_quality=decision_match_quality,
                            confidence_band=decision_confidence_band,
                            violations_raised=len(violations),
                            blockable_rules=[v.get("rule") for v in blockable_now],
                            outcome="blocked",
                            session_id=session_id,
                            content_digest=content_digest,
                        )
                        _emit_posttool_block(
                            f"chameleon blocks this edit: {safe_rules}. "
                            f"Fix before continuing: {safe_msgs}. "
                            f"Override with {_ignore_hint(file_path)} "
                            f"on the offending line if this is intentional.",
                            "<chameleon-context>\n"
                            f"[🦎 chameleon: BLOCKED — {safe_rules}]\n"
                            f"{safe_msgs}\n"
                            "</chameleon-context>",
                        )
                        return 0
                    # shadow: would_block already logged; fall through to advisory.
                    decision_outcome = "would-block"
            except Exception:
                pass

            current_tone = "Fix these."
            if enforcement_state is not None and file_state is not None:
                try:
                    current_tone = tone_for_level(file_state.level)
                except Exception:
                    pass

            # An inline chameleon-ignore directive suppresses the block above; it
            # must also stop the rule being re-surfaced in the advisory, so the
            # escape hatch behaves consistently across every rule (convention
            # rules are already dropped upstream by the lint engine; eval-call and
            # the deterministic secret reach here unfiltered). The raw count still
            # feeds the decision_log/override audit below.
            displayed = _displayable_violations(violations, content, file_path)

            _record_edit_decision(
                repo_id,
                repo_root,
                file_path,
                archetype=archetype_name,
                match_quality=decision_match_quality,
                confidence_band=decision_confidence_band,
                violations_raised=len(violations),
                blockable_rules=[v.get("rule") for v in blockable_now],
                outcome="overridden" if inline_overridden_hard else decision_outcome,
                session_id=session_id,
                content_digest=content_digest,
            )

            if displayed:
                violation_lines = []
                for i, v in enumerate(displayed):
                    msg = sanitize_for_chameleon_context(v.get("message", ""))
                    violation_lines.append(f"{i + 1}. {msg}")

                block = (
                    f"[🦎 chameleon: {len(displayed)} "
                    f"violation{'s' if len(displayed) != 1 else ''}]\n"
                    + "\n".join(violation_lines)
                    + "\n"
                    + current_tone
                )

                if enforcement_state is not None and file_state is not None:
                    try:
                        if should_surface_to_user(file_state):
                            safe_path = sanitize_for_chameleon_context(file_path)
                            block += (
                                f"\n\nchameleon is flagging repeated violations "
                                f"in {safe_path} — run /chameleon-teach if the "
                                f"archetype doesn't fit this file."
                            )
                    except Exception:
                        pass

                _emit_posttool_context(f"<chameleon-context>\n{block}\n</chameleon-context>")
                _update_statusline(
                    f"{len(displayed)} violation{'s' if len(displayed) != 1 else ''}",
                    repo_root=repo_root,
                )
            else:
                # Every fired rule was overridden by an inline directive; the
                # override is recorded above, so surface nothing to the model.
                _emit({})

            if enforcement_state is not None:
                try:
                    save_state(enforcement_state, repo_data_dir, session_id or "")
                except Exception:
                    pass

            _write_verify_marker(marker, content_digest)

            return 0

        had_prior_violation = False
        if enforcement_state is not None and file_state is not None:
            try:
                had_prior_violation = file_state.level > LEVEL_NONE
                record_clean(file_state, now=_started)
            except Exception:
                pass

        _record_edit_decision(
            repo_id,
            repo_root,
            file_path,
            archetype=archetype_name,
            match_quality=decision_match_quality,
            confidence_band=decision_confidence_band,
            violations_raised=0,
            blockable_rules=None,
            outcome="clean",
            session_id=session_id,
            content_digest=content_digest,
        )

        if had_prior_violation:
            _emit_posttool_context(
                "<chameleon-context>\n[🦎 archetype: clean]\n</chameleon-context>"
            )
            _update_statusline("clean", repo_root=repo_root)
        else:
            _emit({})
            _update_statusline("clean", repo_root=repo_root)

        if enforcement_state is not None:
            try:
                save_state(enforcement_state, repo_data_dir, session_id or "")
            except Exception:
                pass

        _write_verify_marker(marker, content_digest)

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


# Complaints about chameleon's distinctive behavior (injection): fire the hint
# unconditionally — these are unambiguously about chameleon.
_CHAMELEON_SPECIFIC_PATTERNS = (
    re.compile(r"stop injecting", re.IGNORECASE),
    re.compile(r"don'?t inject (that|this)", re.IGNORECASE),
)

# Generic frustration / complaint words: fire only when the prompt also mentions
# chameleon, so swearing at unrelated code doesn't surface a spurious hint.
_GENERIC_FRUSTRATION_PATTERNS = (
    re.compile(r"\b(ugh|argh|wtf|nope|sucks|useless|dumb)\b", re.IGNORECASE),
    re.compile(r"\b(annoying|annoyed|frustrating|frustrated|hate|hating)\b", re.IGNORECASE),
    re.compile(r"\b(damn|fuck|fucking|shit|crap|bullshit)\b", re.IGNORECASE),
    re.compile(r"\b(slow|wrong|broken|bad)\b", re.IGNORECASE),
    re.compile(r"this isn'?t right", re.IGNORECASE),
    re.compile(r"don'?t (do|use) (that|this)", re.IGNORECASE),
    re.compile(r"stop (using|doing|adding)", re.IGNORECASE),
)

_CHAMELEON_MENTION_RE = re.compile(r"chameleon|🦎", re.IGNORECASE)

# Harness-injected blocks riding inside the prompt (task notifications, system
# reminders, command transcripts) are machine-generated: "failed"/"broken" in a
# workflow status report is not the user venting. Strip them before the
# frustration scan so only the human-typed remainder is judged.
_MACHINE_BLOCK_RE = re.compile(
    r"<(task-notification|system-reminder|command-name|command-message|"
    r"local-command-stdout|tool_result)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _pending_findings_block(repo_root: Path, repo_data: Path, session_id) -> str | None:
    """Deliver findings a detached judge left pending, or None.

    Consumes ``.judge_pending.<sid>.json`` (written by the async judge after the
    Stop that spawned it already ended). A finding is dropped as stale when its
    file's current first-1MB digest no longer matches the digest recorded at
    review time, or the file is gone -- the review read code that has since
    changed. The file is unlinked whether or not anything survives, so a stale
    batch is consumed exactly once. Trust-hash verification is deliberately
    skipped: this is a first-party plugin-data file this plugin wrote, not
    repo-controlled content, and UserPromptSubmit must stay cheap.
    """
    from chameleon_mcp.optouts import _safe_session_marker

    pending = repo_data / f".judge_pending.{_safe_session_marker(session_id)}.json"
    if not pending.is_file():
        return None
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    try:
        pending.unlink()
    except OSError:
        pass
    if not isinstance(data, dict):
        return None

    recorded = data.get("digests") if isinstance(data.get("digests"), dict) else {}
    live: list[dict] = []
    for finding in data.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        rel = finding.get("file")
        if isinstance(rel, str) and rel in recorded:
            try:
                raw = (repo_root / rel).read_bytes()[:1_000_000]
            except OSError:
                continue  # file gone since the review: stale
            if hashlib.sha256(raw).hexdigest()[:16] != recorded.get(rel):
                continue  # edited since the review: stale
        live.append(finding)
    if not live:
        return None

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    n = len(live)
    lines = [
        f"[🦎 chameleon: independent review of your previous turn flagged "
        f"{n} possible correctness issue{'s' if n != 1 else ''}]",
        "These are advisory; verify each before acting, they may be wrong.",
    ]
    for finding in live:
        rel = finding.get("file")
        loc = sanitize_for_chameleon_context(str(rel)) if rel else "?"
        line_no = finding.get("line")
        if isinstance(line_no, int):
            loc += f":{line_no}"
        message = finding.get("message")
        lines.append(f"- {loc}: {sanitize_for_chameleon_context(str(message or ''))}")
    return "<chameleon-context>\n" + "\n".join(lines) + "\n</chameleon-context>"


def callout_detector() -> int:
    """UserPromptSubmit: frustration hint, intent capture, findings delivery.

    Three individually fail-open stages share the hook. (1) On detected
    frustration during a chameleon-active session, surface a one-line hint
    about /chameleon-disable, /chameleon-pause-15m, and /chameleon-teach.
    (2) Capture prompt-derived intent (extracted assertion tokens + digests,
    hard-secret-scanned, never raw prose) for the Stop-path judge routing;
    CHAMELEON_INTENT_CAPTURE=0 disables it. (3) Deliver findings a detached
    judge left pending from a previous turn. Stages 2 and 3 operate on the
    machine-block-stripped human remainder / first-party plugin data only, and
    a suppressed session stays silent for both. The stage outputs compose into
    a single additionalContext.
    """
    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0

    user_prompt = payload.get("user_prompt") or payload.get("prompt") or ""
    if not isinstance(user_prompt, str):
        # A dict payload can still carry a non-string user_prompt (list/int);
        # re.search would raise TypeError. Fail open at the Python layer.
        _emit({})
        return 0

    # Harness-injected machine blocks are stripped so only the human-typed
    # remainder is scanned for frustration or captured as intent.
    scan_prompt = _MACHINE_BLOCK_RE.sub(" ", user_prompt) if user_prompt else ""
    session_id = payload.get("session_id")
    context_blocks: list[str] = []

    if scan_prompt.strip():
        chameleon_specific = any(p.search(scan_prompt) for p in _CHAMELEON_SPECIFIC_PATTERNS)
        generic = any(p.search(scan_prompt) for p in _GENERIC_FRUSTRATION_PATTERNS)
        mentions_chameleon = _CHAMELEON_MENTION_RE.search(scan_prompt) is not None
        if chameleon_specific or (generic and mentions_chameleon):
            context_blocks.append(
                "<chameleon-context>\n"
                "[🦎 chameleon: detected frustration phrase]\n"
                "If chameleon is the issue, options:\n"
                "  /chameleon-disable      — suppress for the rest of this session\n"
                "  /chameleon-pause-15m    — pause for 15 minutes (auto-resume)\n"
                "  /chameleon-teach <pattern>  — capture the missed pattern as an idiom\n"
                "If chameleon is unrelated, ignore this note.\n"
                "</chameleon-context>"
            )

    # Shared repo resolution for the capture + delivery stages. A suppressed
    # (disabled/paused) session must stay silent, so both stages bail together.
    repo_root: Path | None = None
    repo_data: Path | None = None
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        cwd_raw = payload.get("cwd")
        repo_root = find_repo_root(
            Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else Path(".")
        )
        if repo_root is not None:
            repo_id = _compute_repo_id(repo_root)
            if is_chameleon_suppressed(repo_root, repo_id, session_id) is not None:
                repo_root = None
            else:
                repo_data = _plugin_data_dir() / repo_id
    except Exception:
        repo_root = None
        repo_data = None

    try:
        if (
            repo_data is not None
            and scan_prompt.strip()
            and os.environ.get("CHAMELEON_INTENT_CAPTURE") != "0"
        ):
            from chameleon_mcp import intent_capture

            intent_capture.capture_intent(repo_data, session_id, scan_prompt)
    except Exception:
        pass

    try:
        if repo_root is not None and repo_data is not None:
            block = _pending_findings_block(repo_root, repo_data, session_id)
            if block:
                context_blocks.append(block)
    except Exception:
        pass

    if not context_blocks:
        _emit({})
        return 0
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(context_blocks),
            }
        }
    )
    return 0


def _stop_file_still_blockable(
    repo_root: Path,
    file_path: str,
    loaded=None,
    active=None,
    daemon_state=None,
    out_rules=None,
    level: int = 2,  # LEVEL_L2; a module-level literal so the default binds at import time
) -> bool:
    """Re-lint a candidate file live and report whether an enforceable hard-class
    violation still stands.

    The Stop backstop reads a cached blockable_unresolved flag that the per-edit
    verify set. That flag can go stale: a phantom import that armed it may have
    been resolved by a later edit to a *different* file (the import target was
    created), so the importing file itself was never re-verified. This cold-path
    re-check (only candidate files reach it) reads the live file, re-resolves the
    archetype, and re-runs the same lints, so a resolved violation no longer
    blocks the turn.

    A hard violation counts only if it is enforceable on the live file: an
    archetype-independent rule (a deterministic content/filesystem fact -- a
    leaked credential or a phantom import) always counts at any escalation level,
    because nothing about a wrong archetype guess makes it spurious; an
    archetype-dependent rule (naming/inheritance/file-naming) counts only when the
    archetype is AST-confirmed at high or medium confidence AND the file has
    escalated to L2, matching the per-edit block gate's ladder so a single
    wrong-archetype match cannot trap the turn. ``level`` is the file's current
    escalation level; archetype-independent rules ignore it. Inline-ignored rules
    never count. Returns False on any error (fail-open: a re-check that can't run
    does not block).

    ``loaded`` is the once-per-pass profile the backstop preloads so the per-file
    re-lint does not re-read the profile for every candidate. ``active`` is the
    once-per-pass set of active block rules; when None it is read from disk (the
    per-edit callers pass nothing). ``daemon_state`` is a shared ``{"available":
    bool}`` flag the backstop threads through the candidate loop: once a daemon
    call comes back empty, every later file skips the daemon and goes straight to
    the in-process archetype resolve, so a hung daemon cannot stack per-file
    timeouts past the hook's hard deadline.

    ``out_rules``, when a list is passed, is appended with the enforceable hard
    rule names that still stand on this file, so the shadow would_block row can
    attribute the backstop block to a specific rule. Collecting them here reuses
    the re-lint this function already performs; the bool return is unchanged.
    """
    try:
        p = Path(file_path)
        if not p.is_file():
            return False
        content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")

        archetype_name: str | None = None
        confidence_band: str | None = None
        match_quality: str | None = None
        daemon_usable = daemon_state is None or daemon_state.get("available", True)
        if daemon_usable:
            try:
                from chameleon_mcp import daemon_client

                arch_result = daemon_client.call(
                    "get_archetype", {"repo": str(repo_root), "file_path": file_path}
                )
                if arch_result:
                    data = arch_result.get("data") or {}
                    archetype_name = data.get("archetype")
                    confidence_band = data.get("confidence_band")
                    match_quality = data.get("match_quality")
                elif daemon_state is not None:
                    # Daemon unreachable or timed out: skip it for the rest of the
                    # pass so the remaining files don't each eat a full timeout.
                    daemon_state["available"] = False
            except Exception:
                if daemon_state is not None:
                    daemon_state["available"] = False
        if not archetype_name:
            from chameleon_mcp.tools import get_archetype

            data = (get_archetype(str(repo_root), file_path).get("data")) or {}
            archetype_name = data.get("archetype")
            confidence_band = data.get("confidence_band")
            match_quality = data.get("match_quality")

        from chameleon_mcp.violation_class import (
            build_ignore_index,
            hard_class_violations,
            is_archetype_independent,
            is_violation_ignored,
        )

        if not archetype_name:
            # No archetype: only archetype-independent content facts (phantom
            # imports, deterministic secrets) can still stand. An eval-call is
            # scanned but filtered by the archetype-independent check below,
            # because it stays gated on an archetype match. posttool_verify
            # recorded this file under the synthetic no-archetype label
            # precisely so the credential blocks the turn here.
            indep = _scan_archetype_independent(content, file_path)
            if not indep:
                return False
            if active is None:
                from chameleon_mcp.enforcement_calibration import active_block_rules

                active = active_block_rules(repo_root / ".chameleon")
            hard = hard_class_violations(indep, active)
            idx = build_ignore_index(content, file_path=file_path)
            if idx is not None:
                hard = [v for v in hard if not is_violation_ignored(v, idx)]
            enforceable = [v for v in hard if is_archetype_independent(v.get("rule"))]
            if isinstance(out_rules, list):
                out_rules.extend(v.get("rule") for v in enforceable if v.get("rule"))
            return bool(enforceable)

        violations = _lint_file_in_process(
            repo_root, archetype_name, content, file_path, loaded=loaded
        )
        if not violations:
            return False

        if active is None:
            from chameleon_mcp.enforcement_calibration import active_block_rules

            active = active_block_rules(repo_root / ".chameleon")
        hard = hard_class_violations(violations, active)
        idx = build_ignore_index(content, file_path=file_path)
        if idx is not None:
            hard = [v for v in hard if not is_violation_ignored(v, idx)]
        gate_ok = (match_quality == "ast") and (confidence_band in ("high", "medium"))
        # Archetype-dependent rules honor the per-edit escalation ladder: they
        # only refuse the turn once the file has reached L2, so a single
        # wrong-archetype match cannot trap a turn. Archetype-independent facts
        # (secrets, phantom imports) ignore the level entirely.
        from chameleon_mcp.enforcement import LEVEL_L2

        dep_ok = gate_ok and level >= LEVEL_L2
        enforceable = [v for v in hard if is_archetype_independent(v.get("rule")) or dep_ok]
        if isinstance(out_rules, list):
            out_rules.extend(v.get("rule") for v in enforceable if v.get("rule"))
        return bool(enforceable)
    except Exception:
        return False


_IDIOM_REVIEWED_FILENAME = ".idiom_reviewed.{session}"
_IDIOM_CONTEXT_CHAR_CAP = 1500

# Judged-digest marker namespace for the correctness gate, kept disjoint from
# the duplication gate's default ".dup_judged." namespace.
_CORR_JUDGED_PREFIX = ".corr_judged."

# Per-session marker prefixes reaped at SessionStart. No SessionEnd hook
# exists, so anything session-scoped must age out here. .dup_judged. and
# .corr_judged. are per-(session,file,digest) dedup touch markers.
SESSION_REAP_PREFIXES: tuple[str, ...] = (
    ".judge_pending.",
    ".judge_inflight.",
    ".judge_request.",
    ".corr_judged.",
    ".dup_judged.",
    ".intent.",
    ".correctness_judged.",
)

# Sink kinds from judge.run_correctness_judge that mean the reviewer produced
# no usable verdict; the touched files stay unmarked so the next Stop can
# retry under the session spawn cap.
_JUDGE_FAILURE_KINDS = frozenset(
    {"spawn_timeout", "spawn_exec_error", "spawn_nonzero_exit", "pipeline_error"}
)


def _is_source_for_test_signal(rel_path: str, *, language: str) -> bool:
    """True if ``rel_path`` is a real source file (not a test/spec) worth a
    "did you run the tests" nudge at turn end.

    Reuses the same test-path classification the bootstrap source pool uses, so a
    turn that only touched test files (or docs) does not trigger the nudge. Fails
    closed to False on any error: an unclassifiable path is treated as not
    source, which only suppresses a soft advisory.
    """
    try:
        from chameleon_mcp.conventions import _is_test_path

        return not _is_test_path(rel_path, language=language)
    except Exception:
        return False


def _idiom_review_gate(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
) -> dict | None:
    """Reflexive idiom/principle review at turn end.

    Reached only after the lint-unresolved decision came back with no block: a
    turn that edited files governed by team idioms/principles must self-review
    those changes before ending. Fires AT MOST ONCE per session (a per-session
    marker gates it) so the model is not re-nagged every turn; stop_hook_active
    already prevents the immediate re-block loop.

    Returns the hook output dict to emit, or None to defer to the caller's
    default allow. In enforce mode a fresh review fires a Stop ``block`` whose
    reason lists the edited files and the relevant idioms/principles. In shadow
    mode it records a would_block metric and allows the stop, still writing the
    marker so shadow reflects the real once-per-session frequency. Fails open:
    any error returns None (the caller allows the stop).

    The ``idiom_judge`` flag only strengthens the directive for now; the
    independent-judge spawn (claude -p) is intentionally out of scope here, so no
    LLM is invoked from the hook.
    """
    try:
        if cfg.mode == "off" or not cfg.idiom_review:
            return None

        # Gather idioms/principles text; the gate needs at least one non-empty.
        profile_dir = repo_root / ".chameleon"
        idioms_text = ""
        principles_text = ""
        try:
            ip = profile_dir / "idioms.md"
            if ip.is_file():
                idioms_text = ip.read_text(encoding="utf-8", errors="replace")
        except OSError:
            idioms_text = ""
        try:
            pp = profile_dir / "principles.md"
            if pp.is_file():
                principles_text = pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            principles_text = ""
        if not idioms_text.strip() and not principles_text.strip():
            return None

        # An edited file that still exists this session, and is not opted out via
        # an inline `// chameleon-ignore idioms` (or bare ignore) directive.
        from chameleon_mcp.violation_class import ignored_rules

        edited: list[str] = []
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "idioms" in ign:
                continue
            edited.append(path)
        if not edited:
            return None

        # Test-run signal: when the turn touched real source (not a pure
        # test/docs edit) and no passing test runner was observed this session,
        # the self-review directive is worth strengthening with a "run the
        # suite" nudge. Watch-mode and CI-run-it are unobservable, so a missing
        # signal is a soft strengthen only, never a gating condition.
        no_test_for_source_edit = False
        try:
            from chameleon_mcp.exec_log import session_test_run_seen
            from chameleon_mcp.lint_engine import detect_language

            edited_source = False
            for path in edited:
                lang = detect_language(path)
                if lang is None:
                    continue
                try:
                    rel = os.path.relpath(path, str(repo_root))
                except ValueError:
                    rel = Path(path).name
                if _is_source_for_test_signal(rel, language=lang):
                    edited_source = True
                    break
            if edited_source and session_id:
                no_test_for_source_edit = not session_test_run_seen(repo_id, session_id)
        except Exception:
            no_test_for_source_edit = False

        # Once-per-session marker: fire only the first time. Writing the marker
        # before the decision keeps shadow's frequency honest and prevents the
        # next turn from re-blocking.
        from chameleon_mcp.optouts import _safe_session_marker

        marker = repo_data / _IDIOM_REVIEWED_FILENAME.format(
            session=_safe_session_marker(session_id)
        )
        if marker.exists():
            _emit_check_event(repo_id, session_id, "idiom_review", "skipped", "marker_exists")
            return None

        # Respect the shared stop cap so an idiom block cannot exceed the budget.
        if state.stop_hook_blocks >= cfg.stop_block_cap:
            return None

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

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in edited[:5])
        idioms_block = sanitize_for_chameleon_context(idioms_text.strip())[:_IDIOM_CONTEXT_CHAR_CAP]
        principles_block = sanitize_for_chameleon_context(principles_text.strip())[
            :_IDIOM_CONTEXT_CHAR_CAP
        ]

        body: list[str] = []
        if no_test_for_source_edit:
            body.append(
                "No passing test run was recorded this turn while you changed source "
                "files. Run the suite to confirm your changes pass before ending "
                "(skip only if a watch process or CI is already running them)."
            )
        if idioms_block:
            body.append("")
            body.append("Team idioms:")
            body.append(idioms_block)
        if principles_block:
            body.append("")
            body.append("Principles:")
            body.append(principles_block)

        if cfg.mode != "enforce":
            # Shadow: record the would-have-blocked signal and allow the stop.
            # This gate has no single rule (it nudges a once-per-session
            # self-review of the turn's edits), so it emits under its own hook
            # name with no rule. The shadow report counts it as a turn-level
            # signal, never as a per-rule promotion candidate. The review text
            # itself still goes out as a non-blocking advisory: taught idioms
            # are not in the verify lint path, so without this the default
            # (shadow) config delivers NO turn-end idiom feedback at all.
            try:
                from chameleon_mcp.metrics import emit_hook_metric

                emit_hook_metric(
                    "stop-idiom-review",
                    elapsed_ms=0,
                    repo_id=repo_id,
                    advisory_emitted=True,
                    would_block=True,
                )
                if no_test_for_source_edit:
                    # Separate signal so the test-run nudge's real frequency can
                    # be measured in shadow before anyone relies on it.
                    emit_hook_metric(
                        "stop-test-run-signal",
                        elapsed_ms=0,
                        repo_id=repo_id,
                        advisory_emitted=True,
                        would_block=True,
                    )
            except Exception:
                pass
            _emit_check_event(repo_id, session_id, "idiom_review", "ran")
            advisory = [
                "[🦎 chameleon: idiom self-review (advisory)]",
                f"You edited {names} this turn. Review those changes against the "
                "team idioms/principles below and fix any clear violation in your "
                "next action. This is advisory; the turn ends normally.",
                *body,
            ]
            return {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": (
                        "<chameleon-context>\n" + "\n".join(advisory) + "\n</chameleon-context>"
                    ),
                }
            }

        parts = [
            f"chameleon: you edited {names} this turn. Before ending, verify those "
            "changes comply with the team idioms/principles below. Fix any clear "
            "violation; otherwise you may end.",
            *body,
        ]
        if cfg.idiom_judge:
            parts.append("")
            parts.append(
                "An independent judge is enabled (idiom_judge): make this review "
                "thorough, not a rubber stamp."
            )
        parts.append("")
        parts.append(
            "Ending again confirms the review is done. To skip this check, add "
            "`// chameleon-ignore idioms` (`# chameleon-ignore idioms` in Ruby) in "
            "a file you touched."
        )

        state.stop_hook_blocks += 1
        try:
            from chameleon_mcp.enforcement import save_state

            save_state(state, repo_data, session_id or "")
        except Exception:
            pass

        _emit_check_event(repo_id, session_id, "idiom_review", "ran")
        return {"decision": "block", "reason": "\n".join(parts)}
    except Exception:
        return None


def _archetype_resolver(repo_root: Path, daemon_state: dict):
    """Return a callable ``abs_path -> archetype name or None`` for the judge.

    Resolves through the shared daemon when reachable, falling back to the
    in-process tool, mirroring the backstop's per-file resolution so a hung
    daemon cannot stack timeouts across the touched files.
    """

    def resolve(abs_path: str) -> str | None:
        if daemon_state is None or daemon_state.get("available", True):
            try:
                from chameleon_mcp import daemon_client

                res = daemon_client.call(
                    "get_archetype", {"repo": str(repo_root), "file_path": abs_path}
                )
                if res:
                    return (res.get("data") or {}).get("archetype")
                if daemon_state is not None:
                    daemon_state["available"] = False
            except Exception:
                if daemon_state is not None:
                    daemon_state["available"] = False
        try:
            from chameleon_mcp.tools import get_archetype

            return (get_archetype(str(repo_root), abs_path).get("data") or {}).get("archetype")
        except Exception:
            return None

    return resolve


def _correctness_judge_route(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    daemon_state: dict | None,
    is_subagent: bool,
) -> dict:
    """Decide whether this Stop spawns the correctness reviewer, and on what.

    Per-turn, digest-keyed routing: only files fresh at their current content
    digest count, and fresh turns are routed by cheap risk facts -- security
    surface, unarchetyped files, importer blast radius (unknown ESCALATES,
    never reads as zero) -- under the per-session spawn budget. Captured intent
    tokens force a spawn regardless of tier so a request's checkable specifics
    always get a second read. SubagentStop never routes (a multi-subagent turn
    would multiply spawns; the parent turn's Stop re-sees the edits). Every
    skip is recorded as a check event so the attestation sees the un-run check
    instead of inferring cleanliness. Fails open to no-spawn.

    Returns ``{"spawn", "fresh", "digests", "turn_key", "intent_tokens",
    "skip_reason", "reason"}`` where ``fresh`` is absolute paths, ``digests``
    maps repo-relative path -> 16-hex digest, and ``reason`` names the spawn
    trigger for the event log.
    """
    no_spawn: dict = {
        "spawn": False,
        "fresh": [],
        "digests": {},
        "turn_key": None,
        "intent_tokens": [],
        "skip_reason": None,
        "reason": None,
    }
    try:
        if cfg.mode == "off":
            _emit_check_event(repo_id, session_id, "correctness_judge", "skipped", "mode_off")
            return {**no_spawn, "skip_reason": "mode_off"}
        # multi_lens_review consumes this same route (it replaces the correctness
        # gate), so the route still computes a spawn decision when EITHER review
        # is enabled -- gating only on correctness_judge would silently disable
        # the multi-lens pass.
        if not cfg.correctness_judge and not getattr(cfg, "multi_lens_review", False):
            _emit_check_event(
                repo_id, session_id, "correctness_judge", "skipped", "feature_disabled"
            )
            return {**no_spawn, "skip_reason": "feature_disabled"}
        if is_subagent:
            return {**no_spawn, "skip_reason": "subagent"}

        from chameleon_mcp.violation_class import ignored_rules

        # An edited file that still exists, not opted out via an inline
        # `chameleon-ignore` (bare) directive in the touched file.
        edited: list[str] = []
        for path in state.files:
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
            return {**no_spawn, "skip_reason": "no_edits"}

        from chameleon_mcp import duplication_review as dr

        # Freshness: digest over the same first-1MB byte window the duplication
        # gate keys its markers on, so the two judged-namespaces stay congruent.
        fresh: list[str] = []
        digests: dict[str, str] = {}
        for path in edited:
            try:
                raw = Path(path).read_bytes()[:1_000_000]
            except OSError:
                continue
            digest = hashlib.sha256(raw).hexdigest()[:16]
            rel = dr._repo_rel(repo_root, path)
            digests[rel] = digest
            if not dr.already_judged(
                repo_data, session_id or "", rel, digest, prefix=_CORR_JUDGED_PREFIX
            ):
                fresh.append(path)
        if not fresh:
            _emit_check_event(repo_id, session_id, "correctness_judge", "skipped_digest_dup")
            return {**no_spawn, "digests": digests, "skip_reason": "digest_dup"}

        fresh_rels = [dr._repo_rel(repo_root, p) for p in fresh]
        pair_blob = "\x00".join(f"{rel}\x00{digests[rel]}" for rel in sorted(fresh_rels))
        turn_key = hashlib.sha256(pair_blob.encode("utf-8")).hexdigest()[:32]

        from chameleon_mcp._thresholds import threshold_int

        if state.correctness_spawns >= threshold_int("CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION"):
            _emit_check_event(
                repo_id,
                session_id,
                "correctness_judge",
                "skipped_session_cap",
                detail={"turn_key": turn_key},
            )
            return {
                **no_spawn,
                "digests": digests,
                "turn_key": turn_key,
                "skip_reason": "session_cap",
            }

        try:
            from chameleon_mcp import judge_async

            if judge_async.is_inflight_fresh(repo_data, session_id or ""):
                _emit_check_event(
                    repo_id,
                    session_id,
                    "correctness_judge",
                    "inflight_at_stop",
                    detail={"turn_key": turn_key},
                )
                return {
                    **no_spawn,
                    "digests": digests,
                    "turn_key": turn_key,
                    "skip_reason": "inflight",
                }
        except Exception:
            pass

        # Intent trigger: checkable tokens or a security-lens hit captured since
        # the last spawn force the review regardless of risk tier; the tokens
        # also ride into the prompt.
        intent_tokens: list[str] = []
        security_intent = False
        try:
            from chameleon_mcp import intent_capture
            from chameleon_mcp.exec_log import read_check_events

            entries = intent_capture.read_intent(repo_data, session_id)
            since_ts: float | None = None
            try:
                ev = read_check_events(
                    repo_id, session_id or "", limit=threshold_int("ATTESTATION_MAX_CHECK_EVENTS")
                )
                spawn_ts = [
                    e.get("ts")
                    for e in ev.get("events") or []
                    if e.get("check") == "correctness_judge"
                    and e.get("status") == "spawned"
                    and isinstance(e.get("ts"), (int, float))
                ]
                since_ts = max(spawn_ts) if spawn_ts else None
            except Exception:
                since_ts = None
            intent_tokens = intent_capture.checkable_tokens(entries, since_ts)
            security_intent = intent_capture.security_intent_seen(entries, since_ts)
        except Exception:
            intent_tokens = []
            security_intent = False

        base = {
            "spawn": True,
            "fresh": fresh,
            "digests": digests,
            "turn_key": turn_key,
            "intent_tokens": intent_tokens,
            "skip_reason": None,
        }
        if intent_tokens or security_intent:
            return {**base, "reason": "intent_forced"}

        # Risk facts over the fresh set, every leg fail-open toward spawning.
        try:
            from chameleon_mcp import autopass

            security = bool(autopass.security_surface_categories(fresh_rels))
        except Exception:
            security = True

        resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})
        unarchetyped = 0
        for path in fresh:
            try:
                if resolver(path) is None:
                    unarchetyped += 1
            except Exception:
                unarchetyped += 1

        # Blast radius from the reverse index. UNKNOWN escalates: a missing
        # index or a failed read must route toward review, never read as zero.
        blast = 0
        blast_unknown = False
        try:
            from chameleon_mcp.tools import query_symbol_importers

            for path in fresh:
                envelope = query_symbol_importers(str(repo_root), path)
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
            return {**base, "reason": "risk_high"}
        if unarchetyped > 0 or len(fresh) > threshold_int("AUTOPASS_MAX_FILES"):
            return {**base, "reason": "risk_elevated"}
        # Low risk: the first routed turn of a session still spawns, preserving
        # at-least-once coverage; later low-risk turns skip with a recorded
        # event so the attestation sees the un-run check.
        if state.correctness_spawns == 0:
            return {**base, "reason": "first_low_risk"}
        _emit_check_event(
            repo_id,
            session_id,
            "correctness_judge",
            "routed_skip_low_risk",
            detail={"turn_key": turn_key},
        )
        return {
            **no_spawn,
            "digests": digests,
            "turn_key": turn_key,
            "intent_tokens": intent_tokens,
            "skip_reason": "routed_skip_low_risk",
        }
    except Exception:
        return no_spawn


def _judge_async_mode() -> str | None:
    """Which detached-judge route this Stop should try, or None for sync.

    ``CHAMELEON_JUDGE_ASYNC=1`` is the operator opt-in; ``=0`` is the operator
    override that forces sync even when bare auth is known failed (the sync
    plain spawn then likely times out, which the SessionStart judge-health
    banner and doctor surface). Unset, a known bare-auth failure auto-prefers
    the detached path: the plain fallback spawn pays the full session primer
    and cannot fit the sync Stop budget (the stop-backstop wrapper caps the
    hook at 55s), so detaching is the only spawn shape that can finish. The
    POSIX-only platform gate stays inside ``launch_async_judge``; on other
    platforms the launch returns False and the caller falls back to sync.
    """
    env = os.environ.get("CHAMELEON_JUDGE_ASYNC")
    if env == "0":
        return None
    if env == "1":
        return "async_opt_in"
    try:
        from chameleon_mcp.judge import _bare_auth_known_failed

        if _bare_auth_known_failed():
            return "async_auto_bare_fallback"
    except Exception:
        pass
    return None


def _correctness_judge_gate(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    daemon_state: dict | None,
    route: dict,
) -> dict | None:
    """Independent turn-end correctness review of the turn's edits (advisory).

    Reached only on the no-block stop path, after the idiom gate declined to
    block. The spawn decision was made by ``_correctness_judge_route``; this
    executes it: the per-session spawn counter is persisted BEFORE the
    reviewer runs so an interrupted Stop still consumes budget, each fresh
    file is marked judged at its captured digest only on a completed spawn (a
    failed spawn stays fresh for retry under the cap), and when the async
    route is selected (``_judge_async_mode``: operator opt-in, or
    automatically on a known bare-auth failure) the spawn detaches and the
    findings arrive on the next user prompt instead. The sync path writes the
    spawn outcome back into ``route["spawn_failed"]`` so the duplication gate
    (which runs after this) defers only to a spawn that produced a reviewable
    result; a detached async spawn leaves the key unset (outcome unknown this
    Stop, the deferral holds).

    Returns a hook output dict carrying the findings as ``additionalContext``, or
    None when the gate does not fire / found nothing (the caller then allows the
    stop). It NEVER returns a block: the judge is stochastic and advisory, so its
    findings are surfaced as context the model may act on, never a turn-trap. The
    findings are shadow-logged for later human-labeled precision sampling. Fails
    open: any error returns None.
    """
    try:
        if not route.get("spawn"):
            return None

        from chameleon_mcp import duplication_review as dr
        from chameleon_mcp import judge

        turn_key = route.get("turn_key")
        digests: dict = route.get("digests") or {}
        fresh: list[str] = route.get("fresh") or []
        intent_tokens: list[str] = route.get("intent_tokens") or []

        # Spend a spawn: count it and persist BEFORE the (potentially slow)
        # reviewer call so an interrupted Stop still consumes the budget and
        # the session cap holds.
        state.correctness_spawns += 1
        try:
            from chameleon_mcp.enforcement import save_state

            save_state(state, repo_data, session_id or "")
        except Exception:
            pass

        async_mode = _judge_async_mode()
        launched = False
        if async_mode is not None:
            try:
                from chameleon_mcp import judge_async

                launched = judge_async.launch_async_judge(
                    repo_root=repo_root,
                    repo_data=repo_data,
                    repo_id=repo_id,
                    session_id=session_id or "",
                    fresh_abs_paths=fresh,
                    digests=digests,
                    turn_key=turn_key,
                    intent_tokens=intent_tokens,
                )
            except Exception:
                launched = False

        # The mode rides the spawn event so an attestation replay can tell an
        # auto-detached spawn (bare auth known failed) from the operator
        # opt-in and from a plain synchronous run.
        _emit_check_event(
            repo_id,
            session_id,
            "correctness_judge",
            "spawned",
            route.get("reason") or "started",
            detail={"turn_key": turn_key, "mode": async_mode if launched else "sync"},
        )
        if launched:
            # Findings arrive on the next UserPromptSubmit; the detached
            # child marks the judged digests and records its own events.
            return None

        failures: list[str] = []
        # Every non-facts sink kind (spawn failure, pipeline error, or
        # unparseable output) means the reviewer produced no reviewable
        # verdict this Stop. The duplication gate reads the aggregate through
        # route["spawn_failed"]: deferring behind a review that never
        # happened would starve duplication review for as long as the
        # reviewer stays broken.
        degraded: list[str] = []

        def _sink(kind: str, detail: str | None = None) -> None:
            # The caller-facts outcome rides the same sink but is its own
            # check, not a degradation: one judge_facts event per spawn
            # attempt (included / skipped_no_calls_index / skipped_disabled)
            # so the attestation can tell a grounded review from a blind one.
            if kind.startswith("judge_facts_"):
                _emit_check_event(
                    repo_id,
                    session_id,
                    "judge_facts",
                    kind[len("judge_facts_") :],
                    detail={"turn_key": turn_key},
                )
                return
            if kind in _JUDGE_FAILURE_KINDS:
                failures.append(kind)
            degraded.append(kind)
            _emit_check_event(
                repo_id,
                session_id,
                "correctness_judge",
                "degraded_spawn",
                kind,
                detail={"turn_key": turn_key, "detail": detail},
            )

        # Pessimistic until the run completes: if anything below dies before
        # the outcome is known, the spawn must read as failed, never as a
        # review the duplication gate should keep deferring to.
        route["spawn_failed"] = True

        resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})
        findings = judge.run_correctness_judge(
            repo_root,
            repo_root / ".chameleon",
            fresh,
            resolver,
            intent_tokens=intent_tokens,
            event_sink=_sink,
        )
        route["spawn_failed"] = bool(degraded)

        if not failures:
            _emit_check_event(
                repo_id,
                session_id,
                "correctness_judge",
                "spawned",
                "completed",
                detail={"turn_key": turn_key, "findings": len(findings)},
            )
            for path in fresh:
                rel = dr._repo_rel(repo_root, path)
                dr.mark_judged(
                    repo_data,
                    session_id or "",
                    rel,
                    digests.get(rel, ""),
                    prefix=_CORR_JUDGED_PREFIX,
                )

        # Shadow-log every finding so a lead can sample judge precision over time.
        # The judge never blocks, so would_block is always False; the row records
        # the advisory finding, attributed to the file/line when known.
        try:
            from chameleon_mcp.metrics import emit_hook_metric

            for f in findings:
                emit_hook_metric(
                    "stop-correctness-judge",
                    elapsed_ms=0,
                    repo_id=repo_id,
                    advisory_emitted=True,
                    would_block=False,
                    rule="correctness-judge-finding",
                    # The reviewer reports a repo-relative path already; keep it as
                    # given rather than re-resolving against the working directory.
                    file_rel=f.file,
                    line=f.line,
                )
        except Exception:
            pass

        if not findings:
            return None

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        lines = [
            f"[🦎 chameleon: independent review flagged "
            f"{len(findings)} possible correctness issue"
            f"{'s' if len(findings) != 1 else ''}]",
            "A separate reviewer read this turn's changes. These are advisory; "
            "verify each before acting, they may be wrong.",
        ]
        for f in findings:
            loc = sanitize_for_chameleon_context(f.file) if f.file else "?"
            if f.line is not None:
                loc += f":{f.line}"
            lines.append(f"- {loc}: {sanitize_for_chameleon_context(f.message)}")

        block = "<chameleon-context>\n" + "\n".join(lines) + "\n</chameleon-context>"
        return {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": block,
            }
        }
    except Exception:
        return None


def _stale_test_advisory_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    preloaded,
    daemon_state: dict | None,
) -> list[str]:
    """Build the turn-end stale-test advisory lines, or [] when nothing applies.

    A source file edited this turn whose archetype's siblings overwhelmingly ship
    a paired test, but whose existing paired test went untouched, is at risk of
    going stale. For each such file, name the test path and the exports the edit
    may have moved out from under it. Advisory only: the pairing floor admits a
    sizable fraction of legitimately untested files, so this is a coverage nudge,
    never a block. Returns sanitized lines ready to fold into the Stop context;
    fails open to [] on any error.

    ``preloaded`` is the once-per-pass profile the backstop already loaded, so
    this reuses it rather than re-reading conventions.json. ``daemon_state`` is
    the shared liveness flag the backstop threads through archetype resolution.
    """
    try:
        if cfg.mode == "off" or not cfg.stale_test_advisory:
            return []
        if preloaded is None or not hasattr(preloaded, "conventions"):
            return []
        conv = preloaded.conventions.get("conventions", {}) or {}
        test_pairing = conv.get("test_pairing") or {}
        if not test_pairing:
            return []
        key_exports = conv.get("key_exports") or {}

        from chameleon_mcp.violation_class import ignored_rules

        # Edited files that still exist and are not opted out via an inline
        # `chameleon-ignore tests` (or bare ignore) directive in the touched file.
        edited_abs: set[str] = set()
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "tests" in ign or "stale-test" in ign:
                continue
            edited_abs.add(path)
        if not edited_abs:
            return []

        from chameleon_mcp.cochange import stale_test_items
        from chameleon_mcp.lint_engine import detect_language

        resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})

        def _read_content(abs_path: str) -> str | None:
            try:
                return Path(abs_path).read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                return None

        items = stale_test_items(
            repo_root=repo_root,
            test_pairing=test_pairing,
            key_exports=key_exports,
            edited_abs=edited_abs,
            archetype_of=resolver,
            language_of=detect_language,
            read_content=_read_content,
        )
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        max_files = threshold_int("STALE_TEST_ADVISORY_MAX_FILES")
        shown = items[:max_files]
        extra = len(items) - len(shown)

        lines = [
            f"[🦎 chameleon: {len(items)} edited source file"
            f"{'s' if len(items) != 1 else ''} may have left a paired test stale]",
            "You changed these files but did not touch their paired tests. Confirm "
            "the test still covers the new behavior; this is advisory, not a block.",
        ]
        for it in shown:
            src = sanitize_for_chameleon_context(it.source_rel)
            test = sanitize_for_chameleon_context(it.test_rel)
            line = f"- {src} -> test {test} (unchanged)"
            if it.exports:
                names = ", ".join(sanitize_for_chameleon_context(n) for n in it.exports)
                line += f"; changed exports: {names}"
            lines.append(line)
        if extra > 0:
            lines.append(f"- ...and {extra} more.")
        lines.append(
            "To silence this for a file, add `// chameleon-ignore tests` "
            "(`# chameleon-ignore tests` in Ruby) in the source you touched."
        )
        return lines
    except Exception:
        return []


def _new_files_in_changeset(repo_root: Path, edited_abs: set[str]) -> set[str]:
    """Subset of ``edited_abs`` that did not exist at HEAD (created this turn).

    A change-set-completeness rule only fires on a brand-new file: editing a
    method on an existing model must not demand a fresh migration. "New" is
    decided by git, which is the only turn-end signal available (the enforcement
    state does not record creation). A path is new when ``git ls-files`` does not
    track it AND ``git cat-file`` finds no blob for it at HEAD, so a file the user
    created and `git add`-ed this turn still counts as new.

    Fails safe to the empty set: when git is unavailable or the work tree cannot
    be probed, no file is treated as new and the change-set check stays silent
    rather than guess a creation it cannot confirm.
    """
    try:
        from chameleon_mcp.judge import _git_available, _run_git

        if not edited_abs or not _git_available(repo_root):
            return set()
        new_abs: set[str] = set()
        for ap in edited_abs:
            try:
                rel = Path(ap).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            tracked = _run_git(["ls-files", "--error-unmatch", "--", rel], cwd=repo_root)
            if tracked is not None and tracked.returncode == 0:
                # Tracked in the index; not a file this turn created.
                continue
            at_head = _run_git(["cat-file", "-e", f"HEAD:{rel}"], cwd=repo_root)
            if at_head is not None and at_head.returncode == 0:
                # Exists in the HEAD tree; an existing file, not a creation.
                continue
            new_abs.add(ap)
        return new_abs
    except Exception:
        return set()


def _changeset_completeness_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    daemon_state: dict | None,
) -> list[str]:
    """Build the turn-end change-set-completeness advisory lines, or [].

    When a turn creates a NEW file that conventionally cannot stand alone (a Rails
    model needs a migration, a new controller a route) but the change-set carries
    no matching companion, surface a nudge to add it. Driven by a hand-curated
    framework pair table; each rule is silenced for a repo whose own committed
    files already break the pairing often enough that firing it would nag.
    Advisory only, never a block: a partial edit may legitimately defer its
    companion to a follow-up commit. Returns sanitized lines ready to fold into
    the Stop context; fails open to [] on any error.

    ``daemon_state`` is unused here (the check is path-pattern only and needs no
    archetype resolution) but kept in the signature to mirror the sibling
    advisory builders the backstop calls.
    """
    try:
        if cfg.mode == "off" or not cfg.changeset_completeness:
            return []

        from chameleon_mcp.violation_class import ignored_rules

        # Edited files that still exist and are not opted out via an inline
        # `chameleon-ignore cochange` (or bare ignore) directive in the file.
        edited_abs: set[str] = set()
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "cochange" in ign:
                continue
            edited_abs.add(path)
        if not edited_abs:
            return []

        new_files_abs = _new_files_in_changeset(repo_root, edited_abs)
        if not new_files_abs:
            return []

        from chameleon_mcp.cochange import changeset_completeness_items, cochange_rule_disabled
        from chameleon_mcp.lint_engine import detect_language

        # Cache the per-rule repo-applicability verdict across the turn's new
        # files so the bounded committed-file walk runs at most once per rule.
        disabled_cache: dict[str, bool] = {}

        def _rule_enabled(rule) -> bool:
            verdict = disabled_cache.get(rule.rule_id)
            if verdict is None:
                verdict = cochange_rule_disabled(rule, repo_root)
                disabled_cache[rule.rule_id] = verdict
            return not verdict

        items = changeset_completeness_items(
            repo_root=repo_root,
            new_files_abs=new_files_abs,
            edited_abs=edited_abs,
            language_of=detect_language,
            rule_enabled=_rule_enabled,
        )
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        max_items = threshold_int("COCHANGE_ADVISORY_MAX_ITEMS")
        shown = items[:max_items]
        extra = len(items) - len(shown)

        lines = [
            f"[🦎 chameleon: {len(items)} new file"
            f"{'s' if len(items) != 1 else ''} may be missing a companion change]",
            "You created these files but the change-set has no matching companion. "
            "Add it or confirm it is a separate commit; this is advisory, not a block.",
        ]
        for it in shown:
            src = sanitize_for_chameleon_context(it.source_rel)
            msg = sanitize_for_chameleon_context(it.message)
            lines.append(f"- {src}: {msg}")
        if extra > 0:
            lines.append(f"- ...and {extra} more.")
        lines.append(
            "To silence this for a file, add `// chameleon-ignore cochange` "
            "(`# chameleon-ignore cochange` in Ruby) in the file you touched."
        )
        return lines
    except Exception:
        return []


def _crossfile_existence_advisory_lines(
    *,
    repo_root: Path,
    state,
    cfg,
) -> list[str]:
    """Build the turn-end cross-file existence-break advisory lines, or [].

    For each TypeScript source the turn touched, recompute its current export set
    (a regex read, no parser) and consult the prebuilt reverse index for names an
    indexed importer still references that the file NO LONGER exports. Each such
    removed/renamed export is a call site this turn broke; name it and its still-
    referencing importers. Advisory only, never a block: a mid-rename turn may
    legitimately leave a site for a follow-up edit. Bounded -- the index is read
    once, each touched file's exports come from a single regex pass, and the
    importer presence check is a word-boundary scan, so no caller is parsed at
    Stop. Returns sanitized lines ready to fold into the Stop context; fails open
    to [] on any error.
    """
    try:
        if cfg.mode == "off" or not cfg.crossfile_existence_advisory:
            return []

        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.phantom_imports import _current_export_names
        from chameleon_mcp.symbol_index import load_reverse_index, module_key_for_path
        from chameleon_mcp.violation_class import ignored_rules

        index = load_reverse_index(repo_root)
        if index is None:
            return []

        from chameleon_mcp._thresholds import threshold_int

        max_files = threshold_int("CROSSFILE_STOP_ADVISORY_MAX_FILES")
        max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")

        def _name_present(importer_rel: str, name: str, line: int | None) -> bool:
            # Cheap presence check: the index is a bootstrap snapshot, so confirm
            # the importer still names the binding (the rename may have reached it
            # too) before claiming its call site is broken. No parse -- a
            # word-boundary regex over the importer bytes.
            ip = repo_root / importer_rel
            try:
                text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                return False
            needle = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")
            text_lines = text.splitlines()
            if line is not None and 1 <= line <= len(text_lines):
                if needle.search(text_lines[line - 1]):
                    return True
            return bool(needle.search(text))

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        # symbol@module -> list of sanitized "path:line" sites, in touch order so
        # the advisory lists the breaks this turn introduced.
        breaks: list[tuple[str, str, list[str]]] = []
        seen_files = 0
        for path in state.files:
            if seen_files >= max_files:
                break
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            if detect_language(str(p)) != "typescript":
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "removed-export-breaks-importers" in ign:
                continue
            target_key = module_key_for_path(p, repo_root)
            if target_key is None:
                continue
            current, open_set = _current_export_names(content)
            if open_set:
                # `export * from` re-exports an unknown set, so a name absent from
                # the visible set may still be exported -- skip, matching the
                # edit-time and tool stance.
                continue
            seen_files += 1
            broken = index.broken_importers(target_key, current)
            if not broken:
                continue
            for name in sorted(broken):
                importers = broken[name]
                live = [imp for imp in importers if _name_present(imp.path, name, imp.line)]
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
                )
                sites = [
                    sanitize_for_chameleon_context(
                        f"{imp.path}:{imp.line}" if imp.line is not None else imp.path
                    )
                    for imp in live_sorted[:max_sites]
                ]
                breaks.append((sanitize_for_chameleon_context(name), target_key, sites))

        if not breaks:
            return []

        lines = [
            f"[🦎 chameleon: {len(breaks)} export"
            f"{'s' if len(breaks) != 1 else ''} you removed still ha"
            f"{'ve' if len(breaks) != 1 else 's'} live importers]",
            "These exports are gone from the files you edited but other files "
            "still import them by name; their call sites are now broken. Restore "
            "the export or update the importers. This is advisory, not a block.",
        ]
        for name, _module, sites in breaks:
            shown = ", ".join(sites)
            more = " ..." if len(sites) >= max_sites else ""
            lines.append(f"- '{name}' no longer exported; still imported by {shown}{more}")
        lines.append(
            "To silence this for a file, add "
            "`// chameleon-ignore removed-export-breaks-importers` in the source "
            "you touched."
        )
        return lines
    except Exception:
        return []


def _duplication_advisory_lines(
    *,
    repo_root: Path,
    repo_id: str | None = None,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    corr_spawning: bool,
) -> list[str]:
    """Build the turn-end duplication advisory lines, or [].

    For each function the turn introduced whose body hash matches an existing one
    (the committed catalog or a function added earlier this session), name the
    original so the author can reuse it instead of re-implementing it. A bounded
    judge spawn confirms each body match is a real re-implementation, not just a
    structural coincidence, before it is surfaced. Advisory only, never a block.

    Heavily gated so it costs at most one extra model spawn per session and never
    repeats work: it stays silent when the correctness judge spawned a reviewer
    that produced a reviewable result this Stop (``corr_spawning`` -- the caller
    clears it when that spawn degraded, so a broken reviewer never starves this
    gate), when the per-session spawn cap is reached, and for any (file,
    content-digest) already judged. The spawn is counted and persisted BEFORE it
    runs so a timeout cannot slip past the cap. Returns sanitized lines ready to
    fold into the Stop context; fails open to [] on any error.
    """
    try:
        if not cfg.duplication_review:
            _emit_check_event(
                repo_id, session_id, "duplication_review", "skipped", "feature_disabled"
            )
            return []
        if cfg.mode == "off":
            _emit_check_event(repo_id, session_id, "duplication_review", "skipped", "mode_off")
            return []
        # The correctness judge is the other heavy turn-end spawn; when it is
        # firing this Stop, defer the duplication spawn so a single turn never
        # pays for two reviewer model calls.
        if corr_spawning:
            _emit_check_event(
                repo_id, session_id, "duplication_review", "skipped", "corr_judge_active"
            )
            return []

        from chameleon_mcp._thresholds import threshold_int

        if state.duplication_spawns >= threshold_int("DUPLICATION_REVIEW_MAX_SPAWNS_PER_SESSION"):
            _emit_check_event(repo_id, session_id, "duplication_review", "skipped", "cap_reached")
            return []

        from chameleon_mcp import duplication_review as dr

        edited = [p for p in state.files if Path(p).is_file()]
        if not edited:
            return []

        # Single catalog language per pass: the body-hash catalog is one language,
        # so infer it from the first edited file and let gather drop the rest.
        lang = dr._lang_of(edited[0])

        index = dr.build_candidate_index(repo_root, edited)

        # Only files not already judged at their CURRENT content contribute, so a
        # repeated unchanged turn never re-spawns. Digest is sha256 of the first
        # 1 MB of bytes, the same window the rest of the Stop path reads.
        fresh: list[str] = []
        digests: dict[str, str] = {}
        for p in edited:
            try:
                content = Path(p).read_bytes()[:1_000_000]
            except OSError:
                continue
            d = hashlib.sha256(content).hexdigest()[:16]
            digests[p] = d
            rel = dr._repo_rel(repo_root, p)
            if not dr.already_judged(repo_data, session_id or "", rel, d):
                fresh.append(p)
        if not fresh:
            _emit_check_event(
                repo_id, session_id, "duplication_review", "skipped", "digest_already_judged"
            )
            return []

        # The committed catalog feeds the semantic pass (different-body,
        # same-intent re-implementations the body-hash index cannot see); loaded
        # from cache, None on any issue -> the semantic pass contributes nothing.
        try:
            from chameleon_mcp.function_catalog import load_function_catalog

            catalog = load_function_catalog(repo_root)
        except Exception:
            catalog = None

        findings = dr.gather_findings(repo_root, fresh, index=index, catalog=catalog, lang=lang)
        if not findings:
            return []

        # Spend a spawn: count it and persist BEFORE the (potentially slow) judge
        # call so an interrupted Stop still consumes the budget and the cap holds.
        state.duplication_spawns += 1
        try:
            from chameleon_mcp.enforcement import save_state

            save_state(state, repo_data, session_id or "")
        except Exception:
            pass

        _emit_check_event(repo_id, session_id, "duplication_review", "ran")
        confirmed = dr.judge_body_matches(repo_root, findings, semantic=True)
        # Mark every fresh file judged at its current digest so the next turn over
        # the same content is suppressed regardless of whether it was confirmed.
        for p in fresh:
            dr.mark_judged(
                repo_data, session_id or "", dr._repo_rel(repo_root, p), digests.get(p, "")
            )
        return dr.format_duplication_advisory(confirmed)
    except Exception:
        return []


def _test_integrity_advisory_lines(
    *,
    repo_root: Path,
    repo_id: str | None = None,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
) -> list[str]:
    """Turn-end test-integrity advisory, or [].

    When the turn changed live source AND weakened tests (added skip markers,
    dropped assertions, net test deletion -- the deterministic signals the
    auto-pass router already computes), name what was weakened so the author
    restores coverage before the PR. Deterministic, zero model spawn. Deduped per
    (session, diff-digest) so an unchanged weakening does not re-nag every later
    turn that touches the file. Advisory only; fails open to [].
    """
    try:
        if not getattr(cfg, "test_integrity_review", True):
            _emit_check_event(
                repo_id, session_id, "test_integrity_review", "skipped", "feature_disabled"
            )
            return []
        if cfg.mode == "off":
            _emit_check_event(repo_id, session_id, "test_integrity_review", "skipped", "mode_off")
            return []

        from chameleon_mcp import test_integrity as ti

        edited = list(state.files)
        if not edited:
            return []
        diff_text = ti.build_turn_diff(repo_root, edited)
        assessment = ti.assess_test_weakening(diff_text, edited)
        if not assessment:
            return []

        # Dedup per (session, diff-digest): the same weakening left in place must
        # not re-fire on every later turn. Reuses the duplication gate's marker
        # helpers under a distinct namespace so the two judged-sets never collide.
        digest = hashlib.sha256((diff_text or "").encode()).hexdigest()[:16]
        from chameleon_mcp import duplication_review as dr

        if dr.already_judged(
            repo_data, session_id or "", "testint", digest, prefix=".testint_judged."
        ):
            _emit_check_event(
                repo_id, session_id, "test_integrity_review", "skipped", "digest_already_emitted"
            )
            return []
        dr.mark_judged(repo_data, session_id or "", "testint", digest, prefix=".testint_judged.")
        _emit_check_event(repo_id, session_id, "test_integrity_review", "ran")
        return ti.format_test_integrity_advisory(assessment)
    except Exception:
        return []


def _multi_lens_review_lines(
    *,
    repo_root: Path,
    repo_id: str | None = None,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    daemon_state: dict | None,
    route: dict,
) -> list[str]:
    """Opt-in coordinated multi-lens turn-end review, or [].

    When ``enforcement.multi_lens_review`` is on, this replaces the separate
    correctness-judge and duplication gates: it runs both as lenses (no mutual
    defer, so duplication is not starved) and merges their findings through
    ``lens_synthesis`` -- a finding two lenses raise independently surfaces on
    agreement, a lone lens only at high confidence. Reuses the correctness route's
    spawn decision (cap + per-file digest dedup) as the trigger, spends one review
    budget unit for the pass, and marks the fresh digests judged so an unchanged
    turn does not re-fire. Advisory only, never a block; fails open to [].

    The lenses assemble their inputs lazily (inside the thunks) so nothing heavy
    runs until ``run_lenses`` actually invokes a lens.
    """
    try:
        if not getattr(cfg, "multi_lens_review", False):
            return []
        if cfg.mode == "off":
            _emit_check_event(repo_id, session_id, "multi_lens_review", "skipped", "mode_off")
            return []
        if not route.get("spawn"):
            _emit_check_event(
                repo_id,
                session_id,
                "multi_lens_review",
                "skipped",
                route.get("reason") or "no_spawn",
            )
            return []

        from chameleon_mcp import duplication_review as dr
        from chameleon_mcp import judge, lens_runner

        fresh: list[str] = route.get("fresh") or []
        intent_tokens: list[str] = route.get("intent_tokens") or []
        digests: dict = route.get("digests") or {}
        if not fresh:
            return []

        def _run_correctness():
            resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})
            return judge.run_correctness_judge(
                repo_root,
                repo_root / ".chameleon",
                fresh,
                resolver,
                intent_tokens=intent_tokens,
            )

        def _run_duplication():
            edited = [p for p in state.files if Path(p).is_file()]
            if not edited:
                return []
            lang = dr._lang_of(edited[0])
            index = dr.build_candidate_index(repo_root, edited)
            try:
                from chameleon_mcp.function_catalog import load_function_catalog

                catalog = load_function_catalog(repo_root)
            except Exception:
                catalog = None
            findings = dr.gather_findings(repo_root, fresh, index=index, catalog=catalog, lang=lang)
            if not findings:
                return []
            return dr.judge_body_matches(repo_root, findings, semantic=True)

        # Honor the per-lens enforcement flags: multi_lens replaces the gates but
        # must not resurrect a lens the operator turned off (duplication_review /
        # correctness_judge). A lens left out simply does not run.
        lenses = []
        ran_duplication = False
        if getattr(cfg, "correctness_judge", True):
            lenses.append(lens_runner.correctness_lens(_run_correctness))
        if getattr(cfg, "duplication_review", True):
            lenses.append(lens_runner.duplication_lens(_run_duplication))
            ran_duplication = True
        if not lenses:
            _emit_check_event(repo_id, session_id, "multi_lens_review", "skipped", "no_lenses")
            return []

        # Spend the review budget and persist BEFORE the (slow) lens spawns so an
        # interrupted Stop still consumes the budget and the session cap holds.
        # correctness_spawns is the route's budget counter, so it always advances
        # (the pass is the unit it caps); duplication_spawns advances too when the
        # duplication lens ran, so a later turn with multi_lens off sees the
        # duplication budget already spent rather than double-reviewing.
        state.correctness_spawns += 1
        if ran_duplication:
            state.duplication_spawns += 1
        try:
            from chameleon_mcp.enforcement import save_state

            save_state(state, repo_data, session_id or "")
        except Exception:
            pass

        _emit_check_event(
            repo_id,
            session_id,
            "multi_lens_review",
            "ran",
            detail={"turn_key": route.get("turn_key")},
        )
        synthesized = lens_runner.run_lenses(lenses, max_lenses=2)

        # Mark fresh files judged at their captured digest so the next turn over
        # the same content does not re-spawn (shared with the correctness gate's
        # namespace -- the two never both run a turn).
        for path in fresh:
            rel = dr._repo_rel(repo_root, path)
            dr.mark_judged(
                repo_data, session_id or "", rel, digests.get(rel, ""), prefix=_CORR_JUDGED_PREFIX
            )

        surfaced = [f for f in synthesized if f.get("surface")]
        if not surfaced:
            return []

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        n = len(surfaced)
        lines = [
            f"[🦎 chameleon: multi-lens review flagged {n} possible issue{'s' if n != 1 else ''}]",
            "A coordinated review (correctness + duplication) read this turn's changes. "
            "These are advisory; verify each before acting, they may be wrong.",
        ]
        for f in surfaced:
            loc = sanitize_for_chameleon_context(str(f.get("file"))) if f.get("file") else "?"
            if f.get("line") is not None:
                loc += f":{f.get('line')}"
            lens_tag = sanitize_for_chameleon_context("+".join(f.get("lenses") or []))
            claim = sanitize_for_chameleon_context(str(f.get("claim", "")))
            lines.append(f"- {loc} [{lens_tag}]: {claim}")
        return lines
    except Exception:
        return []


def _stop_gates(
    *,
    payload: dict,
    repo_root: Path,
    repo_id: str,
    session_id,
    is_subagent: bool,
    repo_data: Path,
    daemon_state: dict | None = None,
) -> dict:
    """Run the turn-end gates and return the hook-output dict (never emits).

    Mechanical extraction of stop_backstop's gate pipeline so the caller can
    write the session attestation at a single site after every gate finished
    and saved state. Ordering and blocking semantics are unchanged from when
    this body lived inline. CHAMELEON_ENFORCE=0 is checked here rather than
    before repo resolution so an enforce-off session still reaches the caller's
    attestation write with its env state recorded; it returns {} immediately,
    exactly as the old early return did. Fails open to {}.
    """
    try:
        from chameleon_mcp.enforcement import load_state, save_state
        from chameleon_mcp.profile.config import load_config

        if os.environ.get("CHAMELEON_ENFORCE") == "0":
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "enforce_env_off")
            return {}

        cfg = load_config(repo_root / ".chameleon").enforcement
        if not cfg.stop_backstop:
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "feature_disabled")
            return {}

        state = load_state(repo_data, session_id or "")

        # Cap reached: stay advisory and never block again this session, so a
        # violation the model can't resolve cannot trap the turn in a loop.
        if state.stop_hook_blocks >= cfg.stop_block_cap:
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "cap_reached")
            return {}

        # A candidate (L2 with the cached flag set) is only blocked after a LIVE
        # re-lint confirms an enforceable hard violation still stands; the cached
        # flag alone can be stale (e.g. a phantom import resolved by a later edit
        # to the import target, leaving the importing file un-reverified). Files
        # that re-verify clean have their flag cleared and the state persisted, so
        # the heal sticks and they aren't re-checked next turn.
        #
        # Load the profile once for the whole pass: every candidate re-lints
        # against the same profile, so re-reading it per file just multiplies the
        # stat + witness recalibration cost across the candidate set.
        preloaded = None
        try:
            from chameleon_mcp.profile.loader import load_profile_dir
            from chameleon_mcp.tools import _effective_profile_dir

            preloaded = load_profile_dir(_effective_profile_dir(repo_root))
        except Exception:
            preloaded = None

        # Read the active block rules once for the whole pass. Each candidate
        # re-lint consults the same set, so reading enforcement.json per file just
        # multiplies the disk I/O across the candidate set.
        active_rules: set[str] | None = None
        try:
            from chameleon_mcp.enforcement_calibration import active_block_rules

            active_rules = active_block_rules(repo_root / ".chameleon")
        except Exception:
            active_rules = None

        # Shared liveness flag for the per-file daemon fallback: once a daemon
        # call comes back empty, every later file skips the daemon and resolves
        # the archetype in-process, so a hung daemon cannot stack timeouts. The
        # caller shares the same flag with the attestation writer so the whole
        # Stop pays for at most one failed daemon probe.
        if daemon_state is None:
            daemon_state = {"available": True}

        unresolved: list[str] = []
        # path -> enforceable hard rules still standing, so the shadow would_block
        # row can attribute the backstop block to the specific rules per file.
        unresolved_rules: dict[str, list[str]] = {}
        cleared_any = False
        for path, fs in list(state.files.items()):
            p = Path(path)
            if not p.is_file():
                # The file was deleted since it was recorded; drop its entry so
                # state does not accumulate phantom paths across the session.
                del state.files[path]
                cleared_any = True
                continue
            # Any file the per-edit verifier armed (cached blockable flag) is a
            # re-check candidate, regardless of escalation level. The level gate
            # belongs inside the re-lint, not here: a deterministic content fact
            # (a leaked credential, a phantom import) refuses the turn on the
            # first edit, while an archetype-dependent rule still honors the L2
            # ladder. Filtering on L2 here disarmed the documented turn-end
            # refusal for a single-edit secret/phantom that sits at L0/L1.
            if not fs.blockable_unresolved:
                continue
            file_rules: list[str] = []
            if _stop_file_still_blockable(
                repo_root,
                path,
                loaded=preloaded,
                active=active_rules,
                daemon_state=daemon_state,
                out_rules=file_rules,
                level=fs.level,
            ):
                unresolved.append(path)
                unresolved_rules[path] = file_rules
            else:
                fs.blockable_unresolved = False
                cleared_any = True

        if cleared_any:
            try:
                save_state(state, repo_data, session_id or "", prune_missing=True)
            except Exception:
                pass

        # The candidate re-lint completed (possibly over zero candidates):
        # record it so the attestation can attest the relint ran this Stop.
        _emit_check_event(repo_id, session_id, "stop_relint", "ran")

        if not unresolved:
            # No lint block this stop: run the reflexive idiom/principle review
            # gate. It blocks once per session (enforce) to force a self-review of
            # the turn's edits, else allows the stop.
            gate = _idiom_review_gate(
                repo_root=repo_root,
                repo_id=repo_id,
                session_id=session_id,
                state=state,
                cfg=cfg,
                repo_data=repo_data,
            )
            idiom_advisory: str | None = None
            if gate is not None:
                if gate.get("decision") == "block":
                    return gate
                # Shadow mode hands back the review as a non-blocking advisory;
                # fold it into this Stop's context with the other advisories.
                idiom_advisory = (gate.get("hookSpecificOutput") or {}).get("additionalContext")
            # Whether the correctness judge will spawn its reviewer THIS Stop,
            # routed per turn (digest freshness + risk facts + session budget)
            # before the gate runs. The duplication gate reads this to defer
            # when the judge is already paying for a spawn, so a single turn
            # never fires two reviewer models.
            route = _correctness_judge_route(
                repo_root=repo_root,
                repo_id=repo_id,
                session_id=session_id,
                state=state,
                cfg=cfg,
                repo_data=repo_data,
                daemon_state=daemon_state,
                is_subagent=is_subagent,
            )
            corr_spawning = bool(route.get("spawn"))

            # Idiom gate did not block: the turn is free to end. Run the
            # independent correctness judge (on by default, advisory only,
            # per-turn routed). It never blocks; its findings ride out as
            # additionalContext the model reads after the turn.
            #
            # When the opt-in multi-lens review is on (default off), ONE
            # coordinated pass runs the correctness + duplication lenses together
            # (no mutual defer) and REPLACES both the correctness gate here and
            # the duplication gate below. Subagents keep the standard gate.
            multilens_lines: list[str] = []
            judged = None
            if cfg.multi_lens_review and not is_subagent:
                multilens_lines = _multi_lens_review_lines(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    daemon_state=daemon_state,
                    route=route,
                )
            else:
                judged = _correctness_judge_gate(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    daemon_state=daemon_state,
                    route=route,
                )

            # Stale-test advisory: a turn that edited a paired source but left its
            # existing test untouched gets a coverage nudge. Advisory only, folded
            # into the same Stop context the judge uses so a turn emits one block.
            stale_lines = _stale_test_advisory_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                preloaded=preloaded,
                daemon_state=daemon_state,
            )

            # Change-set completeness: a turn that created a new file whose
            # framework convention demands a companion (a model its migration, a
            # controller its route) but whose change-set carries none gets a
            # nudge. Advisory only, folded into the same Stop context.
            cochange_lines = _changeset_completeness_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                daemon_state=daemon_state,
            )

            # Cross-file existence breaks: a turn that removed/renamed a TS export
            # other files still import by name left their call sites broken. Reuse
            # the persisted reverse index + a regex presence check (no parse at
            # Stop). Advisory only, folded into the same Stop context.
            crossfile_lines = _crossfile_existence_advisory_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
            )

            # Turn-end duplication: a function this turn introduced whose body
            # matches an existing one (catalog or earlier this session) gets named
            # so the author can reuse the original. Confirmed by a bounded judge
            # spawn, skipped on a SubagentStop and when the correctness judge
            # spawned a working reviewer this Stop, so a turn fires at most one
            # reviewer. A DEGRADED judge spawn (nonzero exit, timeout, parse
            # failure -- route["spawn_failed"], written by the gate above) does
            # not defer: a permanently broken reviewer must not starve
            # duplication review forever. Advisory only, folded into the same
            # Stop context.
            # Skipped when multi_lens_review owns duplication this turn (the lens
            # pass above already ran it).
            dup_lines: list[str] = []
            if not is_subagent and not cfg.multi_lens_review:
                dup_lines = _duplication_advisory_lines(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    corr_spawning=corr_spawning and not route.get("spawn_failed"),
                )

            # Turn-end test integrity: a turn that changed live source while
            # weakening tests (added skips, dropped assertions, deleted tests)
            # gets a deterministic advisory naming what was weakened. Zero model
            # spawn, folded into the same Stop context.
            testint_lines = _test_integrity_advisory_lines(
                repo_root=repo_root,
                repo_id=repo_id,
                session_id=session_id,
                state=state,
                cfg=cfg,
                repo_data=repo_data,
            )

            context_blocks: list[str] = []
            if idiom_advisory:
                context_blocks.append(idiom_advisory)
            if judged is not None:
                jb = (judged.get("hookSpecificOutput") or {}).get("additionalContext")
                if jb:
                    context_blocks.append(jb)
            if stale_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(stale_lines) + "\n</chameleon-context>"
                )
            if cochange_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(cochange_lines) + "\n</chameleon-context>"
                )
            if crossfile_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(crossfile_lines) + "\n</chameleon-context>"
                )
            if dup_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(dup_lines) + "\n</chameleon-context>"
                )
            if multilens_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(multilens_lines) + "\n</chameleon-context>"
                )
            if testint_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(testint_lines) + "\n</chameleon-context>"
                )

            if context_blocks:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": "\n\n".join(context_blocks),
                    }
                }
            return {}

        # Shadow mode records the would-have-blocked signal and allows the stop.
        if cfg.mode != "enforce":
            try:
                from chameleon_mcp.metrics import emit_hook_metric

                # One would_block row per rule per unresolved file, so the shadow
                # report attributes the backstop block to the specific rule and
                # can sample the file for spot-check. A file that re-lints
                # blockable but yielded no rule name still gets one row with a
                # null rule so the file:line sample is not lost.
                emitted_any = False
                for path in unresolved:
                    file_rel = _repo_rel(repo_root, path)
                    rules = unresolved_rules.get(path) or [None]
                    for rule in rules:
                        emit_hook_metric(
                            "stop-backstop",
                            elapsed_ms=0,
                            repo_id=repo_id,
                            advisory_emitted=True,
                            would_block=True,
                            rule=rule,
                            file_rel=file_rel,
                        )
                        emitted_any = True
                if not emitted_any:
                    emit_hook_metric(
                        "stop-backstop",
                        elapsed_ms=0,
                        repo_id=repo_id,
                        advisory_emitted=True,
                        would_block=True,
                    )
            except Exception:
                pass
            return {}

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in unresolved[:5])
        state.stop_hook_blocks += 1
        try:
            save_state(state, repo_data, session_id or "")
        except Exception:
            pass
        return {
            "decision": "block",
            "reason": (
                f"chameleon: unresolved convention violations remain in {names}. "
                f"Fix them before ending, or add {_ignore_hint(unresolved[:5])} "
                f"on the offending line."
            ),
        }
    except Exception:
        return {}


def _build_session_attestation(
    *,
    repo_root: Path,
    repo_id: str,
    session_id,
    repo_data: Path,
    suppressed_reason: str | None,
    daemon_state: dict | None = None,
) -> dict:
    """Assemble the Stop attestation payload from hook-observed evidence.

    The attestation is self-signed and raise-only: nothing recorded in it may
    ever lower scrutiny anywhere downstream; a consumer may use it only to
    RAISE gate depth and to make post-incident replay honest.

    Universe caveats, stated plainly: the touched-file list is what the hooks
    observed (EnforcementState), so bash-written files that linted clean and
    sessions run with the plugin disabled never enter it -- the record can
    under-count but never over-claim. An expired pause window is not observable
    after expiry (the marker self-deletes), so suppression captures only the
    live state at this Stop. Files past the ATTESTATION_MAX_FILES cap are not
    classified (classification costs a read plus an archetype resolve each);
    they count toward ungoverned_truncated because unverified coverage must
    raise scrutiny, never lower it.

    Every section degrades independently: a failed read leaves that section at
    its neutral value rather than aborting the payload.
    """
    from chameleon_mcp._thresholds import threshold_int

    verify_off = os.environ.get("CHAMELEON_VERIFY") == "0"
    enforce_off = os.environ.get("CHAMELEON_ENFORCE") == "0"

    payload: dict = {
        "session_id": session_id,
        "engine_version": None,
        "profile_sha256": None,
        "generation": None,
        "schema_version": None,
        "trust_state": None,
        "enforcement_mode": None,
        "env": {"verify_off": verify_off, "enforce_off": enforce_off},
    }
    try:
        from chameleon_mcp import __version__ as engine_version

        payload["engine_version"] = engine_version
    except Exception:
        pass
    try:
        from chameleon_mcp.tools import _peek_profile_provenance

        prov = _peek_profile_provenance(repo_root, repo_id)
        payload["generation"] = prov.get("generation")
        payload["schema_version"] = prov.get("schema_version")
        payload["trust_state"] = prov.get("trust_state")
    except Exception:
        pass
    try:
        from chameleon_mcp.profile.trust import hash_profile

        payload["profile_sha256"] = hash_profile(repo_root / ".chameleon") or None
    except Exception:
        pass
    try:
        from chameleon_mcp.profile.config import load_config

        payload["enforcement_mode"] = load_config(repo_root / ".chameleon").enforcement.mode
    except Exception:
        pass

    suppression: dict = {
        "reason": suppressed_reason,
        "session_disabled_at": None,
        "pause_until": None,
    }
    try:
        from chameleon_mcp.optouts import _safe_session_marker

        marker = repo_data / f".session_disabled.{_safe_session_marker(session_id)}"
        if marker.is_file():
            for line in marker.read_text(encoding="utf-8").splitlines():
                if line.startswith("disabled-at="):
                    suppression["session_disabled_at"] = line[len("disabled-at=") :].strip()
                    break
    except Exception:
        pass
    try:
        pause_path = repo_data / ".pause_until"
        if pause_path.is_file():
            suppression["pause_until"] = pause_path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    payload["suppression"] = suppression

    # Checks: the per-session sidecar aggregated to (check, status, reason)
    # counts, plus synthesized entries for env states under which the writers
    # themselves never ran.
    checks_agg: dict[tuple, int] = {}
    unverified = 0
    try:
        from chameleon_mcp.exec_log import read_check_events

        ev = read_check_events(
            repo_id, session_id or "", limit=threshold_int("ATTESTATION_MAX_CHECK_EVENTS")
        )
        unverified = int(ev.get("unverified") or 0)
        for record in ev.get("events") or []:
            key = (str(record.get("check")), str(record.get("status")), record.get("reason"))
            checks_agg[key] = checks_agg.get(key, 0) + 1
    except Exception:
        pass
    if verify_off:
        key = ("posttool_verify", "skipped", "verify_env_off")
        checks_agg[key] = checks_agg.get(key, 0) + 1
    checks = [
        {"check": c, "status": s, "reason": r, "count": n} for (c, s, r), n in checks_agg.items()
    ]
    checks.sort(key=lambda e: (e["check"], e["status"], e["reason"] or ""))
    payload["checks"] = checks[: threshold_int("ATTESTATION_MAX_CHECK_EVENTS")]
    payload["check_events_unverified"] = unverified

    # Touched files: the hook-observed universe, newest-verified first, capped.
    governed: list[dict] = []
    ungoverned: list[dict] = []
    governed_truncated = 0
    ungoverned_truncated = 0
    stop_hook_blocks = 0
    duplication_spawns = 0
    try:
        from chameleon_mcp.enforcement import load_state

        state = load_state(repo_data, session_id or "")
        stop_hook_blocks = int(state.stop_hook_blocks or 0)
        duplication_spawns = int(state.duplication_spawns or 0)

        entries = sorted(
            state.files.items(), key=lambda kv: kv[1].last_verified_at or 0, reverse=True
        )
        cap = threshold_int("ATTESTATION_MAX_FILES")
        ungoverned_truncated = max(0, len(entries) - cap)

        exports_index = None
        try:
            from chameleon_mcp import symbol_index

            exports_index = symbol_index.load_exports_index(repo_root)
        except Exception:
            exports_index = None
        # Shares the gates' daemon-liveness flag so a hung daemon costs the
        # whole Stop at most one failed probe, not one per touched file.
        resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})

        for path, _fs in entries[:cap]:
            rel = _repo_rel(repo_root, path)
            digest: str | None = None
            try:
                digest = _content_digest_16(
                    Path(path).read_bytes()[:100_000].decode("utf-8", errors="replace")
                )
            except OSError:
                digest = None  # unreadable file: still listed, digest unknown

            # Ungoverned needs ALL THREE coverage legs absent: no archetype, no
            # lint dimension for the extension, and no committed exports-index
            # entry. Any one leg of coverage keeps the file governed (on a repo
            # with no exports index every file fails that leg, which is why the
            # conjunction is required).
            archetype = None
            try:
                archetype = resolver(path)
            except Exception:
                archetype = None
            language = None
            try:
                from chameleon_mcp.lint_engine import detect_language

                language = detect_language(rel)
            except Exception:
                language = None
            symbol_covered = False
            try:
                if exports_index is not None:
                    from chameleon_mcp import symbol_index

                    key = symbol_index.module_key_for_path(path, repo_root)
                    symbol_covered = key is not None and exports_index.lookup(key) is not None
            except Exception:
                symbol_covered = False

            if archetype is None and language is None and not symbol_covered:
                ungoverned.append({"file": rel, "content_digest": digest})
                continue

            # The decision snapshot is embedded INLINE (not just the row id):
            # drift.db's recency trim may drop the row long before the
            # attestation is read, and a dangling id would make replay lie.
            snap = None
            try:
                from chameleon_mcp.drift.observations import decision_snapshot_for

                snap = decision_snapshot_for(
                    repo_id, rel or "", digest or "", session_id=session_id
                )
            except Exception:
                snap = None
            governed.append(
                {
                    "file": rel,
                    "content_digest": digest,
                    "decision_log_id": snap.get("id") if snap else None,
                    "archetype": snap.get("archetype") if snap else None,
                    "match_quality": snap.get("match_quality") if snap else None,
                    "outcome": snap.get("outcome") if snap else None,
                    "observed_at": snap.get("observed_at") if snap else None,
                }
            )
    except Exception:
        pass
    payload["governed_files"] = governed
    payload["governed_truncated"] = governed_truncated
    payload["ungoverned_files"] = ungoverned
    payload["ungoverned_truncated"] = ungoverned_truncated
    payload["stop_hook_blocks"] = stop_hook_blocks
    payload["duplication_spawns"] = duplication_spawns

    overrides: list[dict] = []
    overrides_truncated = 0
    try:
        from chameleon_mcp.drift.observations import (
            session_override_group_count,
            session_override_rows,
        )

        ov_cap = threshold_int("ATTESTATION_MAX_OVERRIDES")
        overrides = session_override_rows(repo_id, session_id or "", limit=ov_cap)
        total_groups = session_override_group_count(repo_id, session_id or "")
        overrides_truncated = max(0, total_groups - len(overrides))
    except Exception:
        overrides, overrides_truncated = [], 0
    payload["overrides"] = overrides
    payload["overrides_truncated"] = overrides_truncated

    return payload


def _write_session_attestation(
    *,
    repo_root: Path,
    repo_id: str,
    session_id,
    repo_data: Path,
    suppressed_reason: str | None,
    daemon_state: dict | None = None,
) -> None:
    """Build and persist this session's Stop attestation. Strictly fail-open.

    The attestation is self-signed and raise-only: nothing recorded in it may
    ever lower scrutiny anywhere downstream; it exists only to RAISE gate depth
    and make post-incident replay honest. It must never change the turn
    outcome, so any exception is swallowed here (and again by the caller).
    """
    try:
        payload = _build_session_attestation(
            repo_root=repo_root,
            repo_id=repo_id,
            session_id=session_id,
            repo_data=repo_data,
            suppressed_reason=suppressed_reason,
            daemon_state=daemon_state,
        )
        from chameleon_mcp.review_ledger import record_session_attestation

        record_session_attestation(repo_id, payload)
    except Exception:
        pass


def stop_backstop() -> int:
    """Stop / SubagentStop: refuse to end the turn while a touched file holds an
    unresolved hard-class violation, then run a once-per-session reflexive
    idiom/principle review of the turn's edits. Fails open; bounded by a
    per-session cap and the stop_hook_active flag so it can never trap the user
    in a loop.

    After the gates finish (see _stop_gates), a top-level Stop writes one
    signed session attestation -- which checks ran/skipped/degraded, the
    governed vs ungoverned touched files with pinned decision snapshots, the
    session's inline overrides, and any observable disable/pause state. The
    write happens strictly after the gates saved state and strictly before the
    hook output is emitted, so it can never race the state it reads (no
    concurrent Stop gate exists in-process and SubagentStop never writes).
    CHAMELEON_ATTESTATION=0 disables the write. Sessions that never reach the
    gates -- no repo, untrusted, stale profile hash, stop_hook_active, and
    CHAMELEON_DISABLE=1 (the bash wrapper exits pre-python) -- write nothing:
    that absence is itself the downstream signal. Paused/disabled and
    enforce-off sessions DO write a minimal attestation, because the disable
    window is exactly the scrutiny-relevant fact, with hook output unchanged.
    """
    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0
    # Never re-block while already continuing due to a prior stop block.
    if payload.get("stop_hook_active") is True:
        _emit({})
        return 0

    session_id = payload.get("session_id")
    # The Stop and SubagentStop events share this handler (hooks.json routes both
    # to stop-backstop). The input payload's hook_event_name distinguishes them;
    # the turn-end duplication spawn runs only on a top-level Stop, never per
    # subagent, so a multi-subagent turn pays for it at most once.
    is_subagent = payload.get("hook_event_name") == "SubagentStop"
    cwd_raw = payload.get("cwd")
    try:
        cwd = Path(cwd_raw).expanduser() if isinstance(cwd_raw, str) and cwd_raw else Path.cwd()
    except (OSError, ValueError):
        cwd = Path.cwd()

    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.profile.trust import hash_profile, trust_state_for
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(cwd)
        if repo_root is None:
            _emit({})
            return 0
        repo_id = _compute_repo_id(repo_root)
        # Suppression no longer exits immediately: a paused or session-disabled
        # session skips the gates (hook output stays {}) but still writes a
        # minimal attestation below carrying the suppression state.
        suppressed_reason = is_chameleon_suppressed(repo_root, repo_id, session_id)
        rec = trust_state_for(repo_id)
        if rec is None or not rec.grants_root(repo_root):
            _emit({})
            return 0
        # A "stale" grant (the profile hash drifted from the one the user trusted)
        # only verifies; it never blocks the turn, mirroring the PreToolUse and
        # PostToolUse gates.
        if rec.hash_for_root(repo_root) != hash_profile(repo_root / ".chameleon"):
            _emit({})
            return 0

        repo_data = _plugin_data_dir() / repo_id
        # One daemon-liveness flag for the whole Stop, shared between the gates
        # and the attestation writer: a hung daemon costs at most one probe.
        daemon_state = {"available": True}

        if suppressed_reason is not None:
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "suppressed")
            output: dict = {}
        else:
            try:
                output = _stop_gates(
                    payload=payload,
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    is_subagent=is_subagent,
                    repo_data=repo_data,
                    daemon_state=daemon_state,
                )
            except Exception:
                output = {}

        if not is_subagent and os.environ.get("CHAMELEON_ATTESTATION") != "0":
            try:
                _write_session_attestation(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    repo_data=repo_data,
                    suppressed_reason=suppressed_reason,
                    daemon_state=daemon_state,
                )
            except Exception:
                pass

        _emit(output)
        return 0
    except Exception:
        _emit({})
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write("hook_helper.py: missing command argument\n")
        return 1
    command = args[0]
    try:
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
        if command == "stop-backstop":
            return stop_backstop()
    except BrokenPipeError:
        # The harness closed our stdout (timeout-kill / teardown). Hooks are
        # fail-open by contract: exit 0 quietly instead of crashing into
        # .hook_errors.log for a consumer that already hung up.
        _absorb_broken_stdout()
        return 0
    sys.stderr.write(f"hook_helper.py: unknown command {command!r}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
