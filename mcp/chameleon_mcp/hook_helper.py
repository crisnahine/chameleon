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
    A fully-closed fd 1 at process start makes CPython set ``sys.stdout`` to
    None; writing then raises AttributeError, which is absorbed the same way
    (no reader, nothing to emit).
    """
    if sys.stdout is None:
        return
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


def _safe_cwd() -> Path:
    """Path.cwd() that never raises when the process cwd was deleted.

    A hook can be spawned with its process cwd already unlinked (a git-worktree
    removal, a repo move, a tmpdir cleanup mid-session). ``Path.cwd()`` /
    ``os.getcwd()`` then raise ``FileNotFoundError`` -- the bash wrapper still
    masks the exit, but the traceback lands in the error log that
    /chameleon-doctor reads for degraded health, so a benign deleted-cwd looks
    like a broken install. The payload's own ``cwd`` field is authoritative for
    the repo anyway; this default only fills the pre-parse gap, so a static
    fallback (home, else root) is correct when the real cwd is gone.
    """
    try:
        return Path.cwd()
    except (FileNotFoundError, OSError):
        try:
            home = os.path.expanduser("~")
            # expanduser returns the literal "~" (a RELATIVE path) when HOME is
            # unset and the pwd lookup can't resolve it; fall through to an
            # absolute root so the fallback is never a relative path.
            if home and home != "~":
                return Path(home)
        except (OSError, RuntimeError):
            pass
        return Path("/")


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


def _observation_rel_path(repo_root: Path | None, file_path: str) -> str:
    """Repo-relative path for an edit_observations row.

    Claude Code passes an ABSOLUTE ``tool_input.file_path``; the drift schema
    documents ``rel_path`` as repo-relative, so an absolute path under the repo
    is relativized. A path that is ALREADY relative is kept verbatim -- it is the
    best repo-relative form available, and resolving it against the process CWD
    (which is not the repo root) would corrupt it. Falls back to the original
    string on any resolution failure (e.g. an absolute path outside the repo).
    Unlike ``_repo_rel`` this never collapses a relative path to its basename.
    """
    if not file_path:
        return file_path
    p = Path(file_path)
    if not p.is_absolute() or repo_root is None:
        return file_path
    try:
        return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return file_path


def _enf_profile_dir(repo_root: Path) -> Path:
    """The ``.chameleon`` dir the hook's enforcement/profile reads should use.

    For a linked git worktree this resolves to the MAIN worktree's profile (the
    worktree's own ``.chameleon`` is gitignored and absent); it is the identity
    for every non-worktree root, so non-worktree behavior is byte-identical.
    Callers keep ``repo_root`` itself as the worktree so archetype-relativity
    (``_repo_rel``), witness reads, and ``repo_id`` are unaffected. Mirrors
    ``tools._effective_profile_dir`` and the worktree-aware trust resolution.
    """
    from chameleon_mcp.worktree import resolve_profile_root

    return resolve_profile_root(repo_root) / ".chameleon"


def _conventions_echo_subset(conv_data: dict, archetype: str) -> dict:
    """Slice conventions.json down to just what the Tier-1 echo actually renders.

    ``format_conventions_echo`` reads EXACTLY four dimensions, each scoped to the
    single edited archetype: ``imports`` / ``naming`` / ``inheritance`` /
    ``class_contract``. Everything else in the artifact never reaches the echo. On
    a large repo (gitlabhq: multi-MB conventions.json) scrubbing + sanitizing the
    WHOLE artifact on every edit costs ~10x the rest of the hot path, and past a
    few MB it can exhaust the advisory wall-clock budget so the whole Tier-1 layer
    dies silently. Extract the archetype's subset FIRST, then scrub/sanitize only
    that -- O(one archetype) instead of O(whole file). Falls back to the full
    object when the shape is unexpected or the archetype is unknown (small repos
    are unaffected either way)."""
    inner = conv_data.get("conventions") if isinstance(conv_data, dict) else None
    if not isinstance(inner, dict) or not archetype:
        return conv_data
    slim_inner: dict = {}
    for dim in ("imports", "naming", "inheritance", "class_contract"):
        dim_val = inner.get(dim)
        if isinstance(dim_val, dict) and archetype in dim_val:
            slim_inner[dim] = {archetype: dim_val[archetype]}
    return {"conventions": slim_inner}


def _note_if_config_malformed(
    exc: BaseException,
    repo_id: str | None,
    session_id: str | None,
    where: str,
) -> bool:
    """Record a degraded check-event when a swallowed enforcement-gate exception
    is a malformed / torn ``config.json``, so the silent fail-open is observable
    in the session attestation and ``/chameleon-doctor`` instead of invisible.
    Returns True when the swallowed exception WAS a malformed config, so a caller
    can also surface it where the user is working (the check-event alone never
    reaches the edit surface).

    Stays fail-open by design: failing closed on an unreadable config is circular
    (the enforcement mode is exactly what could not be parsed) and would wedge
    every edit / turn for a user whose committed config has a stray typo. Matches
    the type by name so this never needs to import (and bind) the config module
    inside an except clause. Never raises.
    """
    try:
        if type(exc).__name__ == "ChameleonConfigError":
            _emit_check_event(
                repo_id,
                session_id,
                "enforcement_config",
                "degraded",
                reason="config_malformed",
                detail={"where": where},
            )
            return True
    except Exception:
        pass
    return False


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


def _duplication_index_files(
    edited: list[str],
    state,
    *,
    repo_id: str | None,
    session_id: str | None,
) -> list[str]:
    """Order the duplication-index input most-recently-edited first.

    build_candidate_index caps its re-parse at DUPLICATION_INDEX_MAX_FILES; that
    cap only knows about the file order it receives, and ``state.files`` is
    insertion-ordered, not recency-ordered. Sort a separate view by
    last_verified_at descending so the freshest working set survives the cap, and
    record the dropped count as a check event so the trim is never silent. The
    caller keeps ``edited`` itself untouched (its first element drives language
    inference).
    """
    ordered = sorted(
        edited,
        key=lambda p: (state.files[p].last_verified_at or 0) if p in state.files else 0,
        reverse=True,
    )
    try:
        from chameleon_mcp._thresholds import threshold_int

        cap = threshold_int("DUPLICATION_INDEX_MAX_FILES")
        dropped = len(ordered) - cap
        if dropped > 0:
            _emit_check_event(
                repo_id,
                session_id,
                "duplication_review",
                "truncated",
                "index_files_capped",
                detail={"dropped": dropped, "cap": cap, "total": len(ordered)},
            )
    except Exception:
        pass
    return ordered


# A `// chameleon-ignore...` / `# chameleon-ignore...` directive comment (any
# variant: bare, `-file`, rule-named). Stripped from content ONLY to re-detect a
# bypassed banned import for override auditing -- never used to change a decision.
_CHAMELEON_IGNORE_DIRECTIVE_RE = re.compile(r"(?://|#)\s*chameleon-ignore[^\n]*")


def _strip_chameleon_ignore_directives(content: str) -> str:
    """Remove chameleon-ignore directive comments so a re-scan sees the import the
    inline ignore otherwise suppresses.

    ``banned_imports_in_content`` (via ``lint_conventions``) returns no violation
    for an inline-ignored rule, which would make the deny-gate override-recording
    dead code (``banned`` and the ignore set can never both be truthy). Re-scanning
    the stripped content recovers the bypassed import so the bypass stays auditable.
    Advisory-only: the result feeds override recording, never the deny decision.
    """
    return _CHAMELEON_IGNORE_DIRECTIVE_RE.sub("", content)


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
    # A notebook cell is Python (detect_language('.ipynb') is None), so a deny on
    # cell content must offer the `#` token, not the `//` fallback that would be a
    # SyntaxError in the cell.
    if any(str(p).lower().endswith(".ipynb") for p in paths if p):
        langs.add("python")
    # Unknown extensions never carry violations, so they don't shape the hint.
    langs.discard(None)
    # Ruby and Python both use `#`; TypeScript/JS uses `//`.
    hash_langs = {"ruby", "python"}
    if langs and langs <= hash_langs:
        return f"`# chameleon-ignore {rule}`"
    if langs & hash_langs:
        return f"`// chameleon-ignore {rule}` (`# chameleon-ignore {rule}` in Ruby/Python)"
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


def _write_timestamp_marker(marker: Path) -> None:
    """Create a cooldown/state marker holding the current epoch seconds.

    Parent dir and marker are locked to owner-only perms (0700/0600); a chmod
    failure on an exotic filesystem is best-effort and never fatal.
    """
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
        base = repo_root if repo_root else _safe_cwd()
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
        _write_timestamp_marker(marker)
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
                _write_timestamp_marker(emarker)
                # When auto-refresh is on, this same SessionStart triggers the
                # re-derive itself — suggesting a manual /chameleon-refresh
                # right after it already ran is stale advice.
                auto_on = True
                try:
                    from chameleon_mcp.profile.config import load_config

                    auto_on = load_config(_enf_profile_dir(resolved_root)).auto_refresh.enabled
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

        _write_timestamp_marker(marker)

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
                # A grounding event (judge_defs_*/judge_transitive_*/judge_facts_*)
                # is NOT a spawn failure; skip it so a healthy reviewer never
                # raises the failed-to-spawn banner. Both the sync gate and the
                # detached child now file these under their own check via
                # judge.grounding_family, but this defensive skip stays: a
                # degraded_spawn row carrying a grounding reason (older
                # attestation, or any future mis-file) must never raise the
                # banner.
                if isinstance(raw, str) and _judge_grounding_family(raw) is not None:
                    continue
                reason = raw if raw in _JUDGE_DEGRADED_REASONS else "unknown"
                break
        if reason is None:
            return None

        marker = _plugin_data_dir() / repo_id / _JUDGE_HEALTH_BANNER_FILENAME
        if _marker_path_is_fresh(marker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
            return None
        _write_timestamp_marker(marker)
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
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.degraded_telemetry import parse_degradations
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
        no_interp, spawn_fail, _ = parse_degradations(tail, cutoff)

        if not (no_interp >= 1 or spawn_fail >= 3):
            return None
        # Count the triggering reason, not the sum: when no-interpreter fires,
        # below-threshold one-off spawn-fails must not inflate the headline.
        count = no_interp if no_interp else spawn_fail

        marker = _plugin_data_dir() / repo_id / _INTERPRETER_BANNER_FILENAME
        if _marker_path_is_fresh(marker, threshold_int("DRIFT_BANNER_TTL_SECONDS")):
            return None
        _write_timestamp_marker(marker)
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

        # Migration trigger: an engine upgrade or a profile missing enforcement.json
        # (an existing user's pre-upgrade profile, built before calibration existed)
        # must auto-upgrade on the next session rather than waiting for drift or age
        # to accumulate. The refresh re-derives the profile, regenerates the
        # calibration, and re-stamps the engine version, so the trigger self-clears
        # and the cooldown marker written after the spawn prevents a re-fire while it
        # runs. Computed BEFORE the cooldown gate: the general cooldown (up to ~42h)
        # is written by the PRE-upgrade refresh, so gating the migration behind it
        # served known-stale facts for that whole window after the fixing engine
        # shipped — the exact wait this trigger exists to avoid.
        migration_due = False
        try:
            from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION
            from chameleon_mcp.tools import _engine_version_changed

            if (
                _engine_version_changed(profile_dir, ENGINE_MIN_VERSION)
                or not (profile_dir / "enforcement.json").is_file()
            ):
                migration_due = True
        except Exception:  # noqa: BLE001
            pass

        cooldown_seconds = max(60, (cfg.auto_refresh.max_age_hours * 3600) // 4)
        # A migration caps the effective cooldown at a short floor so the upgrade
        # repair fires on the next session, yet still cannot storm: the marker
        # written after the spawn suppresses the following session, and a completed
        # refresh re-stamps the engine version so the trigger self-clears. A failed
        # refresh simply retries after the short floor instead of waiting out ~42h.
        effective_cooldown = cooldown_seconds
        if migration_due:
            from chameleon_mcp._thresholds import threshold_int

            effective_cooldown = min(
                cooldown_seconds, threshold_int("MIGRATION_REFRESH_COOLDOWN_SECONDS")
            )
        cooldown_marker = _plugin_data_dir() / repo_id / _AUTO_REFRESH_COOLDOWN_FILENAME
        if _marker_path_is_fresh(cooldown_marker, effective_cooldown):
            return

        should_fire = migration_due
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

        _write_timestamp_marker(cooldown_marker)
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
    (conventions.json keys AND values) BEFORE it is formatted into a block. We
    sanitize the INPUTS, not the assembled block: ``format_conventions_for_session``
    adds its own legitimate ``<chameleon-conventions>`` wrapper, and ``<chameleon``
    is itself a dangerous token, so sanitizing the output would corrupt the
    wrapper. Keys are sanitized too -- the archetype-name key renders as prose
    (``- {arch}: …``), so a tag-boundary token in a key would otherwise break out.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    if isinstance(obj, str):
        return sanitize_for_chameleon_context(obj)
    if isinstance(obj, dict):
        return {
            (sanitize_for_chameleon_context(k) if isinstance(k, str) else k): _sanitize_profile_obj(
                v
            )
            for k, v in obj.items()
        }
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
    # Resolve the repo from the payload's authoritative `cwd`, like every other
    # hook_helper command -- not the hook PROCESS cwd. They coincide in normal
    # Claude Code operation, but an agent/harness whose process cwd diverges from
    # the session cwd would otherwise inject the WRONG repo's conventions and
    # reuse list at session start.
    session_cwd = _safe_cwd()
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
                sid = payload.get("session_id")
                if isinstance(sid, str):
                    session_id = sid
                cwd_raw = payload.get("cwd")
                if isinstance(cwd_raw, str) and cwd_raw:
                    try:
                        session_cwd = Path(cwd_raw).expanduser()
                    except (OSError, ValueError):
                        session_cwd = _safe_cwd()
    except Exception:
        session_id = None
    drift_banner = _drift_banner_for_repo(session_cwd, session_id=session_id)

    repo_root = None
    try:
        from chameleon_mcp.plugin_paths import plugin_data_dir
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(session_cwd)
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
        from chameleon_mcp.profile.trust import (
            profile_diverged_from_grant,
            trust_state_for,
        )
        from chameleon_mcp.worktree import resolve_profile_root

        # Place the statusline cache under the repo root, not the launch cwd. The
        # statusline script reads it at the repo root, so a subdir-launched session
        # (e.g. claude started from repo/tests) must still write there.
        sl_base = repo_root if repo_root else session_cwd
        cache_dir = sl_base / ".claude"
        sl_cache = cache_dir / ".chameleon-statusline-cache"
        profiles: list[dict] = []

        def _trust_for(root: Path) -> str:
            rid = _compute_repo_id(root)
            ts = trust_state_for(rid)
            if ts is None or not ts.grants_root(root):
                # ungranted workspace under a monorepo-shared repo_id
                return "untrusted"
            pdir = resolve_profile_root(root) / ".chameleon"
            if pdir.is_dir() and profile_diverged_from_grant(ts, root, pdir):
                # Only under CHAMELEON_TRUST_REVALIDATE=1; trust persists by default.
                return "stale"
            return "trusted"

        has_own_profile = (
            repo_root
            and (resolve_profile_root(repo_root) / ".chameleon" / "profile.json").is_file()
        )
        if has_own_profile:
            profiles.append({"name": repo_root.name, "trust": _trust_for(repo_root)})
        else:
            # Scan from the repo root, not the launch cwd, so sibling-workspace
            # profiles are discovered when Claude is launched from a subdirectory.
            scan_dir = repo_root if repo_root else session_cwd
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
    _wire_statusline_settings(repo_root if repo_root else _safe_cwd(), plugin_root)

    conventions_block = ""
    try:
        from chameleon_mcp.conventions import format_conventions_for_session
        from chameleon_mcp.worktree import resolve_profile_root

        _prof_root = resolve_profile_root(repo_root) if repo_root else None
        if _prof_root and (_prof_root / ".chameleon" / "conventions.json").is_file():
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

                # This path reads conventions.json straight from disk (not via
                # load_profile_dir), so screen its prose values for injection here --
                # render sanitization does not neutralize injection prose, and trust
                # persists across changes so the staleness gate no longer covers it.
                from chameleon_mcp.profile.loader import safe_prose_text, scrub_conventions_prose

                # principles.md is an INDEPENDENT artifact: read it first and keep it
                # separate from the conventions.json parse, so a corrupt/unparseable
                # conventions.json does not collaterally drop the healthy
                # principles.md-derived PRINCIPLES + ANTI-HALLUCINATION PROTOCOL for
                # the whole session. A conventions parse failure degrades to an empty
                # conventions object; format_conventions_for_session still emits the
                # principle sections from principles_text alone.
                pr_text = safe_prose_text(_prof_root / ".chameleon" / "principles.md")
                conv_data: dict = {}
                try:
                    conv_text = (_prof_root / ".chameleon" / "conventions.json").read_text(
                        encoding="utf-8"
                    )
                    _parsed = _conv_json.loads(conv_text)
                    if isinstance(_parsed, dict):
                        conv_data = _parsed
                        scrub_conventions_prose(conv_data)
                except Exception:
                    conv_data = {}
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
    production_banner = _production_tip_banner(repo_root or _safe_cwd(), session_id=session_id)
    if production_banner:
        wrapped_parts.append("")
        wrapped_parts.append(production_banner)
    judge_health_banner = _judge_spawn_health_banner(
        repo_root or _safe_cwd(), session_id=session_id
    )
    if judge_health_banner:
        wrapped_parts.append("")
        wrapped_parts.append(judge_health_banner)
    interpreter_banner = _interpreter_degraded_banner(
        repo_root or _safe_cwd(), session_id=session_id
    )
    if interpreter_banner:
        wrapped_parts.append("")
        wrapped_parts.append(interpreter_banner)
    wrapped_parts.append("</chameleon-context>")
    wrapped = "\n".join(wrapped_parts)

    _emit_session_context(wrapped)

    # Reuse the repo root already resolved above instead of re-deriving it from
    # cwd inside the helper.
    _maybe_auto_refresh(repo_root or _safe_cwd())

    return 0


# Bound the per-edit idiom dedup pass: an idiom line the canonical witness
# already demonstrates verbatim is redundant tokens in the block, but the scan
# stays off the hot path's critical budget by checking at most this many lines.
_IDIOM_BLOCK_DEDUP_MAX_LINES = 400


def _shape_idioms_for_block(idioms_text: str, witness: str) -> str:
    """Cap and dedup-vs-witness the idioms text for the per-edit block.

    Drops substantive idiom lines that appear verbatim in the canonical witness
    body (complementarity: the model can read those off the witness, so repeating
    them is noise), then caps to the same char budget the PostToolUse path uses.
    Bounded substring containment with an early stop, no nested scan; dedup is
    skipped when there is no witness. May return "" if every line was redundant.
    """
    text = idioms_text
    if witness:
        kept: list[str] = []
        checked = 0
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and checked < _IDIOM_BLOCK_DEDUP_MAX_LINES:
                checked += 1
                if stripped in witness:
                    continue
            kept.append(line)
        text = "\n".join(kept)
    if len(text) > _IDIOM_CONTEXT_CHAR_CAP:
        # Hard char cut so the model sees as much of the last idiom as fits -- most
        # idiom bodies are a single unwrapped paragraph line, so a line-boundary cut
        # would drop the whole description. A partial `### header` this can leave at
        # the tail is handled downstream: _idiom_block_names never records a
        # truncated tail block whose description did not actually appear.
        # Honest overflow: count idiom `### ` headers whose block starts ENTIRELY
        # past the cut (dropped outright), so the tail reports coverage loss instead
        # of a bare "truncated" the reader can't quantify. A repo that invests in
        # /chameleon-teach otherwise silently loses per-edit coverage as it teaches
        # more; the Stop review's full-text-for-unseen pass compensates the rest.
        cap = _IDIOM_CONTEXT_CHAR_CAP
        # Count dropped idiom BLOCKS fence-awarely (a `### ` inside an example code
        # fence is not a header): parse the full text and the kept prefix with the
        # same parser _idiom_block_names uses, and diff the block counts. Bounded to
        # the idioms text, which the reorder+dedup upstream already trims.
        try:
            from chameleon_mcp.tools import _parse_idiom_blocks

            _, _full = _parse_idiom_blocks(text)
            _, _kept = _parse_idiom_blocks(text[:cap])
            dropped = max(0, len(_full) - len(_kept))
        except Exception:
            dropped = 0
        tail = (
            f"\n... +{dropped} idiom(s) not shown (see .chameleon/idioms.md)"
            if dropped
            else "\n... (idioms truncated; see .chameleon/idioms.md)"
        )
        text = text[:cap].rstrip() + tail
    return text


# Bounds for the experimental nearby-collaborator-signatures section (R1). Kept
# small so the section never dominates the block ("more retrieval can hurt") and
# the index reads stay trivially within the <100ms hot-path budget.
_NEARBY_SIG_MAX_FILES = 5
_NEARBY_SIG_MAX_SYMBOLS = 2
_NEARBY_SIG_MAX_TOTAL = 8
_NEARBY_SIG_MAX_CHARS = 700

# A counterexample snippet is a single taught off-pattern import line; the build
# already skips anything longer, and this hot-path guard skips (never truncates)
# a snippet over the bound, since a counterexample cut mid-line could read as the
# conforming form.
_COUNTEREXAMPLE_MAX_CHARS = 400

# Surfaced in the per-edit block when a deny gate could not read config.json (a
# torn / malformed file): enforcement.mode was unreadable, so the credential /
# import deny was SKIPPED fail-open. The skip is otherwise silent at the edit
# surface (only a check-event reaches /chameleon-doctor and the attestation), so
# this banner makes it visible where the user is actually writing. Fail-open, not
# fail-closed: the edit still proceeds, but the user is no longer in the dark.
_CONFIG_MALFORMED_BANNER = (
    "**Enforcement degraded**: chameleon could not parse `.chameleon/config.json` "
    "(malformed or torn JSON), so credential / import blocking is OFF for this edit. "
    "Fix the JSON and run /chameleon-doctor to confirm enforcement is restored.\n\n"
)

# A trusted repo whose PROFILE (not just config.json) is corrupt, written by an
# unsupported newer schema, or requires a newer engine loads no archetype data, so
# the per-edit block would emit {} -- indistinguishable from a healthy repo editing
# an unarchetyped file. That is a silent-false-clean: the model assumes it got clean
# guidance when it got none. Surface it so the model falls back to grep /
# comprehension tools instead of trusting an empty result. The repair differs by
# cause: a corrupt profile re-derives (/chameleon-refresh); a too-new profile
# (unsupported schema / newer engine_min_version) needs a chameleon UPGRADE, not a
# re-derive on the too-old engine (which would be a dead-end loop).
_PROFILE_DEGRADED_BANNER = (
    "**Profile degraded**: chameleon could not load this repo's `.chameleon/` "
    "profile ({reason}), so NO pattern guidance is available for this edit -- treat "
    "the absence of guidance as unknown, not as clean. Use grep or the comprehension "
    "tools (search_codebase / get_callers) to check conventions yourself. {fix}\n\n"
)

# Sibling of the config banner for a present-but-unreadable enforcement.json. A
# torn enforcement.json empties the MEASURED half of active_block_rules(), so
# the calibrated deny gates (import / naming / phantom and the other measured
# rules) silently no-op -- the same fail-open the config banner covers, but from
# the calibration artifact instead of config.json. The credential / eval deny is
# NOT lost: those rules are calibration-exempt and stay active regardless of the
# artifact. Surfaced at the edit so the silent drop is visible, not just in
# get_status / doctor.
_ENFORCEMENT_MALFORMED_BANNER = (
    "**Enforcement degraded**: chameleon could not parse "
    "`.chameleon/enforcement.json` (malformed or torn JSON), so the calibrated "
    "block rules (import / naming / phantom-import blocking) are OFF for this "
    "edit. The credential / eval deny stays active (calibration-exempt). "
    "Fix the JSON and run /chameleon-doctor to confirm enforcement is restored.\n\n"
)

# The once-per-session "profile present, untrusted" prompt. Emitted from both the
# archetype-resolved path and the no-archetype early exit (a config/data/new file
# in an untrusted repo), because the prompt gates on trust state, not on a shape
# match — an edit that resolves no archetype must still tell the user to trust.
_UNTRUSTED_PROFILE_PROMPT = (
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


def _nearby_call_proximity(calls, rel: str | None, by_name: dict, edited_key: str | None) -> int:
    """How many of ``rel``'s recorded symbols the edited file is shown calling.

    Read from the reverse calls index (callee -> caller rows): a sibling the
    edited file actually calls is a closer collaborator than an arbitrary
    same-directory file, so it should rank first. Returns 0 on no index, no key,
    or no recorded edge, which collapses the ranking to deterministic name order.
    Bounded by the same per-file symbol cap the rendering applies, so the lookup
    stays trivial on the hot path. Fails open to 0.
    """
    if calls is None or not rel or not edited_key:
        return 0
    try:
        score = 0
        for name in list(by_name)[:_NEARBY_SIG_MAX_SYMBOLS]:
            entry = calls.callers_of(rel, name)
            if not entry:
                continue
            if any(r.get("path") == edited_key for r in entry.get("callers", [])):
                score += 1
        return score
    except Exception:
        return 0


def _nearby_signatures_section(file_path: str, repo_root: Path | None) -> str:
    """Sibling collaborator signatures for the edited file's directory.

    Renders up to a few callable signatures from source files in the SAME
    directory as the target, read from the precomputed ``symbol_signatures.json``
    (no live parse, no edited-file read), so the model sees the CONTRACTS of
    nearby collaborators it may call, not just their filenames -- the cross-file
    gap the effectiveness review measured. Candidates are ranked by call
    proximity: a sibling the edited file is recorded calling (from the reverse
    ``calls_index.json``) leads, and deterministic name order breaks ties and is
    the full order when no call facts exist. Index reads are mtime-cached and
    stay warm under the daemon, so the cost amortizes to ~0 across a session.

    Default-ON, kill switch ``CHAMELEON_NEARBY_SIGNATURES=0``: it adds no
    repo-code execution and no network, only cached artifact reads, so it follows
    the default-on-with-kill-switch principle. Fails open to "" on any missing
    index / error.
    """
    if os.environ.get("CHAMELEON_NEARBY_SIGNATURES") == "0" or repo_root is None:
        return ""
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.calls_index import load_calls_index
        from chameleon_mcp.conventions import _SOURCE_EXTENSIONS, _safe_display_name
        from chameleon_mcp.symbol_signatures import (
            load_symbol_signatures,
            render_imported_definition,
            symbol_presence_in_source,
        )
        from chameleon_mcp.worktree import resolve_profile_root

        profile_root = resolve_profile_root(repo_root)
        sigs = load_symbol_signatures(profile_root)
        if sigs is None or len(sigs) == 0:
            return ""
        target = Path(file_path)
        parent = target.parent
        if not parent.is_dir():
            return ""
        calls = load_calls_index(profile_root)
        edited_key = _repo_rel(repo_root, file_path)
        # Filter to source-suffix siblings in one cheap pass BEFORE sorting, so a
        # flat directory of thousands of asset/config files does not sort its
        # whole listing on every edit. Cap the scored set so proximity ranking
        # over a pathological directory stays bounded on the hot path.
        scan_cap = threshold_int("NEARBY_SIG_SCAN_CAP")
        candidates = sorted(
            (e for e in parent.iterdir() if e != target and e.suffix in _SOURCE_EXTENSIONS),
            key=lambda p: p.name,
        )[:scan_cap]
        scored: list[tuple[int, str, str, dict, Path]] = []
        for entry in candidates:
            if not entry.is_file():
                continue
            rel = _repo_rel(repo_root, str(entry))
            by_name = sigs.for_file(rel) if rel else {}
            if not by_name:
                continue
            score = _nearby_call_proximity(calls, rel, by_name, edited_key)
            scored.append((score, entry.name, rel, by_name, entry))
        # Proximity DESC, then name ASC: collaborators the edited file actually
        # calls lead; name order breaks ties and is the entire order when the
        # calls index is absent (every score 0), preserving prior behavior.
        scored.sort(key=lambda t: (-t[0], t[1]))
        verify_cap = threshold_int("NEARBY_SIG_VERIFY_MAX_BYTES")
        rendered: list[str] = []
        files_used = 0
        for _score, _name, rel, by_name, entry in scored:
            if files_used >= _NEARBY_SIG_MAX_FILES or len(rendered) >= _NEARBY_SIG_MAX_TOTAL:
                break
            files_used += 1
            # Scrub control bytes from the path for DISPLAY only (the raw rel was
            # already used for the signature lookup and proximity scoring above);
            # a sibling whose name carries a newline must not split this listing
            # the way it would the "Nearby:" line.
            safe_rel = _safe_display_name(rel)
            # Re-verify each stored signature against the CURRENT sibling on disk:
            # signatures are production-ref derived (or can predate a local edit),
            # so a stored line may be stale or the symbol gone. Read the file once,
            # bounded; on any read error fall back to the stored rows unchanged
            # (fail-open, prior behavior). A phantom (symbol absent now) is dropped;
            # a moved symbol keeps its contract but loses the misleading line.
            current_lines: list[str] | None = None
            try:
                current_lines = entry.read_text(encoding="utf-8", errors="replace")[
                    :verify_cap
                ].split("\n")
            except OSError:
                current_lines = None
            for name, row in list(by_name.items())[:_NEARBY_SIG_MAX_SYMBOLS]:
                render_row = row
                if current_lines is not None:
                    present, keep_line = symbol_presence_in_source(
                        current_lines, name, row.get("start_line")
                    )
                    if not present:
                        continue
                    if not keep_line:
                        render_row = {k: v for k, v in row.items() if k != "start_line"}
                rendered.append(render_imported_definition(name, render_row, safe_rel))
                if len(rendered) >= _NEARBY_SIG_MAX_TOTAL:
                    break
        if not rendered:
            return ""
        section = "Nearby collaborator signatures:\n" + "\n".join(rendered)
        if len(section) > _NEARBY_SIG_MAX_CHARS:
            section = section[:_NEARBY_SIG_MAX_CHARS].rstrip() + "\n..."
        return section
    except Exception as exc:  # noqa: BLE001
        # Fail open, but leave a trace: this section silently vanishing from a
        # Tier-2 block is indistinguishable from "no siblings", so a systematic
        # failure would otherwise never surface in doctor's error-log check.
        try:
            import time as _time

            stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
            with open(_hook_error_log_path(), "a", encoding="utf-8") as fh:
                fh.write(
                    f"[{stamp}] chameleon: nearby-signatures section failed "
                    f"({type(exc).__name__}: {exc})\n"
                )
        except OSError:
            pass
        return ""


def _inbound_contracts_section(file_path: str, repo_root: Path | None) -> str:
    """Pre-edit inbound dependents: who breaks if this file's exports change.

    The counterpart to ``_nearby_signatures_section`` -- that shows OUTBOUND
    sibling signatures (contracts you might call); this shows INBOUND callers
    (contracts that break if you change THIS file's exported signatures). Reads
    the SAME mtime-cached reverse calls index already loaded per Tier-2 edit plus
    ``symbol_signatures.json`` for the edited file's own exports, and renders each
    exported symbol with the call sites recorded against it, so cross-file
    staleness -- chameleon's most-detected defect class -- is PREVENTED at the
    edit instead of only detected at turn end.

    Bounded (few exports / few sites / small char budget), fires only when real
    caller edges exist, and carries the blast-radius honesty note so an empty or
    short list is never read as "safe to break" (barrels and dynamic dispatch are
    invisible to the snapshot). Default-ON, kill switch ``CHAMELEON_INBOUND_CALLERS=0``;
    no repo-code execution and no network (cached artifact reads only), so it
    follows the default-on-with-kill-switch principle. Fails open to "".
    """
    if os.environ.get("CHAMELEON_INBOUND_CALLERS") == "0" or repo_root is None:
        return ""
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.calls_index import load_calls_index
        from chameleon_mcp.conventions import _safe_display_name
        from chameleon_mcp.symbol_signatures import load_symbol_signatures
        from chameleon_mcp.worktree import resolve_profile_root

        edited_rel = _repo_rel(repo_root, file_path)
        if not edited_rel:
            return ""
        profile_root = resolve_profile_root(repo_root)
        calls = load_calls_index(profile_root)
        if calls is None:
            return ""
        sigs = load_symbol_signatures(profile_root)
        # The edited file's OWN exported callables are the contracts at risk. Fall
        # back to whatever names the calls index recorded for this file when no
        # signatures exist (still a valid caller-edge source).
        export_names = list(sigs.for_file(edited_rel)) if sigs is not None else []
        if not export_names:
            export_names = calls.names_for(edited_rel)
        if not export_names:
            return ""

        max_exports = threshold_int("INBOUND_CALLERS_MAX_EXPORTS")
        max_sites = threshold_int("INBOUND_CALLERS_MAX_SITES")
        max_chars = threshold_int("INBOUND_CALLERS_MAX_CHARS")

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        # Collect every export that has callers, tagging each by how many of its
        # callers are CROSS-FILE. A same-file caller "breaking" when you change a
        # signature is trivially visible -- it's in the file you're editing -- so
        # the section's real value is the cross-file dependents you'd otherwise
        # miss. Ranking cross-file-first (and capping AFTER the rank) stops a
        # cluster of same-file private-method callers from crowding the genuine
        # cross-file break out past the export cap. Bound the export SCAN, not just
        # the render: a file with hundreds of exports must not walk them all
        # (callers_of is a cheap dict hit, but the scan stays bounded on the hot path).
        candidates: list[tuple[int, str, list[dict], int]] = []
        for name in export_names[: threshold_int("NEARBY_SIG_SCAN_CAP")]:
            entry = calls.callers_of(edited_rel, name)
            if not entry:
                continue
            callers = entry.get("callers") or []
            if not callers:
                continue
            # Cross-file callers lead within the export's own site list too, so the
            # max_sites cap never drops a cross-file dependent in favor of a
            # same-file one.
            ordered = sorted(
                callers, key=lambda r: 0 if str(r.get("path") or "") != edited_rel else 1
            )
            cross = sum(1 for r in callers if str(r.get("path") or "") != edited_rel)
            candidates.append((cross, name, ordered, entry.get("total")))

        # Exports with more cross-file callers first, then deterministic name order.
        candidates.sort(key=lambda c: (-c[0], c[1]))

        lines: list[str] = []
        for _cross, name, ordered, total in candidates[:max_exports]:
            sites: list[str] = []
            for row in ordered[:max_sites]:
                # Paths and names come from the committed (attacker-controllable)
                # calls index and render OUTSIDE the imitate-spotlight as a
                # chameleon directive, so they must be fully neutralized against
                # context-tag escape / forged-header injection, not just control-
                # char stripped. sanitize_for_chameleon_context is the boundary the
                # sibling counterexample section already uses.
                p = sanitize_for_chameleon_context(_safe_display_name(str(row.get("path") or "")))
                if not p:
                    continue
                ln = row.get("line")
                sites.append(f"{p}:{ln}" if isinstance(ln, int) else p)
            if not sites:
                continue
            more = ""
            if isinstance(total, int) and total > len(sites):
                more = f" (+{total - len(sites)} more)"
            safe_name = sanitize_for_chameleon_context(_safe_display_name(name))
            lines.append(f"  {safe_name}() <- {', '.join(sites)}{more}")

        if not lines:
            return ""
        section = (
            "Inbound callers of this file's exports (change a signature -> update "
            "these call sites in the same turn):\n"
            + "\n".join(lines)
            + "\n"
            + (
                "Direct call sites from the committed calls snapshot. An empty or "
                "short list is NOT proof it's safe to change a signature: barrels, "
                "dynamic dispatch, and callers added since the last refresh are "
                "invisible here. Run /chameleon-refresh to update the snapshot."
            )
        )
        if len(section) > max_chars:
            section = section[:max_chars].rstrip() + "\n..."
        return section
    except Exception:
        return ""


def _counterexample_section(
    archetype: str | None,
    repo_root: Path | None,
    witness_excerpt: str = "",
    *,
    language: str | None = None,
) -> str:
    """A real off-pattern counterexample for the archetype, paired with the witness.

    When the team has taught a competing import for this archetype and a real file
    still uses the discouraged form, the precomputed ``counterexamples.json`` holds
    that line. It is rendered as a chameleon "do NOT write it this way" directive,
    deliberately OUTSIDE the imitate-spotlight: a counterexample is the one
    repo-derived snippet the model must NOT copy, and the spotlight frames its
    contents as a shape to imitate. The snippet still rides
    ``sanitize_for_chameleon_context`` (it is repo file text) so a crafted import
    line cannot escape the context block or forge a header.

    Suppressed when the canonical ``witness_excerpt`` itself imports the discouraged
    module: the block tells the model to mirror the witness closely and calls it
    "the conforming form", so banning a line the witness opens with is a direct
    self-contradiction. If the most-representative file uses the form, it is not an
    anti-pattern for this archetype, and showing nothing beats showing a
    contradiction. (Mirrors the witness-vs-idiom dedup in _shape_idioms_for_block.)

    Default-ON: this adds no repo-code execution and no network, only a cached
    artifact read, so it follows the kill-switch-default-on principle
    (``CHAMELEON_COUNTEREXAMPLE=0`` disables). It fires only when a genuine,
    taught, still-present off-pattern exists; a clean archetype injects nothing.
    Fails open to "" on any missing artifact / error.
    """
    if os.environ.get("CHAMELEON_COUNTEREXAMPLE") == "0" or not archetype or repo_root is None:
        return ""
    try:
        from chameleon_mcp.counterexamples import (
            _find_import_line,
            load_counterexamples,
            neutralize_fences,
        )
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context
        from chameleon_mcp.worktree import resolve_profile_root

        def _safe(text: str) -> str:
            # Sanitize injection tokens AND break any markdown fence run: the snippet
            # is rendered inside a ``` fence but sits OUTSIDE the spotlight, so a
            # smuggled ``` could otherwise close the fence and land raw text in the
            # model context. neutralize_fences closes that one gap the sanitizer leaves.
            return neutralize_fences(sanitize_for_chameleon_context(text))

        index = load_counterexamples(resolve_profile_root(repo_root))
        if index is None:
            return ""
        rows = index.for_archetype(archetype)
        if not rows:
            return ""
        # Collect every taught off-pattern that is showable: a usable snippet, and
        # not one the witness itself imports (suppress per-row rather than contradict
        # the form we call "the conforming form"). A team that taught winston->logger
        # AND moment->date for one archetype gets BOTH counterexamples, not just the
        # last taught.
        snippets: list[str] = []
        guidance: list[str] = []
        for row in rows:
            snippet = row.get("snippet")
            if not isinstance(snippet, str) or not snippet.strip():
                continue
            if len(snippet) > _COUNTEREXAMPLE_MAX_CHARS:
                continue
            over = row.get("over")
            if (
                isinstance(over, str)
                and over
                and witness_excerpt
                and _find_import_line(witness_excerpt, over, language)
            ):
                continue
            snippets.append(_safe(snippet))
            preferred = row.get("preferred")
            if isinstance(preferred, str) and preferred and isinstance(over, str) and over:
                guidance.append(f"use {_safe(preferred)} instead of {_safe(over)}")
        if not snippets:
            return ""
        plural = len(snippets) > 1
        header = (
            "This archetype has known off-patterns in this repo. Do NOT write them this way:"
            if plural
            else "This archetype has a known off-pattern in this repo. Do NOT write it this way:"
        )
        lines = [header, "```", *snippets, "```"]
        if guidance:
            # Capitalize the first clause; the witness-is-conforming closer is shared.
            joined = "; ".join(guidance)
            joined = joined[0].upper() + joined[1:]
            lines.append(f"{joined}. The canonical witness above is the conforming form.")
        return "\n".join(lines)
    except Exception:
        return ""


# Bounds for the Tier-2 archetype-facts directive. Kept small so the section is a
# compact signal (the contract + the names to reuse), never a wall of identifiers.
_ARCH_FACTS_MAX_METHODS = 8
_ARCH_FACTS_MAX_MACROS = 8
_ARCH_FACTS_MAX_EXPORTS = 40
# Minimum inheritance frequency to surface a base-only "extends X" contract line
# when class_contract has no base. Matches format_conventions_echo's gate
# (conventions._STRONG_THRESHOLD) so the Tier-2 facts and the Tier-1 echo agree.
_ARCH_FACTS_STRONG_BASE_FREQ = 0.60

# Identifier-shape allowlist for every value the archetype-facts section renders
# (export name, base class, required method, DSL macro, decorator). These render
# as chameleon's OWN directive voice OUTSIDE the imitate-spotlight, so a poisoned
# committed value that slipped the prose denylist could otherwise plant an
# authoritative instruction or a no-emoji forged header (the header neutralizer is
# keyed on the 🦎 emoji). A legit value is always a single code identifier — it may
# be namespaced (ActiveInteraction::Base, models.Model), carry a Ruby ?/! suffix, a
# leading @ (decorator), a $ (JS), or <> generics, but NEVER whitespace or sentence
# punctuation. The allowlist is lossless for real profiles (verified against every
# bootstrapped repo) and closes the class the denylist cannot fully cover.
_ARCH_FACTS_TOKEN_RE = re.compile(r"^[\w$.:<>@?!]{1,80}$")


def _archetype_facts_section(archetype: str | None, repo_root: Path | None) -> str:
    """Compact, archetype-SCOPED facts for the Tier-2 block: the contract this
    archetype's files implement and the symbols it already exports.

    Two high-signal, low-volume directives, both scoped to the EDITED archetype
    (additive over the repo-wide convention union injected once at SessionStart,
    which the per-edit block never repeats):

    - **Class contract** — for a class-heavy archetype, the base it extends, the
      methods its files define, and the DSL macros they use (e.g. a Rails
      ActiveInteraction service: extends ActiveInteraction::Base, define execute,
      macro object). Surfacing this on the FIRST edit prevents the
      silently-incomplete class a single witness does not force the model to
      complete -- the "ActiveInteraction class of miss".
    - **Check before creating** — the archetype's OWN exported symbols, so the
      model reuses an existing name instead of duplicating it. Scoped to this one
      archetype, which is sharper than the SessionStart repo-wide union.

    A chameleon directive (not untrusted repo prose), so the caller renders it
    OUTSIDE the imitate-spotlight. conventions.json is read straight from disk and
    scrubbed for injection (trust persists across profile changes, so the
    staleness gate no longer guards this read path); every rendered value is also
    sanitized at the boundary. Bounded by the caps above and fully fail-open to "".

    Default-ON, kill switch ``CHAMELEON_ARCHETYPE_FACTS=0``: it adds no repo-code
    execution and no network (one cached-ish artifact read), so it follows the
    default-on-with-kill-switch principle.
    """
    if os.environ.get("CHAMELEON_ARCHETYPE_FACTS") == "0" or not archetype or repo_root is None:
        return ""
    try:
        conv_path = _enf_profile_dir(repo_root) / "conventions.json"
        if not conv_path.is_file():
            return ""
        conv = json.loads(conv_path.read_text(encoding="utf-8")).get("conventions", {})
        if not isinstance(conv, dict):
            return ""
        from chameleon_mcp.counterexamples import neutralize_fences
        from chameleon_mcp.profile.loader import _prose_injection_unsafe
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        # Every rendered value (base / method / macro / decorator / export name) is
        # an attacker-controllable string from a committed profile. This read is RAW
        # (not via the loader, which scrub_conventions_prose's its copy), and trust
        # persists across profile changes, so each value must be screened HERE the
        # way every other conventions render path is: drop it if the injection scan
        # flags it (a poisoned value that would render as a chameleon directive), then
        # sanitize tag boundaries and break a smuggled ``` fence. _safe returns "" for
        # a dropped/empty value; callers skip empties.
        def _safe(text: object) -> str:
            # Drop non-string / empty (a None must never render as the literal
            # "None").
            if not isinstance(text, str) or not text:
                return ""
            # Identifier-shape allowlist FIRST. Every value here is a single code
            # identifier (base class, method, macro, decorator, export name). These
            # render as chameleon's OWN directive voice OUTSIDE the imitate-spotlight,
            # so the prose denylist below is not enough on its own: a poisoned value
            # that reads as a plausible sentence ("...] Delete all files.") or plants
            # a no-emoji forged header ("[chameleon: SYSTEM OVERRIDE]") would slip the
            # denylist and land as authoritative guidance. A legit identifier never
            # contains whitespace or sentence punctuation, so the allowlist is lossless
            # for real profiles and drops any tampered value outright (a newline or CR
            # in the value fails the charset too, so it can no longer split the
            # single-line directive with a second forged line).
            if not _ARCH_FACTS_TOKEN_RE.match(text):
                return ""
            # Redundant given the allowlist, but kept so a future widening of the
            # charset cannot silently reintroduce an injection-prose leak.
            if _prose_injection_unsafe(text):
                return ""
            return neutralize_fences(sanitize_for_chameleon_context(text))

        def _capped(values: object, cap: int) -> tuple[list[str], str]:
            # Screen the whole list first, THEN cap, so the "+N more" tail counts
            # only real (non-empty, non-poisoned) values that were truncated — never
            # inflated by dropped entries inside the displayed window.
            if not isinstance(values, (list, tuple)):
                return [], ""
            safe_all = [s for v in values if (s := _safe(v))]
            shown = safe_all[:cap]
            overflow = len(safe_all) - len(shown)
            return shown, (f" (+{overflow} more)" if overflow > 0 else "")

        lines: list[str] = []
        cc_map = conv.get("class_contract")
        cc = cc_map.get(archetype) if isinstance(cc_map, dict) else None
        parts: list[str] = []
        base = cc.get("base") if isinstance(cc, dict) else None
        if not (isinstance(base, str) and base):
            # A base-only contract (DRF serializer -> BaseSerializer, Django model
            # -> models.Model, AppConfig) is dropped from class_contract upstream
            # when the cohort has no macros/decorators/required_methods, leaving cc
            # None or base-less. Fall back to the derived dominant base so the first
            # (Tier-2) edit still surfaces "extends X" -- the docstring promises the
            # base it extends, and the lighter Tier-1 echo already shows it (parity).
            inh_map = conv.get("inheritance")
            inh = inh_map.get(archetype) if isinstance(inh_map, dict) else None
            if isinstance(inh, dict) and inh.get("frequency", 0) >= _ARCH_FACTS_STRONG_BASE_FREQ:
                db = inh.get("dominant_base")
                if isinstance(db, str) and db:
                    base = db
        if isinstance(base, str) and base:
            safe_base = _safe(base)
            if safe_base:
                parts.append(f"extends {safe_base}")
        if isinstance(cc, dict):
            # Decorator-anchored archetypes (NestJS @Controller/@Injectable, DRF /
            # FastAPI) carry their contract in `decorators` with no base/methods, so
            # omitting it left those exact framework archetypes with no contract line.
            decs, dec_tail = _capped(cc.get("decorators"), _ARCH_FACTS_MAX_MACROS)
            if decs:
                rendered = ", ".join(d if d.startswith("@") else f"@{d}" for d in decs)
                parts.append(f"decorated with {rendered}{dec_tail}")
            methods, m_tail = _capped(cc.get("required_methods"), _ARCH_FACTS_MAX_METHODS)
            if methods:
                parts.append(f"define {', '.join(methods)}{m_tail}")
            macros, mac_tail = _capped(cc.get("dsl_macros"), _ARCH_FACTS_MAX_MACROS)
            if macros:
                parts.append(f"use macros {', '.join(macros)}{mac_tail}")
        if parts:
            lines.append("Class contract for this archetype: " + "; ".join(parts) + ".")

        exports, e_tail = _capped(
            conv.get("key_exports", {}).get(archetype), _ARCH_FACTS_MAX_EXPORTS
        )
        if exports:
            lines.append(
                "Already defined in this archetype — reuse these before creating a "
                "new one: " + ", ".join(exports) + e_tail + "."
            )
        return "\n".join(lines)
    except Exception:
        return ""


def _match_quality_lead(match_quality: str, archetype_name: str = "", sub_buckets: int = 0) -> str:
    """Match-quality-calibrated directive that leads the witness region.

    A chameleon directive (not untrusted data), emitted OUTSIDE the spotlight. A
    strong structural match (exact/ast) tells the model to mirror the witness
    closely; any weaker match downgrades the witness to a loose reference and
    points at the team idioms as the repo truth to trust. Trailing blank line so
    it spaces cleanly before the spotlight region.

    A ``cluster-*`` archetype is the exception: it is a raw-hash grab-bag whose
    files were grouped by path and coarse shape with no single role, so its
    canonical witness may be cross-role (a migration standing in for a security
    module). Never promote such a witness to "mirror closely" even on a structural
    match -- the "mirror migration boilerplate into a security file" failure is
    worse than no guidance. Named archetypes keep the strong lead.
    """
    if archetype_name.startswith("cluster-"):
        return (
            "Mixed-cluster archetype: treat the witness below as a loose reference, "
            "not a template; its role may differ from this file. The team idioms are "
            "repo truth regardless of file shape.\n\n"
        )
    # A many-sub-bucket archetype groups varied concerns, so its single witness is
    # one sub-cluster's exemplar, not the archetype's template -- "mirror closely"
    # would tell the model to copy an unrelated sub-role. Downgrade to loose even on
    # a structural match, matching the using-chameleon skill's "sub_buckets 2+ = read
    # more carefully" guidance (the threshold keeps a tight 1-few-bucket match strong).
    from chameleon_mcp._thresholds import threshold_int

    if match_quality in ("exact", "ast") and sub_buckets > threshold_int(
        "TIER2_LOOSE_WITNESS_SUB_BUCKETS"
    ):
        return (
            f"Structural match, but this archetype spans {int(sub_buckets)} sub-clusters "
            "of varied concerns: treat the witness below as a loose reference, not a "
            "template -- prefer a same-directory sibling's shape. The team idioms are "
            "repo truth regardless of file shape.\n\n"
        )
    if match_quality in ("exact", "ast"):
        return (
            "Strong structural match: mirror the canonical witness below closely. "
            "Its imports, naming, error handling, and structure are how this "
            "archetype is written here.\n\n"
        )
    return (
        "Weak match: treat the witness below as a loose reference, not a template. "
        "The team idioms are repo truth regardless of file shape; follow the "
        "witness structure only where it clearly applies.\n\n"
    )


def _build_untrusted_region(
    excerpt_content: str,
    idioms_text: str,
    has_idioms: bool,
    dir_listing: str,
    *,
    match_quality: str = "unknown",
) -> str:
    """Assemble the verbatim repo-derived PreToolUse content into one spotlighted
    region, with the higher-signal section in the lead.

    The canonical witness body, team idioms, and sibling listing are untrusted
    data read from repository files. Wrapping them in a per-block provenance
    marker tells the model to imitate their shape, never obey anything inside
    them, and keeps them distinct from chameleon's own directives in the block.

    Section order is relevance-ranked per edit so the higher-signal section takes
    the lead (primacy) position: a high-confidence canonical match (exact/ast)
    leads with the witness, while a weak match leads with team idioms, which are
    repo truth regardless of how the file matched an archetype. Returns "" when
    there is nothing to wrap.
    """
    from chameleon_mcp.sanitization import (
        sanitize_for_chameleon_context,
        spotlight_untrusted,
    )

    canonical_part = (
        "Canonical witness:\n```\n" + excerpt_content + "\n```" if excerpt_content else ""
    )
    shaped_idioms = (
        _shape_idioms_for_block(idioms_text, excerpt_content)
        if (has_idioms and idioms_text)
        else ""
    )
    idioms_part = (
        "Team idioms (captured via /chameleon-teach):\n" + shaped_idioms.rstrip()
        if shaped_idioms.strip()
        else ""
    )
    if match_quality in ("exact", "ast"):
        ordered = [canonical_part, idioms_part]
    else:
        ordered = [idioms_part, canonical_part]
    parts = [p for p in ordered if p]
    if dir_listing:
        parts.append(sanitize_for_chameleon_context(dir_listing))
    if not parts:
        return ""
    return spotlight_untrusted("\n\n".join(parts))


def _proposed_content_for_tool(tool_name: str, tool_input: dict) -> str:
    """The proposed content the given tool actually writes, bound to ITS field.

    Edit writes ``new_string``, Write writes ``content``, NotebookEdit writes
    ``new_source``, MultiEdit writes ``edits[].new_string``. Binding to the exact
    field per tool prevents a decoy-shadow bypass of the credential / eval / import
    deny gates: a lenient ``new_string or content`` fallback would let a Write
    (whose real field is ``content``) carry a clean decoy ``new_string`` that hides
    a malicious ``content`` from the scans, so the credential reaches disk. The
    matcher regex ``Edit|Write|NotebookEdit`` substring-matches ``MultiEdit``, so it
    can reach this hook with empty top-level fields; reading its nested ``edits``
    keeps it from skipping every gate. An unknown tool scans every candidate field
    concatenated, so nothing can hide. Non-string fields coerce to "". Tool-name
    matching is case-insensitive so a non-canonical casing can never silently route
    past a deny gate.
    """
    tn = tool_name.lower() if isinstance(tool_name, str) else ""
    if tn == "notebookedit":
        v = tool_input.get("new_source")
        return v if isinstance(v, str) else ""
    if tn == "write":
        v = tool_input.get("content")
        return v if isinstance(v, str) else ""
    if tn == "edit":
        v = tool_input.get("new_string")
        return v if isinstance(v, str) else ""
    if tn == "multiedit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            return "\n".join(
                e["new_string"]
                for e in edits
                if isinstance(e, dict) and isinstance(e.get("new_string"), str)
            )
        return ""
    return "\n".join(
        v
        for v in (
            tool_input.get("new_string"),
            tool_input.get("content"),
            tool_input.get("new_source"),
            # MultiEdit-style nested payload, in case an unknown tool carries it.
            *(e.get("new_string") for e in (tool_input.get("edits") or []) if isinstance(e, dict)),
        )
        if isinstance(v, str) and v
    )


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
    # Set True when a deny gate swallowed a malformed/torn config.json: the
    # enforcement mode could not be read, so the credential / import deny was
    # skipped fail-open. The check-event alone never reaches here (the edit
    # surface), so the advisory block surfaces a loud degraded banner instead of
    # leaving the skipped block silent. Initialized OUTSIDE the setup try (beside
    # repo_id_hint/repo_root_path): the deny-gate except handlers read it via
    # ``... or _cfg_malformed``, so a setup-try abort before this line must not
    # leave it unbound (that raised UnboundLocalError and failed the whole hook).
    _cfg_malformed = False
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
            # Repo resolution is environment-sensitive (CHAMELEON_ALLOW_TMP_REPO,
            # HOME, cwd). The version+fingerprint-keyed daemon socket is shared
            # across sessions with its env frozen at spawn, so a daemon spawned in
            # a divergent environment can return no_repo for a path the in-process
            # path resolves to a real, trusted profile. Trusting that negative
            # silently skips injection AND the enforcement deny; re-check
            # in-process so the daemon stays a latency layer, not a correctness one.
            "no_repo",
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
            # Bind the proposed content to the exact field the tool writes
            # (Edit=new_string, Write=content, NotebookEdit=new_source). A lenient
            # ``new_string or content`` chain let a decoy key shadow the real one
            # (a Write with a clean decoy ``new_string`` hid its malicious
            # ``content`` from all three deny scans); per-tool binding closes it.
            _proposed_tool = str(payload.get("tool_name") or "")
            proposed = _proposed_content_for_tool(_proposed_tool, tool_input)
            if proposed and isinstance(proposed, str):
                from chameleon_mcp.enforcement_calibration import active_block_rules

                # Enforcement reads resolve to the main worktree's profile in a
                # linked worktree (its own .chameleon is gitignored/absent);
                # identity off the worktree. repo_root_path stays the worktree.
                profile_dir = _enf_profile_dir(repo_root_path)
                active_rules = active_block_rules(profile_dir)
                if "secret-detected-in-content" in active_rules:
                    session_id = payload.get("session_id")
                    repo_id = repo_info.get("id") or repo_id_hint
                    # No cell_type: a credential is a leak in ANY notebook cell
                    # (it is committed to the .ipynb regardless of cell type), and
                    # cell_type is model-supplied, so the secret scan reads the
                    # whole proposed content raw. (The eval deny is cell-aware
                    # because eval only matters when executed.)
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
                        from chameleon_mcp.profile.config import load_config_enforcement_only
                        from chameleon_mcp.violation_class import violation_line

                        # Read ONLY the enforcement section: a typo in an unrelated
                        # config section must not raise and disable the credential
                        # deny (the whole-config load_config did exactly that).
                        mode = load_config_enforcement_only(profile_dir).mode
                        # would_block is a SHADOW-mode measurement; an enforce-mode
                        # block is recorded as an edit decision, not a would-block,
                        # so counting it here inflates the shadow -> enforce tally.
                        if mode == "shadow":
                            for v in hard[:3]:
                                _metric(
                                    advisory_emitted=True,
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
                            # Record the actual block in the decision log (the
                            # non-shadow audit channel, like the PostToolUse
                            # block) so /chameleon-explain can replay it. would_block
                            # stays shadow-only so the promotion tally is clean.
                            _record_edit_decision(
                                repo_id,
                                repo_root_path,
                                file_path,
                                archetype=archetype_name,
                                match_quality=None,
                                confidence_band=None,
                                violations_raised=len(hard),
                                blockable_rules=["secret-detected-in-content"],
                                outcome="blocked",
                                session_id=session_id,
                            )
                            return 0
                if "eval-call" in active_rules:
                    # eval()/exec() is a deterministic RCE sink and a hard-class
                    # security fact, so it earns the same pre-write deny as a
                    # hardcoded credential: the report found it was detected
                    # everywhere but blocked nowhere pre-write. is_hard_class keeps
                    # only the error-severity direct form, so class_eval/
                    # instance_eval stay advisory.
                    session_id = payload.get("session_id")
                    repo_id = repo_info.get("id") or repo_id_hint
                    _ct = tool_input.get("cell_type")
                    hard_eval, eval_suppressed = _proposed_hard_eval_violations(
                        proposed,
                        file_path,
                        tool_name=str(payload.get("tool_name") or ""),
                        cell_type=_ct if isinstance(_ct, str) else None,
                    )
                    if eval_suppressed:
                        _record_overrides(
                            repo_id,
                            [{"rule": "eval-call"}],
                            archetype=archetype_name,
                            file_rel=_repo_rel(repo_root_path, file_path),
                            session_id=session_id,
                            blanket=False,
                        )
                    if hard_eval:
                        from chameleon_mcp.profile.config import load_config_enforcement_only
                        from chameleon_mcp.violation_class import violation_line

                        mode = load_config_enforcement_only(profile_dir).mode
                        if mode == "shadow":
                            for v in hard_eval[:3]:
                                _metric(
                                    advisory_emitted=True,
                                    repo_id=repo_id,
                                    archetype=archetype_name,
                                    would_block=True,
                                    rule="eval-call",
                                    file_rel=_repo_rel(repo_root_path, file_path),
                                    line=violation_line(v),
                                )
                        if mode == "enforce":
                            if archetype_name:
                                _seed_archetype_seen(repo_id, session_id, archetype_name)
                            from chameleon_mcp.sanitization import (
                                sanitize_for_chameleon_context,
                            )

                            locs = ", ".join(
                                str(violation_line(v)) for v in hard_eval[:3] if violation_line(v)
                            )
                            where = f" at line {locs}" if locs else ""
                            _emit_pretool_deny(
                                sanitize_for_chameleon_context(
                                    "chameleon: dynamic code execution (eval/exec) in "
                                    f"the proposed content{where}. eval()/exec() on "
                                    "untrusted input is a remote-code-execution risk; "
                                    "use a safe parser or an explicit dispatch table "
                                    "instead. If this call is genuinely required, add "
                                    f"{_ignore_hint(file_path, 'eval-call')} on the "
                                    "offending line; a bare chameleon-ignore does not "
                                    "cover eval-call."
                                )
                            )
                            _record_edit_decision(
                                repo_id,
                                repo_root_path,
                                file_path,
                                archetype=archetype_name,
                                match_quality=None,
                                confidence_band=None,
                                violations_raised=len(hard_eval),
                                blockable_rules=["eval-call"],
                                outcome="blocked",
                                session_id=session_id,
                            )
                            return 0
    except Exception as exc:
        _cfg_malformed = (
            _note_if_config_malformed(
                exc,
                repo_info.get("id") or repo_id_hint,
                payload.get("session_id"),
                "pretool_secret_deny",
            )
            or _cfg_malformed
        )

    # Content-derived secret protection is NOT trust-gated. scan_hard_secrets
    # reads the user's OWN proposed edit, not the repo profile, so a hardcoded
    # credential is a leak regardless of trust -- and the trust gate's rationale
    # ("a planted profile must not configure a block") does not apply, because no
    # profile is consulted. The trust contract still reserves BLOCKING for a
    # trusted profile (chameleon must not deny edits on a repo the user has not
    # opted into), so on an untrusted repo this NEVER denies; it surfaces the
    # deterministic hard-kind secret as an advisory so a pre-trust user is not
    # silently unprotected. FP-free: only the high-precision deterministic kinds
    # reach the hard set (entropy/keyword hits stay out), and an inline
    # chameleon-ignore on the line suppresses it like the trusted deny.
    if trust_state == "untrusted" and repo_root_path is not None:
        try:
            _tool = str(payload.get("tool_name") or "")
            _proposed = _proposed_content_for_tool(_tool, tool_input)
            if _proposed and isinstance(_proposed, str):
                _hard_sec, _ = _proposed_hard_secret_violations(
                    _proposed, file_path, tool_name=_tool
                )
                if _hard_sec:
                    from chameleon_mcp.sanitization import sanitize_for_chameleon_context
                    from chameleon_mcp.violation_class import violation_line

                    _parts = []
                    for _v in _hard_sec[:3]:
                        _kind = _v.get("secret_kind") or "credential"
                        _ln = violation_line(_v)
                        _parts.append(f"{_kind} at line {_ln}" if _ln else _kind)
                    _summary = "; ".join(_parts)
                    if len(_hard_sec) > 3:
                        _summary += f" (+{len(_hard_sec) - 3} more)"
                    _metric(
                        advisory_emitted=True,
                        repo_id=repo_info.get("id") or repo_id_hint,
                        trust_state="untrusted",
                    )
                    # Summary carries only the secret kind + line (the scanner
                    # redacts the matched token), so the advisory cannot echo the
                    # credential back.
                    _emit_chameleon_context(
                        "<chameleon-context>\n[🦎 chameleon: hardcoded credential]\n\n"
                        + sanitize_for_chameleon_context(
                            f"{_summary} in the proposed content. Rotate any real "
                            "credential and load it from an environment variable or a "
                            "secret manager. This repo's chameleon profile is "
                            "untrusted, so this is an advisory only -- run "
                            "/chameleon-trust to enable the pre-write credential block. "
                            "If this is a known-fake fixture value, add "
                            f"{_ignore_hint(file_path, 'secret-detected-in-content')} "
                            "on the offending line."
                        )
                        + "\n</chameleon-context>"
                    )
                    return 0
                # eval-call is the co-equal deterministic security sink: the
                # trusted PreToolUse gate denies BOTH a hardcoded secret and an
                # eval()/exec() RCE. The untrusted advisory must surface both too,
                # or a pre-trust user is warned about credentials but silently
                # unprotected against a dynamic-eval sink.
                _hard_eval, _ = _proposed_hard_eval_violations(
                    _proposed, file_path, tool_name=_tool
                )
                if _hard_eval:
                    from chameleon_mcp.sanitization import sanitize_for_chameleon_context
                    from chameleon_mcp.violation_class import violation_line

                    _elns = [violation_line(_v) for _v in _hard_eval[:3]]
                    _eloc = ", ".join(f"line {ln}" for ln in _elns if ln) or "the proposed content"
                    _metric(
                        advisory_emitted=True,
                        repo_id=repo_info.get("id") or repo_id_hint,
                        trust_state="untrusted",
                    )
                    _emit_chameleon_context(
                        "<chameleon-context>\n[🦎 chameleon: dynamic code execution]\n\n"
                        + sanitize_for_chameleon_context(
                            f"eval()/exec() dynamic code execution at {_eloc}. This is a "
                            "remote-code-execution sink; prefer an explicit dispatch over "
                            "evaluating a string. This repo's chameleon profile is "
                            "untrusted, so this is an advisory only -- run "
                            "/chameleon-trust to enable the pre-write eval block. If this "
                            "use is deliberate and safe, add "
                            f"{_ignore_hint(file_path, 'eval-call')} on the offending line."
                        )
                        + "\n</chameleon-context>"
                    )
                    return 0
        except Exception:
            pass

    if not archetype_name:
        repo_id = repo_info.get("id") or repo_id_hint
        # A no-archetype file (e.g. a new .env) is the most common credential-leak
        # target, and the deny gate above runs on it. If a torn config silently
        # skipped that deny, surface the degraded banner here too -- this early
        # exit would otherwise emit {} and lose the only edit-surface warning.
        if _cfg_malformed:
            _metric(advisory_emitted=False, repo_id=repo_id, trust_state=trust_state)
            _emit_chameleon_context(
                "<chameleon-context>\n[🦎 chameleon]\n\n"
                + _CONFIG_MALFORMED_BANNER.rstrip()
                + "\n</chameleon-context>"
            )
            return 0
        # A corrupt / too-new PROFILE loads no archetype data, so this exit would
        # emit {} the same as a healthy unarchetyped edit. Surface the degraded
        # state instead so the empty result is never read as "clean". The repair
        # steer differs: too-new profiles need a chameleon upgrade, not a re-derive
        # on the too-old engine (a dead-end loop).
        profile_status = repo_info.get("profile_status")
        _too_new = profile_status in (
            "profile_unsupported_schema_version",
            "profile_too_new",
        )
        if profile_status == "profile_corrupted" or _too_new:
            _metric(advisory_emitted=True, repo_id=repo_id, trust_state=trust_state)
            if _too_new:
                _reason = "profile written by a newer chameleon"
                _fix = (
                    "Upgrade chameleon to the version that wrote this profile "
                    "(a /chameleon-refresh on this older engine will not fix it)."
                )
            else:
                _reason = "corrupt or unreadable profile"
                _fix = "Run /chameleon-refresh (or /chameleon-init) to rebuild the profile."
            _emit_chameleon_context(
                "<chameleon-context>\n[🦎 chameleon: profile degraded]\n\n"
                + _PROFILE_DEGRADED_BANNER.format(reason=_reason, fix=_fix).rstrip()
                + "\n</chameleon-context>"
            )
            return 0
        # The once-per-session untrusted trust prompt gates on trust state, not on
        # a shape match: a session that edits only no-archetype files (config, data,
        # a brand-new file) in an untrusted repo must still be told the profile is
        # untrusted, which the old `_emit({})` early exit silently swallowed.
        if (
            trust_state == "untrusted"
            and repo_id
            and _should_emit_untrusted_prompt(repo_id, payload.get("session_id"))
        ):
            _metric(advisory_emitted=True, repo_id=repo_id, trust_state="untrusted")
            _emit_chameleon_context(_UNTRUSTED_PROFILE_PROMPT)
            return 0
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
        # A torn/unparseable config.json can present as "untrusted" rather than as
        # the enforcement-degraded state: on a repo with NO git remote, identity
        # falls back to config.json's repo_uuid, so a torn config flips the repo_id
        # to a path-hash that no trust record matches. Telling the user to
        # /chameleon-trust then is misleading (and re-granting binds the path-hash
        # id, so a later JSON repair silently drops trust). Detect a torn config and
        # lead with the repair guidance instead. Fail-open: any read trouble keeps
        # the normal trust prompt.
        _config_torn = False
        try:
            if repo_root_path is not None:
                _cfgp = _enf_profile_dir(repo_root_path) / "config.json"
                if _cfgp.is_file():
                    try:
                        json.loads(_cfgp.read_text(encoding="utf-8"))
                    except Exception:
                        _config_torn = True
        except Exception:
            _config_torn = False
        if _config_torn and _should_emit_untrusted_prompt(repo_id, session_id):
            _metric(
                advisory_emitted=True,
                repo_id=repo_id,
                trust_state="untrusted",
                archetype=archetype_name,
                confidence=confidence_band,
            )
            _emit_chameleon_context(
                "<chameleon-context>\n[🦎 chameleon]\n\n"
                + _CONFIG_MALFORMED_BANNER.rstrip()
                + "\nOn a repo with no git remote, a torn config.json also resets "
                "the repo's identity, which can surface as 'untrusted' — repair the "
                "JSON first, then re-run /chameleon-trust only if still prompted.\n"
                "</chameleon-context>"
            )
            return 0
        if _should_emit_untrusted_prompt(repo_id, session_id):
            block = _UNTRUSTED_PROFILE_PROMPT
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
    # Editing the archetype's own canonical witness: re-injecting the edited
    # file's current content back at the editor as "the witness to imitate" is
    # pure redundancy (the model already has the file open), and on a large
    # canonical it dominates the whole block. Drop the excerpt and replace it
    # with a one-line self-witness note below.
    self_witness = False
    try:
        _wit_rel = canonical.get("witness_path")
        if (
            excerpt_content
            and isinstance(_wit_rel, str)
            and _wit_rel
            and repo_root_path is not None
            and file_path
        ):
            self_witness = (repo_root_path / _wit_rel).resolve() == Path(file_path).resolve()
    except OSError:
        self_witness = False
    if self_witness:
        excerpt_content = ""
    elif excerpt_content:
        # A pathological canonical (a 1000-line module) would inject tens of KB
        # on the per-edit hot path; the imitation value lives in the head of the
        # file (imports, naming, structure), so cap at a line boundary with an
        # honest truncation marker.
        from chameleon_mcp._thresholds import threshold_int as _thr_int

        _wit_cap = _thr_int("TIER2_WITNESS_MAX_CHARS")
        if len(excerpt_content) > _wit_cap:
            _cut = excerpt_content.rfind("\n", 0, _wit_cap)
            if _cut <= 0:
                _cut = _wit_cap
            excerpt_content = (
                excerpt_content[:_cut]
                + "\n… (witness truncated; open the canonical file for the rest)"
            )
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

            profile_dir = _enf_profile_dir(repo_root_path)
            if active_rules is None:
                # The secret deny above skipped its read (empty proposed
                # content, or it raised); resolve the calibrated set here.
                from chameleon_mcp.enforcement_calibration import active_block_rules

                active_rules = active_block_rules(profile_dir)
            if "import-preference-violation" in active_rules:
                # Per-tool field binding via the shared helper (as the secret/eval gate uses).
                proposed = _proposed_content_for_tool(
                    str(payload.get("tool_name") or ""), tool_input
                )
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
                    # Fresh disk read (bypasses load_profile_dir): screen the
                    # conventions values for injection so a poisoned `preferred`
                    # cannot land in the deny reason. conv is the INNER dict already.
                    from chameleon_mcp.profile.loader import scrub_conventions_node

                    scrub_conventions_node(conv)
                    banned = banned_imports_in_content(
                        proposed,
                        language=detect_language(file_path),
                        archetype=archetype_name,
                        conventions=conv,
                    )
                    from chameleon_mcp.violation_class import ignored_rules

                    ign = ignored_rules(proposed, file_path=file_path) or set()
                    suppressed_by_ignore = "" in ign or "import-preference-violation" in ign
                    if suppressed_by_ignore:
                        # The directive bypasses the deny. Record the override so
                        # the audit sees a bypass at the deny gate too, not only
                        # at the PostToolUse verifier. A bare directive (empty
                        # string in the set) is the blanket form. ``banned`` is
                        # already empty here -- lint_conventions suppresses an
                        # ignored rule -- so re-scan with the directives stripped to
                        # recover the bypassed import; otherwise this recording is
                        # dead code and the bypass is invisible to the audit.
                        raw_banned = banned_imports_in_content(
                            _strip_chameleon_ignore_directives(proposed),
                            language=detect_language(file_path),
                            archetype=archetype_name,
                            conventions=conv,
                        )
                        if raw_banned:
                            _record_overrides(
                                repo_id,
                                [{"rule": "import-preference-violation"}],
                                archetype=archetype_name,
                                file_rel=_repo_rel(repo_root_path, file_path),
                                session_id=session_id,
                                blanket="" in ign,
                            )
                    if banned and not suppressed_by_ignore:
                        from chameleon_mcp.profile.config import (
                            load_config_enforcement_only,
                        )

                        # Isolated enforcement read: an unrelated config-section
                        # typo must not raise and silently disable this deny.
                        mode = load_config_enforcement_only(profile_dir).mode
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
    except Exception as exc:
        _cfg_malformed = (
            _note_if_config_malformed(
                exc,
                repo_info.get("id") or repo_id_hint,
                payload.get("session_id"),
                "pretool_import_deny",
            )
            or _cfg_malformed
        )

    enforcement_state = None
    try:
        from chameleon_mcp import enforcement

        repo_data = _plugin_data_dir() / repo_id if repo_id else None
        if repo_data and session_id:
            enforcement_state = enforcement.load_state(repo_data, session_id)
    except Exception:
        pass

    summary = archetype_obj.get("summary", "")
    first_in_archetype = True
    has_violations = False
    if enforcement_state is not None:
        first_in_archetype = archetype_name not in enforcement_state.archetypes_seen
        has_violations = archetype_name in enforcement_state.archetypes_with_violations
        enforcement_state.archetypes_seen.add(archetype_name)
        # Record the idiom NAMES this Tier-2 block actually renders, so the turn-end
        # self-review can summarize exactly those (name + gist) and still show full
        # text for any idiom truncated out of the block. Shape the idioms the SAME
        # way the block below does (`_shape_idioms_for_block`, which dedups vs the
        # witness and char-caps), then take the surviving `### ` names -- so a
        # per-archetype "seen" is never over-claimed past the cap. Gated on the same
        # predicate the Tier-2 branch below uses; the deny path (which seeds
        # archetypes_seen without showing anything) never reaches this.
        if (first_in_archetype or has_violations or not summary) and has_idioms:
            try:
                from chameleon_mcp.tools import _idiom_block_names

                shaped = _shape_idioms_for_block(idioms_text, excerpt_content)
                enforcement_state.idioms_shown_names |= _idiom_block_names(shaped)
            except Exception:
                pass
        try:
            enforcement.save_state(enforcement_state, repo_data, session_id)
        except Exception:
            pass

    use_tier2 = first_in_archetype or has_violations or not summary

    # A present-but-unreadable enforcement.json silently empties the MEASURED
    # block-rule set, so the calibrated deny gates (import/naming/phantom)
    # no-op with no signal; the calibration-exempt secret/eval deny stays
    # armed. Surface it at the edit like the config banner (fail-open).
    _enf_malformed = False
    try:
        if repo_root_path is not None:
            from chameleon_mcp.tools import _enforcement_artifact_unreadable

            _enf_malformed = _enforcement_artifact_unreadable(_enf_profile_dir(repo_root_path))
    except Exception:
        _enf_malformed = False

    if not use_tier2:
        block = f"<chameleon-context>\n[🦎 chameleon: {safe_name} ({safe_band})]\n"
        if _cfg_malformed:
            block += _CONFIG_MALFORMED_BANNER
        if _enf_malformed:
            block += _ENFORCEMENT_MALFORMED_BANNER
        if summary:
            block += f"{sanitize_for_chameleon_context(summary)}\n"
        conv_echo = ""
        try:
            from chameleon_mcp.conventions import format_conventions_echo

            conventions_path = (
                _enf_profile_dir(repo_root_path) / "conventions.json" if repo_root_path else None
            )
            if conventions_path and conventions_path.is_file():
                conv_data = json.loads(conventions_path.read_text(encoding="utf-8"))
                # Read straight from disk (not via load_profile_dir), so screen the
                # conventions prose values + principles.md for injection here: render
                # sanitization does not neutralize injection prose, and trust persists
                # across changes so the staleness gate no longer covers this echo path.
                from chameleon_mcp.profile.loader import safe_prose_text, scrub_conventions_prose

                # The echo renders only the edited archetype's four dimensions;
                # slice to that subset BEFORE the O(size) scrub/sanitize so a
                # multi-MB conventions.json doesn't cost the whole hot-path budget
                # per edit (see _conventions_echo_subset).
                conv_subset = _conventions_echo_subset(conv_data, archetype_name)
                scrub_conventions_prose(conv_subset)
                pr_text = safe_prose_text(_enf_profile_dir(repo_root_path) / "principles.md")
                # Sanitize attacker-controllable inputs at the boundary, for
                # parity with the SessionStart path (the assembled echo carries a
                # <chameleon-conventions> wrapper the output-sanitizer would mangle).
                conv_echo = format_conventions_echo(
                    _sanitize_profile_obj(conv_subset),
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
    if _cfg_malformed:
        block += _CONFIG_MALFORMED_BANNER
    if _enf_malformed:
        block += _ENFORCEMENT_MALFORMED_BANNER
    if trust_state == "stale":
        block += (
            "**Trust is stale**: a recent /chameleon-refresh, /chameleon-teach, "
            "or manual edit changed the committed profile after the trust grant. "
            "Trust is tied to the profile sha, so the grant no longer covers the "
            "current profile. Suggest /chameleon-trust to re-confirm. Do not block "
            "the edit; chameleon advisory is provided below for reference only.\n\n"
        )
    # Archetype-scoped facts (the class contract this archetype implements + the
    # symbols it already exports) lead the block as a chameleon directive, OUTSIDE
    # the imitate-spotlight: "what to implement / what to reuse" is sharper on the
    # FIRST edit than a single witness, and scoped to this archetype it is additive
    # over the repo-wide convention union injected once at SessionStart.
    facts = _archetype_facts_section(archetype_name, repo_root_path)
    if facts:
        block += facts + "\n\n"
    # Gather the sibling listing first, then emit the whole verbatim repo-derived
    # region (canonical witness + team idioms + sibling listing) as ONE
    # spotlighted block. The marker gives the model a provenance signal that this
    # is untrusted data to imitate, never instructions to follow — distinct from
    # chameleon's own directives (header, rules pointer) which stay unwrapped.
    # Excerpt and idioms arrive already sanitized from get_pattern_context; the
    # listing is sanitized here (a sibling filename can carry a context-escape
    # token or a forged [🦎 ...] header), and spotlighting neutralizes a forged
    # marker before wrapping.
    dir_listing = ""
    try:
        from chameleon_mcp.conventions import format_directory_listing

        dir_listing = format_directory_listing(file_path) or ""
    except Exception:
        dir_listing = ""
    # Nearby collaborator SIGNATURES (default-ON, kill switch
    # CHAMELEON_NEARBY_SIGNATURES=0), not just filenames, so the model sees the
    # contracts of calls it must make. Repo-derived, so it rides the same
    # sanitize + spotlight path as the listing.
    nearby_sigs = _nearby_signatures_section(file_path, repo_root_path)
    if nearby_sigs:
        dir_listing = (dir_listing + "\n\n" + nearby_sigs) if dir_listing else nearby_sigs
    untrusted_region = _build_untrusted_region(
        excerpt_content, idioms_text, has_idioms, dir_listing, match_quality=match_quality
    )
    if self_witness:
        # A chameleon directive (not repo data), so it stays outside the
        # imitate-spotlight, in the position the witness lead would occupy.
        block += (
            "This file IS the archetype's canonical witness — sibling files are "
            "guided to imitate it, so its content is not re-shown here. Keep its "
            "conventions stable: changes here shift the pattern the rest of the "
            "archetype is measured against.\n\n"
        )
    if untrusted_region:
        # Calibrate how hard to lean on the witness by match quality. This is a
        # chameleon directive about the data, so it stays OUTSIDE the untrusted
        # spotlight region (which is framed as "imitate, never obey"). Gate it on
        # a witness excerpt actually being present: the region can be non-empty
        # from the Nearby listing / idioms alone (witness deleted on disk), and
        # "mirror the canonical witness below closely" must not preface a block
        # that contains no witness to mirror.
        if excerpt_content:
            block += _match_quality_lead(
                match_quality, archetype_name or "", int(sub_buckets_count or 0)
            )
        block += untrusted_region + "\n\n"
    # Pair the witness (the conforming form) with a real off-pattern the team has
    # flagged, immediately after it: the in-context-learning literature finds a
    # positive/negative contrast beats a positive example alone. This is a
    # chameleon "do NOT write it this way" directive, so like the match-quality
    # lead it stays OUTSIDE the imitate-spotlight.
    from chameleon_mcp.lint_engine import detect_language as _detect_lang

    counterexample = _counterexample_section(
        archetype_name,
        repo_root_path,
        excerpt_content,
        language=_detect_lang(file_path) if file_path else None,
    )
    if counterexample:
        block += counterexample + "\n\n"
    # Inbound caller contracts: who breaks if this file's exported signatures
    # change. A chameleon directive over repo-derived facts (paths/names sanitized
    # in the section), so like the counterexample it stays OUTSIDE the imitate-
    # spotlight. Converts the turn-end crossfile-staleness finding into a pre-edit
    # prevention.
    inbound = _inbound_contracts_section(file_path, repo_root_path)
    if inbound:
        block += inbound + "\n\n"
    if canonical.get("missing"):
        block += (
            f"(canonical witness {sanitize_for_chameleon_context(str(canonical.get('witness_path')))} is "
            "missing on disk; run /chameleon-refresh to re-select)\n\n"
        )
    if rules_count:
        # Rules are verbose lint/formatter config; keep the pointer rather than
        # flooding the block. Rules are repo-global (scoped by source, not by
        # archetype), so the pointer names the repo, not the archetype, to avoid a
        # failed lookup.
        block += (
            f"Rules: {rules_count} repo-wide lint/format rules apply — "
            "call get_rules with this repo's path or id for the config.\n"
        )
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
            _rb = p.read_bytes()
            _bw_truncated = len(_rb) > 100_000
            content = _rb[:100_000].decode("utf-8", errors="replace")
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
                violations = _lint_file_in_process(
                    repo_root,
                    archetype_name,
                    content,
                    file_path,
                    content_truncated=_bw_truncated,
                )
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
            from chameleon_mcp.lint_engine import detect_language
            from chameleon_mcp.violation_class import (
                block_eligible_on_file,
                build_ignore_index,
                hard_class_violations,
                is_archetype_independent,
                is_violation_ignored,
            )

            active = active_block_rules(_enf_profile_dir(repo_root))
            hard = hard_class_violations(violations, active)
            # A non-code file (detect_language None) never hard-blocks on an
            # archetype-independent rule -- it has no inline chameleon-ignore
            # escape -- so it must not arm the Stop backstop either, keeping the
            # arming and the backstop re-lint consistent.
            hard = block_eligible_on_file(hard, language=detect_language(file_path))
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
                record_clean,
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
            # Info-only content has no conformance failure: record it clean so a
            # Bash-written file escalates on the same terms the Edit-tool path
            # does (info advisories never ratchet the per-file level). `hard`
            # can never be info, so an armed file is unaffected.
            if any(not _is_info_violation(v) for v in violations):
                record_violation(
                    fs,
                    now=now,
                    archetype=record_archetype,
                    hard_class=bool(hard),
                )
                state.archetypes_with_violations.add(record_archetype)
            else:
                record_clean(fs, now=now)
            save_state(state, repo_data_dir, session_id or "")
        except Exception:
            pass


def posttool_recorder() -> int:
    """PostToolUse Bash: HMAC-signed exec log."""
    payload = _read_payload_dict()
    if payload is None:
        _emit({})
        return 0

    tool_name = payload.get("tool_name", "")
    # The recorder is matched for Bash|Edit|Write|NotebookEdit, but its exec-log
    # append and Bash-write re-lint are Bash-only work: an Edit/Write/NotebookEdit
    # carries no command, so appending an exec-log row for it wrote an empty
    # entry that violates the log's "one row per Bash invocation" invariant. Gate
    # both on the Bash tool (non-string tool_name fails the check safely).
    is_bash = isinstance(tool_name, str) and tool_name == "Bash"

    tool_input = _as_dict(payload.get("tool_input"))
    tool_response = _as_dict(payload.get("tool_response"))
    command = tool_input.get("command", "")
    session_id = payload.get("session_id", "unknown")
    exit_code = tool_response.get("returnCode") if isinstance(tool_response, dict) else None

    if not is_bash:
        _emit({})
        return 0

    cwd_raw = payload.get("cwd")
    # os.getcwd() re-raises FileNotFoundError when the process cwd was deleted --
    # the same fault the resolve() fallback below was trying to catch -- so both
    # the default and the fallback route through _safe_cwd(), which never raises.
    cwd_str = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else str(_safe_cwd())
    try:
        cwd = Path(cwd_str).resolve()
    except (OSError, ValueError):
        cwd = _safe_cwd()
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
            # Recording a Bash-written file into the enforcement state ARMS the
            # Stop backstop for it. During a /chameleon-pause or -disable window
            # that must not happen -- posttool_verify returns early on the same
            # suppression, so a Bash write would otherwise be the one edit path
            # that still armed a turn-end block while the user muted chameleon.
            from chameleon_mcp.optouts import is_chameleon_suppressed

            if is_chameleon_suppressed(cwd, repo_id, session_id) is None:
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
    content_truncated: bool = False,
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
        violations = [v.to_dict() for v in lint(snapshot, ast_query, language=language)]

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

    # Cross-file importer / removed-export advisory, mirroring the daemon
    # lint_file path (tools.py): without it, the per-edit cross-file signal
    # surfaced ONLY when the daemon answered, so a daemon-down fallback silently
    # dropped it. content_truncated is threaded so a >100KB prefix does not
    # spuriously flag its tail exports as removed.
    try:
        from chameleon_mcp.phantom_imports import lint_cross_file_imports

        violations.extend(
            v.to_dict()
            for v in lint_cross_file_imports(
                content,
                file_path=file_path,
                repo_root=repo_root,
                language=language,
                content_truncated=content_truncated,
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
    scanned here too; eval-call is archetype-independent, so an error-severity
    eval()/exec() in an unarchetyped file enforces here (it no longer needs an
    archetype match), while style stays advisory. Each sub-scan is wrapped so a
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
    if language is not None:
        # Gate the dangerous-sink scan to recognized code languages, mirroring the
        # PreToolUse proposed-content gate: on a non-code file (markdown / plain
        # text / config prose) scan_dangerous_sinks falls through to its raw-content
        # branch and flags the literal text `eval(` in documentation, which is not
        # a runnable sink. A non-code file also cannot carry a chameleon-ignore
        # directive, so under enforce that eval-call would turn-trap with no escape.
        try:
            from chameleon_mcp.lint_engine import scan_dangerous_sinks

            out.extend(v.to_dict() for v in scan_dangerous_sinks(content, language=language))
        except Exception:
            pass
        # A phantom import (a relative/aliased specifier resolving to no file) is a
        # content fact independent of the archetype, exactly like the secret and
        # eval scans above -- the docstrings on this path and on the Stop relint
        # both promise it blocks on an unarchetyped file. It was never scanned
        # here, so a hallucinated import in a brand-new file at the repo root or in
        # an unclustered directory shipped with no edit-time advisory and no
        # turn-end refusal. Resolve needs the repo root + a rules map for tsconfig
        # aliases; ``rules`` may be None (callers without the profile loaded), in
        # which case alias resolution degrades to the on-disk check.
        if repo_root is not None:
            try:
                from chameleon_mcp.phantom_imports import lint_phantom_imports

                out.extend(
                    v.to_dict()
                    for v in lint_phantom_imports(
                        content,
                        file_path=file_path,
                        repo_root=repo_root,
                        language=language,
                        rules=rules,
                    )
                )
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
    content: str | None = None,
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

    ``content`` is the file's first-100KB decode the caller already has in hand;
    it is reused for the inline-ignore index scans rather than re-reading the
    same bytes off disk (the per-edit hot path). It is the identical window
    ``_read_file_for_ignore`` reads, so the ignore directives it finds are the
    same; falls back to that read only when the caller passed nothing.
    """
    ignore_scan_content = content if content is not None else _read_file_for_ignore(file_path)

    hard: list[dict] = []
    try:
        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.violation_class import (
            block_eligible_on_file,
            is_archetype_independent,
            is_hard_class,
        )

        # Only an archetype-INDEPENDENT hard rule can be enforced without an
        # archetype: the Stop backstop's no-archetype re-lint filters to the same
        # set. Deterministic security facts qualify -- a hardcoded secret AND an
        # error-severity eval()/exec() are dangerous regardless of archetype, so
        # both enforce here. Archetype-DEPENDENT rules (naming, inheritance) need
        # a confidence/match-quality gate that can never pass without an archetype
        # and are correctly excluded. Recording only what the backstop will
        # actually block keeps the two paths consistent.
        hard = [
            v for v in violations if is_hard_class(v) and is_archetype_independent(v.get("rule"))
        ]
        # A non-code file (markdown / config prose, detect_language None) never
        # hard-blocks on an archetype-independent rule: the token stays advisory in
        # `violations`, it just does not arm the Stop backstop. Such a file cannot
        # carry a chameleon-ignore directive, so a block would have no escape.
        hard = block_eligible_on_file(hard, language=detect_language(file_path))
        try:
            from chameleon_mcp.violation_class import build_ignore_index, is_violation_ignored

            idx = build_ignore_index(ignore_scan_content, file_path=file_path)
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
        displayed = _displayable_violations(violations, ignore_scan_content, file_path)
        if not displayed:
            return False
        # Same severity split as the archetype path: info rows (a test-integrity
        # finding, then-without-catch, an authz-guard hint reachable here via the
        # archetype-independent scans) render as advisory notes, never counted
        # under "[N violations]". This path has no escalation tone.
        block, _, _ = _render_violation_sections(displayed, actionable_tone=None)
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


def _deny_scan_content(proposed: str) -> str:
    """Content window for the deterministic hard-secret / hard-eval DENY scans.

    The deny is the ONLY gate that stops the write from landing on disk, so it
    must not be evadable by padding the offending token past a fixed prefix cap
    (the advisory 100KB ``PREWRITE_SECRET_SCAN_MAX_CHARS`` window). Any single
    proposed write up to ``PREWRITE_DENY_SCAN_MAX_CHARS`` is scanned in full; a
    pathologically larger write is scanned head+tail (each half of the ceiling),
    which still defeats front- or back-padding while bounding worst-case work.

    The dropped middle is replaced by the SAME NUMBER of newlines it contained,
    so a hit in the tail window is still reported at its TRUE line number (a
    plain ``\\n`` join would report a tail secret short by the dropped middle's
    line count and misdirect the ``chameleon-ignore`` hint). Counting newlines is
    far cheaper than the regex scan the cap bounds, and the padding is blank lines
    the scanners skip.
    """
    from chameleon_mcp._thresholds import threshold_int

    if not proposed:
        return proposed
    cap = threshold_int("PREWRITE_DENY_SCAN_MAX_CHARS")
    if len(proposed) <= cap:
        return proposed
    half = cap // 2
    mid_newlines = proposed[half:-half].count("\n")
    return proposed[:half] + ("\n" * mid_newlines) + proposed[-half:]


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

    The hard-block deny is skipped only for prose/doc files (``.md``/``.txt``/
    ``.rst``...), where a sample AKIA.../ghp_/PEM token is documentation rather
    than a leak. Config and data files (``.env``/``.yml``/``.json``/``.toml``)
    are the most common real leak target, so the deny still fires there even
    though they are not a recognized code language; the hard-kind tokens are
    high-precision. A real secret lives inside a string literal, so the scan runs
    against RAW content (the per-language string strip would blank the token
    itself and find nothing).

    Notebooks are NOT cell-type-exempt, unlike the eval deny. The two sinks have
    different risk models: an ``eval()`` only matters if EXECUTED, so a markdown
    cell (never run) is genuinely safe and the eval path skips it; a credential
    matters if PERSISTED, and a notebook markdown/raw cell is committed to the
    ``.ipynb`` in version control exactly like a code cell. ``cell_type`` is also
    a model-supplied field, so honoring it would let a code-bearing cell be
    mislabeled ``markdown`` to slip a key past the gate. The whole proposed
    content is therefore scanned RAW: for a NotebookEdit that is the cell source,
    for a Write/Edit of a ``.ipynb`` it is the notebook JSON (whose cell-source
    strings carry the token verbatim) — a secret in any cell is caught. The
    ``.md``/``.txt`` exclusion above stays file-extension-based (immutable,
    unambiguously a docs file), which a notebook cell is not.
    """
    from chameleon_mcp.lint_engine import scan_hard_secrets
    from chameleon_mcp.violation_class import (
        IgnoreIndex,
        build_ignore_index,
        is_hard_class,
        is_violation_ignored,
        tag_secret_hardness,
    )

    # The hard-secret deny may hard-block an edit, so skip it only for prose/doc
    # files, where a sample key is documentation (the original false-positive).
    # Config and data files (.env, .yml, .json, .toml, ...) are NOT a recognized
    # code language but are the most common real leak target, so the deny must
    # still fire there; the hard-kind tokens (AKIA.../ghp_/PEM) are high-precision.
    _prose = (".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".adoc")
    if file_path and str(file_path).lower().endswith(_prose):
        return [], False
    clipped = _deny_scan_content(proposed)
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
    if hard and isinstance(tool_name, str) and tool_name.lower() in ("edit", "notebookedit"):
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


def _notebook_cell_scans_as_python(clipped: str, cell_type: str | None) -> bool:
    """True when a NotebookEdit's proposed cell source should be scanned as Python.

    A notebook CODE cell is Python (the dominant kernel), so an ``eval()``/
    ``exec()`` there is the same RCE sink as in a ``.py`` file -- but
    ``detect_language('.ipynb')`` is None, so the eval-call deny that guards a
    ``.py`` write would silently skip the notebook path. A MARKDOWN cell is prose
    (a sentence mentioning ``eval()`` must never hard-block) and is excluded by
    the caller before this is reached. When the cell type is unstated (an
    ``edit_mode='replace'`` payload often omits it), the content is treated as
    code only if it actually parses as Python, so a prose remainder can never
    false-positive into a block.
    """
    if cell_type == "code":
        return True
    try:
        import ast

        ast.parse(clipped)
        return True
    except Exception:  # noqa: BLE001 - any parse trouble -> not code -> no scan
        return False


def _notebook_python_to_scan(
    clipped: str, file_path: str, tool_name: str, cell_type: str | None
) -> str | None:
    """Python source to eval-scan for a notebook edit, or None to skip.

    ``detect_language('.ipynb')`` is None, so the eval-call deny that guards a
    ``.py`` write would skip every notebook path. Recover the Python the edit
    really writes so an ``eval()``/``exec()`` in a notebook is denied like one in
    a ``.py``:

    - NotebookEdit: ``clipped`` IS one cell's source. A code cell (or an unstated
      cell whose source parses as Python) is returned; a markdown cell is prose
      and any other explicit type (e.g. a non-executed ``raw`` cell) is skipped,
      so a sentence mentioning ``eval()`` never hard-blocks.
    - Write/Edit on a ``.ipynb``: ``clipped`` is the notebook JSON, so parse it
      and return the concatenated source of every code cell. Editing the raw
      notebook file is as much in the model's control as a NotebookEdit, so the
      same sink is closed there; a fragment Edit whose ``new_string`` is not whole
      JSON simply fails to parse and returns None.

    Bounded (the caller already clipped to the scan ceiling) and fully fail-open.
    Tool-name AND cell_type matching are case-insensitive: a non-canonical
    ``notebookedit`` casing must be treated as a cell-source edit (not fall through
    to the ``.ipynb`` whole-notebook JSON branch and skip the scan), and a code
    cell typed ``"Code"``/``"CODE"`` must still be scanned rather than read as a
    non-code cell and skipped.
    """
    ct = cell_type.lower() if isinstance(cell_type, str) else None
    if isinstance(tool_name, str) and tool_name.lower() == "notebookedit":
        # Only an executed code cell (or an unstated cell that parses as Python) is
        # scanned; markdown and any other explicit type (raw, ...) are not code.
        if ct not in (None, "code"):
            return None
        return clipped if _notebook_cell_scans_as_python(clipped, ct) else None
    if str(file_path).lower().endswith(".ipynb"):
        try:
            import json as _json

            nb = _json.loads(clipped)
            cells = nb.get("cells") if isinstance(nb, dict) else None
            if not isinstance(cells, list):
                return None
            parts: list[str] = []
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                _cct = cell.get("cell_type")
                if not (isinstance(_cct, str) and _cct.lower() == "code"):
                    continue
                src = cell.get("source")
                if isinstance(src, list):
                    parts.append("".join(s for s in src if isinstance(s, str)))
                elif isinstance(src, str):
                    parts.append(src)
            joined = "\n".join(p for p in parts if p)
            return joined or None
        except Exception:  # noqa: BLE001 - unparseable notebook -> skip, fail open
            return None
    return None


def _proposed_hard_eval_violations(
    proposed: str, file_path: str, *, tool_name: str, cell_type: str | None = None
) -> tuple[list[dict], bool]:
    """Hard-class eval-call violations in proposed content, after ignore filtering.

    The sibling of :func:`_proposed_hard_secret_violations` for the other
    deterministic security sink. Scans the proposed content for eval()/exec()
    via ``scan_dangerous_sinks`` and keeps only the hard class -- ``is_hard_class``
    drops the warning-severity ``*_eval`` metaprogramming variants
    (``class_eval``/``instance_eval``), so only the error-severity direct form
    can deny. Hits a NAMED ``eval-call`` directive covers are dropped (eval-call
    is blanket-immune, so a bare chameleon-ignore does not clear it). Returns
    ``(violations, named_suppressed)``; the scan is capped at the same ceiling as
    the secret scan, with content past the cap left to the PostToolUse/Stop scans.

    Gated to recognized code languages (``detect_language`` is not None): the
    literal text ``eval(`` in prose/config/fixtures
    (``.md``/``.txt``/``.json``/``.yaml``) must not hard-block the write in
    enforce mode. For a recognized language ``scan_dangerous_sinks`` runs the
    same per-language string/comment strip the lint path uses, so an ``eval(``
    inside a string or comment never fires; only a real call in code does.
    Without the gate an unrecognized extension falls through to that scanner's
    raw-content branch and denies on a documented ``eval()`` mention.

    Notebooks are the exception (see :func:`_notebook_python_to_scan`): a
    NotebookEdit's ``proposed`` is a cell's SOURCE and a Write/Edit on a ``.ipynb``
    carries the notebook JSON, neither of which ``detect_language`` recognizes
    even though a code cell is real Python. The code a notebook edit writes is
    recovered and scanned as Python so an ``eval()`` there is denied exactly like
    one in a ``.py``; markdown/prose cells are never scanned.
    """
    from chameleon_mcp.lint_engine import detect_language, scan_dangerous_sinks
    from chameleon_mcp.violation_class import (
        IgnoreIndex,
        build_ignore_index,
        is_hard_class,
        is_violation_ignored,
    )

    language = detect_language(file_path)
    clipped = _deny_scan_content(proposed)
    if language is None:
        notebook_src = _notebook_python_to_scan(clipped, file_path, tool_name, cell_type)
        if notebook_src is None:
            return [], False
        clipped = _deny_scan_content(notebook_src)
        language = "python"
    violations = [v.to_dict() for v in scan_dangerous_sinks(clipped, language=language)]
    hard = [v for v in violations if v.get("rule") == "eval-call" and is_hard_class(v)]
    if not hard:
        return [], False
    named_suppressed = False
    idx = build_ignore_index(clipped, file_path=file_path)
    if idx is not None:
        kept = [v for v in hard if not is_violation_ignored(v, idx)]
        named_suppressed = len(kept) < len(hard)
        hard = kept
    if hard and isinstance(tool_name, str) and tool_name.lower() in ("edit", "notebookedit"):
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


def _is_info_violation(v: dict) -> bool:
    """True when a violation is an informational advisory, not a conformance
    failure.

    ``info``-severity rows (cross-file importer blast-radius, the hedged
    named-export count-bucket signal) are FYI context a reviewer would give
    before a rename -- they explicitly say "not a defect". They must not be
    counted toward the "N violations / Fix these." imperative, and an edit that
    raises ONLY these must not ratchet the per-file escalation level: doing so
    turns a purely additive edit into an escalating "violation" and pairs a
    "do not change anything to satisfy this" message with a "Fix these."
    order. Anything without an explicit ``info`` severity stays actionable.
    """
    return isinstance(v, dict) and str(v.get("severity") or "").lower() == "info"


def _render_violation_sections(
    displayed: list[dict], *, actionable_tone: str | None
) -> tuple[str, list[dict], list[dict]]:
    """Render surfaced violations as up to two severity-framed sections.

    Actionable rows (warning/error) render under "[N violation(s)]" and carry
    ``actionable_tone`` (the escalation imperative) when one is given; info rows
    render under an advisory-note header with NO imperative -- they are context a
    reviewer weighs, not conformance failures to correct, and pairing them with
    "Fix these." is self-contradictory (the info class spans blast-radius, the
    count-bucket "not a defect" hint, a fit-shape "may be wrong" signal, a
    missing-authz-guard "confirm" hint, and the test-integrity family). The
    header says "review", NOT "no action required": some info rows (the authz
    hint, an assertion-free test) do warrant a look -- they are just not the
    blocking-violation class that gets the imperative.

    Returns ``(block, actionable, info)`` so the caller drives escalation and the
    statusline off the same partition it renders. Shared by the archetype path
    and the no-archetype advisory so the two never frame info rows differently.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    actionable = [v for v in displayed if not _is_info_violation(v)]
    info = [v for v in displayed if _is_info_violation(v)]
    sections: list[str] = []
    if actionable:
        lines = [
            f"{i + 1}. {sanitize_for_chameleon_context(v.get('message', ''))}"
            for i, v in enumerate(actionable)
        ]
        sec = (
            f"[🦎 chameleon: {len(actionable)} "
            f"violation{'s' if len(actionable) != 1 else ''}]\n" + "\n".join(lines)
        )
        if actionable_tone:
            sec += "\n" + actionable_tone
        sections.append(sec)
    if info:
        lines = [
            f"{i + 1}. {sanitize_for_chameleon_context(v.get('message', ''))}"
            for i, v in enumerate(info)
        ]
        sections.append(
            f"[🦎 chameleon: {len(info)} "
            f"advisory note{'s' if len(info) != 1 else ''} "
            "— review, not conformance violations]\n" + "\n".join(lines)
        )
    return "\n\n".join(sections), actionable, info


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
    # A non-string tool_name (malformed payload — e.g. a list or dict) is
    # unhashable and raises TypeError from the set-membership check; guard it the
    # same way the file_path check below does, so the hook fails open silently
    # instead of surfacing a traceback in the error log doctor reads.
    if not isinstance(tool_name, str) or tool_name not in _EDIT_TOOLS:
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
        _raw_bytes = p.read_bytes()
        _capped = _raw_bytes[:100_000]
        content = _capped.decode("utf-8", errors="replace")
        # The cap above hides every export defined past it; carry the fact forward
        # so lint_file skips the removed-export check on this prefix (else a
        # >cap file's tail exports all read as spuriously removed). Derived from
        # the slice itself so the two can never drift on the cap value.
        content_truncated = len(_raw_bytes) > len(_capped)

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
                        rel_path=_observation_rel_path(repo_root, file_path),
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
                    content=content,
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
                    rel_path=_observation_rel_path(repo_root, file_path),
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
                            from chameleon_mcp.lint_engine import detect_language

                            # Only a recognized code language arms the backstop:
                            # a credential-shaped token in markdown / config PROSE
                            # (detect_language None) stays advisory, has no inline
                            # chameleon-ignore escape, and the Stop re-lint drops
                            # it anyway. Arming it would only over-arm, diverging
                            # from the other arming sites and the re-lint, which
                            # all gate non-code via block_eligible_on_file.
                            if detect_language(file_path) is not None:
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
                    "content_truncated": content_truncated,
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
            violations = _lint_file_in_process(
                repo_root, archetype_name, content, file_path, content_truncated=content_truncated
            )

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
                from chameleon_mcp.lint_engine import detect_language
                from chameleon_mcp.violation_class import (
                    block_eligible_on_file,
                    build_ignore_index,
                    hard_class_violations,
                    is_deferred_to_turn_end,
                    is_violation_ignored,
                )

                active = active_block_rules(_enf_profile_dir(repo_root))
                # A non-code file (detect_language None) never hard-blocks on an
                # archetype-independent rule: it can resolve to an archetype via a
                # legacy extension-blind paths_pattern, but it still has no inline
                # chameleon-ignore escape, so eval/secret stay advisory, not armed.
                hard = block_eligible_on_file(
                    hard_class_violations(violations, active),
                    language=detect_language(file_path),
                )
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

            # An edit that raises ONLY info-severity advisories (cross-file
            # blast-radius, the hedged count-bucket signal) has no conformance
            # failure: escalating its level would penalize a purely additive
            # edit and, on the next edit, greet it with a sterner "STOP. Fix
            # these" tone for advisories that explicitly say "not a defect".
            # Treat an info-only turn as clean for the per-file escalation while
            # still surfacing the notes below.
            actionable_violations = [v for v in violations if not _is_info_violation(v)]
            info_only = not actionable_violations
            if enforcement_state is not None and file_state is not None:
                try:
                    if info_only:
                        record_clean(file_state, now=_started)
                    else:
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
                from chameleon_mcp.profile.config import load_config_enforcement_only
                from chameleon_mcp.profile.trust import profile_diverged_from_grant

                enforce_off = os.environ.get("CHAMELEON_ENFORCE") == "0"
                # Isolated enforcement read: an unrelated config-section typo must
                # not raise and silently demote this block to advisory.
                mode = load_config_enforcement_only(_enf_profile_dir(repo_root)).mode
                data = arch_result.get("data") or {}
                match_quality = data.get("match_quality")
                gate_band = data.get("confidence_band")
                gate_ok = (match_quality == "ast") and (gate_band in ("high", "medium"))
                at_l2 = file_state is not None and file_state.level >= 2
                # Trust persists across profile changes by default; only stale under
                # CHAMELEON_TRUST_REVALIDATE=1.
                trusted_not_stale = not profile_diverged_from_grant(
                    _gate_rec, repo_root, _enf_profile_dir(repo_root)
                )
                if (
                    not enforce_off
                    and mode != "off"
                    and blockable_now
                    and gate_ok
                    and at_l2
                    and trusted_not_stale
                ):
                    # would_block is a SHADOW-mode measurement; the enforce-mode
                    # block below is recorded as an edit decision, so emitting a
                    # would_block row here too would inflate the shadow tally that
                    # drives the shadow -> enforce promotion.
                    if mode == "shadow":
                        try:
                            from chameleon_mcp.metrics import emit_hook_metric

                            # One would_block row per blockable rule on this file,
                            # so the shadow report attributes counts to the
                            # specific rule and can sample the file for spot-check.
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
                        # Name the actual failing rule in the override hint, not the
                        # literal "<rule>" placeholder. For a blanket-immune rule
                        # (eval-call, secret-detected-in-content) a bare-token ignore
                        # does NOT clear the block, so the model must be told the real
                        # rule to type. Mirrors the Stop backstop (hint_rule below).
                        _distinct_block_rules = sorted(
                            r for r in {v.get("rule") for v in blockable_now} if r
                        )
                        _hint_rule = _distinct_block_rules[0] if _distinct_block_rules else "<rule>"
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
                            f"Override with {_ignore_hint(file_path, _hint_rule)} "
                            f"on the offending line if this is intentional.",
                            "<chameleon-context>\n"
                            f"[🦎 chameleon: BLOCKED — {safe_rules}]\n"
                            f"{safe_msgs}\n"
                            "</chameleon-context>",
                        )
                        return 0
                    # shadow: would_block already logged; fall through to advisory.
                    decision_outcome = "would-block"
            except Exception as exc:
                _note_if_config_malformed(exc, repo_id, session_id, "posttool_block")

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
                # Split the surfaced rows by severity (shared with the
                # no-archetype advisory so both frame info rows the same way): an
                # actionable violation (warning/error) carries the "Fix these."
                # imperative and the escalation tone; an info advisory is FYI
                # context and must NOT be ordered "fixed".
                block, displayed_actionable, displayed_info = _render_violation_sections(
                    displayed, actionable_tone=current_tone
                )

                # The "repeated violations" nudge is an escalation signal: only
                # append it when an actionable violation drove the surface, never
                # for an info-only FYI turn.
                if (
                    displayed_actionable
                    and enforcement_state is not None
                    and file_state is not None
                ):
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
                if displayed_actionable:
                    _status = (
                        f"{len(displayed_actionable)} "
                        f"violation{'s' if len(displayed_actionable) != 1 else ''}"
                    )
                else:
                    _status = f"{len(displayed_info)} note{'s' if len(displayed_info) != 1 else ''}"
                _update_statusline(_status, repo_root=repo_root)
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


_FINDING_HIGH_CONFIDENCE = 0.7


def _finding_fingerprint(lens: str, rel: str | None, line, message: str | None) -> str:
    """Stable per-(lens, file, locus) dedup key so the same finding across turns
    is one logical ledger row. Uses a message PREFIX (wording is stable enough at
    80 chars; the full message drifts less than the line does under edits)."""
    loc = line if isinstance(line, int) else ""
    key = f"{lens}|{rel or ''}|{loc}|{(message or '')[:80]}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def _finding_severity(f: dict) -> str | None:
    """Normalize a finding's severity across lens shapes: an explicit ``severity``
    string wins; a correctness finding carries only ``confidence`` (0..1), mapped
    to high at/above the confidence floor; a multi-lens finding surfaced by TWO
    lenses independently agreeing reads high; else medium/unknown."""
    sev = f.get("severity")
    if isinstance(sev, str) and sev:
        return sev
    conf = f.get("confidence")
    if isinstance(conf, (int, float)):
        return "high" if conf >= _FINDING_HIGH_CONFIDENCE else "medium"
    lenses = f.get("lenses")
    if isinstance(lenses, list) and len(lenses) >= 2:
        return "high"
    return None


def _finding_message(f: dict) -> str | None:
    """The finding's human message across shapes: correctness uses ``message``,
    the multi-lens synthesis uses ``claim``."""
    m = f.get("message") or f.get("claim")
    return m if isinstance(m, str) else None


def _finding_is_high(severity: str | None) -> bool:
    return isinstance(severity, str) and severity.strip().lower() in ("high", "block", "critical")


# The stop-backstop wrapper SIGKILLs the hook at 55s measured from PROCESS start
# (hooks/stop-backstop). The sync VERIFY stage anchors its remaining-budget math to
# the same clock -- module import time, which for the one-shot hook process is within
# ~1s of exec -- so pre-judge hook work (route risk facts, ledger recheck, idiom gate)
# is counted, not just the judge spawn. In a long-lived process (daemon, MCP server)
# the anchor is stale and the budget reads exhausted: VERIFY then passes findings
# through unverified, which is the safe direction (never a drop, never an overrun).
_PROCESS_START_MONOTONIC = time.monotonic()
_SYNC_STOP_WALL_BUDGET_SECONDS = 55
_SYNC_VERIFY_SAFETY_SECONDS = 12


def _sync_verify_stop_findings(repo_root: Path, findings):
    """Budget-adaptive VERIFY for the synchronous Stop path.

    VERIFY spawns a refuter only when the wall-clock remaining under the 55s wrapper
    (measured from process start) fits a full timeout window -- the fast
    bare-auth-works turns; otherwise it passes findings through unverified, today's
    behavior, no regression. Spawns are retry-free (stop_verify passes retry=False),
    so one slot is bounded by exactly one timeout window and the wrapper cap holds.
    The duplication gate already defers whenever the judge spawned, so VERIFY takes
    the second-spawn slot without competing with it. Fails open to pass-through on
    any error.
    """
    from chameleon_mcp import stop_verify

    n = len(findings or [])
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.judge import _valid_model

        elapsed = time.monotonic() - _PROCESS_START_MONOTONIC
        remaining = _SYNC_STOP_WALL_BUDGET_SECONDS - elapsed - _SYNC_VERIFY_SAFETY_SECONDS
        model = os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
        if not _valid_model(model):
            model = "sonnet"
        timeout = max(15, min(threshold_int("REFUTER_TIMEOUT_SECONDS"), int(max(0, remaining))))
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
            list(findings or []), ["unverified"] * n, 0, 0, n, False, "sync verify error"
        )


