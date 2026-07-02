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
    the turn (only the detached async child, which holds no turn open, may use
    the longer fallback budget);
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
from chameleon_mcp.blast_radius import transitive_caller_chains as _transitive_caller_chains
from chameleon_mcp.safe_open import is_forbidden_segment_path

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
# time budget. Truncation is line-boundaried (see _truncate_on_line_boundary) so
# the judge never reasons against a function severed mid-body.
_WITNESS_CHAR_CAP = 3000

# Cap on idioms/principles text injected. Same rationale as the witness cap:
# enough to ground the review, not the whole document, cut on a line boundary so
# a mandatory-wrapper / banned-import rule is never half-shown.
_GUIDANCE_CHAR_CAP = 3000

# Cap on the joined intent-token list appended to the prompt. Intent is a hint
# pointing the reviewer at the request's checkable specifics (constants,
# identifiers, quoted strings), not a transcript. Enforced over whole tokens so a
# constant is never sliced into a value the judge would "verify the code against".
_INTENT_CHAR_CAP = 900


def _truncate_on_line_boundary(text: str, cap: int, notice: str) -> str:
    """Truncate ``text`` to at most ``cap`` chars on a line boundary.

    A hard mid-line chop can sever a function body or a rule mid-sentence, leaving
    the reviewer to reason against corrupted grounding. Cutting at the last newline
    within the budget keeps every shown line whole; ``notice`` flags the omission
    the way the per-file diff cap already does. Falls back to a hard cut when no
    newline falls within the budget after the first character (a single over-long
    line, or a leading blank line followed by one) -- the visible content is then
    one line too long to break cleanly.
    """
    if len(text) <= cap:
        return text
    head = text[:cap]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    return head.rstrip() + notice


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
    """Return the combined idioms + principles text, length-capped, or ''.

    idioms.md / principles.md are attacker-controllable committed artifacts, so
    their text is run through sanitize_for_chameleon_context before it enters the
    reviewer prompt -- the same scrub every other artifact-derived prompt string
    gets, keeping the invariant uniform. Sanitizing before the truncation cap
    means the cap bites on the final neutralized text.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    parts: list[str] = []
    for name, label in (("idioms.md", "Team idioms"), ("principles.md", "Principles")):
        try:
            fp = profile_dir / name
            if fp.is_file():
                text = sanitize_for_chameleon_context(
                    fp.read_text(encoding="utf-8", errors="replace").strip()
                )
                if text:
                    parts.append(
                        f"{label}:\n"
                        + _truncate_on_line_boundary(
                            text, _GUIDANCE_CHAR_CAP, "\n... (truncated; see the file)"
                        )
                    )
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
            return _truncate_on_line_boundary(
                content, _WITNESS_CHAR_CAP, "\n... (witness truncated; full file in the repo)"
            )
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


_PARSE_MEMO: dict[tuple, list] = {}


def _parse_changed_file(repo_root: Path, path: str):
    """Indirection over the duplication gate's per-file parse so tests can stub it.

    Memoized by (path, mtime, size) so the one-hop and transitive caller-fact
    builders, which both walk the same changed files in one judge run, parse each
    file once instead of spawning the parser subprocess twice. The mtime+size key
    invalidates correctly across turns (a re-edited file re-parses); the memo is
    bounded.
    """
    from chameleon_mcp.tools import parse_edited_functions

    try:
        st = os.stat(path)
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return parse_edited_functions(repo_root, path)
    cached = _PARSE_MEMO.get(key)
    if cached is not None:
        return cached
    result = parse_edited_functions(repo_root, path)
    if len(_PARSE_MEMO) > 256:
        _PARSE_MEMO.clear()
    _PARSE_MEMO[key] = result
    return result


# "Committed callers", not "cross-file": same_file-grade rows list callers
# from the changed file itself, so a cross-file claim would oversell them.
_FACTS_HEADER = (
    "Committed callers of the changed functions "
    "(snapshot at profile derivation; deterministic grades only):"
)


_CALLER_TOO_LARGE = object()  # sentinel: caller file exists but is over the read cap


def _caller_needle(callee_name: str):
    """Word-boundary regex for a call site of ``callee_name``, or None when the
    name cannot be reliably bareword-matched (so the caller must not be dropped).

    A Ruby setter ``url=`` is written ``record.url = v`` at the call site, never
    the literal ``url=``, so match the base name. Ruby predicate/bang methods
    (``foo?`` / ``foo!``) DO appear verbatim, so keep their suffix. An operator
    method (``[]``, ``[]=``, ``<=>``) has no bareword form, so return None and let
    the caller be kept rather than falsely verified stale.
    """
    name = str(callee_name)
    if len(name) > 1 and name.endswith("=") and name[:-1].isidentifier():
        name = name[:-1]  # setter: the call writes the base name with a space
    core = name[:-1] if name[-1:] in "?!" else name
    if not core or not all(c == "_" or c == "$" or c.isalnum() for c in core):
        return None  # operator / unverifiable name
    return re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")


def _caller_site_live(repo_root: Path, caller_rel, callee_name: str, line, *, cache=None) -> bool:
    """True if ``caller_rel`` still exists and still references ``callee_name``.

    The calls index is a bootstrap snapshot; a caller deleted, moved, or
    refactored to no longer call the changed function is stale. The judge prompt
    tells the reviewer to flag a finding for any listed caller the change would
    break, so a stale caller site fed here surfaces as a phantom "stale index"
    finding. Re-verify the cited edge against the working tree: the file must be
    readable and still name ``callee_name`` as a whole word (recorded line first,
    then a whole-file scan). Advisory grounding, so the bias is to KEEP rather
    than wrongly drop: a deleted file drops (read error), but an over-cap file or
    a name with no bareword form (Ruby operators) is kept (cannot be confirmed
    stale). ``cache`` is an optional per-build {rel: content|sentinel|None} dict
    so a file referenced by many callers/edges is read once.
    """
    if not isinstance(caller_rel, str) or not caller_rel:
        return False
    if cache is not None and caller_rel in cache:
        content = cache[caller_rel]
    else:
        from chameleon_mcp.safe_open import FileTooLargeError, safe_read_text

        try:
            content = safe_read_text(repo_root, caller_rel, max_size_bytes=1_000_000)
        except FileTooLargeError:
            content = _CALLER_TOO_LARGE  # exists but unverifiable -> keep
        except Exception:
            content = None  # deleted / symlinked / unreadable -> drop
        if cache is not None:
            cache[caller_rel] = content
    if content is _CALLER_TOO_LARGE:
        return True
    if content is None:
        return False
    needle = _caller_needle(callee_name)
    if needle is None:
        return True  # name has no bareword form -> cannot confirm stale, keep
    lines = content.splitlines()
    if isinstance(line, int) and not isinstance(line, bool) and 1 <= line <= len(lines):
        if needle.search(lines[line - 1]):
            return True
    return bool(needle.search(content))


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
    # Per-build cache so a file referenced by many callers is read once, not once
    # per caller row (the re-verify scans the full caller list, not just the
    # shown subset). Local to this call -> never serves a stale snapshot.
    _content_cache: dict = {}

    def _render_sites(rows) -> str:
        return ", ".join(
            (
                f"{sanitize_for_chameleon_context(r['path'])}:{r['line']}"
                if isinstance(r["line"], int) and not isinstance(r["line"], bool)
                else sanitize_for_chameleon_context(r["path"])
            )
            + f" ({sanitize_for_chameleon_context(r['caller'])})"
            for r in rows
        )

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
                # Re-verify each snapshot caller against the working tree before
                # citing it as a live call site (see _caller_site_live): a deleted,
                # moved, or no-longer-calling caller is stale and would steer the
                # judge to flag a phantom finding.
                live = (
                    [
                        r
                        for r in entry["callers"]
                        if _caller_site_live(
                            repo_root, r.get("path"), fn.name, r.get("line"), cache=_content_cache
                        )
                    ]
                    if entry is not None
                    else []
                )
                if entry is None or not entry["callers"]:
                    lines.append(
                        f"- {s_fn_name}() in {s_rel_path}: no committed callers found "
                        "(new, unused, or called dynamically)"
                    )
                    listed += 1
                elif entry["truncated"]:
                    # A god-function's caller list is a partial sample, so an empty
                    # live sample is NOT zero live callers: keep the snapshot total
                    # as a lower bound and cite whatever live sample sites remain.
                    # Only add "[+N more]" when examples ARE shown -- otherwise an
                    # example-less "N callers [+N more]" doubles the apparent count.
                    shown = live[:max_sites]
                    line = (
                        f"- {s_fn_name}() in {s_rel_path}: {entry['total']} committed "
                        f"caller{'s' if entry['total'] != 1 else ''}"
                    )
                    sites = _render_sites(shown)
                    if sites:
                        line += f", e.g. {sites}"
                        if entry["total"] > len(shown):
                            line += f" [+{entry['total'] - len(shown)} more]"
                    line += " (count is a lower bound)"
                    lines.append(line)
                    listed += 1
                elif not live:
                    # Complete list, every caller verified stale. We cannot tell
                    # "all deleted" (truly none) from "all renamed/moved" (callers
                    # exist at paths the snapshot does not record), so OMIT the line
                    # rather than assert a false "no callers" that would steer the
                    # reviewer to skip a real caller. Consumes no cap budget.
                    pass
                else:
                    shown = live[:max_sites]
                    total = len(live)
                    line = (
                        f"- {s_fn_name}() in {s_rel_path}: {total} committed "
                        f"caller{'s' if total != 1 else ''}, e.g. {_render_sites(shown)}"
                    )
                    if total > len(shown):
                        line += f" [+{total - len(shown)} more]"
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


_TRANSITIVE_HEADER = (
    "Transitive impact of the changed functions (callers-of-callers from the "
    "committed calls snapshot; deterministic grades only, absence is not dead "
    "code, and a stale intermediate edge can shorten a chain):"
)


def _render_transitive_chain(chain: list[tuple], sanitize) -> str:
    root_path, root_name, _ = chain[0]
    parts = [f"{sanitize(str(root_name))}() [{sanitize(str(root_path))}]"]
    for path, name, line in chain[1:]:
        has_line = isinstance(line, int) and not isinstance(line, bool)
        loc = f"{sanitize(str(path))}:{line}" if has_line else sanitize(str(path))
        parts.append(f"{sanitize(str(name))}() [{loc}]")
    return "- " + " <- ".join(parts)


def _live_transitive_chain(repo_root: Path, chain: list[tuple], *, cache=None) -> list[tuple]:
    """Truncate a snapshot caller chain at the first stale edge against the working
    tree, returning the still-valid prefix.

    The chain is ``[(changed_fn), (caller), (caller-of-caller), ...]``; edge i is
    valid when the caller file ``chain[i][0]`` still references the function it is
    recorded calling (``chain[i-1][1]``). A deleted intermediate file or a caller
    that dropped the call breaks the chain there. The header already warns a stale
    intermediate edge can shorten a chain; this enforces it so a deleted/refactored
    node is never cited with an exact file:line. Fails open to the full chain on
    error (never lengthens it)."""
    try:
        if not chain:
            return chain
        kept = [chain[0]]
        for i in range(1, len(chain)):
            caller_path = chain[i][0]
            callee_name = chain[i - 1][1]
            caller_line = chain[i][2]
            if _caller_site_live(
                repo_root, caller_path, str(callee_name), caller_line, cache=cache
            ):
                kept.append(chain[i])
            else:
                break
        return kept
    except Exception:
        return chain


def caller_facts_transitive_for_diffs(repo_root: Path, diffs: list[FileDiff], index=None) -> str:
    """Bounded multi-hop transitive caller-impact block, or ''.

    For each changed callable, walks the committed caller graph upward and
    renders the chains that reach at least two hops (the information the one-hop
    caller_facts block does not carry: which entry points and intermediate
    services a change transitively reaches). Same honesty posture as
    :func:`caller_facts_for_diffs` -- a committed snapshot, deterministic grades
    only, absence of an edge is never evidence of dead code. Hard-bounded
    (depth / fanout / total-nodes / char caps) and fails open to '' everywhere;
    a caller chain with fewer than two hops adds nothing over the one-hop block
    and is dropped.
    """
    try:
        if index is None:
            from chameleon_mcp.calls_index import load_calls_index

            index = load_calls_index(repo_root)
        if index is None:
            return ""

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        depth = threshold_int("JUDGE_TRANSITIVE_DEPTH")
        fanout = threshold_int("JUDGE_TRANSITIVE_FANOUT_PER_NODE")
        total = threshold_int("JUDGE_TRANSITIVE_TOTAL_NODES")
        char_cap = threshold_int("JUDGE_TRANSITIVE_CHAR_CAP")
        max_callables = threshold_int("JUDGE_FACTS_MAX_CALLABLES")

        rendered: list[str] = []
        listed = 0
        _content_cache: dict = {}
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
                    if fn.name in seen or fn.kind == "constructor":
                        continue
                    if not fd.is_whole_file:
                        if fn.start_line is None or fn.end_line is None:
                            continue
                        if not any(fn.start_line <= hi and fn.end_line >= lo for lo, hi in ranges):
                            continue
                    seen.add(fn.name)
                    chains, _fanout_clipped = _transitive_caller_chains(
                        index,
                        fd.rel_path,
                        fn.name,
                        max_depth=depth,
                        fanout=fanout,
                        total_nodes=total,
                    )
                    # Keep chains that reach the intended hop count: 2 hops at
                    # the default depth (the info one-hop caller_facts lacks), but
                    # honor a lowered depth override instead of silently emitting
                    # nothing. min_hops = min(2, depth); a chain has len-1 hops.
                    min_hops = min(2, depth)
                    multi = [c for c in chains if len(c) - 1 >= min_hops]
                    if not multi:
                        continue
                    listed_any = False
                    for c in multi:
                        # Re-verify the chain against the working tree: a deleted or
                        # no-longer-calling intermediate truncates it, and a chain
                        # shortened below the hop threshold is dropped (it would
                        # duplicate the one-hop caller_facts block).
                        live_c = _live_transitive_chain(repo_root, c, cache=_content_cache)
                        if len(live_c) - 1 < min_hops:
                            continue
                        rendered.append(
                            _render_transitive_chain(live_c, sanitize_for_chameleon_context)
                        )
                        listed_any = True
                    if listed_any:
                        listed += 1
            except Exception:
                continue

        if not rendered:
            return ""
        # Dedupe identical chains; preserve first-seen (already deterministic) order.
        uniq: list[str] = []
        seen_r: set[str] = set()
        for r in rendered:
            if r not in seen_r:
                seen_r.add(r)
                uniq.append(r)

        lines = [_TRANSITIVE_HEADER] + uniq
        dropped = 0

        def _tail() -> list[str]:
            noun = "chain" if dropped == 1 else "chains"
            return [f"(+{dropped} more transitive {noun} not shown)"] if dropped else []

        while len(lines) > 1 and len("\n".join(lines + _tail())) > char_cap:
            lines.pop()
            dropped += 1
        if len(lines) == 1:
            return ""
        return "\n".join(lines + _tail())
    except Exception:
        return ""


def imported_definition_facts(repo_root: Path, diffs: list[FileDiff]) -> str:
    """Forward block: the signatures of the symbols the changed files import.

    Resolves each changed file's named imports through the committed
    symbol-signature index and renders the matched definitions, so the reviewer
    reads each call site with the contract it must satisfy. Bounded and
    sanitized; returns "" when the index is absent or nothing resolves.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context
        from chameleon_mcp.symbol_signatures import hydrate_imported_definitions

        abs_paths = [Path(repo_root) / fd.rel_path for fd in diffs]
        lines = hydrate_imported_definitions(
            repo_root, abs_paths, max_items=threshold_int("JUDGE_IMPORTED_DEFS_MAX_ITEMS")
        )
        if not lines:
            return ""
        char_cap = threshold_int("JUDGE_IMPORTED_DEFS_CHAR_CAP")
        rendered: list[str] = []
        used = 0
        dropped = 0
        for i, ln in enumerate(lines):
            row = "- " + sanitize_for_chameleon_context(ln)
            if used + len(row) + 1 > char_cap and rendered:
                dropped = len(lines) - i
                break
            rendered.append(row)
            used += len(row) + 1
        body = "\n".join(rendered)
        if dropped:
            body += f"\n- (+{dropped} more imported definitions omitted for length)"
        return (
            "Definitions of symbols this change IMPORTS (the contracts the changed "
            "code calls into; verify each call matches its signature):\n" + body
        )
    except Exception:
        return ""


