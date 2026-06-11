"""Independent turn-end correctness judge (advisory only).

At the Stop gate, when the session edited governed files and the feature is
enabled, this module spawns a separate ``claude -p`` reviewer whose only job is
to read the turn's changes for correctness bugs the static engine cannot see:
logic errors, off-by-one, inverted conditions, missing guards, dropped awaits,
unhandled error paths. The author model self-reviewing shares its own blind
spots; a separate spawn does not.

It is ADVISORY ONLY and never blocks the turn. The findings are emitted as Stop
``additionalContext`` the model reads after the turn (so it may act on them) and
shadow-logged as metrics for later human-labeled precision sampling. There is no
calibration-to-FP-epsilon step: an LLM verdict is stochastic and cannot clear a
near-zero reproducible bar, so a blocking variant does not belong on the hot
path. If blocking is ever wanted, it belongs at PR-review time.

Design constraints (every one fails open, returning no findings):
  - the diff is reconstructed via ``git diff`` against HEAD, falling back to
    whole-file content when git is unavailable or the path is untracked;
  - the spawn has a short hard wall-clock budget so a slow review never traps
    the turn;
  - the prompt and the parsed output are size-capped;
  - spawns are routed per turn by the caller (digest-keyed freshness + risk
    facts under a per-session budget), so an unchanged or low-risk turn never
    pays for a reviewer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

# Reuse the canonical-loader git timeout budget for the per-file diff reads: each
# `git diff` is a cheap local read, and a hung git call must not eat the judge's
# own wall-clock budget before the spawn even starts.
_GIT_TIMEOUT_SECONDS = 5

# Per-file slice of the reconstructed-diff budget: a single huge file should not
# consume the whole CORRECTNESS_JUDGE_MAX_DIFF_BYTES allowance and starve the
# other touched files of any representation in the prompt.
_PER_FILE_DIFF_CAP = 12_000

# Cap on canonical-witness bytes injected per archetype. The witness grounds the
# reviewer in the sibling shape without ballooning the prompt past the spawn's
# time budget.
_WITNESS_CHAR_CAP = 1500

# Cap on idioms/principles text injected. Same rationale as the idiom gate's
# own context cap: enough to ground the review, not the whole document.
_GUIDANCE_CHAR_CAP = 1500

# Cap on the joined intent-token list appended to the prompt. Intent is a hint
# pointing the reviewer at the request's checkable specifics (constants,
# identifiers, quoted strings), not a transcript.
_INTENT_CHAR_CAP = 600


@dataclass
class Finding:
    """One correctness finding from the judge.

    ``confidence`` is the reviewer's self-rated 0..1 score; it is advisory
    metadata only and never gates a block. ``file`` and ``line`` locate the
    finding when the reviewer supplies them.
    """

    message: str
    confidence: float
    file: str | None = None
    line: int | None = None


@dataclass
class FileDiff:
    """A touched file's reconstructed change fed to the reviewer."""

    rel_path: str
    archetype: str | None
    diff_text: str
    is_whole_file: bool