def _ledger_persist(repo_id, session_id, repo_root: Path, lens: str, findings) -> None:
    """Persist surfaced findings to the judge_findings ledger (the finding->fix
    loop). ``findings`` is a list of ``{file, line, message, confidence?/severity?}``.
    Records the reviewed file's content digest as the addressed/ignored anchor.
    Gated by CHAMELEON_FINDING_LEDGER, fail-open, off the per-edit hot path."""
    if os.environ.get("CHAMELEON_FINDING_LEDGER") == "0" or not repo_id or not findings:
        return
    try:
        from chameleon_mcp.drift.observations import record_judge_finding

        for f in findings:
            if not isinstance(f, dict):
                continue
            rel = f.get("file")
            rel = rel if isinstance(rel, str) else None
            line = f.get("line")
            anchor = None
            if rel:
                try:
                    anchor = _content_digest_16(
                        (repo_root / rel).read_bytes()[:100_000].decode("utf-8", errors="replace")
                    )
                except OSError:
                    anchor = None
            record_judge_finding(
                repo_id,
                lens=lens,
                fingerprint=_finding_fingerprint(lens, rel, line, _finding_message(f)),
                severity=_finding_severity(f),
                rel_path=rel,
                line=line if isinstance(line, int) else None,
                anchor_digest=anchor,
                ws_root=str(repo_root),
                session_id=session_id,
            )
    except Exception:
        return


