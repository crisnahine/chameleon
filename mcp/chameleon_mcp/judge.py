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
  - it runs at most once per session, gated by a per-session marker the caller
    owns (mirroring the idiom gate).
"""

from __future__ import annotations

import json
import os
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


def build_prompt(repo_root: Path, profile_dir: Path, diffs: list[FileDiff]) -> str:
    """Assemble the reviewer prompt from diffs, witnesses, and guidance.

    The prompt is deliberately narrow: the reviewer is told it is a second pair
    of eyes looking only for correctness defects, and to return a strict JSON
    array of findings with self-rated confidence. Convention/style is explicitly
    out of scope (the static engine already covers it).
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


def _parse_findings(stdout: str) -> list[Finding]:
    """Parse the reviewer's stream-json output into findings.

    The reviewer is asked for a bare JSON array, but it speaks through
    ``claude -p --output-format stream-json``, so the array lands inside an
    assistant ``result``/``text`` block. This extracts the last JSON array found
    in any text the model emitted and coerces each element into a Finding.
    Malformed or partial output yields no findings (fail open).
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
            return findings
    return []


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


def _spawn_reviewer(prompt: str, cwd: Path) -> str | None:
    """Spawn ``claude -p`` for a one-shot correctness review, returning stdout.

    Runtime-owned spawn wrapper (the journey-harness wrapper under ``tests/`` is
    never importable by the shipped plugin). Hard wall-clock budget, no tools,
    minimal turns, output captured. Returns None on timeout, a non-zero exit, or
    any spawn error so the caller fails open to no findings.
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
    # Spawn into an empty throwaway config dir so the judge inherits none of the
    # user's settings, plugins, or hooks: a SessionStart hook stack alone can
    # burn the whole wall-clock budget before the prompt is read. Auth still
    # resolves from the environment/keychain, which the config dir does not own.
    _sweep_stale_judge_dirs()
    env = dict(os.environ)
    cfg_dir: str | None = None
    try:
        cfg_dir = tempfile.mkdtemp(prefix="chameleon-judge-")
        env["CLAUDE_CONFIG_DIR"] = cfg_dir
    except OSError:
        cfg_dir = None  # fall back to the inherited config; the timeout still bounds us
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        if cfg_dir is not None:
            shutil.rmtree(cfg_dir, ignore_errors=True)
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


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
) -> list[Finding]:
    """Run the full judge pipeline for one turn, returning advisory findings.

    Reconstructs diffs, builds the prompt, spawns the reviewer, and parses
    findings. Every stage fails open: an empty file set, a spawn failure, a
    timeout, or unparseable output all return ``[]`` so the turn ends normally.
    """
    try:
        diffs = collect_file_diffs(repo_root, abs_paths, archetype_for)
        if not diffs:
            return []
        prompt = build_prompt(repo_root, profile_dir, diffs)
        stdout = _spawn_reviewer(prompt, repo_root)
        if stdout is None:
            return []
        return _parse_findings(stdout)
    except Exception:
        return []