def _run_git(args: list[str], *, cwd: Path):
    """Run ``git`` with a short timeout, returning the completed process or None.

    Returns None on any failure (timeout, git not on PATH, OSError). Callers
    treat None as "git is unavailable here" and fall back to whole-file content.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_available(repo_root: Path) -> bool:
    """True when ``repo_root`` is inside a usable git work tree."""
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_root)
    return result is not None and result.returncode == 0 and "true" in (result.stdout or "")


def reconstruct_diff(repo_root: Path, abs_path: str, rel_path: str) -> FileDiff | None:
    """Reconstruct the turn's change for one file as a unified diff.

    Prefers ``git diff HEAD -- <rel>`` so the reviewer sees only what changed.
    Falls back to whole-file content (flagged ``is_whole_file``) when git is
    unavailable, the file is untracked, or the diff is empty (the file changed
    but is, for example, staged or identical to HEAD). Returns None only when the
    file cannot be read at all (fail open: nothing to review).
    """
    p = Path(abs_path)
    if not p.is_file():
        return None

    diff_text = ""
    if _git_available(repo_root):
        result = _run_git(["diff", "HEAD", "--", rel_path], cwd=repo_root)
        if result is not None and result.returncode == 0:
            diff_text = result.stdout or ""

    if diff_text.strip():
        if len(diff_text) > _PER_FILE_DIFF_CAP:
            diff_text = diff_text[:_PER_FILE_DIFF_CAP] + "\n... (diff truncated)\n"
        return FileDiff(rel_path=rel_path, archetype=None, diff_text=diff_text, is_whole_file=False)

    # Whole-file fallback: untracked file, dirty-but-unstaged-vs-HEAD mismatch, or
    # no git. The reviewer reads the current content and judges it as a whole.
    try:
        content = p.read_bytes()[:_PER_FILE_DIFF_CAP].decode("utf-8", errors="replace")
    except OSError:
        return None
    return FileDiff(rel_path=rel_path, archetype=None, diff_text=content, is_whole_file=True)


def _load_guidance(profile_dir: Path) -> str:
    """Return the combined idioms + principles text, length-capped, or ''."""
    parts: list[str] = []
    for name, label in (("idioms.md", "Team idioms"), ("principles.md", "Principles")):
        try:
            fp = profile_dir / name
            if fp.is_file():
                text = fp.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    parts.append(f"{label}:\n{text[:_GUIDANCE_CHAR_CAP]}")
        except OSError:
            continue
    return "\n\n".join(parts)


def _witness_for(repo_root: Path, archetype: str | None) -> str:
    """Return the canonical witness excerpt for an archetype, or ''.

    Reuses ``get_canonical_excerpt`` so the reviewer can compare the change
    against the same sibling shape chameleon already trusts. Best-effort: any
    failure yields no witness rather than aborting the review.
    """
    if not archetype:
        return ""
    try:
        from chameleon_mcp.tools import get_canonical_excerpt

        env = get_canonical_excerpt(str(repo_root), archetype)
        data = env.get("data") if isinstance(env, dict) else None
        content = (data or {}).get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and content.strip():
            return content[:_WITNESS_CHAR_CAP]
    except Exception:
        return ""
    return ""


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _changed_lines(diff_text: str) -> list[tuple[int, int]]:
    """New-side line ranges from unified-diff hunk headers."""
    out = []
    for m in _HUNK_RE.finditer(diff_text or ""):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        out.append((start, start + max(count - 1, 0)))
    return out


def _parse_changed_file(repo_root: Path, path: str):
    """Indirection over the duplication gate's per-file parse so tests can stub it."""
    from chameleon_mcp.tools import parse_edited_functions

    return parse_edited_functions(repo_root, path)


# "Committed callers", not "cross-file": same_file-grade rows list callers
# from the changed file itself, so a cross-file claim would oversell them.
_FACTS_HEADER = (
    "Committed callers of the changed functions "
    "(snapshot at profile derivation; deterministic grades only):"
)