def _ledger_recheck_and_resurface(repo_id, session_id, repo_root: Path) -> list[str]:
    """At Stop, BEFORE this turn's findings persist: re-check every open ledger
    finding against the reviewed file's CURRENT digest -- changed or gone since
    review => addressed (mark + drop) -- and re-surface an unaddressed
    high-severity finding ONCE (mark resurfaced, emit one advisory line). A
    finding already resurfaced and still unchanged is left alone (no re-nag).
    Gated, fail-open to []."""
    if os.environ.get("CHAMELEON_FINDING_LEDGER") == "0" or not repo_id:
        return []
    try:
        from chameleon_mcp.drift.observations import mark_judge_finding, open_judge_findings
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        now = int(time.time())
        resurfaced: list[dict] = []
        # Scope to THIS workspace: in a shared-repo_id monorepo several workspaces
        # share one drift.db, and a finding's rel_path is relative to the root
        # that persisted it, so an unscoped re-check would mis-resolve a sibling
        # workspace's findings against this root (file-not-here read as addressed).
        for row in open_judge_findings(repo_id, ws_root=str(repo_root)):
            fid = row.get("id")
            rel = row.get("rel_path")
            anchor = row.get("anchor_digest")
            has_path = isinstance(rel, str)
            current = None
            if has_path:
                try:
                    current = _content_digest_16(
                        (repo_root / rel).read_bytes()[:100_000].decode("utf-8", errors="replace")
                    )
                except OSError:
                    current = None  # file gone since review -> treat as addressed
            # Changed or gone => the cited content moved => addressed (a proxy;
            # aggregate telemetry, never per-row enforcement). The digest proxy
            # only applies to a finding that CITED a file: a file-less finding
            # (rel_path=None -- a whole-diff or lens finding with no anchor) has no
            # content to compare, so it must NOT be auto-addressed here (that
            # silently loses a real high-severity finding); it falls through to the
            # one-shot high-severity resurface below and otherwise stays open.
            if has_path and (current is None or (anchor is not None and current != anchor)):
                mark_judge_finding(repo_id, fid, status="addressed", resolved_at=now)
                continue
            # A file-less finding (no digest proxy) that is NOT high never
            # resurfaces, so leaving it open would clog the recheck window forever.
            # Resolve it here (its pre-fix fate); only a file-less HIGH finding
            # escapes to the one-shot resurface below.
            if not has_path and not _finding_is_high(row.get("severity")):
                mark_judge_finding(repo_id, fid, status="addressed", resolved_at=now)
                continue
            # Unchanged and still 'open' and high-severity => one re-surface.
            if row.get("status") == "open" and _finding_is_high(row.get("severity")):
                mark_judge_finding(repo_id, fid, status="resurfaced")
                resurfaced.append(row)
        if not resurfaced:
            return []
        lines = [
            f"[🦎 chameleon: {len(resurfaced)} unaddressed high-severity finding(s) "
            "from a previous turn's review, surfaced once more]",
            "Advisory; verify each before acting -- they may be wrong, or already handled.",
        ]
        for row in resurfaced[:8]:
            rel = row.get("rel_path")
            loc = sanitize_for_chameleon_context(str(rel)) if rel else "?"
            ln = row.get("line")
            if isinstance(ln, int):
                loc += f":{ln}"
            lines.append(f"- {loc} ({sanitize_for_chameleon_context(str(row.get('lens') or '?'))})")
        return lines
    except Exception:
        return []


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

    from chameleon_mcp.judge import _excerpt_sha_stale
    from chameleon_mcp.stop_verify import _contained_rel, _excerpt_window

    recorded = data.get("digests") if isinstance(data.get("digests"), dict) else {}
    live: list[dict] = []
    for finding in data.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        rel = finding.get("file")
        stale = False
        if isinstance(rel, str) and rel:
            # rel is untrusted model output; contain it BEFORE any filesystem touch
            # (an absolute or ``..`` path must never read an out-of-repo file), and
            # this runs on the UserPromptSubmit hot path so the touch stays cheap.
            safe_rel = _contained_rel(repo_root, rel)
            if safe_rel is None:
                continue  # escapes the repo: not a reviewable in-repo finding
            abs_path = Path(repo_root) / safe_rel
            if not abs_path.is_file():
                continue  # file gone since review: the cited code no longer exists
            excerpt_sha = finding.get("excerpt_sha")
            if isinstance(excerpt_sha, str) and excerpt_sha:
                # G1' excerpt-level precision: ANNOTATE on change, never drop -- the
                # refuter is the only dropper (contract §5). A file edited ELSEWHERE
                # (its cited excerpt unchanged) is recovered clean, which the coarse
                # whole-file digest wrongly dropped.
                stale = _excerpt_sha_stale(
                    excerpt_sha, _excerpt_window(repo_root, safe_rel, finding.get("line"))
                )
            elif safe_rel in recorded:
                # No excerpt pin: keep the conservative whole-file drop, reading only
                # the first 1MB (not the whole cited file) on this hot path.
                try:
                    with open(abs_path, "rb") as fh:
                        raw = fh.read(1_000_000)
                except OSError:
                    continue
                if hashlib.sha256(raw).hexdigest()[:16] != recorded.get(safe_rel):
                    continue
        live.append({**finding, "_stale": stale})
    if not live:
        return None

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    n = len(live)
    lines = [
        f"[🦎 chameleon: independent review of your previous turn flagged "
        f"{n} possible correctness issue{'s' if n != 1 else ''}]",
        "These are advisory; verify each before acting, they may be wrong.",
    ]
    # Grounding banner: when the VERIFY stage ran, show how many findings a second
    # independent reviewer refuted (dropped) vs confirmed, so the surviving list
    # reads as verified rather than raw.
    verify = data.get("verify") if isinstance(data.get("verify"), dict) else None
    if verify and verify.get("ran"):
        refuted = verify.get("refuted") or 0
        confirmed = verify.get("confirmed") or 0
        lines.append(
            f"Independently verified: {refuted} refuted and dropped, "
            f"{confirmed} confirmed. A '[confirmed]' finding survived a second reviewer."
        )
    for finding in live:
        rel = finding.get("file")
        loc = sanitize_for_chameleon_context(str(rel)) if rel else "?"
        line_no = finding.get("line")
        if isinstance(line_no, int):
            loc += f":{line_no}"
        message = finding.get("message")
        verdict = finding.get("verify")
        tag = " [confirmed]" if verdict == "confirmed" else ""
        stale_tag = "  [stale: code changed since review]" if finding.get("_stale") else ""
        entry = f"- {loc}{tag}: {sanitize_for_chameleon_context(str(message or ''))}{stale_tag}"
        fix = finding.get("suggested_fix")
        if isinstance(fix, str) and fix.strip():
            entry += f"  (suggested fix: {sanitize_for_chameleon_context(fix.strip())})"
        cmds = finding.get("evidence_cmds")
        if isinstance(cmds, list) and cmds:
            entry += f"  ({len(cmds)} pinned check{'s' if len(cmds) != 1 else ''})"
        lines.append(entry)
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