def build_prompt(
    repo_root: Path,
    profile_dir: Path,
    diffs: list[FileDiff],
    intent_tokens: list[str] | None = None,
    caller_facts: str | None = None,
    transitive_facts: str | None = None,
    imported_defs: str | None = None,
    include_style_context: bool = False,
) -> str:
    """Assemble the reviewer prompt from diffs, the checklist, and caller facts.

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
    view instead of guessing the blast radius. ``imported_defs`` is the parallel
    FORWARD block from :func:`imported_definition_facts` -- the signatures of the
    symbols the change imports -- so the reviewer reads each call site with the
    contract it calls into; also bounded and labeled, riding just below the
    caller facts.

    ``include_style_context`` defaults to ``False``. An interleaved A/B on a
    real TypeScript and a real Ruby repo (both arms under identical conditions)
    found that injecting the team-idiom/principles guidance plus a sibling
    canonical witness into this correctness-only prompt measurably *lowers*
    recall on unguarded-deref / dropped-await / off-by-one defects with no
    false-positive benefit: the style context crowds out the bug signal the
    reviewer is told to ignore for style anyway. The flag is kept so a caller
    that genuinely wants convention context (e.g. a future style-aware lens)
    can opt back in.
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
        "",
        "Before you return, work through this checklist against the CHANGED lines "
        "below - these are the defects most often missed. For EACH dereference and "
        "call in the change, either flag a defect or satisfy yourself it is safe:",
        "1. Optional/absent-on-miss lookups dereferenced without a guard: a "
        "`Map.get(...)` / `dict.get(...)` / `Array.find(...)` / `.find` / "
        "`.find_by` / `[...]` hash lookup whose result is then used (`.field`, "
        "`[...]`, a call, arithmetic) with no preceding presence check. These "
        "return `undefined`/`nil`/`None` on a miss, so the deref throws or yields "
        "garbage. `Map.get` is ALWAYS optional by language rule - you do not need "
        "the value's type to know this. A lookup that supplies its own fallback "
        "(`hash.fetch(k, default)`, `dict.get(k, default)`, `lookup ?? x`, "
        "`lookup || x`, `lookup&.field`, `lookup?.field`) is already guarded - "
        "do NOT flag those.",
        "2. Nilable/undefined receivers dereferenced without `?.` / `&.` / an "
        "explicit guard - INCLUDING any value a nearby comment, parameter name, or "
        "signature marks as possibly null/nil/None. An earlier guard counts even "
        "when it is OUTSIDE the changed lines you can see: if the receiver was "
        "already null-checked (`if (!x) return` / `return unless x` / `x ||= ...` "
        "/ an early raise) before the deref, it is safe - do NOT assume it is "
        "unguarded just because the guard line is not in the diff.",
        "3. A Promise/async/coroutine-returning call whose result is used as a "
        "plain value (a dropped `await`): the variable holds a Promise, not the "
        "resolved value, so field access or further use is wrong.",
        "4. Off-by-one and index/bounds errors: a loop or index condition using "
        "`<=` where `<` is meant against a `.length`/`.size`/count (reads one "
        "past the last element), an index that can run past the end, or slice/"
        "substring bounds that overrun.",
        "5. An assignment (`=`) used where a comparison (`==` / `===` / `.eql?`) "
        "was intended, inside an `if` / `while` / ternary condition - it mutates "
        "and is always truthy.",
        "6. Unreachable or dead code after a `return` / `throw` / `raise` / "
        "`break` / `continue`; and early-return, error, or empty branches that "
        "are skipped, fall through, or return the wrong value.",
        "7. Inverted conditions and wrong comparison/boolean operators - a check "
        "or a returned boolean that is the opposite of what the name or intent "
        "implies.",
        "Flagging one of these when present is the whole point of this review; do "
        "not skip a deref, index, or condition just because the change looks "
        "otherwise reasonable.",
    ]

    if include_style_context:
        guidance = _load_guidance(profile_dir)
        if guidance:
            sections.append("")
            sections.append("Project guidance (context, not a checklist):")
            sections.append(guidance)

    if intent_tokens:
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        clean_tokens = [sanitize_for_chameleon_context(t) for t in intent_tokens]
        kept: list[str] = []
        used = 0
        for tok in clean_tokens:
            if not kept and len(tok) > _INTENT_CHAR_CAP:
                # A single oversized token (e.g. a pasted 5000-digit numeral) would
                # otherwise ride in unbounded, since the first token is always kept;
                # cap it so one token cannot blow the intent budget.
                kept.append(tok[:_INTENT_CHAR_CAP])
                break
            extra = (2 if kept else 0) + len(tok)  # ", " separator
            if kept and used + extra > _INTENT_CHAR_CAP:
                break
            kept.append(tok)
            used += extra
        joined = ", ".join(kept)
        if len(kept) < len(clean_tokens):
            joined += f", ... (+{len(clean_tokens) - len(kept)} more)"
        sections.append("")
        sections.append(
            "The user's request for this work mentioned these specific values, "
            "identifiers, and strings. Verify the changed code is consistent "
            "with each one (a wrong constant, a renamed identifier, a "
            "mismatched string is a finding):"
        )
        sections.append(joined)

    if caller_facts:
        sections.append("")
        sections.append(caller_facts)
        # The caller facts are not just context: a change that breaks one of these
        # call sites is a correctness defect, so the reviewer must diff the
        # contract against them. Kept within the correctness remit (a broken caller
        # is a real bug), unlike style/convention which stays out of this prompt.
        sections.append(
            "If a change below alters one of these functions' signature (parameter "
            "list / arity), return shape, or the errors it throws/raises, check each "
            "listed call site and flag a finding for any caller the change would "
            "break: a wrong argument count, a removed return field the caller reads, "
            "or a newly-thrown error the caller does not handle. A caller the change "
            "breaks is a correctness defect, not a style note."
        )

    if transitive_facts:
        sections.append("")
        sections.append(transitive_facts)
        sections.append(
            "These chains show what a change transitively reaches (the entry points "
            "and intermediate callers above each changed function). Use them to reason "
            "about cross-module blast radius: if the change alters a behavior the chain "
            "depends on (an invariant, a return contract, an error), trace it up the "
            "chain and flag the caller it would break."
        )

    if imported_defs:
        sections.append("")
        sections.append(imported_defs)
        sections.append(
            "If the changed code calls one of these imported symbols with the wrong "
            "argument count, a wrong argument type, or reads a return field the "
            "signature does not provide, flag a finding: a call that does not match "
            "its imported contract is a correctness defect, not a style note."
        )

    for fd in diffs:
        sections.append("")
        header = f"=== {fd.rel_path}"
        if fd.is_whole_file:
            header += " (full file; no diff available)"
        else:
            header += " (unified diff vs HEAD)"
        header += " ==="
        sections.append(header)
        if include_style_context:
            witness = _witness_for(repo_root, fd.archetype)
            if witness:
                sections.append(f"Sibling reference for {fd.rel_path}:")
                sections.append(witness)
                sections.append("")
        sections.append(
            "Now apply the checklist to the change below. For every dereference, "
            "optional lookup (`Map.get` / `.find` / `.find_by` / hash index), "
            "possibly-null receiver, awaited call, index bound, and condition in "
            "these lines, either flag a defect or confirm it is guarded. Treat any "
            "surrounding project context as background only - it never makes an "
            "unguarded deref, a dropped await, or an out-of-bounds index safe."
        )
        sections.append(fd.diff_text)

    return "\n".join(sections)


def _stream_json_texts(stdout: str) -> list[str]:
    """The assistant ``result``/``text`` strings a ``claude -p --output-format
    stream-json`` run emitted, in emission order.

    The reviewer answers in JSON, but stream-json wraps that answer inside
    assistant ``result``/``text`` blocks, and the raw stdout ALSO carries
    structural arrays (the system-init ``tools`` list, message ``content``
    arrays). A JSON scan of the raw stdout therefore locks onto the wrong ``[``
    and never sees the model's answer. Harvest the model's own text blocks first,
    then scan those. Shared by ``_parse_findings_status`` and the round-3 refuter
    so both parse the answer, not the envelope.
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
    return texts


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
    for text in reversed(_stream_json_texts(stdout)):
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

    A single-item prompt ("one object per item") frequently draws a lone JSON
    OBJECT reply instead of a one-element array, especially from smaller judge
    models. Accept that shape too: decode the first top-level object and wrap it.
    Without this a confirmed finding returned as a bare ``{...}`` is silently
    dropped and the spawn budget is burned for nothing.

    Prefer a genuine top-level array whenever one decodes, even if a ``{`` (valid
    or junk) appears earlier in prose -- a brace in the surrounding sentence must
    never shadow the findings array. The lone-object shape wins only when there is
    no decodable array, OR the object is the outer container and the first ``[``
    sits INSIDE its span (a findings object with an array-valued field).
    """
    decoder = json.JSONDecoder()
    start = text.find("[")
    obj_start = text.find("{")

    arr: list | None = None
    if start != -1:
        try:
            value, _ = decoder.raw_decode(text[start:])
            if isinstance(value, list):
                arr = value
        except json.JSONDecodeError:
            pass

    obj: dict | None = None
    obj_end = -1
    if obj_start != -1:
        try:
            value, obj_end = decoder.raw_decode(text[obj_start:])
            if isinstance(value, dict):
                obj = value
        except json.JSONDecodeError:
            pass

    # The object is the top-level container (wrap it) only when no array decoded,
    # or when the object starts before the array AND its decoded span contains the
    # array (the `[` is a nested field, not the findings list).
    if obj is not None and (
        arr is None or (start != -1 and obj_start < start and obj_start + obj_end > start)
    ):
        return [obj]
    if arr is not None:
        return arr
    return None


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
        # bool is an int subclass: a JSON `true` would coerce to 1, so reject it.
        if isinstance(raw_conf, bool):
            confidence = 0.0
        else:
            try:
                confidence = float(raw_conf)
            except (TypeError, ValueError):
                confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        file = item.get("file") if isinstance(item.get("file"), str) else None
        raw_line = item.get("line")
        line = raw_line if isinstance(raw_line, int) and not isinstance(raw_line, bool) else None
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


# Grounding-event families the reviewer emits ONCE PER SPAWN to report what
# context was available (caller facts / imported defs / transitive chains). They
# ride the event sink the way a failure would, but they are NOT degradations: a
# spawn that ran fine but had no calls index still emits judge_defs_skipped_no_index.
# A consumer that treats them as "reviewer failed to spawn" (the doctor health
# check, the SessionStart banner) reports a phantom failure for a healthy reviewer.
JUDGE_GROUNDING_FAMILIES = ("judge_facts_", "judge_defs_", "judge_transitive_")


def is_grounding_event(reason: object) -> bool:
    """True when ``reason`` is a per-spawn grounding event, not a real failure."""
    return isinstance(reason, str) and reason.startswith(JUDGE_GROUNDING_FAMILIES)


def grounding_family(kind: object) -> str | None:
    """Return the ``JUDGE_GROUNDING_FAMILIES`` prefix ``kind`` starts with, else
    None. The canonical home for the families lives here, so both the sync gate
    and the detached-child sink translate a grounding event to its own check
    (``judge_facts`` / ``judge_defs`` / ``judge_transitive``) the same way,
    instead of one path misfiling defs/transitive events as a spawn degradation.
    """
    if not isinstance(kind, str):
        return None
    for fam in JUDGE_GROUNDING_FAMILIES:
        if kind.startswith(fam):
            return fam
    return None


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


# True only inside the detached judge_async child process. Only that child may
# take the generous fallback budget below: a spawn inside the Stop hook never
# can, because the stop-backstop wrapper caps the whole hook at 55s and the
# host kills hooks at 60s regardless of any threshold override.
_RUNNING_DETACHED = False


def mark_detached_run() -> None:
    """Record that this process is the detached async judge child."""
    global _RUNNING_DETACHED
    _RUNNING_DETACHED = True


def detached_spawn_budget_seconds() -> int:
    """Wall-clock budget the detached judge child applies to its reviewer spawn.

    With bare auth known failed the child runs the plain (non --bare) spawn,
    which pays the full session primer before it can review and cannot finish
    inside the short sync budget; running detached, it can afford the generous
    fallback budget instead. Also readable from the parent process: the async
    in-flight orphan-sweep window scales with the same number.
    """
    if _bare_auth_known_failed():
        return threshold_int("CORRECTNESS_JUDGE_FALLBACK_TIMEOUT_SECONDS")
    return threshold_int("CORRECTNESS_JUDGE_TIMEOUT_SECONDS")


def _reviewer_timeout_seconds() -> int:
    """Effective budget for one reviewer spawn: synchronous spawns keep the
    short budget; only the detached child may take the fallback budget."""
    if _RUNNING_DETACHED:
        return detached_spawn_budget_seconds()
    return threshold_int("CORRECTNESS_JUDGE_TIMEOUT_SECONDS")


def _spawn_reviewer_status(
    prompt: str, cwd: Path, *, model: str | None = None, timeout_s: int | None = None
) -> tuple[str | None, str | None]:
    """Spawn ``claude -p`` for a one-shot review, returning ``(stdout, reason)``.

    Runtime-owned spawn wrapper (the journey-harness wrapper under ``tests/`` is
    never importable by the shipped plugin). Hard wall-clock budget, no tools,
    minimal turns, output captured. On success returns ``(stdout, None)``; on
    failure returns ``(None, reason)`` where reason is one of ``spawn_timeout``,
    ``spawn_exec_error``, ``spawn_nonzero_exit`` so the caller can record WHY a
    review silently produced nothing instead of collapsing every failure mode
    into an indistinguishable None.

    ``model`` and ``timeout_s`` override the env-var defaults when provided;
    pass nothing to get the standard judge behavior.
    """
    if timeout_s is None:
        timeout_s = _reviewer_timeout_seconds()
    resolved_model = (
        model if model is not None else os.environ.get("CHAMELEON_JUDGE_MODEL", "sonnet")
    )
    # SECURITY: the prompt embeds file diffs and is fed on STDIN, never as an
    # argv positional. A `-p <prompt>` argument is visible in `ps aux` /
    # /proc/<pid>/cmdline to any local process for the spawn's lifetime, which
    # would expose whatever the reviewed diff contains (a secret a developer
    # edited, a `.env` line) to a co-located user. `claude -p` reads the prompt
    # from stdin when no positional is given.
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
        "--model",
        resolved_model,
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
                    input=prompt,
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


def _spawn_reviewer(
    prompt: str, cwd: Path, *, model: str | None = None, timeout_s: int | None = None
) -> str | None:
    """Stdout-only view of ``_spawn_reviewer_status`` (None on any failure)."""
    return _spawn_reviewer_status(prompt, cwd, model=model, timeout_s=timeout_s)[0]


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
        # SECURITY: never diff a secret-bearing file into the review prompt.
        # reconstruct_diff reads via raw `git diff` / read_bytes (not safe_open),
        # so without this filter a `.env` / `.ssh` / credential file a developer
        # edited would be reconstructed and embedded in the reviewer's input.
        # Mirrors safe_open's forbidden-segment set; a config diff has no review
        # value anyway.
        if is_forbidden_segment_path(rel):
            continue
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
        # One config read for all three grounding flags (each default on; an
        # unreadable config fails open to on).
        try:
            from chameleon_mcp.profile.config import load_config

            _enf = load_config(profile_dir).enforcement
            facts_enabled = _enf.judge_crossfile_facts
            defs_enabled = _enf.judge_imported_definitions
            trans_enabled = _enf.judge_transitive_impact
        except Exception:
            facts_enabled = defs_enabled = trans_enabled = True
        caller_facts: str | None = None
        if not facts_enabled:
            _sink("judge_facts_skipped_disabled")
        else:
            block = caller_facts_for_diffs(repo_root, diffs)
            caller_facts = block or None
            _sink("judge_facts_included" if block else "judge_facts_skipped_no_calls_index")
        # Forward definition hydration: the signatures of the symbols the change
        # imports. Gated on enforcement.judge_imported_definitions (default on;
        # unreadable config fails open to on). Additive, like the caller facts.
        imported_defs: str | None = None
        if not defs_enabled:
            _sink("judge_defs_skipped_disabled")
        else:
            defs_block = imported_definition_facts(repo_root, diffs)
            imported_defs = defs_block or None
            _sink("judge_defs_included" if defs_block else "judge_defs_skipped_no_index")
        # Multi-hop transitive caller-impact: the callers-of-callers chains
        # the one-hop facts don't carry. Gated on enforcement.judge_transitive_impact
        # (default on; unreadable config fails open to on). Bounded + fail-open.
        transitive_facts: str | None = None
        if not trans_enabled:
            _sink("judge_transitive_skipped_disabled")
        else:
            try:
                from chameleon_mcp.calls_index import load_calls_index

                trans_index = load_calls_index(repo_root)
            except Exception:
                trans_index = None
            if trans_index is None:
                _sink("judge_transitive_skipped_no_index")
            else:
                trans_block = caller_facts_transitive_for_diffs(repo_root, diffs, trans_index)
                transitive_facts = trans_block or None
                _sink(
                    "judge_transitive_included"
                    if trans_block
                    else "judge_transitive_skipped_no_chains"
                )
        prompt = build_prompt(
            repo_root,
            profile_dir,
            diffs,
            intent_tokens=intent_tokens,
            caller_facts=caller_facts,
            transitive_facts=transitive_facts,
            imported_defs=imported_defs,
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