def caller_facts_for_diffs(repo_root: Path, diffs: list[FileDiff]) -> str:
    """Bounded caller-facts block for the callables this turn changed, or ''.

    Honest by construction: every caller row comes from the committed calls
    snapshot, whose grades are deterministic only (same_file / import /
    constant_receiver -- name-only matches are never stored, so none can appear
    here), and the header labels the data a snapshot at profile derivation, not
    a live scan. A "no committed callers found" line explicitly allows new,
    unused, or dynamic/unsupported call paths: absence of an edge is never
    evidence of dead code. A changed callable is one whose current on-disk span
    (the same parse the duplication gate uses) intersects the diff's new-side
    hunk ranges; a whole-file diff counts every parsed callable. When the char
    cap forces callable lines out, the block ends with a "(+N more changed
    callables not shown)" tail inside the cap, so a shortened list never reads
    as complete. Returns "" when the index is absent or nothing changed
    resolves, so the consumer records a skipped check event instead of feeding
    the reviewer an empty section. Fails open everywhere: any exception inside
    per-file processing skips that file's facts, never raises.
    """
    try:
        from chameleon_mcp.calls_index import load_calls_index

        index = load_calls_index(repo_root)
    except Exception:
        return ""
    if index is None:
        return ""

    # The index is trust-hashed, but every other artifact-derived string entering
    # a prompt is sanitized; keep the invariant uniform.
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    max_callables = threshold_int("JUDGE_FACTS_MAX_CALLABLES")
    max_sites = threshold_int("JUDGE_FACTS_MAX_SITES")
    char_cap = threshold_int("JUDGE_FACTS_CHAR_CAP")

    lines: list[str] = [_FACTS_HEADER]
    listed = 0
    for fd in diffs:
        if listed >= max_callables:
            break
        try:
            fns = _parse_changed_file(repo_root, str(repo_root / fd.rel_path))
            ranges = _changed_lines(fd.diff_text) if not fd.is_whole_file else []
            seen: set[str] = set()
            for fn in fns:
                if listed >= max_callables:
                    break
                if fn.name in seen:
                    continue
                # The index records `new Klass()` under the exported class
                # name, never under "constructor"; a constructor row would
                # render a false "no committed callers" line, so it renders
                # nothing. Kind-keyed: a plain function NAMED constructor is
                # indexed under that name and still renders.
                if fn.kind == "constructor":
                    continue
                if not fd.is_whole_file:
                    # Span intersection against the hunks; a span-less entry
                    # (old dump) cannot be located, so it is never claimed as
                    # changed by a partial diff.
                    if fn.start_line is None or fn.end_line is None:
                        continue
                    if not any(fn.start_line <= hi and fn.end_line >= lo for lo, hi in ranges):
                        continue
                seen.add(fn.name)
                entry = index.callers_of(fd.rel_path, fn.name)
                s_fn_name = sanitize_for_chameleon_context(fn.name)
                s_rel_path = sanitize_for_chameleon_context(fd.rel_path)
                if entry is None or not entry["callers"]:
                    lines.append(
                        f"- {s_fn_name}() in {s_rel_path}: no committed callers found "
                        "(new, unused, or called dynamically)"
                    )
                else:
                    shown = entry["callers"][:max_sites]
                    sites = ", ".join(
                        (
                            f"{sanitize_for_chameleon_context(r['path'])}:{r['line']}"
                            if r["line"] is not None
                            else sanitize_for_chameleon_context(r["path"])
                        )
                        + f" ({sanitize_for_chameleon_context(r['caller'])})"
                        for r in shown
                    )
                    total = entry["total"]
                    line = (
                        f"- {s_fn_name}() in {s_rel_path}: {total} committed "
                        f"caller{'s' if total != 1 else ''}, e.g. {sites}"
                    )
                    if total > len(shown):
                        line += f" [+{total - len(shown)} more]"
                    if entry["truncated"]:
                        line += " (count is a lower bound)"
                    lines.append(line)
                listed += 1
        except Exception:
            continue

    if listed == 0:
        return ""
    # Char cap bites at a line boundary: drop whole fact lines from the end
    # until the block fits, reserving room for a tail that says how many
    # callable lines were dropped (a silently shortened list would read as
    # the complete set). A block reduced to its bare header carries no fact
    # and reads as absent.
    dropped = 0

    def _tail() -> list[str]:
        noun = "callable" if dropped == 1 else "callables"
        return [f"(+{dropped} more changed {noun} not shown)"] if dropped else []

    while len(lines) > 1 and len("\n".join(lines + _tail())) > char_cap:
        lines.pop()
        dropped += 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines + _tail())