def _stop_block_scope(repo_root: Path) -> str:
    """Stable per-workspace key for the anti-loop block budget.

    Both the lint backstop and the idiom-review gate charge and read the block
    count under this key, in BOTH single- and multi-root mode, so a workspace
    stays capped across a mid-session single<->multi cardinality change (the
    scalar and the per-workspace map are otherwise disjoint counters, letting a
    persistent violation exceed the cap after a mode flip).
    """
    try:
        return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    except OSError:
        return hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:12]


def _effective_stop_blocks(state, scope: str) -> int:
    """The workspace's block count so far, reconciling the legacy scalar with the
    per-workspace map.

    New blocks always charge the per-workspace map, but a pre-fix single-root
    session (or an old committed state file) recorded them on the scalar. Taking
    the max means a workspace capped under either representation stays capped,
    with no migration step and no way for a mode flip to re-arm a spent cap.
    """
    try:
        scalar = int(state.stop_hook_blocks or 0)
    except (TypeError, ValueError):
        scalar = 0
    try:
        per_root = int(state.stop_hook_blocks_by_root.get(scope, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        per_root = 0
    return max(scalar, per_root)


def _stop_file_still_blockable(
    repo_root: Path,
    file_path: str,
    loaded=None,
    active=None,
    daemon_state=None,
    out_rules=None,
    level: int = 2,  # LEVEL_L2; a module-level literal so the default binds at import time
) -> bool | None:
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
        try:
            _sb_raw = p.read_bytes()
            _sb_truncated = len(_sb_raw) > 100_000
            content = _sb_raw[:100_000].decode("utf-8", errors="replace")
        except OSError:
            # The file exists but could not be read this turn (a permissions flip,
            # a network-FS hiccup, an editor lock). "Couldn't check" is NOT
            # "resolved": returning False here would let the caller clear the
            # armed flag and permanently disarm the backstop for a violation still
            # on disk. Signal unknown so the caller keeps the file armed and
            # re-checks it next Stop, without blocking this (unverifiable) turn.
            return None

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

        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.violation_class import (
            block_eligible_on_file,
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
            indep = _scan_archetype_independent(
                content, file_path, _load_rules_for_style(repo_root), repo_root=repo_root
            )
            if not indep:
                return False
            if active is None:
                from chameleon_mcp.enforcement_calibration import active_block_rules

                active = active_block_rules(_enf_profile_dir(repo_root))
            # A non-code file (detect_language None) cannot hard-block on an
            # archetype-independent rule: a credential-shaped token in doc/config
            # prose stays advisory but never turn-traps (it has no inline-ignore
            # escape). Matches the posttool arming gate so the two paths agree.
            hard = block_eligible_on_file(
                hard_class_violations(indep, active), language=detect_language(file_path)
            )
            idx = build_ignore_index(content, file_path=file_path)
            if idx is not None:
                hard = [v for v in hard if not is_violation_ignored(v, idx)]
            enforceable = [v for v in hard if is_archetype_independent(v.get("rule"))]
            if isinstance(out_rules, list):
                out_rules.extend(v.get("rule") for v in enforceable if v.get("rule"))
            return bool(enforceable)

        violations = _lint_file_in_process(
            repo_root,
            archetype_name,
            content,
            file_path,
            loaded=loaded,
            content_truncated=_sb_truncated,
        )
        if not violations:
            return False

        if active is None:
            from chameleon_mcp.enforcement_calibration import active_block_rules

            active = active_block_rules(_enf_profile_dir(repo_root))
        # A non-code file (detect_language None) can resolve to an archetype via a
        # legacy extension-blind paths_pattern; it still has no inline
        # chameleon-ignore escape, so archetype-independent rules (eval/secret)
        # stay advisory and never turn-trap here. Mirrors the no-archetype branch.
        hard = block_eligible_on_file(
            hard_class_violations(violations, active), language=detect_language(file_path)
        )
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

# Turn-end idiom self-review (terse rendering) display bounds. Inline module
# constants matching the sibling caps above (_IDIOM_CONTEXT_CHAR_CAP) and the
# nearby-signature / counterexample bounds; they shape one advisory block, not an
# operator-tunable threshold. Terse (already-shown) idioms render as one line
# each, capped by count; the full-text section (idioms not yet shown this
# session) is budgeted by _STOP_IDIOM_FULLTEXT_CHAR_CAP below.
_STOP_IDIOM_MAX_TERSE = 25
_STOP_IDIOM_SUMMARY_MAX_CHARS = 160
# Whole-block char budget for the full-text (not-yet-shown) idiom section. Larger
# than _IDIOM_CONTEXT_CHAR_CAP because it bounds only the EXCEPTIONAL case where an
# in-scope idiom was never surfaced this session (E escalation) -- the common
# already-shown path renders one terse line per idiom and never touches this. Not
# a hot path, so it can afford to show a few whole unseen idioms before pointing to
# idioms.md.
_STOP_IDIOM_FULLTEXT_CHAR_CAP = 3000

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
    ".dup_surfaced.",
    ".intent.",
    ".correctness_judged.",
    # The once-per-session idiom-review marker had no reaper: a null-session turn
    # collapses every marker to ".idiom_reviewed.unknown" and would then skip the
    # idiom review forever. Age it out like the other once-per-session markers.
    ".idiom_reviewed.",
)

# Sink kinds from judge.run_correctness_judge that mean the reviewer produced
# no usable verdict; the touched files stay unmarked so the next Stop can
# retry under the session spawn cap.
_JUDGE_FAILURE_KINDS = frozenset(
    {"spawn_timeout", "spawn_exec_error", "spawn_nonzero_exit", "pipeline_error"}
)


# Informational grounding-event families the judge emits ONCE PER SPAWN to report
# what context was available (caller facts / imported defs / transitive chains).
# These are NOT degradations: a spawn that ran fine but had no calls index still
# emits `judge_defs_skipped_no_index`. They must be recorded as their own check
# events, never folded into the degraded/spawn_failed tally -- doing so fired a
# false turn-end-reviewer-failed health banner for a healthy reviewer AND flipped
# `spawn_failed` True so the duplication gate stopped deferring, firing a second
# reviewer model in the same Stop. The earlier code special-cased only
# `judge_facts_`, so `judge_defs_`/`judge_transitive_` leaked into degraded.
def _judge_grounding_family(kind: str) -> str | None:
    """The grounding-event family prefix ``kind`` belongs to, or None.

    Single source of truth for the family tuple is
    ``judge.JUDGE_GROUNDING_FAMILIES`` (where the events originate), imported
    lazily so hook_helper's module load -- on every hook, including the per-edit
    hot path -- never pulls in judge. This function is reached only on the Stop /
    SessionStart paths, where judge is already loaded, so the two consumers
    (doctor via judge.is_grounding_event, the banner via this) can never drift.
    """
    from chameleon_mcp.judge import JUDGE_GROUNDING_FAMILIES

    for fam in JUDGE_GROUNDING_FAMILIES:
        if kind.startswith(fam):
            return fam
    return None


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
    marker_scope: str | None = None,
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
        profile_dir = _enf_profile_dir(repo_root)
        # safe_prose_text drops a poisoned idioms.md / principles.md: this Stop
        # backstop reads them straight from disk (not via load_profile_dir), so the
        # loader-side scan does not cover it, and trust persists across changes so
        # the staleness gate no longer does either.
        from chameleon_mcp.idiom_coverage import has_idiom_content
        from chameleon_mcp.profile.loader import safe_prose_text

        idioms_text = safe_prose_text(profile_dir / "idioms.md")
        principles_text = safe_prose_text(profile_dir / "principles.md")
        # A scaffold-only idioms.md (the common no-/chameleon-teach case) is no
        # signal: drop it so the judge does not surface "no idioms yet" placeholder
        # prose as team-idiom content, matching the per-edit get_pattern_context path.
        if not has_idiom_content(idioms_text):
            idioms_text = ""
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

        # marker_scope discriminates the once-per-session marker by workspace so
        # a shared-repo_id monorepo (apps/a + apps/b under ONE repo_data) reviews
        # each workspace's distinct idioms.md instead of collapsing them onto the
        # first root's marker. None (single-root) keeps the legacy filename.
        _marker_name = _IDIOM_REVIEWED_FILENAME.format(session=_safe_session_marker(session_id))
        if marker_scope:
            _marker_name = f"{_marker_name}.{marker_scope}"
        marker = repo_data / _marker_name
        if marker.exists():
            _emit_check_event(repo_id, session_id, "idiom_review", "skipped", "marker_exists")
            return None

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        # Resolve the edited files' archetypes: the terse rendering scopes idioms to
        # them (filter), and the legacy full-dump reorders by them. Turn-end Stop
        # path (not the <100ms hot path), so resolving a few is fine; cap edited[:5]
        # and fail open to no archetypes (the renderer then keeps all -- cannot
        # scope -- rather than hiding everything).
        edited_archetypes: list[str] = []
        try:
            from chameleon_mcp.tools import get_pattern_context

            for f in edited[:5]:
                arch = get_pattern_context(file_path=f)["data"]["archetype"]["archetype"]
                if arch:
                    edited_archetypes.append(arch)
        except Exception:
            edited_archetypes = []

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in edited[:5])

        # Terse rendering is default-ON (kill switch CHAMELEON_STOP_IDIOM_TERSE=0
        # restores the legacy full dump of idioms + principles).
        terse = os.environ.get("CHAMELEON_STOP_IDIOM_TERSE", "1") != "0"

        def _capped_block(text: str, source: str) -> str:
            # Hard-slicing at the cap can cut an idiom mid-block -- e.g. inside a
            # counterexample fence, so the model reads an anti-pattern as the
            # recommended form, or mid-sentence so a directive's polarity is lost.
            # Append a marker (mirroring the per-edit path) so a truncated block
            # never reads as the complete, authoritative idiom set.
            s = sanitize_for_chameleon_context(text.strip())
            if len(s) > _IDIOM_CONTEXT_CHAR_CAP:
                return (
                    s[:_IDIOM_CONTEXT_CHAR_CAP].rstrip()
                    + f"\n... ({source} truncated; see {source}.md)"
                )
            return s

        body: list[str] = []
        surfaced_review = False
        if no_test_for_source_edit:
            body.append(
                "No passing test run was recorded this turn while you changed source "
                "files. Run the suite to confirm your changes pass before ending "
                "(skip only if a watch process or CI is already running them)."
            )

        if terse:
            # A + B + C + E. Scope idioms to the edited archetypes (B), summarize the
            # ones the model already saw this session (C), and show full text only
            # for unseen ones (E). Principles were injected at SessionStart (when
            # trusted) and live in the repo, so a one-line pointer replaces the full
            # re-dump (A) -- keyed on the honest per-idiom idioms_shown_names signal
            # (the actual `### ` names a Tier-2 block rendered), not archetypes_seen.
            from chameleon_mcp.tools import _render_stop_idioms

            idioms_rendered = _render_stop_idioms(
                idioms_text,
                edited_archetypes,
                state.idioms_shown_names,
                char_cap=_STOP_IDIOM_FULLTEXT_CHAR_CAP,
                max_terse=_STOP_IDIOM_MAX_TERSE,
                summary_max_chars=_STOP_IDIOM_SUMMARY_MAX_CHARS,
            )
            if idioms_rendered:
                body.append("")
                body.append("Team idioms in scope for what you edited - re-check each:")
                body.append(idioms_rendered)
                surfaced_review = True
            # Principles ride on an idiom review, they do not trigger one on their
            # own: they were injected at SessionStart and are generic, so a turn
            # that touched no idiom-governed file needs no turn-end principle stop
            # (and must not burn the once-per-session marker, so a later governed
            # edit still gets its idiom review). The kill-switch/legacy path below
            # keeps the old idioms-OR-principles trigger.
            if surfaced_review and principles_text.strip():
                body.append("")
                body.append(
                    "Also re-check your edits against the team principles in "
                    "`.chameleon/principles.md`."
                )
        else:
            legacy_idioms = idioms_text
            if edited_archetypes:
                from chameleon_mcp.tools import _reorder_idioms_by_archetypes

                legacy_idioms = _reorder_idioms_by_archetypes(idioms_text, edited_archetypes)
            idioms_block = _capped_block(legacy_idioms, "idioms")
            principles_block = _capped_block(principles_text, "principles")
            if idioms_block:
                body.append("")
                body.append("Team idioms:")
                body.append(idioms_block)
                surfaced_review = True
            if principles_block:
                body.append("")
                body.append("Principles:")
                body.append(principles_block)
                surfaced_review = True

        # Nothing to review: the edited archetypes are not governed by any idiom and
        # there are no principles. Do NOT fire an empty gate, and do NOT burn the
        # once-per-session marker -- a later turn editing a governed file must still
        # get its review. (The test-run nudge alone never fires this idiom gate; it
        # is a strengthener on a real review, not a standalone trigger.)
        if not surfaced_review:
            _emit_check_event(repo_id, session_id, "idiom_review", "skipped", "nothing_in_scope")
            return None

        # Respect the stop cap so an idiom block cannot exceed the budget, reading
        # the SAME reconciled per-workspace counter the lint backstop charges (in
        # both modes) -- otherwise an idiom block could exceed the per-workspace
        # cap the lint path already spent.
        _block_scope = _stop_block_scope(repo_root)
        if _effective_stop_blocks(state, _block_scope) >= cfg.stop_block_cap:
            return None

        # Marker is written only now that a review will actually be surfaced, so a
        # nothing-in-scope turn above does not consume the once-per-session budget.
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
            # signal, never as a per-rule promotion candidate. The review text
            # itself still goes out as a non-blocking advisory: taught idioms
            # are not in the verify lint path, so without this an explicitly
            # shadow config delivers NO turn-end idiom feedback at all.
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
                "Hold this to a high bar (idiom_judge): check each edited file "
                "against the idioms above and fix any clear violation; do not "
                "rubber-stamp it."
            )
        parts.append("")
        parts.append(
            "Ending again confirms the review is done. To skip this check, add "
            f"{_ignore_hint(edited[:5], 'idioms')} in a file you touched."
        )

        # Charge the same reconciled per-workspace counter the cap check read, so
        # an idiom block counts toward the workspace's shared budget in both modes.
        state.stop_hook_blocks_by_root[_block_scope] = (
            _effective_stop_blocks(state, _block_scope) + 1
        )
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
                    route_reason=route.get("reason"),
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
            # A grounding-context outcome rides the same sink but is its own
            # check, not a degradation: one event per spawn attempt per family
            # (facts / imported defs / transitive chains -- included /
            # skipped_no_index / skipped_disabled) so the attestation can tell a
            # grounded review from a blind one. All three families are handled
            # here so none leaks into the degraded/spawn_failed tally.
            fam = _judge_grounding_family(kind)
            if fam is not None:
                _emit_check_event(
                    repo_id,
                    session_id,
                    fam.rstrip("_"),
                    kind[len(fam) :],
                    detail={"turn_key": turn_key},
                )
                return
            if kind in _JUDGE_FAILURE_KINDS:
                failures.append(kind)
            if kind == "spawn_timeout":
                # A timeout is the one failure that consumes the FULL judge
                # budget (45s). A second sequential reviewer spawn in the same
                # Stop would blow the wrapper's 55s wall-clock cap and get the
                # process SIGKILLed mid-review, so the duplication gate must skip
                # its spawn after a judge timeout (it reads this flag).
                route["spawn_timed_out"] = True
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
            _enf_profile_dir(repo_root),
            fresh,
            resolver,
            intent_tokens=intent_tokens,
            event_sink=_sink,
            # Reviewer model ladder: a high-risk / intent-forced route escalates
            # the judge to a stronger model; low-risk routes keep the base.
            model=judge.judge_model_for_route(route.get("reason")),
        )
        route["spawn_failed"] = bool(degraded)

        # Shadow-log every RAW finding before VERIFY so a lead can sample judge
        # precision over time -- the refuted ones are exactly the rows a precision
        # sample needs. The judge never blocks, so would_block is always False.
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

        # VERIFY stage: independently refute each finding before REPORT, within the
        # wall-clock left under the 55s wrapper. Only a refuted finding is dropped;
        # the rest are surfaced labeled. A slow judge leaves no budget ->
        # pass-through (today's behavior). Fails open so a broken refuter never
        # drops a real finding.
        verify = _sync_verify_stop_findings(repo_root, findings)
        findings = verify.kept
        kept_verdicts = verify.kept_verdicts
        if verify.ran:
            _emit_check_event(
                repo_id,
                session_id,
                "correctness_judge",
                "verified",
                f"refuted={verify.refuted} confirmed={verify.confirmed}",
                detail={"turn_key": turn_key},
            )

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

        # Persist to the finding->fix ledger (mirrors the multi-lens gate) so an
        # unaddressed high-severity correctness finding is re-surfaced once next
        # Stop. Correctness Findings carry `confidence`, mapped to severity. Only
        # VERIFY survivors persist: a refuted finding must never nag a later turn.
        _ledger_persist(
            repo_id,
            session_id,
            repo_root,
            "correctness",
            [
                {"file": f.file, "line": f.line, "message": f.message, "confidence": f.confidence}
                for f in findings
            ],
        )

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
        # Grounding banner: when the VERIFY stage ran, report how many findings a
        # second independent reviewer refuted (dropped) vs confirmed.
        if verify.ran:
            lines.append(
                f"Independently verified: {verify.refuted} refuted and dropped, "
                f"{verify.confirmed} confirmed. A '[confirmed]' finding survived a "
                "second reviewer."
            )
        for f, vd in zip(findings, kept_verdicts, strict=False):
            loc = sanitize_for_chameleon_context(f.file) if f.file else "?"
            if f.line is not None:
                loc += f":{f.line}"
            tag = " [confirmed]" if vd == "confirmed" else ""
            lines.append(f"- {loc}{tag}: {sanitize_for_chameleon_context(f.message)}")

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
    persist=None,
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
        # Once per session per (file, rule): the same still-unresolved pairing
        # would otherwise re-render verbatim on every consecutive Stop. The
        # caller persists `state` after the gates run, so the marker survives
        # the turn. Only items actually DISPLAYED are recorded: one that fell
        # past the display cap (surfaced only as "...and N more") has not been
        # shown and may resurface on a later Stop.
        items = [it for it in items if f"{it.source_rel}::{it.rule_id}" not in state.cochange_shown]
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        max_items = threshold_int("COCHANGE_ADVISORY_MAX_ITEMS")
        shown = items[:max_items]
        extra = len(items) - len(shown)
        state.cochange_shown.update(f"{it.source_rel}::{it.rule_id}" for it in shown)
        # The advisory path has no downstream save_state (only the block paths
        # save), so without persisting here the marker dies with this process
        # and the same advisory re-renders on every consecutive Stop. The
        # caller supplies the writer; a persist failure must not cost the
        # advisory itself.
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

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
        # The comment token depends on the flagged file's language: `#` for
        # Ruby/Python, `//` for TS/JS. The Django model->migration rule fires
        # EXCLUSIVELY on Python, where `//` is a syntax error, so a blind `//`
        # hint was actively wrong. Scope the token to the languages actually shown.
        _langs = {detect_language(it.source_rel) for it in shown}
        if _langs and _langs <= {"ruby", "python"}:
            _ign = "`# chameleon-ignore cochange`"
        elif _langs and _langs <= {"typescript", "javascript"}:
            _ign = "`// chameleon-ignore cochange`"
        else:
            _ign = (
                "`# chameleon-ignore cochange` (Ruby/Python) or "
                "`// chameleon-ignore cochange` (TS/JS)"
            )
        lines.append(f"To silence this for a file, add {_ign} in the file you touched.")
        return lines
    except Exception:
        return []


