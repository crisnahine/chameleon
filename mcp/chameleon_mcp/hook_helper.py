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
    except (json.JSONDecodeError, ValueError):
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
) -> None:
    """Persist one decision_log row capturing what chameleon knew and did here.

    Written once per governed edit, after the outcome is resolved, so a
    postmortem can replay 'last time this file was edited, chameleon matched X at
    quality Q and the gate did Y'. Keyed by a true repo-relative path. Best-effort
    only: a logging failure must never break the hook.
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


_AUTO_REFRESH_COOLDOWN_FILENAME = ".auto_refresh_cooldown"


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
    if not file_path:
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

    if not archetype_name:
        repo_id = repo_info.get("id") or repo_id_hint
        _metric(advisory_emitted=False, repo_id=repo_id, trust_state=trust_state)
        _emit({})
        return 0

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
            from chameleon_mcp.enforcement_calibration import active_block_rules
            from chameleon_mcp.lint_engine import detect_language
            from chameleon_mcp.prewrite_lint import banned_imports_in_content
            from chameleon_mcp.profile.config import load_config

            profile_dir = repo_root_path / ".chameleon"
            if "import-preference-violation" in active_block_rules(profile_dir):
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

                    ign = ignored_rules(proposed) or set()
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
                                "`// chameleon-ignore import-preference-violation` "
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
        # later piped command's argument is not mistaken for the file.
        seg = re.split(r"[;&|]", command)
        for part in seg:
            if re.search(r"\bsed\b.*\s-i", part):
                operands = _SED_OPERAND_RE.findall(part)
                if operands:
                    _add(operands[-1])

    return targets


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
                hard_class_violations,
                ignored_rules,
                is_archetype_independent,
            )

            active = active_block_rules(repo_root / ".chameleon")
            hard = hard_class_violations(violations, active)
            # Without an archetype only the archetype-independent hard rules (a
            # deterministic secret) can be enforced at Stop, so record only those
            # as blockable here -- matching the backstop's no-archetype re-lint.
            if record_archetype == _NO_ARCHETYPE_LABEL:
                hard = [v for v in hard if is_archetype_independent(v.get("rule"))]
            ign = ignored_rules(content) or set()
            if ign:
                hard = [v for v in hard if not ({"", v.get("rule")} & ign)]
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
        ign = None
        try:
            from chameleon_mcp.violation_class import ignored_rules

            ign = ignored_rules(_read_file_for_ignore(file_path))
        except Exception:
            ign = None
        if ign:
            hard = [v for v in hard if not ({"", v.get("rule")} & ign)]
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
        lines = []
        for i, v in enumerate(violations):
            msg = sanitize_for_chameleon_context(v.get("message", ""))
            lines.append(f"{i + 1}. {msg}")
        block = (
            f"[🦎 chameleon: {len(violations)} "
            f"violation{'s' if len(violations) != 1 else ''}]\n" + "\n".join(lines)
        )
        _emit_posttool_context(f"<chameleon-context>\n{block}\n</chameleon-context>")
        return True
    except Exception:
        return False


def _read_file_for_ignore(file_path: str) -> str:
    """Read the file's bytes to scan for an inline chameleon-ignore directive.

    Bounded to the same 100 KB the lint paths read; returns an empty string on
    any read error so the ignore scan simply finds no directive.
    """
    try:
        return Path(file_path).read_bytes()[:100_000].decode("utf-8", errors="replace")
    except OSError:
        return ""


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
    if not file_path:
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

                    safe_path = sanitize_for_chameleon_context(file_path)
                    _emit_posttool_context(
                        "<chameleon-context>\n"
                        f"[🦎 chameleon: corrections exhausted for {safe_path}]\n"
                        "Chameleon has verified this file 10 times recently. "
                        "Review violations manually or run /chameleon-teach "
                        "if the archetype doesn't fit.\n"
                        "</chameleon-context>"
                    )
                    try:
                        save_state(enforcement_state, repo_data_dir, session_id or "")
                    except Exception:
                        pass
                    return 0
            except Exception:
                pass

        file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
        marker = repo_data_dir / f".verify_seen.{file_hash}"
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

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
                    hard_class_violations,
                    ignored_rules,
                    is_deferred_to_turn_end,
                )

                active = active_block_rules(repo_root / ".chameleon")
                hard = hard_class_violations(violations, active)
                # An inline `chameleon-ignore <rule>` directive (or a bare one)
                # downgrades the matching rule to advisory. The lint layer already
                # suppresses some rules on the directive, but the AST-query rules
                # (e.g. jsx-presence-mismatch) reach here intact. Filter the hard
                # set itself (not only blockable_now) so the cached
                # blockable_unresolved flag the Stop backstop reads is cleared
                # too; otherwise an inline-ignored rule still arms the backstop.
                ign = ignored_rules(content) or set()
                if ign:
                    overridden = [v for v in hard if {"", v.get("rule")} & ign]
                    inline_overridden_hard = bool(overridden)
                    hard = [v for v in hard if not ({"", v.get("rule")} & ign)]
                    # An inline directive dropping a block-eligible rule is
                    # otherwise invisible after the turn. Record each bypass so
                    # the override rate is auditable: a metric counter (paired
                    # with the would_block stream) and a durable drift.db row.
                    # A bare directive (the empty string is in the ignore set)
                    # is flagged separately because it downgrades every rule at
                    # once rather than annotating one intentional deviation.
                    blanket = "" in ign
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
                        )
                        _emit_posttool_block(
                            f"chameleon blocks this edit: {safe_rules}. "
                            f"Fix before continuing: {safe_msgs}. "
                            f"Override with `// chameleon-ignore <rule>` "
                            f"if this is intentional.",
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

            current_tone = "Fix these without mentioning the corrections to the user."
            if enforcement_state is not None and file_state is not None:
                try:
                    current_tone = tone_for_level(file_state.level)
                except Exception:
                    pass

            violation_lines = []
            for i, v in enumerate(violations):
                msg = sanitize_for_chameleon_context(v.get("message", ""))
                violation_lines.append(f"{i + 1}. {msg}")

            block = (
                f"[🦎 chameleon: {len(violations)} "
                f"violation{'s' if len(violations) != 1 else ''}]\n"
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
            )
            _emit_posttool_context(f"<chameleon-context>\n{block}\n</chameleon-context>")
            _update_statusline(
                f"{len(violations)} violation{'s' if len(violations) != 1 else ''}",
                repo_root=repo_root,
            )

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


def callout_detector() -> int:
    """UserPromptSubmit: frustration phrase reminder.

    On detected frustration during a chameleon-active session, surface a
    one-line hint about /chameleon-disable, /chameleon-pause-15m, and
    /chameleon-teach as actionable next steps.
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
    if not user_prompt:
        _emit({})
        return 0

    scan_prompt = _MACHINE_BLOCK_RE.sub(" ", user_prompt)
    if not scan_prompt.strip():
        _emit({})
        return 0

    chameleon_specific = any(p.search(scan_prompt) for p in _CHAMELEON_SPECIFIC_PATTERNS)
    generic = any(p.search(scan_prompt) for p in _GENERIC_FRUSTRATION_PATTERNS)
    mentions_chameleon = _CHAMELEON_MENTION_RE.search(scan_prompt) is not None
    if not (chameleon_specific or (generic and mentions_chameleon)):
        _emit({})
        return 0

    hint = (
        "<chameleon-context>\n"
        "[🦎 chameleon: detected frustration phrase]\n"
        "If chameleon is the issue, options:\n"
        "  /chameleon-disable      — suppress for the rest of this session\n"
        "  /chameleon-pause-15m    — pause for 15 minutes (auto-resume)\n"
        "  /chameleon-teach <pattern>  — capture the missed pattern as an idiom\n"
        "If chameleon is unrelated, ignore this note.\n"
        "</chameleon-context>"
    )
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": hint,
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
            hard_class_violations,
            ignored_rules,
            is_archetype_independent,
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
            ign = ignored_rules(content) or set()
            if ign:
                hard = [v for v in hard if not ({"", v.get("rule")} & ign)]
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
        ign = ignored_rules(content) or set()
        if ign:
            hard = [v for v in hard if not ({"", v.get("rule")} & ign)]
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