def build_prompt(
    repo_root: Path,
    profile_dir: Path,
    diffs: list[FileDiff],
    intent_tokens: list[str] | None = None,
    caller_facts: str | None = None,
) -> str:
    """Assemble the reviewer prompt from diffs, witnesses, and guidance.

    The prompt is deliberately narrow: the reviewer is told it is a second pair
    of eyes looking only for correctness defects, and to return a strict JSON
    array of findings with self-rated confidence. Convention/style is explicitly
    out of scope (the static engine already covers it). ``intent_tokens`` are
    checkable specifics extracted from the user's request (values, identifiers,
    quoted strings); when present they are appended, sanitized and length-capped,
    so the reviewer can cross-check the change against what was actually asked.
    ``caller_facts`` is the pre-built (already bounded and labeled) committed-
    caller block from :func:`caller_facts_for_diffs`; when present it rides
    above the diffs so the reviewer reads each change with its consumers in
    view instead of guessing the blast radius.
    """
    sections: list[str] = [
        "You are an independent code reviewer giving a finished change a second "
        "read for CORRECTNESS only. Look for logic errors, off-by-one mistakes, "
        "inverted conditions, missing guards or null checks, dropped awaits, and "
        "unhandled error paths in the changed code below. Do NOT comment on "
        "style, naming, formatting, or convention conformance; another tool "
        "covers those. Only flag a defect you are confident the author did not "
        "intend.",
        "",
        "Return ONLY a JSON array (no prose, no code fence). Each element is an "
        'object: {"file": "<relative path>", "line": <int or null>, '
        '"message": "<one-sentence description of the bug and its consequence>", '
        '"confidence": <float 0..1>}. Return [] if you find no correctness bug.',
    ]

    guidance = _load_guidance(profile_dir)
    if guidance:
        sections.append("")
        sections.append("Project guidance (context, not a checklist):")
        sections.append(guidance)

    if intent_tokens:
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        joined = ", ".join(sanitize_for_chameleon_context(t) for t in intent_tokens)
        sections.append("")
        sections.append(
            "The user's request for this work mentioned these specific values, "
            "identifiers, and strings. Verify the changed code is consistent "
            "with each one (a wrong constant, a renamed identifier, a "
            "mismatched string is a finding):"
        )
        sections.append(joined[:_INTENT_CHAR_CAP])

    if caller_facts:
        sections.append("")
        sections.append(caller_facts)

    for fd in diffs:
        sections.append("")
        header = f"=== {fd.rel_path}"
        if fd.is_whole_file:
            header += " (full file; no diff available)"
        else:
            header += " (unified diff vs HEAD)"
        header += " ==="
        sections.append(header)
        witness = _witness_for(repo_root, fd.archetype)
        if witness:
            sections.append(f"Sibling reference for {fd.rel_path}:")
            sections.append(witness)
            sections.append("")
        sections.append(fd.diff_text)

    return "\n".join(sections)


def _parse_findings_status(stdout: str) -> tuple[list[Finding], bool]:
    """Parse the reviewer's stream-json output into ``(findings, parsed_ok)``.

    The reviewer is asked for a bare JSON array, but it speaks through
    ``claude -p --output-format stream-json``, so the array lands inside an
    assistant ``result``/``text`` block. This extracts the last JSON array found
    in any text the model emitted and coerces each element into a Finding.
    ``parsed_ok`` is True when a JSON array was extracted (including an explicit
    ``[]`` meaning "reviewed, no bugs") and False when no text block yielded an
    array -- the caller records that as a degraded review rather than treating
    garbage output as a clean verdict.
    """
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            texts.append(obj["result"])
        elif obj.get("type") == "assistant":
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            texts.append(t)

    for text in reversed(texts):
        arr = _extract_json_array(text)
        if arr is None:
            continue
        findings = _coerce_findings(arr)
        if findings or arr == []:
            return findings, True
    return [], False


def _parse_findings(stdout: str) -> list[Finding]:
    """Findings-only view of ``_parse_findings_status`` (fail open to [])."""
    return _parse_findings_status(stdout)[0]


def _extract_json_array(text: str) -> list | None:
    """Return the first top-level JSON array embedded in ``text``, or None.

    Handles the common case where the model wraps the array in a ```json fence
    or surrounds it with a sentence despite the instruction. Scans for the first
    ``[`` and decodes from there with a raw decoder so trailing prose is ignored.
    """
    start = text.find("[")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def _coerce_findings(arr: list) -> list[Finding]:
    """Coerce a parsed JSON array into validated Finding objects."""
    out: list[Finding] = []
    cap = threshold_int("CORRECTNESS_JUDGE_MAX_FINDINGS")
    for item in arr:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        raw_conf = item.get("confidence")
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        file = item.get("file") if isinstance(item.get("file"), str) else None
        line = item.get("line") if isinstance(item.get("line"), int) else None
        out.append(Finding(message=message.strip(), confidence=confidence, file=file, line=line))
    # Highest-confidence findings first; cap the list so advisory output stays
    # short. Stable sort keeps the model's own ordering among ties.
    out.sort(key=lambda f: f.confidence, reverse=True)
    return out[:cap]