# A single-line Python `from <module> import <names>` (relative or absolute).
# Captures the dotted module spec (with any leading dots) and the import clause,
# so the crossfile advisory can tell a repointed import (the name now sourced
# from a different module) from a genuinely dangling one.
_PY_FROM_IMPORT_RE = re.compile(r"(?m)^[ \t]*from\s+(\.*[A-Za-z0-9_.]*)\s+import\b(.*)$")


def _imported_source_keys(content: str, name: str, importer_dir: Path, lang: str, resolver) -> set:
    """Module keys every current import that BINDS ``name`` resolves to in ``content``.

    Parses the importer's CURRENT import statements (TS ``import { ... } from
    '<spec>'``; Python ``from <module> import ...``), keeps the ones that bind
    the imported name ``name``, and resolves each specifier with ``resolver`` --
    the same per-build resolver the reverse index used, so a key here is directly
    comparable to a reverse-index target key. Default/namespace imports carry no
    named binding (and the reverse index never records them), so they contribute
    nothing. Returns the set of resolved keys (possibly empty); fails open to an
    empty set so a parse miss can never manufacture a suppression.
    """
    keys: set = set()
    try:
        if lang == "python":
            # AST first so a multi-line parenthesized `from x import (\n A,\n)` --
            # the dominant style after black/isort -- is read, not just the first
            # line. The single-line regex below is the fallback for content that
            # does not parse (a partial mid-edit), so a syntax error still gets
            # best-effort coverage rather than zero keys (which would wrongly
            # suppress nothing / fail to suppress a real repoint).
            import ast as _ast

            tree = None
            try:
                tree = _ast.parse(content)
            except (SyntaxError, ValueError):
                tree = None
            if tree is not None:
                for node in _ast.walk(tree):
                    if not isinstance(node, _ast.ImportFrom):
                        continue
                    if not any(a.name == name for a in node.names):
                        continue
                    module_spec = "." * (node.level or 0) + (node.module or "")
                    if not module_spec:
                        continue
                    key = resolver(module_spec, importer_dir)
                    if key:
                        keys.add(key)
            else:
                from chameleon_mcp.phantom_imports import _py_imported_names

                for m in _PY_FROM_IMPORT_RE.finditer(content):
                    module_spec = m.group(1)
                    if not module_spec:
                        continue
                    if name not in _py_imported_names(m.group(2) or ""):
                        continue
                    key = resolver(module_spec, importer_dir)
                    if key:
                        keys.add(key)
        else:
            from chameleon_mcp.phantom_imports import _TS_IMPORT_SPEC_RE, _named_specifiers

            # Blank comments (keeping string specifiers intact) before the scan so a
            # commented-out stale import -- `// import { foo } from './t'` left behind
            # after repointing foo to another module -- cannot re-introduce the old
            # target key and defeat repoint detection. Length-preserving, so match
            # offsets and the named-specifier extraction below are unaffected.
            scan = _blank_strings_comments(content, "typescript", keep_strings=True)
            for m in _TS_IMPORT_SPEC_RE.finditer(scan):
                raw = m.group(1) or m.group(2) or m.group(3)
                if not raw:
                    continue
                spec = raw.split("?", 1)[0].split("#", 1)[0]
                if not spec:
                    continue
                names = _named_specifiers(m.group(0))
                if not names or name not in names:
                    continue
                key = resolver(spec, importer_dir)
                if key:
                    keys.add(key)
    except Exception:
        return set()
    return keys


# Any quoted string literal (both kinds), used only for offset-preserving
# DETECTION passes where every string must disappear regardless of interpolation.
_QUOTED_ANY_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
# Ruby %-literals with a bracket delimiter. The captured letter selects the
# interpolation rule: lowercase w/i/q/s never interpolate; W/I/Q/r/x and the
# bare `%(` do. Non-nesting single-delimiter match -- the common Rails style.
_RUBY_PERCENT_RE = re.compile(
    r"%([wWiIqQrsx]?)\[[^\]]*\]"
    r"|%([wWiIqQrsx]?)\([^)]*\)"
    r"|%([wWiIqQrsx]?)\{[^}]*\}"
    r"|%([wWiIqQrsx]?)<[^>]*>"
)
_RUBY_PERCENT_NONINTERP = frozenset({"w", "i", "q", "s"})
# A heredoc: `<<`, optional `-`/`~`, an optionally-quoted tag, arbitrary opener
# tail, then the body up to a line that is only the tag. Single-quoted tag =
# non-interpolating.
_RUBY_HEREDOC_RE = re.compile(
    r"<<[-~]?(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n.*?^[ \t]*\2\b",
    re.DOTALL | re.MULTILINE,
)


def _blank_keep_newlines(s: str) -> str:
    """Replace every non-newline char with a space (equal length, newlines kept)
    so downstream offset/line math over the blanked copy stays aligned."""
    return re.sub(r"[^\n]", " ", s)


def _blank_ruby_noncode(text: str) -> str:
    """Blank Ruby comments, %-literals, and heredocs that carry a constant name
    only as inert text after a rename, WHILE preserving `#{Const}` interpolation
    (a real reference). Keep-biased: an unparseable form stays counted (a
    false-positive over-fire) rather than dropped (a false-negative that hides a
    real break). Offset/length-preserving throughout.
    """
    try:
        # Heredocs first: a heredoc body can hold `#` chars that would otherwise
        # look like comment starts. Blank a non-interpolating heredoc (single-quoted
        # tag), or an interpolating one with no `#{` in its body.
        def _heredoc_sub(m: re.Match) -> str:
            body = m.group(0)
            quoted = m.group(1) == "'"
            if quoted or "#{" not in body:
                return _blank_keep_newlines(body)
            return body

        s = _RUBY_HEREDOC_RE.sub(_heredoc_sub, text)

        # %-literals: non-interpolating letters always blank; interpolating ones
        # only when they carry no `#{`.
        def _percent_sub(m: re.Match) -> str:
            lit = m.group(0)
            letter = next((g for g in m.groups() if g is not None), "")
            if letter in _RUBY_PERCENT_NONINTERP or "#{" not in lit:
                return _blank_keep_newlines(lit)
            return lit

        s = _RUBY_PERCENT_RE.sub(_percent_sub, s)

        # Comments: detect on a copy with ALL quoted strings blanked, so a `#`
        # inside a string (including a literal `#` before a `#{Const}` in an
        # interpolating string) is never mistaken for a comment start. `#{` is
        # excluded so interpolation openers are never treated as comments. Blank
        # the detected [start, EOL) ranges in `s` (offsets align: string blanking
        # is equal-length).
        detect = _QUOTED_ANY_RE.sub(lambda mm: _blank_keep_newlines(mm.group(0)), s)
        chars = list(s)
        for cm in re.finditer(r"#(?!\{)", detect):
            start = cm.start()
            eol = detect.find("\n", start)
            if eol == -1:
                eol = len(chars)
            for j in range(start, eol):
                chars[j] = " "
        s = "".join(chars)

        # Finally the non-interpolating quoted strings (the round-1 behaviour):
        # a plain "..." / '...' with no `#{` is inert text.
        def _quoted_sub(m: re.Match) -> str:
            lit = m.group(0)
            return _blank_keep_newlines(lit) if "#{" not in lit else lit

        return _QUOTED_ANY_RE.sub(_quoted_sub, s)
    except Exception:
        # Any regex failure falls back to the plain-string-only blanking so the
        # check never crashes; keep-biased means it just over-fires at worst.
        return _QUOTED_ANY_RE.sub(
            lambda m: _blank_keep_newlines(m.group(0)) if "#{" not in m.group(0) else m.group(0),
            text,
        )


# Keywords after which a `/` begins a regex literal, not division (the char-level
# regex-position heuristic misses these because the keyword ends in a letter).
_REGEX_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)


def _blank_strings_comments(text: str, language: str | None, *, keep_strings: bool = False) -> str:
    """Blank string-literal and comment CONTENT to spaces (newlines preserved),
    leaving code intact, via a single left-to-right character scan.

    ``keep_strings`` (default False) blanks ONLY comments, leaving string content
    intact. It still tracks string state (so a `//` inside a string never starts a
    comment), but does not blank the string chars. Used where a downstream scan
    must still see string literals -- e.g. an import specifier `from './x'` -- yet
    must NOT be fooled by a commented-out import line. Default False preserves the
    blank-both behavior every existing caller relies on.

    A scan -- not regex ordering -- is the only sound way to blank both without
    one hiding a real reference: comments are recognized BEFORE strings, so an
    apostrophe inside a comment (`// don't`) never opens a string, and a `//`
    inside a string (`"http://x"`) is inside string state so it never starts a
    comment. TS/JS: `//` line, `/* */` block, and `'`/`"`/`` ` `` strings (a
    template's `${}` interpolation is blanked too -- a real named-import break is
    anchored by the import binding, which is code). Python: `#` line, `'`/`"` and
    triple-quoted strings; `#` is NOT a comment in TS (it is a private-field
    sigil). None (unknown): superset (both comment forms). Offset/length
    preserving; a single/double quote left open at end-of-line is treated as
    unterminated (not a real string) so the rest of the line stays code.
    """
    is_py = language == "python"
    ts_comments = language != "python"  # TS/JS and unknown honor // and /* */
    out = list(text)
    i, n = 0, len(text)
    # Last significant (non-space) CODE char, for TS regex-vs-division disambiguation.
    prev_sig = ""

    while i < n:
        c = text[i]
        # line comment
        if is_py and c == "#":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ts_comments and c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        # block comment (TS/JS)
        if ts_comments and c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n:
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    out[i] = out[i + 1] = " "
                    i += 2
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        # regex literal (TS/JS): a `/` in expression position, but NEVER when it is
        # immediately preceded by `<` -- that is a JSX closing tag `</Tag>` or a
        # `< /re/` comparison, and treating `</` as a regex would blank the closing
        # tag and could hide a real reference between two closing tags (a false
        # negative). Conservative: only blank when the regex closes on the same line
        # (`_scan_regex_literal` non-None); otherwise the `/` is division -> code.
        if ts_comments and c == "/" and prev_sig != "<":
            from chameleon_mcp.phantom_imports import _regex_allowed_at, _scan_regex_literal

            allowed = _regex_allowed_at(prev_sig)
            if not allowed and (prev_sig.isalnum() or prev_sig in "_$"):
                # A `/` right after `return`/`typeof`/etc. is a regex, not division
                # (you cannot divide immediately after these keywords); the char
                # heuristic misses it because the keyword ends in a letter. Grab the
                # trailing identifier and allow the regex when it is such a keyword.
                k = i - 1
                while k >= 0 and text[k].isspace():
                    k -= 1
                end_w = k + 1
                while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
                    k -= 1
                if text[k + 1 : end_w] in _REGEX_KEYWORDS:
                    allowed = True
            if allowed:
                end = _scan_regex_literal(text, i)
                if end is not None:
                    for j in range(i, end):
                        if text[j] != "\n":
                            out[j] = " "
                    i = end
                    prev_sig = "/"  # a regex is a value; a following `/` is division
                    continue
        # triple-quoted string (Python docstring)
        if is_py and c in ("'", '"') and i + 2 < n and text[i + 1] == c and text[i + 2] == c:
            q = c
            if not keep_strings:
                out[i] = out[i + 1] = out[i + 2] = " "
            i += 3
            while i < n:
                if (
                    text[i] == q
                    and i + 1 < n
                    and text[i + 1] == q
                    and i + 2 < n
                    and text[i + 2] == q
                ):
                    if not keep_strings:
                        out[i] = out[i + 1] = out[i + 2] = " "
                    i += 3
                    break
                if text[i] != "\n" and not keep_strings:
                    out[i] = " "
                i += 1
            continue
        # string literal
        if c in ("'", '"') or (ts_comments and c == "`"):
            q = c
            if not keep_strings:
                out[i] = " "
            i += 1
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    if not keep_strings:
                        out[i] = " "
                        if text[i + 1] != "\n":
                            out[i + 1] = " "
                    i += 2
                    continue
                if ch == q:
                    if not keep_strings:
                        out[i] = " "
                    i += 1
                    break
                if ch == "\n" and q != "`":
                    # single/double quotes do not span a raw newline; an unclosed
                    # one was not a real string literal -- stop, leave rest as code.
                    break
                if ch != "\n" and not keep_strings:
                    out[i] = " "
                i += 1
            # A string is a VALUE, so a following `/` is division, not a regex.
            prev_sig = q
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return "".join(out)