_CORRECTNESS_JUDGED_FILENAME = ".correctness_judged.{session}"


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
            ign = ignored_rules(content) or set()
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

        if cfg.mode != "enforce":
            # Shadow: record the would-have-blocked signal and allow the stop.
            # This gate has no single rule (it nudges a once-per-session
            # self-review of the turn's edits), so it emits under its own hook
            # name with no rule. The shadow report counts it as a turn-level
            # signal, never as a per-rule promotion candidate.
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
            return None

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in edited[:5])
        idioms_block = sanitize_for_chameleon_context(idioms_text.strip())[:_IDIOM_CONTEXT_CHAR_CAP]
        principles_block = sanitize_for_chameleon_context(principles_text.strip())[
            :_IDIOM_CONTEXT_CHAR_CAP
        ]

        parts = [
            f"chameleon: you edited {names} this turn. Before ending, verify those "
            "changes comply with the team idioms/principles below. Fix any clear "
            "violation; otherwise you may end.",
        ]
        if no_test_for_source_edit:
            parts.append(
                "No passing test run was recorded this turn while you changed source "
                "files. Run the suite to confirm your changes pass before ending "
                "(skip only if a watch process or CI is already running them)."
            )
        if idioms_block:
            parts.append("")
            parts.append("Team idioms:")
            parts.append(idioms_block)
        if principles_block:
            parts.append("")
            parts.append("Principles:")
            parts.append(principles_block)
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