def _sweep_stale_judge_dirs(max_age_seconds: int = 3600) -> None:
    """Best-effort GC of throwaway judge config dirs older than an hour.

    The normal path removes its own dir, but a SIGKILL (wrapper timeout, host
    hook timeout) lands before the cleanup runs. Sweeping at the next spawn
    keeps the leak bounded to at most one stale dir between judge runs.
    """
    try:
        tmp = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age_seconds
        for entry in tmp.glob("chameleon-judge-*"):
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except Exception:
        pass


# Once-per-process probe; the spawn itself costs 30s+, so one bounded
# `claude --help` to learn the flag set is noise.
_BARE_SUPPORTED: bool | None = None

# Whether a --bare spawn keeps working credentials on this install. Flag
# existence is not enough: current CLIs strip OAuth/keychain auth under
# --bare (the spawn exits nonzero with a not-logged-in message) while the
# identical plain spawn works. None = unknown (the next spawn doubles as the
# probe), True = bare spawns authenticate, False = bare loses auth here.
_BARE_AUTH_OK: bool | None = None

_BARE_AUTH_MARKER = ".bare_auth_failed"
# Re-try --bare after a day: a login-mode change (an API key appearing in the
# environment) or a CLI fix can restore bare auth, and the re-probe costs one
# fast-failing spawn at most once per window.
_BARE_AUTH_TTL_SECONDS = 86_400

_NOT_LOGGED_IN_RE = re.compile(
    r"not logged in"
    r"|please run /login"
    r"|invalid api key"
    r"|authentication[_ ]error"
    r"|oauth token (?:is )?(?:expired|revoked|invalid)",
    re.IGNORECASE,
)