# `export * from '<spec>'` -- captures the re-export source spec so a barrel's
# star sources can be expanded (the plain `export * from` regex in
# phantom_imports only detects presence, not the spec).
_TS_EXPORT_STAR_FROM_RE = re.compile(r"\bexport\s*\*\s*from\s*['\"]([^'\"]+)['\"]")


def _barrel_reexports_stem(barrel_content: str, stem: str) -> bool:
    """True if ``barrel_content`` re-exports the sibling module ``stem`` -- either
    ``export * from './stem'`` or ``export { ... } from './stem'`` (with or without
    an extension). Used to decide whether an edited origin file's removed export
    flows through this barrel."""
    esc = re.escape(stem)
    pat = re.compile(
        r"\bexport\s*(?:\*|\{[^}]*\})\s*from\s*['\"]\.[^'\"]*/?" + esc + r"(?:\.[cm]?[jt]sx?)?['\"]"
    )
    return bool(pat.search(barrel_content))


def _barrel_effective_exports(barrel_path: Path, ws_root: Path, resolver):
    """Compute the set of names a TS barrel currently exports, expanding its
    ``export * from`` sources ONE level, or signal it cannot be trusted.

    Returns ``(names, resolvable)``. ``resolvable`` is False -- and the caller
    must then NOT report a break -- when any star source cannot be read or is
    itself a star barrel (a deeper chain we do not expand): the removed name might
    still be provided there, so firing would be a false positive. Fail-safe by
    construction: uncertainty suppresses the finding.
    """
    from chameleon_mcp.phantom_imports import _current_export_names

    try:
        content = barrel_path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
    except OSError:
        return frozenset(), False
    names: set[str] = set()
    # The barrel's own direct exports: strip the `export * from` lines first so
    # _current_export_names sees a closed set (those stars are expanded below).
    without_stars = _TS_EXPORT_STAR_FROM_RE.sub(" ", content)
    direct, direct_open = _current_export_names(without_stars)
    if direct_open:
        return frozenset(), False  # a non-star open shape we can't enumerate
    names |= set(direct)
    for m in _TS_EXPORT_STAR_FROM_RE.finditer(content):
        spec = m.group(1)
        src_key = resolver(spec, barrel_path.parent)
        if not src_key:
            return frozenset(), False  # unresolvable source -> can't confirm
        src_path = ws_root / src_key
        try:
            src_content = src_path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        except OSError:
            return frozenset(), False
        src_names, src_open = _current_export_names(src_content)
        if src_open:
            return frozenset(), False  # nested star barrel -> not expanded, bail
        names |= set(src_names)
    return frozenset(names), True


# JSX children are LITERAL text interleaved with `{expr}` code and nested
# elements: `<div>Foo {bar}<span>x</span></div>` renders text "Foo", evaluates
# `bar`, and nests `<span>`. The literal text is NOT a reference to a variable
# (that would be `{Foo}`); the expressions ARE code. Run ONLY on `_strip_ts_noise`
# output, where strings/comments/regex are already blanked -- so a closing
# `</[A-Za-z>]` is UNFORGEABLE by real code (`</` is not a JS operator and a regex
# `/` is already gone). A COMPLETE element `<tag ...>...</tag>` / `<tag .../>` /
# `<>...</>` therefore only exists in genuine JSX, never in code like `a>Foo<b`
# (which has no `</`). We iteratively collapse INNERMOST complete elements
# (blanking their tags + text, keeping `{expr}` spans), which exposes the parent's
# text for the next pass, then blank the top-level children text. This blanks all
# JSX text -- including text before a nested tag -- while never touching a
# comparison/division/generic (none forms a complete element). `{expr}` contents
# are always preserved, even inside nested elements, so a real reference in an
# expression still fires. Zero false-negative risk.
_JSX_EXPR_RE = re.compile(r"\{[^{}]*\}")
# An innermost complete element: an opening tag, then content with NO nested
# `<`/`>`, then a matching-shaped close; or a self-closing tag; or a fragment.
_JSX_ELEMENT_RE = re.compile(
    r"<[A-Za-z][^<>]*/>"  # self-closing <br/>, <Foo attr={x} />
    r"|<[A-Za-z][^<>]*>[^<>]*</[A-Za-z][^<>]*>"  # <tag ...>text</tag>
    r"|<>[^<>]*</>"  # fragment <>text</>
)
_JSX_CHILDREN_RE = re.compile(r"(?<=>)([^<>]*?)(?=</[A-Za-z>])")


def _blank_jsx_text_run(s: str) -> str:
    """Blank the literal-text runs of ``s`` to spaces, keeping ``{expr}`` spans
    (real code) intact. Length/newline preserving."""
    out: list[str] = []
    pos = 0
    for em in _JSX_EXPR_RE.finditer(s):
        out.append(re.sub(r"[^\n]", " ", s[pos : em.start()]))
        out.append(em.group(0))
        pos = em.end()
    out.append(re.sub(r"[^\n]", " ", s[pos:]))
    return "".join(out)


def _blank_jsx_text(stripped: str) -> str:
    from chameleon_mcp._thresholds import threshold_int

    cur = stripped
    # Collapse innermost complete elements to (blanked tags/text, kept exprs),
    # exposing each parent's text for the next pass. Bounded to the max nesting
    # depth we handle; beyond it the remaining outer text stays a safe over-fire.
    for _ in range(threshold_int("JSX_MAX_NEST_DEPTH")):
        nxt = _JSX_ELEMENT_RE.sub(lambda m: _blank_jsx_text_run(m.group(0)), cur)
        if nxt == cur:
            break
        cur = nxt
    # Any remaining top-level children text (between a `>` and a `</`).
    return _JSX_CHILDREN_RE.sub(lambda m: _blank_jsx_text_run(m.group(0)), cur)


def _reference_present(
    content: str, name: str, line: int | None, language: str | None = None
) -> bool:
    """True if ``name`` appears as a bareword code reference in ``content`` -- not
    only inside a string literal (an import specifier path) or a comment (a stale
    mention left after a rename).

    Checks the recorded ``line`` first (cheap), then the whole file; comments AND
    string literals are blanked before each scan so a name that survives only in
    a module path or a comment is not counted as a live use. ``language`` selects
    the comment token set (TS `//` vs Python `#`); None uses the superset. Shared
    by the TS/Python existence checks -- the Stop advisory (``_live_break``) and
    the tool (``_live_importer_break``) -- so the two cannot drift.

    Safe for these callers ONLY because a genuine named-import break is anchored
    by the import binding (``import {{ name }}``), which is code and survives the
    blanking; a usage that survives only inside a comment or a template literal
    can therefore never hide a real break. It is NOT used for the Ruby constant
    paths, which have no import anchor and whose interpolating ``"#{{...}}"``
    strings carry real references.
    """
    # Exclude a preceding `.` so a member access (`self.name`, `props.name`,
    # `obj.name`) is NOT counted as a use of the imported bareword `name`. Once an
    # importer drops the `import { name }` binding but keeps an unrelated
    # `self.name()` member call, the old needle matched that member access and
    # fired a phantom "no longer exported; still imported by ..." break. A genuine
    # named-import use is always bareword (`name(...)` / `name.foo`), never
    # `.name`, so this only removes false positives; the lookahead is unchanged so
    # `name.foo` (name used, then a member off it) still counts.
    needle = re.compile(r"(?<![A-Za-z0-9_$.])" + re.escape(name) + r"(?![A-Za-z0-9_$])")

    def _blank(text: str) -> str:
        # Blank strings, comments and templates so a name that survives only inside
        # one is not counted as a live reference, via the char-scan tokenizer.
        # NOTE: we deliberately do NOT reuse `_strip_ts_noise` here even though it
        # also handles regex literals -- its regex detector reads the `/` in a JSX
        # closing tag `</span>` as a regex start and pairs it with the next `</`,
        # blanking the region between them. In a JSX file that HIDES a real
        # reference sitting between two closing tags (`<A><B>x</B>{Foo}</A>`) -- a
        # false negative, the worst class. `_blank_strings_comments` leaves `<`,
        # `>`, `/` as code (only `//` and `/* */` are comments), so JSX tags
        # survive; the only cost is that a name lingering ONLY inside a regex
        # literal (`/Foo/`) over-fires -- a rare, safe-direction over-fire, far
        # better than hiding a real break. Python has its own comment/string forms.
        blanked = _blank_strings_comments(text, language)
        if language == "python":
            return blanked
        # Blank literal JSX text children (safe: a complete element ends in the
        # unforgeable `</`, which real code cannot produce), keeping `{expr}` spans,
        # so `<Tag>Name</Tag>` text is not a live reference while `{Name}` is.
        return _blank_jsx_text(blanked)

    # Blank the WHOLE file first, THEN index the recorded line -- never blank a
    # single line in isolation. A token that opens on an earlier line (a
    # multi-line block comment or a template literal) is only resolved by the
    # whole-file scan; blanking one line alone misreads a name surviving inside
    # such a span as a live reference, which reports a false cross-file break
    # (and a false deny on the crossfile deny path). The line short-circuit is a
    # precision fast-path over the already-blanked text, mirroring the Ruby
    # sibling _name_present.
    blanked = _blank(content)
    blanked_lines = blanked.splitlines()
    if line is not None and 1 <= line <= len(blanked_lines):
        if needle.search(blanked_lines[line - 1]):
            return True
    return bool(needle.search(blanked))


def _pending_deletions_path(repo_data: Path, session_id) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker

    return Path(repo_data) / f".crossfile_deleted.{_safe_session_marker(session_id)}.json"


def _load_pending_deletions(repo_data: Path, session_id) -> dict:
    """Session map of {absolute deleted-module path -> already-surfaced bool}.

    A module deleted on a turn whose Stop short-circuits before the advisory
    pipeline (the once-per-session idiom-review block) is pruned from enforcement
    state on that Stop, so the next Stop's crossfile advisory would never see it.
    Persisting the deletion here lets the advisory surface it on a later Stop,
    exactly once. Fails open to {}.
    """
    try:
        raw = _pending_deletions_path(repo_data, session_id).read_text(encoding="utf-8")
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_pending_deletions(repo_data: Path, session_id, d: dict) -> None:
    try:
        p = _pending_deletions_path(repo_data, session_id)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def _record_pending_deletions(repo_data: Path, session_id, paths: list[str]) -> None:
    """Add newly-deleted module paths (surfaced=False) at prune time."""
    if not paths:
        return
    try:
        d = _load_pending_deletions(repo_data, session_id)
        changed = False
        for p in paths:
            if p not in d:
                d[p] = False
                changed = True
        if changed:
            _write_pending_deletions(repo_data, session_id, d)
    except Exception:
        pass


def _consume_pending_deletions(repo_data: Path, session_id) -> list[str]:
    """Return not-yet-surfaced deleted paths still absent on disk; drop entries
    whose file came back (resurrected). Does NOT mark them surfaced -- that
    happens only after the advisory actually renders them, so a render failure
    can't silently swallow the one surfacing."""
    try:
        d = _load_pending_deletions(repo_data, session_id)
        if not d:
            return []
        out: list[str] = []
        changed = False
        for p in list(d.keys()):
            if Path(p).is_file():
                del d[p]
                changed = True
                continue
            if d[p] is not True:
                out.append(p)
        if changed:
            _write_pending_deletions(repo_data, session_id, d)
        return out
    except Exception:
        return []


def _mark_pending_deletions_surfaced(repo_data: Path, session_id, paths: list[str]) -> None:
    if not paths:
        return
    try:
        d = _load_pending_deletions(repo_data, session_id)
        changed = False
        for p in paths:
            if p in d and d[p] is not True:
                d[p] = True
                changed = True
        if changed:
            _write_pending_deletions(repo_data, session_id, d)
    except Exception:
        pass


def _module_exports_at_head(ws_root: Path, target_key: str, lang: str) -> set[str] | None:
    """The named-export set of ``target_key`` at git HEAD, or None if unknowable.

    F3 scope check for the crossfile BLOCK: a break is deny-eligible only when the
    turn INTRODUCED it -- the name was exported at HEAD and is gone now. A name
    already absent at HEAD (a pre-existing broken import surfaced only because the
    module was edited this turn for an unrelated reason) must NOT block. Returns
    None -- read as "cannot confirm, do not block" -- when git is unavailable, the
    blob is not in HEAD (a file created this session), or the export set is open
    (`export *`, an unexpandable star). TS/Python only; the reverse index records
    import intent, not export reality, so only HEAD tells us the name was real.
    """
    try:
        from chameleon_mcp.judge import _run_git
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )
        from chameleon_mcp.production_ref import git_toplevel

        top = git_toplevel(ws_root)
        if top is None:
            return None
        try:
            git_rel = (ws_root / target_key).resolve().relative_to(top).as_posix()
        except (ValueError, OSError):
            return None
        res = _run_git(["show", f"HEAD:{git_rel}"], cwd=ws_root)
        if res is None or res.returncode != 0:
            return None
        content = res.stdout or ""
        if lang == "python":
            # Absolute path so the __init__.py sibling-listing resolves against the
            # module's real directory, not the process cwd (the content is HEAD's,
            # but the dirname must still be the package dir).
            names, open_set = _python_current_export_names(content, ws_root / target_key)
        elif lang == "typescript":
            names, open_set = _current_export_names(content)
        else:
            return None
        if open_set:
            return None
        return set(names)
    except Exception:
        return None


def _importer_confirms_crossfile_break(
    ws_root: Path, importer_rel: str, name: str, line, target_key: str, lang: str
) -> bool:
    """STRICT per-importer block confirmation (F2): the importer still references
    ``name`` AND that reference still POSITIVELY resolves to ``target_key``.

    Stricter than the advisory's ``_live_break``, which keep-biases to "broken"
    when the import specifier does not resolve (empty keys). For a DENY the
    keep-bias is an over-block vector: a same-turn repoint of ``name`` to a
    bare-package / out-of-repo module yields empty keys, and blocking on that is a
    false positive. So the block requires keys NON-EMPTY and containing the target;
    anything less is "cannot confirm still-sourced-from-target" -> advisory only.
    """
    try:
        from chameleon_mcp.symbol_index import make_module_resolver

        ip = ws_root / importer_rel
        text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        if not _reference_present(text, name, line, lang):
            return False
        try:
            resolver = make_module_resolver(Path(ws_root).resolve(), lang)
        except Exception:
            return False
        keys = _imported_source_keys(text, name, ip.parent, lang, resolver)
        return bool(keys) and target_key in keys
    except Exception:
        return False


def _target_still_provides(ws_root: Path, target_key: str, name: str, lang: str) -> bool:
    """True if the target module's CURRENT content still provides ``name``.

    Defense-in-depth so the deny is self-contained: the advisory only emits a break
    for a currently-removed name, but the block predicate must not TRUST that
    invariant. A name the target still provides -- re-added this turn, re-exported
    (`export { name } from './impl'`), behind an open `export *`, or converted from
    an ES export to a CommonJS one (`module.exports` / `exports.name`, which the
    ES-only export scan reads as "removed") -- must never reach a hard block. Fails
    open to False (cannot confirm it provides) so a genuine break is not suppressed
    by a read error; F2/F3 then decide.
    """
    try:
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )

        abs_target = Path(ws_root) / target_key
        content = abs_target.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        if lang == "python":
            names, open_set = _python_current_export_names(content, abs_target)
        else:
            names, open_set = _current_export_names(content)
        if open_set or name in names:
            return True
        if lang == "typescript" and (
            re.search(r"\bmodule\.exports\b", content)
            or re.search(r"(?<![A-Za-z0-9_$])exports\." + re.escape(name) + r"\b", content)
        ):
            return True
    except Exception:
        return False
    return False


def _confirmed_crossfile_break_sites(rec: dict) -> list[tuple[str, int | None]]:
    """Deny-eligible importer sites for one structured break, or [] if none.

    Applies F3 (turn-introduced: name exported by the module at HEAD) then F2 (each
    importer strictly re-confirmed to still source ``name`` from the target). Block
    scope for v1: TS/Python ``export`` (a named export removed from an existing
    module) only. ``deleted`` is advisory-only for now -- a gone target makes the
    importer specifier unresolvable, so the strict F2 sourcing check (which
    separates "still points at the target" from "repointed to a bare package")
    cannot run without raw-specifier comparison; keep-biasing instead would
    reintroduce the very over-block F2 exists to stop. Ruby ``constant`` (global
    resolution -- cannot cheaply prove no other file defines it at Stop) and
    ``barrel`` (star-expansion at HEAD too costly) are advisory-only too.
    Under-block is the safe direction; a deny must be FP-free.
    """
    kind = rec.get("kind")
    lang = rec.get("lang")
    if kind != "export" or lang not in ("typescript", "python"):
        return []
    ws_root = rec.get("ws_root")
    name = rec.get("name")
    target_key = rec.get("target_key")
    if not (ws_root and isinstance(name, str) and isinstance(target_key, str)):
        return []
    head_exports = _module_exports_at_head(ws_root, target_key, lang)
    if head_exports is None or name not in head_exports:
        return []  # cannot confirm the removal was introduced this turn -> advisory only
    if _target_still_provides(ws_root, target_key, name, lang):
        return []  # target still provides it (re-added / re-exported / CJS) -> not a break
    confirmed: list[tuple[str, int | None]] = []
    for imp_rel, line in rec.get("importers") or []:
        if _importer_confirms_crossfile_break(ws_root, imp_rel, name, line, target_key, lang):
            confirmed.append((imp_rel, line))
    return confirmed


def _resolve_coordinator_cross_index(ws_root: Path):
    """(mono_root, (ReverseIndex, packages)) for the coordinator of ``ws_root``, or
    (None, None). Reads the workspace profile's ``workspace.parent.repo_id`` (the
    coordinator repo_id, written at bootstrap) and loads the coordinator cross
    index from the PLUGIN DATA DIR. ``mono_root`` is ws_root with its mono-relative
    workspace path stripped, so the consumer can join mono-relative index paths.
    Fail-open to (None, None)."""
    try:
        from chameleon_mcp.symbol_index import (
            CROSS_REVERSE_INDEX_FILENAME,
            load_cross_reverse_index,
        )

        data = json.loads((ws_root / ".chameleon" / "profile.json").read_text(encoding="utf-8"))
        parent = (data.get("workspace") or {}).get("parent") or {}
        coord_id = parent.get("repo_id")
        ws_mono_rel = parent.get("workspace_path")
        if not (isinstance(coord_id, str) and coord_id and isinstance(ws_mono_rel, str)):
            return (None, None)
        mono_root = ws_root
        for _ in Path(ws_mono_rel).parts:
            mono_root = mono_root.parent
        res = load_cross_reverse_index(_plugin_data_dir() / coord_id / CROSS_REVERSE_INDEX_FILENAME)
        return (mono_root, res)
    except Exception:
        return (None, None)


def _owning_package(mono_key: str, packages: dict) -> str | None:
    """The package NAME whose monorepo-relative dir owns ``mono_key`` (longest
    prefix wins), or None. ``packages`` is the cross index's name -> mono-dir map."""
    best: str | None = None
    best_len = -1
    for pname, pdir in (packages or {}).items():
        if not isinstance(pname, str) or not isinstance(pdir, str):
            continue
        d = pdir.rstrip("/")
        if (mono_key == d or mono_key.startswith(d + "/")) and len(d) > best_len:
            best, best_len = pname, len(d)
    return best


def _importer_cleanly_repointed(itext: str, name: str, owning_pkg: str, packages: dict) -> bool:
    """True (=> SUPPRESS the cross-workspace break) ONLY when the importer now
    imports ``name`` from a DIFFERENT KNOWN workspace package and no longer from
    ``owning_pkg`` (the package the removed export lived in).

    The cross-package analog of :func:`_live_break`'s repoint suppression, but
    fail-SAFE toward keeping the advisory: suppression requires a POSITIVE match
    to another package in the cross index's ``packages`` map. Anything ambiguous
    -- a relative or tsconfig-alias specifier that may still resolve INTO
    ``owning_pkg``, an external/unmapped bare package, a re-export, an import
    still from ``owning_pkg``, or an unparseable form -- returns False so the
    advisory is KEPT. A name-prefix-only check would wrongly suppress a still-
    broken relative import like ``../a/src/foo`` (it doesn't start with the
    package name yet targets the package), i.e. MISS a genuine break; that is the
    one direction this must never take. TypeScript/JS only (the v1 consumer scope).
    """
    try:
        from chameleon_mcp.phantom_imports import _TS_IMPORT_SPEC_RE, _named_specifiers

        known_others = {p for p in (packages or {}) if isinstance(p, str) and p and p != owning_pkg}
        scan = _blank_strings_comments(itext, "typescript", keep_strings=True)
        from_owning = False
        from_other_known = False
        for m in _TS_IMPORT_SPEC_RE.finditer(scan):
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            spec = raw.split("?", 1)[0].split("#", 1)[0]
            if not spec:
                continue
            names = _named_specifiers(m.group(0))
            if not names or name not in names:
                continue
            if spec == owning_pkg or spec.startswith(owning_pkg + "/"):
                from_owning = True
            elif any(spec == k or spec.startswith(k + "/") for k in known_others):
                from_other_known = True
            # A relative / alias / external-bare spec is left AMBIGUOUS on purpose
            # (it may still target owning_pkg), so it never drives suppression.
        return from_other_known and not from_owning
    except Exception:
        return False


def _crossworkspace_existence_advisory_lines(*, repo_root: Path, state, cfg) -> list[str]:
    """Turn-end advisory for cross-WORKSPACE existence breaks (WP-C5): a monorepo
    workspace file that removed a named export a SIBLING workspace still imports.

    The counterpart to :func:`_crossfile_existence_advisory_lines` (which is
    scoped to one workspace's own reverse index): this consults the coordinator
    cross index in the plugin data dir, resolved per edited file via its workspace
    profile's parent repo_id. Advisory ONLY, never a block; a repoint or a stale
    row is tolerable noise. Each break is confirmed by a live presence re-check on
    the importer's CURRENT content (importer paths are monorepo-root-relative, so
    they join against the coordinator root, not a workspace root). Fail-open to [];
    gated by CHAMELEON_CROSSWS_INDEX and cfg.mode != off.
    """
    try:
        if cfg.mode == "off" or os.environ.get("CHAMELEON_CROSSWS_INDEX") == "0":
            return []
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.phantom_imports import _current_export_names
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        def _s(v: str) -> str:
            return sanitize_for_chameleon_context(re.sub(r"[\x00-\x1f\x7f]", "", v))

        max_files = threshold_int("CROSSFILE_STOP_ADVISORY_MAX_FILES")
        max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")
        coord_cache: dict[str, tuple] = {}
        # (symbol, edited-module mono-key, [importer sites])
        # (symbol, edited-module mono-key, [importer sites], truncated?)
        breaks: list[tuple[str, str, list[str], bool]] = []
        seen = 0
        for path in state.files:
            if seen >= max_files:
                break
            p = Path(path)
            if not p.is_file():
                continue
            lang = detect_language(str(p))
            # v1 resolves cross-package specifiers for TypeScript/JS only (the
            # coordinator JOIN probes JS extensions + package.json main); Python
            # cross-package resolution is a documented follow-up, so no Python
            # cross-workspace edge is ever produced and checking a .py edit here
            # would only ever no-op. Scope to TS so the code matches the pipeline.
            if lang != "typescript":
                continue
            ws_root = find_repo_root(p)
            if ws_root is None:
                continue
            key = str(ws_root)
            if key not in coord_cache:
                coord_cache[key] = _resolve_coordinator_cross_index(ws_root)
            mono_root, res = coord_cache[key]
            if mono_root is None or res is None:
                continue
            ri, _packages = res
            try:
                mono_key = p.resolve().relative_to(mono_root).as_posix()
            except (ValueError, OSError):
                continue
            try:
                content = p.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            current, open_set = _current_export_names(content)
            if open_set:
                continue
            seen += 1
            broken = ri.broken_importers(mono_key, current)
            if not broken:
                continue
            # The package that owns the edited/removed-from file, so a same-turn
            # repoint away from it can be suppressed (parity with _live_break).
            owning_pkg = _owning_package(mono_key, _packages)
            for name in sorted(broken):
                live = []
                for imp in broken[name]:
                    ip = mono_root / imp.path
                    try:
                        itext = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    if not _reference_present(itext, name, imp.line, detect_language(str(ip))):
                        continue
                    # Repoint suppression: if the importer cleanly repointed the
                    # name to a DIFFERENT known workspace package, this
                    # cross-workspace break is stale (the removal no longer affects
                    # it). Ambiguous specifiers keep the advisory (never miss).
                    if owning_pkg is not None and _importer_cleanly_repointed(
                        itext, name, owning_pkg, _packages
                    ):
                        continue
                    live.append(imp)
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda i: (i.path, i.line if i.line is not None else -1)
                )
                sites = [
                    _s(f"{i.path}:{i.line}" if i.line is not None else i.path)
                    for i in live_sorted[:max_sites]
                ]
                breaks.append((_s(name), _s(mono_key), sites, len(live_sorted) > max_sites))

        if not breaks:
            return []
        plural = len(breaks) != 1
        lines = [
            f"[🦎 chameleon: {len(breaks)} export{'s' if plural else ''} you removed "
            f"{'are' if plural else 'is'} still imported by ANOTHER workspace]",
            "These names are gone from the workspace file you edited but a sibling "
            "workspace still imports them across the package boundary; their call "
            "sites are now broken. Restore the export or update the importers. "
            "This is advisory, not a block.",
        ]
        for name, module, sites, truncated in breaks:
            shown = ", ".join(sites)
            more = " ..." if truncated else ""
            lines.append(
                f"- '{name}' no longer exported by {module}; still imported by {shown}{more}"
            )
        return lines
    except Exception:
        return []