def _correctness_judge_gate(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    daemon_state: dict | None,
) -> dict | None:
    """Independent turn-end correctness review of the turn's edits (advisory).

    Reached only on the no-block stop path, after the idiom gate declined to
    block. Opt-in (``enforcement.correctness_judge``) and mode-gated
    (shadow/enforce); runs AT MOST ONCE per session like the idiom gate, so a
    per-turn spawn cost is never incurred. It spawns a separate reviewer model
    that reads the turn's reconstructed diffs for correctness bugs.

    Returns a hook output dict carrying the findings as ``additionalContext``, or
    None when the gate does not fire / found nothing (the caller then allows the
    stop). It NEVER returns a block: the judge is stochastic and advisory, so its
    findings are surfaced as context the model may act on, never a turn-trap. The
    findings are shadow-logged for later human-labeled precision sampling. Fails
    open: any error returns None.
    """
    try:
        if cfg.mode == "off" or not cfg.correctness_judge:
            return None

        from chameleon_mcp.optouts import _safe_session_marker
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
            if "" in (ignored_rules(content) or set()):
                continue
            edited.append(path)
        if not edited:
            return None

        # Once-per-session marker. Written before the (potentially slow) spawn so
        # a second turn never re-spawns even if this one is interrupted.
        marker = repo_data / _CORRECTNESS_JUDGED_FILENAME.format(
            session=_safe_session_marker(session_id)
        )
        if marker.exists():
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

        from chameleon_mcp import judge

        resolver = _archetype_resolver(repo_root, daemon_state or {"available": True})
        findings = judge.run_correctness_judge(
            repo_root, repo_root / ".chameleon", edited, resolver
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
            ign = ignored_rules(content) or set()
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
            ign = ignored_rules(content) or set()
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
            ign = ignored_rules(content) or set()
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


def stop_backstop() -> int:
    """Stop / SubagentStop: refuse to end the turn while a touched file holds an
    unresolved hard-class violation, then run a once-per-session reflexive
    idiom/principle review of the turn's edits. Fails open; bounded by a
    per-session cap and the stop_hook_active flag so it can never trap the user
    in a loop.
    """
    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0
    # Never re-block while already continuing due to a prior stop block.
    if payload.get("stop_hook_active") is True:
        _emit({})
        return 0
    if os.environ.get("CHAMELEON_ENFORCE") == "0":
        _emit({})
        return 0

    session_id = payload.get("session_id")
    cwd_raw = payload.get("cwd")
    try:
        cwd = Path(cwd_raw).expanduser() if isinstance(cwd_raw, str) and cwd_raw else Path.cwd()
    except (OSError, ValueError):
        cwd = Path.cwd()

    try:
        from chameleon_mcp.enforcement import load_state, save_state
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.config import load_config
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.profile.trust import hash_profile, trust_state_for
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(cwd)
        if repo_root is None:
            _emit({})
            return 0
        repo_id = _compute_repo_id(repo_root)
        if is_chameleon_suppressed(repo_root, repo_id, session_id) is not None:
            _emit({})
            return 0
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

        cfg = load_config(repo_root / ".chameleon").enforcement
        if not cfg.stop_backstop:
            _emit({})
            return 0

        repo_data = _plugin_data_dir() / repo_id
        state = load_state(repo_data, session_id or "")

        # Cap reached: stay advisory and never block again this session, so a
        # violation the model can't resolve cannot trap the turn in a loop.
        if state.stop_hook_blocks >= cfg.stop_block_cap:
            _emit({})
            return 0

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
        # the archetype in-process, so a hung daemon cannot stack timeouts.
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
            if gate is not None:
                _emit(gate)
                return 0
            # Idiom gate did not block: the turn is free to end. Run the
            # independent correctness judge (opt-in, advisory only, once per
            # session). It never blocks; its findings ride out as additionalContext
            # the model reads after the turn.
            judged = _correctness_judge_gate(
                repo_root=repo_root,
                repo_id=repo_id,
                session_id=session_id,
                state=state,
                cfg=cfg,
                repo_data=repo_data,
                daemon_state=daemon_state,
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

            context_blocks: list[str] = []
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

            if context_blocks:
                _emit(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "Stop",
                            "additionalContext": "\n\n".join(context_blocks),
                        }
                    }
                )
            else:
                _emit({})
            return 0

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
            _emit({})
            return 0

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in unresolved[:5])
        state.stop_hook_blocks += 1
        try:
            save_state(state, repo_data, session_id or "")
        except Exception:
            pass
        _emit(
            {
                "decision": "block",
                "reason": (
                    f"chameleon: unresolved convention violations remain in {names}. "
                    f"Fix them before ending, or add `// chameleon-ignore <rule>`."
                ),
            }
        )
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