def _bare_flag_supported() -> bool:
    global _BARE_SUPPORTED
    if _BARE_SUPPORTED is None:
        try:
            out = subprocess.run(
                ["claude", "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            _BARE_SUPPORTED = "--bare" in (out.stdout or "")
        except Exception:  # noqa: BLE001
            _BARE_SUPPORTED = False
    return _BARE_SUPPORTED


def _bare_auth_marker_path() -> Path:
    from chameleon_mcp.plugin_paths import plugin_data_dir

    return plugin_data_dir() / _BARE_AUTH_MARKER


def _bare_auth_known_failed() -> bool:
    """True when a prior spawn proved --bare loses credentials on this install.

    Process cache first, then the data-dir TTL marker, so each session pays
    the discovery (one fast-failing bare spawn) at most once. An expired or
    unreadable marker reads as unknown: the next spawn re-probes --bare.
    """
    global _BARE_AUTH_OK
    if _BARE_AUTH_OK is not None:
        return not _BARE_AUTH_OK
    try:
        marker = _bare_auth_marker_path()
        raw = marker.read_text(encoding="utf-8").strip()
        if time.time() - float(raw or 0) < _BARE_AUTH_TTL_SECONDS:
            _BARE_AUTH_OK = False
            return True
        marker.unlink()
    except (OSError, ValueError):
        pass
    return False


def _record_bare_auth(ok: bool) -> None:
    """Cache a bare-spawn auth outcome: process-wide, plus the failure marker."""
    global _BARE_AUTH_OK
    _BARE_AUTH_OK = ok
    try:
        marker = _bare_auth_marker_path()
        if ok:
            if marker.is_file():
                marker.unlink()
            return
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(str(int(time.time())), encoding="utf-8")
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _spawn_lost_auth(proc) -> bool:
    """True when a failed spawn's output matches the not-logged-in shape."""
    blob = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
    return bool(_NOT_LOGGED_IN_RE.search(blob))


def _spawn_reviewer_status(prompt: str, cwd: Path) -> tuple[str | None, str | None]:
    """Spawn ``claude -p`` for a one-shot review, returning ``(stdout, reason)``.

    Runtime-owned spawn wrapper (the journey-harness wrapper under ``tests/`` is
    never importable by the shipped plugin). Hard wall-clock budget, no tools,
    minimal turns, output captured. On success returns ``(stdout, None)``; on
    failure returns ``(None, reason)`` where reason is one of ``spawn_timeout``,
    ``spawn_exec_error``, ``spawn_nonzero_exit`` so the caller can record WHY a
    review silently produced nothing instead of collapsing every failure mode
    into an indistinguishable None.
    """
    timeout_s = threshold_int("CORRECTNESS_JUDGE_TIMEOUT_SECONDS")
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
        "--model",
        os.environ.get("CHAMELEON_JUDGE_MODEL", "sonnet"),
        "--permission-mode",
        "default",
        "--disallowedTools",
        "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
    ]
    # A one-shot JSON verdict needs none of the user's session environment.
    # Without --bare the reviewer inherits every installed plugin's
    # SessionStart hooks and CLAUDE.md discovery (~18k tokens of primer per
    # spawn observed) — pure latency and cost for a reviewer that may not
    # use tools anyway. But on current CLIs --bare also drops OAuth/keychain
    # credentials (the spawn exits nonzero with "Not logged in"), so the flag
    # is gated on a FUNCTIONAL probe, not existence: the first bare spawn is
    # the probe, an auth-shaped failure falls back to a plain spawn within
    # this same call, and the outcome is cached (process + TTL marker) so
    # later spawns skip the dead flag. The plain spawn stays isolated where
    # it matters: CHAMELEON_DISABLE=1 no-ops every chameleon hook in the
    # child (no Stop-hook recursion into another judge), all tools are
    # disallowed, and the wall-clock timeout bounds any other inherited
    # hooks. Older CLIs without the flag get the plain spawn directly.
    use_bare = _bare_flag_supported() and not _bare_auth_known_failed()
    if use_bare:
        args.insert(1, "--bare")
    # Inherit the user's real config dir so the judge stays AUTHENTICATED. An
    # empty throwaway CLAUDE_CONFIG_DIR (the prior approach) strips OAuth /
    # subscription auth -- the spawn returns "Not logged in" and the judge
    # silently never fires on any non-API-key install. Sweep any config dirs
    # the prior buggy version leaked.
    _sweep_stale_judge_dirs()
    env = dict(os.environ)
    env["CHAMELEON_DISABLE"] = "1"
    deadline = time.monotonic() + timeout_s

    def _run(spawn_args: list[str], budget: float):
        try:
            return (
                subprocess.run(
                    spawn_args,
                    cwd=str(cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=budget,
                    check=False,
                ),
                None,
            )
        except subprocess.TimeoutExpired:
            return None, "spawn_timeout"
        except OSError:
            return None, "spawn_exec_error"

    proc, fail = _run(args, timeout_s)
    if use_bare and proc is not None:
        if proc.returncode == 0:
            _record_bare_auth(ok=True)
        elif _spawn_lost_auth(proc):
            # The functional probe's verdict: --bare stripped the credentials.
            # Remember it and retry plain within the remaining wall budget.
            _record_bare_auth(ok=False)
            proc, fail = _run(
                [a for a in args if a != "--bare"],
                max(1.0, deadline - time.monotonic()),
            )
    if proc is None:
        return None, fail
    if proc.returncode != 0:
        return None, "spawn_nonzero_exit"
    return proc.stdout or "", None


def _spawn_reviewer(prompt: str, cwd: Path) -> str | None:
    """Stdout-only view of ``_spawn_reviewer_status`` (None on any failure)."""
    return _spawn_reviewer_status(prompt, cwd)[0]


def collect_file_diffs(
    repo_root: Path,
    abs_paths: list[str],
    archetype_for,
) -> list[FileDiff]:
    """Reconstruct diffs for the touched files, bounded by the byte/file caps.

    ``abs_paths`` are absolute paths in last-edited order. ``archetype_for`` is a
    callable ``abs_path -> archetype name or None`` so the caller controls how the
    archetype is resolved (daemon or in-process). The newest files are kept up to
    the file cap, and the running byte total is held under the diff-bytes cap so
    the assembled prompt stays small enough for the short-budget spawn.
    """
    max_files = threshold_int("CORRECTNESS_JUDGE_MAX_FILES")
    max_bytes = threshold_int("CORRECTNESS_JUDGE_MAX_DIFF_BYTES")
    diffs: list[FileDiff] = []
    used = 0
    for abs_path in abs_paths[:max_files]:
        rel = _repo_rel(repo_root, abs_path)
        fd = reconstruct_diff(repo_root, abs_path, rel)
        if fd is None:
            continue
        if used + len(fd.diff_text) > max_bytes and diffs:
            # Budget spent and we already have at least one file; stop adding.
            break
        try:
            fd.archetype = archetype_for(abs_path)
        except Exception:
            fd.archetype = None
        diffs.append(fd)
        used += len(fd.diff_text)
    return diffs


def _repo_rel(repo_root: Path, abs_path: str) -> str:
    """Repo-relative POSIX path, falling back to the basename outside the root."""
    try:
        return Path(abs_path).resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return Path(abs_path).name


def run_correctness_judge(
    repo_root: Path,
    profile_dir: Path,
    abs_paths: list[str],
    archetype_for,
    *,
    intent_tokens: list[str] | None = None,
    event_sink=None,
) -> list[Finding]:
    """Run the full judge pipeline for one turn, returning advisory findings.

    Reconstructs diffs, builds the prompt, spawns the reviewer, and parses
    findings. Every stage fails open: an empty file set, a spawn failure, a
    timeout, or unparseable output all return ``[]`` so the turn ends normally.

    ``event_sink`` is an optional ``callable(kind, detail)`` that receives the
    degradation reason whenever the pipeline produced nothing for a cause the
    caller should record: the spawn failure reason (``spawn_timeout`` /
    ``spawn_exec_error`` / ``spawn_nonzero_exit``), ``unparseable_output`` when
    the reviewer ran but no JSON array could be extracted, and
    ``pipeline_error`` with a repr-capped detail for any other exception. Each
    sink call is guarded so a raising sink never changes the judge outcome.
    ``intent_tokens`` ride into the prompt (see ``build_prompt``).

    The sink also receives exactly one ``judge_facts_*`` kind per run, naming
    the caller-facts outcome -- ``judge_facts_included`` (block fed to the
    prompt), ``judge_facts_skipped_no_calls_index`` (feature on, no block:
    index absent or nothing changed resolves), or
    ``judge_facts_skipped_disabled`` (``enforcement.judge_crossfile_facts``
    off). These are informational, never failure kinds: the review proceeds
    identically with or without facts.
    """

    def _sink(kind: str, detail: str | None = None) -> None:
        if event_sink is None:
            return
        try:
            event_sink(kind, detail)
        except Exception:
            pass

    try:
        diffs = collect_file_diffs(repo_root, abs_paths, archetype_for)
        if not diffs:
            return []
        # Committed caller facts from the calls snapshot ground the review in
        # the change's consumers. Gated on
        # enforcement.judge_crossfile_facts (default on; an unreadable config
        # fails open to on); the sink records exactly one judge_facts_* outcome
        # per run so the attestation distinguishes a grounded review from a
        # blind one.
        caller_facts: str | None = None
        facts_enabled = True
        try:
            from chameleon_mcp.profile.config import load_config

            facts_enabled = load_config(profile_dir).enforcement.judge_crossfile_facts
        except Exception:
            facts_enabled = True
        if not facts_enabled:
            _sink("judge_facts_skipped_disabled")
        else:
            block = caller_facts_for_diffs(repo_root, diffs)
            caller_facts = block or None
            _sink("judge_facts_included" if block else "judge_facts_skipped_no_calls_index")
        prompt = build_prompt(
            repo_root,
            profile_dir,
            diffs,
            intent_tokens=intent_tokens,
            caller_facts=caller_facts,
        )
        stdout, fail_reason = _spawn_reviewer_status(prompt, repo_root)
        if stdout is None:
            _sink(fail_reason or "spawn_exec_error")
            return []
        findings, parsed_ok = _parse_findings_status(stdout)
        if not parsed_ok:
            _sink("unparseable_output")
        return findings
    except Exception as exc:
        _sink("pipeline_error", repr(exc)[:200])
        return []