def _crossfile_existence_advisory_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    deleted_paths: list[str] | None = None,
    out_breaks: list | None = None,
    for_block: bool = False,
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
        # for_block: the deny caller collects out_breaks under its own feature flag,
        # so it bypasses the advisory nudge flag here (mode==off still short-circuits
        # both -- an enforcement-off repo runs neither the advisory nor the block).
        if cfg.mode == "off" or (not for_block and not cfg.crossfile_existence_advisory):
            return []

        from chameleon_mcp.constant_index import load_constant_index
        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.symbol_index import (
            load_reverse_index,
            make_module_resolver,
            module_key_for_path,
        )
        from chameleon_mcp.violation_class import ignored_rules

        # Per-edited-file WORKSPACE resolution. In a monorepo the cwd's repo_root
        # is the git top-level, whose reverse/constant index covers only the root
        # workspace; each edited file's OWN workspace (nearest ancestor `.chameleon`)
        # owns the index that records its importers -- the same per-file resolution
        # posttool-verify uses (find_repo_root(file)). Resolving one index for
        # repo_root left every non-root workspace's breaks silent at Stop. Cache the
        # workspace root + its indexes so repeated files in one workspace load once.
        _ws_root_cache: dict[str, Path] = {}
        _ws_index_cache: dict[str, object] = {}
        _ws_cidx_cache: dict[str, object] = {}

        def _ws_root_for(abs_path: Path) -> Path:
            key = str(abs_path)
            r = _ws_root_cache.get(key)
            if r is None:
                try:
                    r = find_repo_root(Path(abs_path)) or repo_root
                except Exception:
                    r = repo_root
                _ws_root_cache[key] = r
            return r

        def _ws_index_for(ws_root: Path):
            key = str(ws_root)
            if key not in _ws_index_cache:
                try:
                    _ws_index_cache[key] = load_reverse_index(ws_root)
                except Exception:
                    _ws_index_cache[key] = None
            return _ws_index_cache[key]

        def _ws_cidx_for(ws_root: Path):
            key = str(ws_root)
            if key not in _ws_cidx_cache:
                try:
                    _ws_cidx_cache[key] = load_constant_index(ws_root)
                except Exception:
                    _ws_cidx_cache[key] = None
            return _ws_cidx_cache[key]

        # Cheap early-out: if the cwd workspace AND no child workspace can hold an
        # index (neither reverse nor constant at repo_root, and repo_root has no
        # nested .chameleon), there is nothing to check. A monorepo root with child
        # workspaces still proceeds -- the per-file resolution finds their indexes.
        _has_child_ws = False
        try:
            from chameleon_mcp.tools import _iter_workspace_chameleon_dirs

            _has_child_ws = next(_iter_workspace_chameleon_dirs(Path(repo_root)), None) is not None
        except Exception:
            _has_child_ws = False
        if (
            load_reverse_index(repo_root) is None
            and load_constant_index(repo_root) is None
            and not _has_child_ws
        ):
            return []

        from chameleon_mcp._thresholds import threshold_int

        max_files = threshold_int("CROSSFILE_STOP_ADVISORY_MAX_FILES")
        max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")

        # One specifier resolver per (language, workspace root), built lazily: it
        # joins a relative/aliased import onto the importer's dir exactly the way
        # that workspace's reverse index did at build, so a key it returns is
        # comparable to that workspace's target keys. Used to drop a REPOINTED
        # import (the name now sourced from a different module) from the finding.
        _resolver_cache: dict[str, object] = {}

        def _resolver_for(language: str, ws_root: Path):
            key = f"{language}\x00{ws_root}"
            r = _resolver_cache.get(key)
            if r is None:
                try:
                    resolved = Path(ws_root).resolve()
                except OSError:
                    resolved = Path(ws_root)
                r = make_module_resolver(resolved, language)
                _resolver_cache[key] = r
            return r

        def _live_break(
            importer_rel: str,
            name: str,
            line: int | None,
            target_key: str,
            language: str,
            ws_root: Path,
        ) -> bool:
            # One read of the importer answers both questions a real break needs:
            # (1) does it still reference ``name`` (word-boundary bareword), and
            # (2) is that reference still sourced from the TARGET module. A
            # move-and-reimport refactor leaves the bareword present but repoints
            # the import to a NEW module; the reverse index is a bootstrap snapshot
            # that still attributes the import to the OLD module, so the bareword
            # check alone would fire a phantom "you broke a call site" finding every
            # turn until the next refresh -- the recurring "stale index" complaint.
            # Suppress only when ``name`` IS imported but NONE of those imports
            # resolve to the target; an unresolved/absent binding falls back to the
            # bareword result so a parse miss never hides a genuine break. The
            # importer path is relative to the file's OWN workspace root, so join
            # to ws_root (the monorepo root would miss a nested-workspace importer).
            ip = ws_root / importer_rel
            try:
                text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                return False
            # (1) still references ``name`` as CODE (not only inside the import path
            # string OR a stale comment), and (2) the reference is still sourced
            # from the TARGET module.
            if not _reference_present(text, name, line, language):
                return False
            keys = _imported_source_keys(
                text, name, ip.parent, language, _resolver_for(language, ws_root)
            )
            if keys and target_key not in keys:
                return False  # repointed to a different module -> not broken
            return True

        def _name_present(importer_rel: str, name: str, line: int | None, ws_root: Path) -> bool:
            # Cheap presence check: the index is a bootstrap snapshot, so confirm
            # the importer still names the binding (the rename may have reached it
            # too) before claiming its call site is broken. No parse -- a
            # word-boundary scan over the importer bytes. A Ruby constant has no
            # import binding to anchor a code-only check, so a bareword surviving
            # only as INERT TEXT (a `# comment`, a plain "..."/'...' string, a
            # %w/%i/%q/%Q/%() literal, or a heredoc body) after a completed rename
            # is NOT a live reference and must not fire the advisory. `_blank_ruby_
            # noncode` neutralizes all of those while KEEPING `#{Const}`
            # interpolation (a real reference). Keep-biased: an unparseable form
            # stays counted (a harmless over-fire) rather than dropped. The
            # importer path is relative to the file's own workspace root.
            ip = ws_root / importer_rel
            try:
                text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                return False
            needle = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")

            blanked = _blank_ruby_noncode(text)
            text_lines = blanked.splitlines()
            if line is not None and 1 <= line <= len(text_lines):
                if needle.search(text_lines[line - 1]):
                    return True
            return bool(needle.search(blanked))

        # Ruby move-in-one-turn suppression: the class/module names each edited
        # Ruby file currently defines, computed once and memoized. A class moved to
        # another file edited the SAME turn is not a broken reference -- Ruby
        # constants resolve globally (Zeitwerk/autoload/require), so a still-loaded
        # redefinition elsewhere keeps every referencer valid. This is the Ruby
        # analog of the TS/Python repoint suppression; it covers the dominant
        # same-turn refactor (a cross-turn move would need a full-repo scan, too
        # costly at Stop, and falls back to the bareword behavior).
        _ruby_defs_cache: dict | None = None

        def _ruby_defs_by_file() -> dict:
            nonlocal _ruby_defs_cache
            if _ruby_defs_cache is None:
                _ruby_defs_cache = {}
                for sp in state.files:
                    spp = Path(sp)
                    if not spp.is_file() or detect_language(str(spp)) != "ruby":
                        continue
                    try:
                        txt = spp.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    try:
                        srel = spp.resolve().relative_to(Path(repo_root).resolve()).as_posix()
                    except (ValueError, OSError):
                        continue
                    _ruby_defs_cache[srel] = set(
                        re.findall(r"(?m)^[ \t]*(?:class|module)\s+([A-Z]\w*)", txt)
                    )
            return _ruby_defs_cache

        def _const_redefined_elsewhere(const: str, current_rrel: str) -> bool:
            return any(
                const in names for rel, names in _ruby_defs_by_file().items() if rel != current_rrel
            )

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        def _safe_ref_field(s: str) -> str:
            # A symbol/module/site field rendered on a SINGLE advisory line. Strip
            # line-splitting and other control bytes FIRST (a crafted filename with
            # a newline would otherwise break the one-line format or forge a marker
            # on its own line), THEN run the standard context sanitizer (neutralize
            # forged [chameleon-untrusted-data:]/[chameleon:] markers and residual
            # escapes). sanitize_for_chameleon_context alone preserves newlines (it
            # is built for multi-line prose), so a path-typed field needs both.
            return sanitize_for_chameleon_context(re.sub(r"[\x00-\x1f\x7f]", "", s))

        # (symbol, module, sites, kind) where kind is "export" (TS/Python named
        # export) or "constant" (Ruby class/module), in touch order so the
        # advisory lists the breaks this turn introduced.
        breaks: list[tuple[str, str, list[str], str]] = []
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
            lang = detect_language(str(p))
            # This file's own workspace root (nearest .chameleon), so the indexes,
            # module keys, and importer paths below are workspace-correct in a
            # monorepo instead of all keyed on the cwd's git-top-level root.
            ws_root = _ws_root_for(p)
            if lang == "ruby":
                # Ruby uses the constant graph, not a named-export reverse index:
                # a class/module the index records as defined here that the file
                # no longer defines, while referencers still name it, is the Ruby
                # existence break. High-confidence only: one defining file, bare
                # top-level name.
                from chameleon_mcp.constant_index import referencing_files

                cidx = _ws_cidx_for(ws_root)
                if cidx is None:
                    continue
                ign = ignored_rules(content, file_path=path) or set()
                if "" in ign or "removed-export-breaks-importers" in ign:
                    continue
                try:
                    rrel = p.resolve().relative_to(Path(ws_root).resolve()).as_posix()
                except (ValueError, OSError):
                    continue
                seen_files += 1
                for const, entry in sorted((cidx.get("constants") or {}).items()):
                    dl = entry.get("defined_in") or []
                    if "::" in const or dl != [rrel] or not entry.get("referenced_by"):
                        continue
                    if re.search(r"(?m)^(?:class|module)\s+" + re.escape(const) + r"\b", content):
                        continue
                    # The class was removed from THIS file but redefined in another
                    # file edited the same turn -> a move, not a break. Ruby resolves
                    # the constant globally, so every referencer is still valid.
                    if _const_redefined_elsewhere(const, rrel):
                        continue
                    rlive = [
                        r
                        for r in referencing_files(cidx, const)
                        if _name_present(r, const, None, ws_root)
                    ]
                    if not rlive:
                        continue
                    rsites = [_safe_ref_field(r) for r in sorted(rlive)[:max_sites]]
                    breaks.append(
                        (_safe_ref_field(const), _safe_ref_field(rrel), rsites, "constant")
                    )
                    if out_breaks is not None:
                        # Raw (unsanitized) fields the Stop block branch needs to
                        # re-verify with its stricter predicate + HEAD check.
                        out_breaks.append(
                            {
                                "name": const,
                                "target_key": rrel,
                                "kind": "constant",
                                "lang": "ruby",
                                "ws_root": ws_root,
                                "importers": [(r, None) for r in sorted(rlive)],
                            }
                        )
                continue
            # The reverse index spans the TS and Python module graphs, so both
            # languages are checked here; anything else is skipped. A Ruby-only
            # workspace has no reverse index, so a stray TS/Python file is skipped.
            ws_index = _ws_index_for(ws_root)
            if lang not in ("typescript", "python") or ws_index is None:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "removed-export-breaks-importers" in ign:
                continue
            target_key = module_key_for_path(p, ws_root)
            if target_key is None:
                continue
            # Read each module's live export set with its own language reader: the
            # TS regex finds zero exports in a Python module, which would misreport
            # every Python importer as a broken reference. Pass the path so the
            # Python reader can add an __init__.py package's sibling re-exports.
            if lang == "python":
                current, open_set = _python_current_export_names(content, p)
            else:
                current, open_set = _current_export_names(content)
            if open_set:
                # `export * from` re-exports an unknown set, so a name absent from
                # the visible set may still be exported -- skip, matching the
                # edit-time and tool stance.
                continue
            seen_files += 1
            broken = ws_index.broken_importers(target_key, current)
            if not broken:
                continue
            for name in sorted(broken):
                importers = broken[name]
                live = [
                    imp
                    for imp in importers
                    if _live_break(imp.path, name, imp.line, target_key, lang, ws_root)
                ]
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
                )
                sites = [
                    _safe_ref_field(f"{imp.path}:{imp.line}" if imp.line is not None else imp.path)
                    for imp in live_sorted[:max_sites]
                ]
                breaks.append((_safe_ref_field(name), _safe_ref_field(target_key), sites, "export"))
                if out_breaks is not None:
                    out_breaks.append(
                        {
                            "name": name,
                            "target_key": target_key,
                            "kind": "export",
                            "lang": lang,
                            "ws_root": ws_root,
                            "importers": [(imp.path, imp.line) for imp in live_sorted],
                        }
                    )

        # Deleted TS/Python modules: a file the turn edited then removed exports
        # nothing now, so every importer the reverse index still attributes to it
        # is a genuine break -- the strongest existence break there is, and the one
        # shape the loop above never sees (the file is gone from state.files). A
        # deleted module has a CLOSED empty export set, so broken_importers(key,
        # {}) returns every still-referencing importer, and the per-site _live_break
        # re-check keeps only importers that still name it and still resolve here (a
        # same-turn move that redefined the module elsewhere and repointed the
        # importer drops out). No inline-ignore is possible (the source is gone), so
        # this is advisory-only, like the tool-level path (_module_file_missing).
        for path in deleted_paths or []:
            if seen_files >= max_files:
                break
            if detect_language(str(Path(path))) not in ("typescript", "python"):
                continue
            # A deleted file still resolves its workspace: find_repo_root walks up
            # from the (still-present) parent dir to the nearest .chameleon.
            del_ws_root = _ws_root_for(Path(path))
            del_index = _ws_index_for(del_ws_root)
            if del_index is None:
                continue
            target_key = module_key_for_path(Path(path), del_ws_root)
            if target_key is None:
                continue
            # Only a genuinely-gone file counts; a path that merely failed a
            # read for another reason must not fabricate a deleted-module break.
            try:
                if (del_ws_root / target_key).exists():
                    continue
            except OSError:
                continue
            seen_files += 1
            broken = del_index.broken_importers(target_key, frozenset())
            if not broken:
                continue
            for name in sorted(broken):
                importers = broken[name]
                lang = detect_language(str(Path(path)))
                live = [
                    imp
                    for imp in importers
                    if _live_break(imp.path, name, imp.line, target_key, lang, del_ws_root)
                ]
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
                )
                sites = [
                    _safe_ref_field(f"{imp.path}:{imp.line}" if imp.line is not None else imp.path)
                    for imp in live_sorted[:max_sites]
                ]
                # target_key (the deleted module PATH) is the ONLY break field
                # rendered from the module itself -- and it is attacker-influenced
                # (a crafted deleted filename). Sanitize it like name/sites so a
                # forged marker / ANSI / newline in the path cannot reach the
                # advisory verb below.
                breaks.append(
                    (_safe_ref_field(name), _safe_ref_field(target_key), sites, "deleted")
                )
                if out_breaks is not None:
                    out_breaks.append(
                        {
                            "name": name,
                            "target_key": target_key,
                            "kind": "deleted",
                            "lang": lang,
                            "ws_root": del_ws_root,
                            "importers": [(imp.path, imp.line) for imp in live_sorted],
                        }
                    )

        # Barrel (star re-export) breaks: an edited TS ORIGIN file whose exports
        # flow to importers through a sibling `index.*` barrel (`export * from
        # './origin'`). A name removed from the origin leaves the barrel's
        # importers broken, but the origin's OWN module key has no reverse-index
        # entry (importers import from the barrel, not the origin), so the loop
        # above never sees it. For each edited origin, find sibling barrels that
        # re-export it, recompute the barrel's effective export set (expanding its
        # stars ONE level), and report the barrel's broken importers. Fail-safe:
        # `_barrel_effective_exports` returns resolvable=False on any unreadable /
        # nested-star source, and we then skip -- an uncertain barrel never fires,
        # so this can only ADD a genuine finding, never a false positive from a
        # name still provided by another star.
        try:
            barrel_seen: set = set()
            already = {(b[0], b[1]) for b in breaks}  # (name, module) dedup vs direct path
            for path in list(state.files):
                if seen_files >= max_files:
                    break
                p = Path(path)
                if not p.is_file() or detect_language(str(p)) != "typescript":
                    continue
                ws_root = _ws_root_for(p)
                ws_index = _ws_index_for(ws_root)
                if ws_index is None:
                    continue
                stem = p.stem
                for bx in ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs"):
                    barrel = p.parent / bx
                    if barrel == p or not barrel.is_file():
                        continue
                    bkey = module_key_for_path(barrel, ws_root)
                    if bkey is None or bkey in barrel_seen:
                        continue
                    try:
                        bcontent = barrel.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    if not _barrel_reexports_stem(bcontent, stem):
                        continue
                    ign = ignored_rules(bcontent, file_path=str(barrel)) or set()
                    if "" in ign or "removed-export-breaks-importers" in ign:
                        continue
                    eff, resolvable = _barrel_effective_exports(
                        barrel, ws_root, _resolver_for("typescript", ws_root)
                    )
                    if not resolvable:
                        continue
                    broken = ws_index.broken_importers(bkey, eff)
                    if not broken:
                        continue
                    barrel_seen.add(bkey)
                    seen_files += 1
                    for name in sorted(broken):
                        if (name, bkey) in already:
                            continue
                        live = [
                            imp
                            for imp in broken[name]
                            if _live_break(imp.path, name, imp.line, bkey, "typescript", ws_root)
                        ]
                        if not live:
                            continue
                        live_sorted = sorted(
                            live,
                            key=lambda imp: (imp.path, imp.line if imp.line is not None else -1),
                        )
                        bsites = [
                            _safe_ref_field(
                                f"{imp.path}:{imp.line}" if imp.line is not None else imp.path
                            )
                            for imp in live_sorted[:max_sites]
                        ]
                        breaks.append(
                            (_safe_ref_field(name), _safe_ref_field(bkey), bsites, "barrel")
                        )
                        if out_breaks is not None:
                            out_breaks.append(
                                {
                                    "name": name,
                                    "target_key": bkey,
                                    "kind": "barrel",
                                    "lang": "typescript",
                                    "ws_root": ws_root,
                                    "importers": [(imp.path, imp.line) for imp in live_sorted],
                                }
                            )
        except Exception:
            pass

        if not breaks:
            return []

        plural = len(breaks) != 1
        lines = [
            f"[🦎 chameleon: {len(breaks)} definition{'s' if plural else ''} you "
            f"removed still ha{'ve' if plural else 's'} live call sites]",
            "These names are gone from the files you edited but other files still "
            "reference them; their call sites are now broken. Restore the "
            "definition or update the references. This is advisory, not a block.",
        ]
        for name, module, sites, kind in breaks:
            shown = ", ".join(sites)
            more = " ..." if len(sites) >= max_sites else ""
            if kind == "constant":
                verb = "defined; still referenced by"
            elif kind == "deleted":
                verb = f"exported ({module} was deleted); still imported by"
            elif kind == "barrel":
                verb = f"re-exported (via {module}); still imported by"
            else:
                verb = "exported; still imported by"
            lines.append(f"- '{name}' no longer {verb} {shown}{more}")
        # Infer the ignore-comment token from the turn's touched files. Include
        # deleted_paths: on a pure-deletion turn the deleted .py/.rb was pruned
        # from state.files, leaving an EMPTY set that falls through to the `//`
        # default -- a syntax error a Python/Ruby author would paste. The deleted
        # path's extension still tells us the language for the surviving-importer
        # source the author edits.
        hint_paths = [path for path in state.files] + list(deleted_paths or [])
        lines.append(
            "To silence this for a file, add "
            + _ignore_hint(hint_paths, "removed-export-breaks-importers")
            + " in the source you touched."
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

        index = dr.build_candidate_index(
            repo_root,
            _duplication_index_files(edited, state, repo_id=repo_id, session_id=session_id),
        )

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
        # Surface each duplication PAIR at most once per session. Re-editing a file
        # changes its digest and re-runs this gate; without this a pre-existing
        # duplication the author already saw (and chose to keep) is re-flagged on
        # every later turn that touches the file.
        unsurfaced = [
            c for c in confirmed if not dr.finding_already_surfaced(repo_data, session_id or "", c)
        ]
        for c in unsurfaced:
            dr.mark_finding_surfaced(repo_data, session_id or "", c)
        # Mark every fresh file judged at its current digest so the next turn over
        # the same content is suppressed regardless of whether it was confirmed.
        for p in fresh:
            dr.mark_judged(
                repo_data, session_id or "", dr._repo_rel(repo_root, p), digests.get(p, "")
            )
        return dr.format_duplication_advisory(unsurfaced)
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
            turn_key = route.get("turn_key")

            # Record the same grounding + degraded-spawn check events the separate
            # gate records, so a silently-dead reviewer surfaces the SessionStart
            # health banner under the DEFAULT (multi-lens) path too -- without a
            # sink here the default path emitted no degraded_spawn event and the
            # banner could never fire, defeating its whole purpose.
            def _sink(kind: str, detail: str | None = None) -> None:
                fam = _judge_grounding_family(kind)
                if fam is not None:
                    _emit_check_event(
                        repo_id,
                        session_id,
                        fam.rstrip("_"),
                        kind[len(fam) :],
                        detail={"turn_key": turn_key},
                    )
                    return
                _emit_check_event(
                    repo_id,
                    session_id,
                    "correctness_judge",
                    "degraded_spawn",
                    kind,
                    detail={"turn_key": turn_key, "detail": detail},
                )

            return judge.run_correctness_judge(
                repo_root,
                _enf_profile_dir(repo_root),
                fresh,
                resolver,
                intent_tokens=intent_tokens,
                event_sink=_sink,
                # Same reviewer model ladder as the single-lens gate: escalate a
                # high-risk / intent-forced route's judge to the stronger model.
                model=judge.judge_model_for_route(route.get("reason")),
            )

        # Duplication pairs to mark surfaced only AFTER they are actually rendered
        # (see the render block), so an exception in synthesis/render never
        # persistently suppresses a duplication the user never saw.
        dup_to_mark: list = []

        def _run_duplication():
            edited = [p for p in state.files if Path(p).is_file()]
            if not edited:
                return []
            lang = dr._lang_of(edited[0])
            index = dr.build_candidate_index(
                repo_root,
                _duplication_index_files(edited, state, repo_id=repo_id, session_id=session_id),
            )
            try:
                from chameleon_mcp.function_catalog import load_function_catalog

                catalog = load_function_catalog(repo_root)
            except Exception:
                catalog = None
            findings = dr.gather_findings(repo_root, fresh, index=index, catalog=catalog, lang=lang)
            if not findings:
                return []
            confirmed = dr.judge_body_matches(repo_root, findings, semantic=True)
            # Surface each duplication pair at most once per session (see the
            # standalone duplication gate): a later edit re-runs this lens, and a
            # pre-existing duplication already shown must not be re-flagged.
            unsurfaced = [
                c
                for c in confirmed
                if not dr.finding_already_surfaced(repo_data, session_id or "", c)
            ]
            dup_to_mark.extend(unsurfaced)
            return unsurfaced

        # When the async/detach route is selected (operator opt-in, or
        # automatically on a known bare-auth failure) the correctness lens cannot
        # run synchronously here: the plain full-primer spawn pays the full
        # session primer and cannot fit the sync Stop budget, so under the short
        # 45s cap it reliably times out and contributes nothing. Detach it through
        # the same path the standard gate uses; its findings arrive on the next
        # prompt. The duplication lens still runs synchronously this Stop.
        corr_detached = False
        if getattr(cfg, "correctness_judge", True) and _judge_async_mode() is not None:
            try:
                from chameleon_mcp import judge_async

                corr_detached = judge_async.launch_async_judge(
                    repo_root=repo_root,
                    repo_data=repo_data,
                    repo_id=repo_id or "",
                    session_id=session_id or "",
                    fresh_abs_paths=fresh,
                    digests=digests,
                    turn_key=route.get("turn_key"),
                    intent_tokens=intent_tokens,
                    route_reason=route.get("reason"),
                )
            except Exception:
                corr_detached = False

        # Honor the per-lens enforcement flags: multi_lens replaces the gates but
        # must not resurrect a lens the operator turned off (duplication_review /
        # correctness_judge). A lens left out simply does not run.
        from chameleon_mcp._thresholds import threshold_int

        lenses = []
        ran_duplication = False
        if getattr(cfg, "correctness_judge", True) and not corr_detached:
            lenses.append(lens_runner.correctness_lens(_run_correctness))
        # Gate the duplication lens on its OWN per-session cap, not just the
        # correctness route's: the two caps differ, so without this the lens would
        # run up to the (larger) correctness budget instead of the duplication one.
        if getattr(cfg, "duplication_review", True) and state.duplication_spawns < threshold_int(
            "DUPLICATION_REVIEW_MAX_SPAWNS_PER_SESSION"
        ):
            lenses.append(lens_runner.duplication_lens(_run_duplication))
            ran_duplication = True
        # Name the lenses that actually run SYNCHRONOUSLY this turn, so the
        # advisory header does not claim "correctness + duplication" when the
        # correctness lens detached (its findings arrive next turn, separately).
        ran_lens_names = [ln.name for ln in lenses]
        if not lenses and not corr_detached:
            _emit_check_event(repo_id, session_id, "multi_lens_review", "skipped", "no_lenses")
            return []

        # Spend the review budget and persist BEFORE the (slow) lens spawns so an
        # interrupted Stop still consumes the budget and the session cap holds.
        # correctness_spawns is the route's budget counter, so it always advances
        # (the pass is the unit it caps, and a detached correctness spawn already
        # spent its budget); duplication_spawns advances too when the duplication
        # lens ran, so a later turn with multi_lens off sees the duplication
        # budget already spent rather than double-reviewing.
        state.correctness_spawns += 1
        if ran_duplication:
            state.duplication_spawns += 1
        try:
            from chameleon_mcp.enforcement import save_state

            save_state(state, repo_data, session_id or "")
        except Exception:
            pass

        # The correctness lens detached and there is no sync lens to run: the
        # detached child surfaces its findings on the next prompt and marks its
        # own digests judged, so this pass is done.
        if not lenses:
            _emit_check_event(
                repo_id,
                session_id,
                "multi_lens_review",
                "ran",
                "correctness_detached",
                detail={"turn_key": route.get("turn_key")},
            )
            return []

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

        # VERIFY stage: independently refute the lone-correctness-lens findings
        # before REPORT (the default-config counterpart of the single-lens gate's
        # verify). Only single-lens correctness findings are eligible: cross-lens
        # agreement is already an independent-verification signal, and a
        # duplication finding references a SECOND location a one-file excerpt
        # cannot show, so a refuter would reject it on missing evidence. A refuted
        # finding is dropped; the rest surface labeled. Fails open to the raw set.
        verify_eligible = [f for f in surfaced if f.get("lenses") == ["correctness"]]
        verdict_by_key: dict = {}
        verify = None
        if verify_eligible:
            verify = _sync_verify_stop_findings(repo_root, verify_eligible)
            if verify.ran:
                # Identity-based membership: dict equality would alias two
                # identical findings and drop both when one was refuted.
                eligible_ids = {id(f) for f in verify_eligible}
                kept_ids = {id(f) for f in verify.kept}
                surfaced = [f for f in surfaced if id(f) not in eligible_ids or id(f) in kept_ids]
                verdict_by_key = {
                    id(f): v for f, v in zip(verify.kept, verify.kept_verdicts, strict=False)
                }
                _emit_check_event(
                    repo_id,
                    session_id,
                    "multi_lens_review",
                    "verified",
                    f"refuted={verify.refuted} confirmed={verify.confirmed}",
                    detail={"turn_key": route.get("turn_key")},
                )

        # Persist to the finding->fix ledger so the next Stop can track whether
        # each was addressed and re-surface an unaddressed high-severity one once.
        # Only VERIFY survivors persist: a refuted finding must never nag a later
        # turn.
        _ledger_persist(repo_id, session_id, repo_root, "multi_lens", surfaced)
        if not surfaced:
            return []

        # The findings are about to be rendered: mark the duplication pairs
        # surfaced NOW (not inside the lens), so an earlier synthesis/render
        # failure could never persistently suppress a duplication never shown.
        for c in dup_to_mark:
            dr.mark_finding_surfaced(repo_data, session_id or "", c)

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        n = len(surfaced)
        ran_label = " + ".join(ran_lens_names) if ran_lens_names else "review"
        lines = [
            f"[🦎 chameleon: multi-lens review flagged {n} possible issue{'s' if n != 1 else ''}]",
            f"A turn-end review ({ran_label}) read this turn's changes. "
            "These are advisory; verify each before acting, they may be wrong.",
        ]
        if verify is not None and verify.ran:
            lines.append(
                f"Independently verified: {verify.refuted} refuted and dropped, "
                f"{verify.confirmed} confirmed. A '[confirmed]' finding survived a "
                "second reviewer."
            )
        for f in surfaced:
            loc = sanitize_for_chameleon_context(str(f.get("file"))) if f.get("file") else "?"
            if f.get("line") is not None:
                loc += f":{f.get('line')}"
            lens_tag = sanitize_for_chameleon_context("+".join(f.get("lenses") or []))
            tag = " [confirmed]" if verdict_by_key.get(id(f)) == "confirmed" else ""
            claim = sanitize_for_chameleon_context(str(f.get("claim", "")))
            lines.append(f"- {loc} [{lens_tag}]{tag}: {claim}")
        return lines
    except Exception:
        return []


def _scope_drift_advisory_lines(
    *,
    repo_root: Path,
    repo_data: Path,
    session_id: str | None,
    state,
    cfg,
) -> list[str]:
    """Turn-end advisory naming changed files that look unrequested, or [].

    The turn's captured request -- the LATEST prompt, not the whole session --
    named specific identifiers (symbol / file / module names). A changed file
    whose path shares no word with any of them is a candidate unrequested
    change. Scoping to the latest prompt is what keeps a bare "commit this"
    turn silent: whole-session aggregation let a stale first prompt's
    identifiers govern every later turn, flagging the same files on every
    Stop. Stays silent unless that request named enough identifiers AND at
    least one changed file matched. Advisory only, never a block.
    Privacy-preserving: reads only the stored identifier tokens, never prompt
    prose. Fails open to [].
    """
    try:
        if cfg.mode == "off" or not getattr(cfg, "intent_scope_advisory", True):
            return []
        from chameleon_mcp import intent_capture

        entries = intent_capture.read_intent(repo_data, session_id)
        idents = intent_capture.latest_request_identifiers(entries)
        if not idents:
            return []
        root = repo_root.resolve()
        rel_paths: list[str] = []
        for path in state.files:
            if not isinstance(path, str):
                continue
            try:
                rel_paths.append(str(Path(path).resolve().relative_to(root)))
            except (ValueError, OSError):
                rel_paths.append(Path(path).name)
        drifted = intent_capture.scope_drift_files(idents, rel_paths)
        if not drifted:
            return []
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        listed = ", ".join(sanitize_for_chameleon_context(p) for p in drifted)
        return [
            f"[🦎 chameleon: possible scope drift — {len(drifted)} changed file(s) "
            f"share nothing with what the request named ({listed}). Confirm they are "
            "intended, not unrequested changes.]"
        ]
    except Exception:  # noqa: BLE001
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
    only_files: set[str] | None = None,
    allow_model_spawn: bool = True,
) -> dict:
    """Run the turn-end gates and return the hook-output dict (never emits).

    Mechanical extraction of stop_backstop's gate pipeline so the caller can
    write the session attestation at a single site after every gate finished
    and saved state. Ordering and blocking semantics are unchanged from when
    this body lived inline. CHAMELEON_ENFORCE=0 is checked here rather than
    before repo resolution so an enforce-off session still reaches the caller's
    attestation write with its env state recorded; it returns {} immediately,
    exactly as the old early return did. Fails open to {}.

    Multi-root (coordinator monorepo) parameters, all defaulting to the
    single-root behavior so the ordinary path is unchanged:

    - ``only_files``: when set, ``state.files`` is filtered to just these
      absolute paths right after load, scoping the candidate re-lint AND every
      advisory helper (they all read ``state.files``) to one workspace's edits.
      A shared-repo_id monorepo keeps ALL workspaces' files in one state file;
      scoping lets each workspace re-lint against its own profile. In this mode
      the internal saves use ``prune_missing=False`` so root-A's save cannot
      delete root-B's just-deleted entry before root-B's scoped pass records it.
    - ``allow_model_spawn``: when False, the correctness judge, multi-lens, AND
      duplication gates (every ``claude -p`` spawn site) are skipped so the whole
      Stop pays for at most one reviewer across all roots; deterministic
      advisories still run.

    The multi-root caller short-circuits on the first blocking root (armed roots
    rank first), so ``stop_hook_blocks`` is incremented for exactly one root per
    Stop even when several workspaces share one ``repo_data`` -- the anti-loop
    cap cannot be double-spent.
    """
    try:
        from chameleon_mcp.enforcement import load_state, save_state

        if os.environ.get("CHAMELEON_ENFORCE") == "0":
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "enforce_env_off")
            return {}

        from chameleon_mcp.profile.config import load_config_enforcement_only

        # Isolated enforcement read: an unrelated config-section typo must not
        # raise and silently disable the Stop backstop.
        cfg = load_config_enforcement_only(_enf_profile_dir(repo_root))
        if not cfg.stop_backstop:
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "feature_disabled")
            return {}

        state = load_state(repo_data, session_id or "")
        # Multi-root scoping: keep only this workspace's files so the candidate
        # loop and every advisory helper (which all iterate state.files) see just
        # the edits that belong to repo_root. The session counters ride along on
        # the same loaded state; the additive save-merge preserves other roots'
        # entries. prune_missing must be off here (see the save below).
        if only_files is not None:
            state.files = {k: v for k, v in state.files.items() if k in only_files}
        _prune_on_save = only_files is None
        # Per-workspace block scope: computed in BOTH modes so the anti-loop cap
        # stays consistent across a mid-session single<->multi cardinality change.
        # ``_ws_scope`` (the once-per-session idiom marker discriminator) stays
        # None in single-root to keep the legacy marker filename + tests unchanged;
        # the block budget always keys by ``_block_scope``.
        _block_scope = _stop_block_scope(repo_root)
        _ws_scope: str | None = _block_scope if only_files is not None else None

        # Cap reached: the backstop must never BLOCK again this session (so an
        # unresolvable violation cannot trap the turn in a loop), but the turn-end
        # advisories still run below -- silencing them once the block budget was
        # spent was a coverage gap. cap_reached suppresses only the block, not the
        # advisory pipeline. The per-workspace budget means one dirty workspace's
        # blocks never downgrade a sibling's hard block to advisory.
        _block_count = _effective_stop_blocks(state, _block_scope)
        cap_reached = _block_count >= cfg.stop_block_cap
        if cap_reached:
            _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "cap_reached")

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

            active_rules = active_block_rules(_enf_profile_dir(repo_root))
        except Exception:
            active_rules = None

        # Shared liveness flag for the per-file daemon fallback: once a daemon
        # call comes back empty, every later file skips the daemon and resolves
        # the archetype in-process, so a hung daemon cannot stack timeouts. The
        # caller shares the same flag with the attestation writer so the whole
        # Stop pays for at most one failed daemon probe.
        if daemon_state is None:
            daemon_state = {"available": True}

        # Finding->fix loop re-check (#9): run BEFORE this Stop's gates persist
        # their findings, so it only ever re-checks PRIOR-Stop findings. It marks
        # each addressed (the cited file changed since review) or leaves it open,
        # and returns re-surface lines for an unaddressed high-severity finding
        # (once each). Gated by CHAMELEON_FINDING_LEDGER, fail-open to [].
        resurface_lines = _ledger_recheck_and_resurface(repo_id, session_id, repo_root)

        unresolved: list[str] = []
        # path -> enforceable hard rules still standing, so the shadow would_block
        # row can attribute the backstop block to the specific rules per file.
        unresolved_rules: dict[str, list[str]] = {}
        # Files this turn recorded as edited that no longer exist: a module the
        # turn DELETED. It exports nothing now, so its importers' call sites are
        # broken -- the strongest existence break there is. The prune below drops
        # them from state (so it does not accumulate phantom paths), so capture
        # them here first and hand them to the crossfile advisory, which the loop
        # otherwise never sees (it iterates the surviving state.files).
        deleted_paths: list[str] = []
        cleared_any = False
        for path, fs in list(state.files.items()):
            p = Path(path)
            if not p.is_file():
                # The file was deleted since it was recorded; drop its entry so
                # state does not accumulate phantom paths across the session.
                deleted_paths.append(path)
                del state.files[path]
                cleared_any = True
                # Persist it: if THIS Stop short-circuits before the advisory
                # pipeline (the once-per-session idiom block), the prune above
                # would otherwise lose the deletion before its crossfile advisory
                # ever runs. Surfaced exactly once from the persisted set.
                _record_pending_deletions(repo_data, session_id, [path])
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
            verdict = _stop_file_still_blockable(
                repo_root,
                path,
                loaded=preloaded,
                active=active_rules,
                daemon_state=daemon_state,
                out_rules=file_rules,
                level=fs.level,
            )
            if verdict is None:
                # Re-verify could not run this turn (the file was unreadable);
                # keep the file armed and re-check next Stop rather than clearing
                # the flag on a violation that may still stand. Do not add it to
                # ``unresolved`` -- an unverifiable file must not block THIS turn.
                continue
            if verdict:
                unresolved.append(path)
                unresolved_rules[path] = file_rules
            else:
                fs.blockable_unresolved = False
                cleared_any = True

        if cleared_any:
            try:
                save_state(state, repo_data, session_id or "", prune_missing=_prune_on_save)
            except Exception:
                pass

        # The candidate re-lint completed (possibly over zero candidates):
        # record it so the attestation can attest the relint ran this Stop.
        _emit_check_event(repo_id, session_id, "stop_relint", "ran")

        def _run_advisories() -> dict:
            # Turn-end advisory pipeline, extracted so it runs in EVERY
            # non-blocking case -- a clean turn, a shadow turn, an off turn, and a
            # capped/shadow turn that had an unresolved violation the backstop did
            # not block. Silencing these advisories whenever a would-block file was
            # present (or the cap was spent) was a coverage gap. It leads with the
            # reflexive idiom/principle review gate, which blocks once per session
            # in enforce to force a self-review of the turn's edits, else allows
            # the stop. Top-level Stop ONLY -- a
            # SubagentStop must not run this whole-turn self-review: it would both
            # false-block a subagent on its narrow task AND burn the once-per-
            # session marker, so the real parent Stop then short-circuits and the
            # turn-end review the enforcement is meant to force is silently
            # skipped. Mirrors the is_subagent guard on every other top-level-only
            # gate below (multi-lens, duplication, scope-drift, attestation).
            gate = (
                None
                if is_subagent
                else _idiom_review_gate(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    marker_scope=_ws_scope,
                )
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
            #
            # allow_model_spawn is False on every non-first root of a multi-root
            # Stop: the reviewer budget (one claude -p across the whole 55s Stop)
            # was spent by the ranked-first root. Skip the route computation
            # entirely (its risk facts / blast-radius reads are the fixed cost we
            # do not want to pay per root) and force a non-spawning route so the
            # multi-lens, correctness, AND duplication gates below all read
            # spawn=False. Deterministic advisories still run for this root.
            if allow_model_spawn:
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
            else:
                _emit_check_event(
                    repo_id, session_id, "correctness_judge", "skipped", "multiroot_budget"
                )
                route = {
                    "spawn": False,
                    "fresh": [],
                    "digests": {},
                    "turn_key": None,
                    "intent_tokens": [],
                    "skip_reason": "multiroot_budget",
                    "reason": None,
                }
            corr_spawning = bool(route.get("spawn"))

            # Idiom gate did not block: the turn is free to end. Run the
            # independent correctness judge (on by default, advisory only,
            # per-turn routed). It never blocks; its findings ride out as
            # additionalContext the model reads after the turn.
            #
            # When the multi-lens review is on (default on), ONE coordinated pass
            # runs the correctness + duplication lenses together (no mutual defer)
            # and REPLACES both the correctness gate here and the duplication gate
            # below -- but ONLY on a turn the route actually spawns. On a low-risk
            # turn the route skips (no reviewer spend), the lens pass bails, and
            # then the standalone duplication gate below must still run so
            # duplication is not silently starved for the rest of the session.
            # Subagents keep the standard gate.
            multilens_owns_dup = bool(
                cfg.multi_lens_review and not is_subagent and route.get("spawn")
            )
            multilens_lines: list[str] = []
            judged = None
            # allow_model_spawn=False (a non-first root) skips the reviewer gates
            # outright: the ranked-first root owns the session's one spawn. Both
            # bail internally on a non-spawning route anyway, but skipping the call
            # also spares the per-root fixed cost of their pre-spawn reads.
            if allow_model_spawn:
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
                persist=lambda: save_state(state, repo_data, session_id or ""),
            )

            # Cross-file existence breaks: a turn that removed/renamed a TS export
            # other files still import by name left their call sites broken. Reuse
            # the persisted reverse index + a regex presence check (no parse at
            # Stop). Advisory only, folded into the same Stop context.
            # deleted_paths carries THIS turn's deletions plus any persisted from a
            # prior Stop that short-circuited (idiom block) before this pipeline ran;
            # dedup and mark surfaced afterwards so a deleted module is reported once.
            pending_del = _consume_pending_deletions(repo_data, session_id)
            all_deleted = list(dict.fromkeys(list(deleted_paths) + pending_del))
            crossfile_lines = _crossfile_existence_advisory_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                deleted_paths=all_deleted,
            )
            _mark_pending_deletions_surfaced(repo_data, session_id, all_deleted)

            # WP-C5: cross-WORKSPACE existence breaks -- an export this workspace
            # file removed that a SIBLING workspace still imports (read from the
            # coordinator cross index in the plugin data dir). Advisory only.
            crossws_lines = _crossworkspace_existence_advisory_lines(
                repo_root=repo_root, state=state, cfg=cfg
            )

            # Turn-end duplication: a function this turn introduced whose body
            # matches an existing one (catalog or earlier this session) gets named
            # so the author can reuse the original. Confirmed by a bounded judge
            # spawn, skipped on a SubagentStop and when the correctness judge
            # spawned a working reviewer this Stop, so a turn fires at most one
            # reviewer. A FAST-DEGRADED judge spawn (nonzero exit, parse failure)
            # does not defer: it finished quickly and left budget, so a
            # permanently broken reviewer must not starve duplication review
            # forever. A judge TIMEOUT is different -- it consumed the full 45s
            # budget, so the duplication gate must still defer (route
            # ["spawn_timed_out"]); a second sequential spawn would blow the 55s
            # wall-clock cap and SIGKILL the process mid-review. Advisory only,
            # folded into the same Stop context.
            # Skipped only when the multi-lens pass OWNED duplication this turn
            # (multi-lens on AND the route spawned). When multi-lens is on but the
            # route skipped a low-risk turn, the lens pass bailed without running
            # duplication, so the standalone gate must run here -- otherwise the
            # default config silently starves duplication after the session's first
            # spawn.
            # allow_model_spawn gates the standalone duplication gate too: it
            # spawns its own reviewer independently of the correctness route, so
            # forcing route.spawn=False would REMOVE the defer and let a non-first
            # root spawn a second claude -p, blowing the 55s wall cap. On a
            # non-first root skip it entirely -- the ranked-first root already
            # owns the session's one reviewer spawn.
            dup_lines: list[str] = []
            if allow_model_spawn and not is_subagent and not multilens_owns_dup:
                _corr_active = bool(
                    corr_spawning
                    and (not route.get("spawn_failed") or route.get("spawn_timed_out"))
                )
                dup_lines = _duplication_advisory_lines(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    corr_spawning=_corr_active,
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

            # Intent scope drift: when the session's captured request named specific
            # identifiers, flag any changed file that shares nothing with them as a
            # possibly-unrequested change. Advisory only; top-level Stop only (a
            # subagent is dispatched for a scoped sub-task, so its file set is
            # expected to differ from the parent request).
            scope_lines: list[str] = []
            if not is_subagent:
                scope_lines = _scope_drift_advisory_lines(
                    repo_root=repo_root,
                    repo_data=repo_data,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                )

            context_blocks: list[str] = []
            if resurface_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(resurface_lines) + "\n</chameleon-context>"
                )
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
            if crossws_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(crossws_lines) + "\n</chameleon-context>"
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
            if scope_lines:
                context_blocks.append(
                    "<chameleon-context>\n" + "\n".join(scope_lines) + "\n</chameleon-context>"
                )

            if context_blocks:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": "\n\n".join(context_blocks),
                    }
                }
            return {}

        # Block decision first, THEN the advisory pipeline. An unresolved
        # violation only ever suppresses the Stop under enforce with cap budget
        # left; shadow, off, and a capped enforce turn never block. In every
        # non-blocking case the turn-end advisories still run below -- the
        # previous early returns silenced them whenever a would-block file was
        # present, which starved duplication / crossfile / scope-drift review.
        if unresolved:
            hard_block = cfg.mode == "enforce" and not cap_reached
            # Record the would-have-blocked signal for shadow (feeds the
            # promotion report) AND for a capped enforce turn (the block the cap
            # swallowed is still real signal). off stays fully silent: a
            # would_block row on an enforcement-off repo is itself misleading.
            if cfg.mode == "shadow" or (cap_reached and cfg.mode == "enforce"):
                try:
                    from chameleon_mcp.metrics import emit_hook_metric

                    # One would_block row per rule per unresolved file, so the
                    # shadow report attributes the backstop block to the specific
                    # rule and can sample the file for spot-check. A file that
                    # re-lints blockable but yielded no rule name still gets one
                    # row with a null rule so the file:line sample is not lost.
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

            if hard_block:
                from chameleon_mcp.sanitization import sanitize_for_chameleon_context

                names = ", ".join(
                    sanitize_for_chameleon_context(Path(p).name) for p in unresolved[:5]
                )
                more = f" (+{len(unresolved) - 5} more)" if len(unresolved) > 5 else ""
                # Name the actual failing rules in the reason and the ignore hint,
                # so the model can construct a working escape instead of typing the
                # literal placeholder `<rule>`. Distinct across the named files, in
                # first-seen order.
                distinct_rules = list(
                    dict.fromkeys(
                        r for p in unresolved[:5] for r in (unresolved_rules.get(p) or []) if r
                    )
                )
                hint_rule = distinct_rules[0] if distinct_rules else "<rule>"
                rules_clause = (
                    f" (rule{'s' if len(distinct_rules) > 1 else ''}: {', '.join(distinct_rules)})"
                    if distinct_rules
                    else ""
                )
                # The multi-root caller short-circuits on the FIRST blocking root
                # (armed roots rank first), so exactly one root reaches this
                # increment per Stop even when several workspaces share one
                # repo_data -- the anti-loop cap cannot be double-spent in a
                # single Stop. Always charge the per-workspace budget (both modes),
                # off the reconciled effective count, so one workspace never
                # exhausts a sibling's cap and a single<->multi flip cannot re-arm
                # a spent one.
                state.stop_hook_blocks_by_root[_block_scope] = _block_count + 1
                try:
                    save_state(state, repo_data, session_id or "")
                except Exception:
                    pass
                return {
                    "decision": "block",
                    "reason": (
                        f"chameleon: unresolved convention violations remain in "
                        f"{names}{more}{rules_clause}. Fix them before ending, or add "
                        f"{_ignore_hint(unresolved[:5], hint_rule)} on the offending line."
                    ),
                }

            if cfg.mode == "off":
                # off is advisory-only and stays fully silent, even on an
                # unresolved violation.
                return {}
            # shadow / capped enforce: fall through to the advisories below.

        # Cross-file existence BLOCK: a named export the turn removed from an
        # existing module that indexed importers still reference. Runs AFTER the
        # calibrated-lint block above (that gate wins) and only when it did not
        # block. Stop-only, never inline. Each break is re-verified live and
        # HEAD-scoped by _confirmed_crossfile_break_sites (F3 turn-introduced +
        # F2 strict target-sourcing), so a mid-turn fix, a bare-package repoint,
        # or a pre-existing HEAD break never reaches here. Mirrors the unresolved
        # branch: shadow / capped-enforce emit a would_block row (carrying the
        # session id) and fall through to the advisory; enforce hard-blocks.
        # Fail-open: any error leaves the advisory pass untouched.
        if cfg.mode in ("shadow", "enforce") and getattr(cfg, "crossfile_existence_block", False):
            try:
                cf_breaks: list = []
                # for_block bypasses the advisory feature flag: the deny is gated by
                # its own crossfile_existence_block flag, not the advisory nudge's.
                _crossfile_existence_advisory_lines(
                    repo_root=repo_root,
                    state=state,
                    cfg=cfg,
                    out_breaks=cf_breaks,
                    for_block=True,
                )
                cf_confirmed: list = []
                for rec in cf_breaks:
                    sites = _confirmed_crossfile_break_sites(rec)
                    if sites:
                        cf_confirmed.append((rec, sites))
                if cf_confirmed:
                    cf_count = _effective_stop_blocks(state, _block_scope)
                    cf_cap_reached = cf_count >= cfg.stop_block_cap
                    cf_hard = cfg.mode == "enforce" and not cf_cap_reached
                    if cfg.mode == "shadow" or (cf_cap_reached and cfg.mode == "enforce"):
                        try:
                            from chameleon_mcp.metrics import emit_hook_metric

                            for rec, _sites in cf_confirmed:
                                mod_abs = str(Path(rec["ws_root"]) / rec["target_key"])
                                emit_hook_metric(
                                    "stop-backstop",
                                    elapsed_ms=0,
                                    repo_id=repo_id,
                                    advisory_emitted=True,
                                    would_block=True,
                                    rule="removed-export-breaks-importers",
                                    file_rel=_repo_rel(repo_root, mod_abs),
                                    session_id=session_id,
                                )
                        except Exception:
                            pass
                    if cf_hard:
                        from chameleon_mcp.sanitization import (
                            sanitize_for_chameleon_context as _cf_s,
                        )

                        parts: list[str] = []
                        for rec, sites in cf_confirmed[:5]:
                            nm = _cf_s(str(rec.get("name")))
                            tgt = _cf_s(str(rec.get("target_key")))
                            shown = ", ".join(
                                _cf_s(f"{s}:{ln}" if ln is not None else s) for s, ln in sites[:5]
                            )
                            parts.append(f"'{nm}' (removed from {tgt}) still imported by {shown}")
                        more = f" (+{len(cf_confirmed) - 5} more)" if len(cf_confirmed) > 5 else ""
                        # Charge the per-workspace anti-loop budget, same as the
                        # unresolved branch, so a persistent break cannot loop.
                        state.stop_hook_blocks_by_root[_block_scope] = cf_count + 1
                        try:
                            save_state(state, repo_data, session_id or "")
                        except Exception:
                            pass
                        hint_files = [
                            str(
                                Path(cf_confirmed[0][0]["ws_root"])
                                / cf_confirmed[0][0]["target_key"]
                            )
                        ]
                        return {
                            "decision": "block",
                            "reason": (
                                "chameleon: you removed exports still imported elsewhere: "
                                + "; ".join(parts)
                                + more
                                + ". Restore the export or update the call sites before "
                                "ending, or add "
                                + _ignore_hint(hint_files, "removed-export-breaks-importers")
                                + " in the source you touched."
                            ),
                        }
            except Exception:
                pass

        return _run_advisories()
    except Exception as exc:
        _note_if_config_malformed(exc, repo_id, session_id, "stop_relint")
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

        payload["profile_sha256"] = hash_profile(_enf_profile_dir(repo_root)) or None
    except Exception:
        pass
    try:
        from chameleon_mcp.profile.config import load_config_enforcement_only

        payload["enforcement_mode"] = load_config_enforcement_only(_enf_profile_dir(repo_root)).mode
    except Exception as exc:
        _note_if_config_malformed(exc, repo_id, session_id, "attestation_enforcement_mode")

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
        # Count both counters: multi-root charges the per-workspace map, so the
        # scalar alone would under-report blocks in a coordinator monorepo. The
        # attestation is raise-only, so it must never under-count activity.
        stop_hook_blocks = int(state.stop_hook_blocks or 0) + sum(
            int(v or 0) for v in state.stop_hook_blocks_by_root.values()
        )
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


def _discover_stop_roots(cwd: Path, session_id) -> list[dict]:
    """Every workspace root whose enforcement state was touched this session.

    Closes the coordinator-root dead spot: a session launched at a monorepo
    coordinator (its cwd resolves to a profile-less git root, or edits landed in
    sibling repos) has its per-edit state written under EACH edited file's own
    workspace repo_id, not the cwd's. This globs the session-keyed state files
    across every repo_id dir and regroups their recorded files by each file's
    OWN ``find_repo_root``, so the Stop can gate every touched workspace against
    its own profile instead of the one cwd resolves to.

    Each state file's parent dir NAME is the authoritative repo_id (the dir the
    armed state actually lives in). It is NOT recomputed via
    ``_compute_repo_id(ws_root)`` -- a workspace's live git identity can shift
    between the posttool write and the Stop (a remote added mid-session, a
    transient ``git remote`` failure), which would point the gate at a different,
    empty state dir and silently miss the armed block.

    Returns an ordered list of dicts (armed-bearing roots first, then by
    descending touched-file count, then path -- a deterministic tiebreak so the
    single model-spawn budget and any replay are stable), each:
    ``{"ws_root", "repo_id", "repo_data", "files": set[str], "has_armed": bool}``.
    Fails open to the cwd root alone (or []) on any error.
    """
    from chameleon_mcp.optouts import _safe_session_marker
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.tools import _compute_repo_id

    groups: dict[str, dict] = {}

    def _add(ws_root: Path, repo_id: str, repo_data: Path, *, path=None, armed=False):
        try:
            ws_key = str(ws_root.resolve())
        except OSError:
            ws_key = str(ws_root)
        # Key by (repo_data, ws_root), NOT ws_root alone: if a workspace's git
        # identity shifts mid-session (an origin remote added, a transient git
        # failure changing the _compute_repo_id fallback), the same ws_root has
        # armed state under TWO repo_data dirs. Keying by ws_root alone would
        # collapse them and gate only the first dir's state, silently missing the
        # other's armed block. A (repo_data, ws_root) key gates each contributing
        # state file so every armed entry is re-linted. Normal topologies (one
        # repo_data per ws_root) produce one group either way.
        key = f"{repo_data}\x00{ws_key}"
        g = groups.get(key)
        if g is None:
            g = {
                "ws_root": ws_root,
                "repo_id": repo_id,
                "repo_data": repo_data,
                "files": set(),
                "has_armed": False,
            }
            groups[key] = g
        if path is not None:
            g["files"].add(path)
        if armed:
            g["has_armed"] = True

    marker = _safe_session_marker(session_id)
    # A degenerate empty/None session_id collapses to the shared "unknown" marker.
    # Globbing that bucket would pull in unrelated repos' leftover "unknown" state
    # (state files are never reaped), so restrict discovery to the cwd root only.
    if marker != "unknown":
        from chameleon_mcp.enforcement import load_state

        try:
            # Sorted so the discovered order (and, with the (repo_data, ws_root)
            # keying above, which group a ws_root's files land in) is stable
            # rather than filesystem glob-order dependent.
            state_files = sorted(_plugin_data_dir().glob(f"*/.enforcement.{marker}.json"))
        except OSError:
            state_files = []
        for sf in state_files:
            repo_data = sf.parent
            repo_id = repo_data.name
            try:
                st = load_state(repo_data, session_id or "")
            except Exception:
                continue
            for path, fs in st.files.items():
                try:
                    ws = find_repo_root(Path(path))
                except Exception:
                    ws = None
                if ws is None:
                    continue
                _add(
                    ws,
                    repo_id,
                    repo_data,
                    path=path,
                    armed=bool(getattr(fs, "blockable_unresolved", False)),
                )

    # Always include the cwd root if it resolves + carries a profile, so the
    # idiom review and attestation run for the primary repo even with zero armed
    # files (today's behavior). A cwd root already grouped from a state file keeps
    # that state file's authoritative repo_id.
    try:
        cwd_root = find_repo_root(cwd)
    except Exception:
        cwd_root = None
    if cwd_root is not None:
        try:
            cwd_id = _compute_repo_id(cwd_root)
            _add(cwd_root, cwd_id, _plugin_data_dir() / cwd_id)
        except Exception:
            pass

    ordered = sorted(
        groups.values(),
        key=lambda g: (0 if g["has_armed"] else 1, -len(g["files"]), str(g["ws_root"])),
    )
    try:
        from chameleon_mcp._thresholds import threshold_int

        cap = threshold_int("STOP_MAX_ROOTS")
    except Exception:
        cap = 16
    if len(ordered) > cap:
        # No silent truncation of ENFORCEMENT: armed roots rank first, so the cap
        # normally drops only advisory-only roots. But a session touching more
        # than `cap` ARMED workspaces would leave the overflow ungated -- record a
        # check event so a green Stop never reads as "every workspace was checked"
        # when it was not. Best-effort; a telemetry failure must not break the Stop.
        dropped = [g for g in ordered[cap:] if g["has_armed"]]
        if dropped:
            try:
                _emit_check_event(
                    dropped[0]["repo_id"],
                    session_id,
                    "stop_relint",
                    "skipped",
                    f"multiroot_cap_dropped_{len(dropped)}_armed",
                )
            except Exception:
                pass
    return ordered[:cap]


def _gate_one_root(
    *,
    payload: dict,
    root: dict,
    session_id,
    is_subagent: bool,
    daemon_state: dict,
    only_files: set[str] | None,
    allow_model_spawn: bool,
) -> dict:
    """Trust / suppression / stale gates + ``_stop_gates`` for one workspace.

    Returns ``{"output", "attest", "gated", "suppressed_reason"}``. ``gated`` is
    False for an untrusted or stale grant -- that root is skipped entirely and,
    matching today's single-root behavior, writes no attestation. A suppressed
    (paused / session-disabled) root skips the gates (output {}) but still
    attests, because the disable window is the scrutiny-relevant fact.
    """
    from chameleon_mcp.optouts import is_chameleon_suppressed
    from chameleon_mcp.profile.trust import profile_diverged_from_grant, trust_state_for

    ws_root = root["ws_root"]
    repo_id = root["repo_id"]
    repo_data = root["repo_data"]

    rec = trust_state_for(repo_id)
    # Per-root trust, never unioned: a grant on one workspace (or the coordinator)
    # does not vouch for another workspace's unreviewed profile. grants_root
    # resolves membership correctly even under a monorepo-shared repo_id.
    if rec is None or not rec.grants_root(ws_root):
        return {"output": {}, "attest": False, "gated": False, "suppressed_reason": None}
    if profile_diverged_from_grant(rec, ws_root, _enf_profile_dir(ws_root)):
        return {"output": {}, "attest": False, "gated": False, "suppressed_reason": None}

    suppressed_reason = is_chameleon_suppressed(ws_root, repo_id, session_id)
    if suppressed_reason is not None:
        _emit_check_event(repo_id, session_id, "stop_relint", "skipped", "suppressed")
        return {
            "output": {},
            "attest": True,
            "gated": True,
            "suppressed_reason": suppressed_reason,
        }

    try:
        output = _stop_gates(
            payload=payload,
            repo_root=ws_root,
            repo_id=repo_id,
            session_id=session_id,
            is_subagent=is_subagent,
            repo_data=repo_data,
            daemon_state=daemon_state,
            only_files=only_files,
            allow_model_spawn=allow_model_spawn,
        )
    except Exception:
        output = {}
    return {"output": output, "attest": True, "gated": True, "suppressed_reason": None}


def stop_backstop() -> int:
    """Stop / SubagentStop: refuse to end the turn while a touched file holds an
    unresolved hard-class violation, then run a once-per-session reflexive
    idiom/principle review of the turn's edits. Fails open; bounded by a
    per-session cap and the stop_hook_active flag so it can never trap the user
    in a loop.

    Multi-root by default (kill switch ``CHAMELEON_MULTIROOT_STOP=0``): the turn
    edited files whose per-edit hooks wrote state under EACH file's own workspace
    repo_id, so a session launched at a monorepo coordinator (whose own root is
    profile-less/untrusted) would otherwise leave every touched workspace
    ungated. Discovery regroups the session's state by each file's workspace and
    runs the gate pipeline per workspace against its own profile, honoring
    per-workspace trust (never unioned) and spending at most one reviewer spawn
    across the whole Stop. It short-circuits on the first blocking root (armed
    roots rank first), so the anti-loop cap is charged to one root per Stop;
    advisories from every non-blocking root are merged into one Stop context.

    After the gates finish, each distinct run-root (top-level Stop only) writes
    one signed session attestation -- checks ran/skipped/degraded, governed vs
    ungoverned touched files with pinned decision snapshots, inline overrides,
    and any observable disable/pause state. CHAMELEON_ATTESTATION=0 disables the
    write. Untrusted/stale roots, stop_hook_active, and CHAMELEON_DISABLE=1 (the
    bash wrapper exits pre-python) write nothing; a Stop that discovers no
    trusted run-root at all writes nothing, so that absence stays a downstream
    signal. Paused/disabled and enforce-off roots DO write a minimal attestation,
    because the disable window is the scrutiny-relevant fact.
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
        cwd = Path(cwd_raw).expanduser() if isinstance(cwd_raw, str) and cwd_raw else _safe_cwd()
    except (OSError, ValueError):
        cwd = _safe_cwd()

    try:
        multiroot = os.environ.get("CHAMELEON_MULTIROOT_STOP") != "0"
        if multiroot:
            roots = _discover_stop_roots(cwd, session_id)
        else:
            # Kill switch: today's single-root discovery (cwd only), so the
            # legacy path is available if the fan-out ever misbehaves.
            from chameleon_mcp.profile.loader import find_repo_root
            from chameleon_mcp.tools import _compute_repo_id

            cwd_root = find_repo_root(cwd)
            if cwd_root is None:
                _emit({})
                return 0
            cwd_id = _compute_repo_id(cwd_root)
            roots = [
                {
                    "ws_root": cwd_root,
                    "repo_id": cwd_id,
                    "repo_data": _plugin_data_dir() / cwd_id,
                    "files": set(),
                    "has_armed": False,
                }
            ]

        if not roots:
            _emit({})
            return 0

        single = len(roots) == 1
        # One daemon-liveness flag for the whole Stop, shared across every root's
        # gates and attestation, so a hung daemon costs at most one probe.
        daemon_state = {"available": True}
        attested: set[str] = set()
        allow_spawn = True
        block_output: dict | None = None
        advisory_contexts: list[str] = []

        def _attest(root: dict, suppressed_reason) -> None:
            if is_subagent or os.environ.get("CHAMELEON_ATTESTATION") == "0":
                return
            if root["repo_id"] in attested:
                return
            attested.add(root["repo_id"])
            try:
                _write_session_attestation(
                    repo_root=root["ws_root"],
                    repo_id=root["repo_id"],
                    session_id=session_id,
                    repo_data=root["repo_data"],
                    suppressed_reason=suppressed_reason,
                    daemon_state=daemon_state,
                )
            except Exception:
                pass

        for root in roots:
            # Single root scopes to nothing (only_files=None) so the fast path is
            # output-equivalent to today, including prune_missing=True. Multi-root
            # scopes each pass to its workspace's files so a shared-repo_id state
            # file is re-linted per profile.
            only_files = None if single else set(root["files"])
            res = _gate_one_root(
                payload=payload,
                root=root,
                session_id=session_id,
                is_subagent=is_subagent,
                daemon_state=daemon_state,
                only_files=only_files,
                allow_model_spawn=allow_spawn,
            )
            if not res["gated"]:
                continue
            _attest(root, res["suppressed_reason"])
            out = res["output"]
            if out.get("decision") == "block":
                # Short-circuit: refuse the turn on the first blocking workspace
                # (armed roots rank first). A block discards advisories, exactly
                # as the single-root path does.
                block_output = out
                break
            ac = (out.get("hookSpecificOutput") or {}).get("additionalContext")
            if ac:
                advisory_contexts.append(ac)
            # The first gated, non-suppressed root spent the session's one
            # reviewer-spawn budget; every later root runs deterministic-only.
            if res["suppressed_reason"] is None:
                allow_spawn = False

        if block_output is not None:
            _emit(block_output)
            return 0
        if advisory_contexts:
            _emit(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": "\n\n".join(advisory_contexts),
                    }
                }
            )
            return 0
        _emit({})
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
    # Rotate the hook error log in-process, the metrics.py pattern, instead of
    # the shell hooks spawning a second `python -m chameleon_mcp.log_rotation`
    # interpreter before every helper spawn -- an edit then pays one interpreter
    # per hook, not two. Best-effort: a rotation failure must never break a hook.
    try:
        from chameleon_mcp.log_rotation import rotate_if_needed

        rotate_if_needed(_hook_error_log_path())
    except Exception:
        pass
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
