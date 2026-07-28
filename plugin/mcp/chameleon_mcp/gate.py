"""Headless, diff-scoped conventions gate.

Every enforcement surface chameleon has runs inside an interactive session, so
a write that never passed through a PreToolUse hook is ungoverned: a
hand-authored commit, a session with chameleon disabled, paused or untrusted, a
different agent. This is the backstop for those, and only those -- guidance that
arrives before the write is strictly better, and nothing here changes that.

Three properties are load-bearing rather than stylistic:

DIFF-SCOPED. The count is what a commit INTRODUCED, computed by
``commit_scope`` -- the same module the effectiveness studies use, so the gate
and the measurement of the gate cannot disagree. Whole-file counting was
measured at 5.2-5.7x the introduced count on the dogfood repos, which would fail
an author for violations they inherited.

SOFT BY DEFAULT. ``lint_file`` is heuristic regex extraction, not a parser, and
this repo's architecture deliberately keeps CI a build/lint pipeline rather than
a correctness gate. Findings report at exit 0 unless ``--strict`` is passed.

TRUST IS NOT OPTIONAL. Without a grant, ``lint_file`` returns no convention
findings at all, so an ungranted run would print a clean build for a repo it
never linted. That exits 2 (configuration error), never 0. Granting trust inside
CI is a real decision with a real cost -- it machine-stamps the human read the
trust model asks for, and lets a PR that edits the profile feed its own strings
into the gate's output -- so the caller must make it explicitly rather than
inheriting it as a side effect of running this.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.commit_scope import PER_EDIT_SUPPRESSED

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNUSABLE = 2

# Generous next to the 5s the per-edit path allows itself: this runs once per CI
# job, not on a hook hot path, and merge-base over a deep history is genuinely
# slower than the reads a hook makes. It exists to bound a hang, not to tune.
_GIT_TIMEOUT_SECONDS = 60


@dataclass
class GateResult:
    """What the gate concluded, separated from how it is printed."""

    introduced: int = 0
    findings: list[dict] = field(default_factory=list)
    by_rule: dict[str, int] = field(default_factory=dict)
    untrusted: bool = False
    exit_code: int = EXIT_OK


def decide(findings: list[dict], *, strict: bool, trusted: bool = True) -> GateResult:
    """Turn raw lint rows into a verdict.

    Kept free of I/O so the policy is testable on its own: what counts, what
    fails a build, and what an unusable configuration looks like.
    """
    if not trusted:
        return GateResult(untrusted=True, exit_code=EXIT_UNUSABLE)
    counted = [f for f in findings if f.get("rule") not in PER_EDIT_SUPPRESSED]
    by_rule = dict(Counter(str(f.get("rule")) for f in counted))
    return GateResult(
        introduced=len(counted),
        findings=counted,
        by_rule=by_rule,
        untrusted=False,
        exit_code=EXIT_FINDINGS if (counted and strict) else EXIT_OK,
    )


def render_text(result: GateResult) -> str:
    """Human-readable report. Names the scope, because a reader who takes an
    introduced-only count for a whole-file count reads a low number as proof the
    files are clean."""
    if result.untrusted:
        return (
            "chameleon gate: profile is not trusted here, so no convention rules ran.\n"
            "  Grant trust for this checkout before relying on this gate; a silent\n"
            "  pass would be indistinguishable from a clean diff."
        )
    if not result.introduced:
        return "chameleon gate: no new convention violations introduced by this diff."
    lines = [
        f"chameleon gate: {result.introduced} convention violation(s) "
        "introduced by this diff (pre-existing violations are not counted).",
        "",
    ]
    for rule, count in sorted(result.by_rule.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {rule}  ({count})")
        for f in result.findings:
            if f.get("rule") != rule:
                continue
            where = str(f.get("file") or "")
            lines.append(f"    {where}  {f.get('message') or ''}".rstrip())
    return "\n".join(lines)


class GateUnusable(Exception):
    """The gate could not run. Never reported as a clean build."""


def _run_git(repo: Path, args: list[str]):
    """Run ``git`` in ``repo`` under a timeout, returning the process or None.

    Bytes, not text: a path is a byte string, and decoding git's stdout with the
    ambient locale would raise on a name this gate is specifically meant to keep
    in the change set. Callers decode what they know the shape of.

    None means the spawn itself never produced a result -- git missing from PATH,
    a hung transfer, a ref carrying an embedded null. Every caller turns that into
    GateUnusable rather than an empty result, because an exception escaping here
    would exit 1, and exit 1 under --strict is the code that means "this diff
    introduced violations".
    """
    import subprocess

    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _stderr(r) -> str:
    return (r.stderr or b"").decode("utf-8", errors="replace").strip()[:200]


def _merge_base(repo: Path, base: str, head: str) -> str:
    """The commit the branch actually diverged at.

    The file list uses the three-dot range, so the baseline blob has to come
    from the SAME commit. Reading it at the base TIP instead would count a
    violation someone else fixed on base after the branch point as introduced by
    this diff, and subtract one they added there.
    """
    r = _run_git(repo, ["merge-base", base, head])
    if r is None:
        raise GateUnusable(f"git merge-base {base}...{head} did not run")
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    if r.returncode != 0 or not out:
        raise GateUnusable(f"no merge base between {base!r} and {head!r}")
    return out


def _changed_source_files(repo: Path, base: str, head: str) -> list[str]:
    from chameleon_mcp.lint_engine import detect_language

    # -z, not line-splitting: git C-quotes any path holding a non-ASCII byte, a
    # backslash, a quote or a newline (`"caf\303\251/sub/plain.ts"`), and
    # detect_language is a plain endswith -- a quoted name resolves to no language
    # and the file leaves the change set silently. That is this module's one
    # forbidden outcome, a clean report for a file nothing linted, and unlike a
    # failed diff it raises no GateUnusable. One accented DIRECTORY component
    # un-gates every ordinary file beneath it. core.quotePath=false alone covers
    # only the non-ASCII case -- it governs bytes >= 0x80 and nothing else, so a
    # quote, a backslash or a newline in a name stays quoted with it set. -z is
    # the only form that emits every path raw, and the NUL separator is also what
    # makes an embedded newline unambiguous.
    r = _run_git(repo, ["diff", "-z", "--name-only", f"{base}...{head}"])
    if r is None:
        raise GateUnusable(f"git diff {base}...{head} did not run (is git on PATH?)")
    if r.returncode != 0:
        # A bad revision, a shallow clone missing the base, or no git at all.
        # Returning [] here would render as "no new violations" at exit 0 -- a
        # green build for a diff nothing ever looked at.
        raise GateUnusable(f"git diff {base}...{head} failed: {_stderr(r)}")
    out = []
    for raw in (r.stdout or b"").split(b"\0"):
        # surrogateescape round-trips through os.fsencode, so the name handed back
        # to `git show` is byte-for-byte the one git emitted even when it is not
        # valid UTF-8. Decoding strictly would raise and take the whole gate down;
        # decoding lossily would produce a path git cannot resolve.
        rel = raw.decode("utf-8", errors="surrogateescape").strip()
        if rel and detect_language(rel):
            out.append(rel)
    return out


def collect(repo: Path, base: str, head: str) -> tuple[list[dict], bool]:
    """Lint every changed source file at ``head`` and at ``base``, keep the net-new.

    Returns (introduced rows, trusted). A file that cannot be read at either
    revision is skipped rather than counted: a new file has no baseline, so its
    rows ARE introduced, while an unreadable one is unknown and must not be
    guessed at in either direction.
    """
    from chameleon_mcp.commit_scope import BlobUnreadable, actionable, blob_at, net_new
    from chameleon_mcp.tools import _compute_repo_id, get_archetype, lint_file

    introduced: list[dict] = []
    trusted = True
    max_bytes = threshold_int("GATE_MAX_FILE_BYTES")
    repo_id = _compute_repo_id(repo)
    # Same commit the file list was computed against; see _merge_base.
    baseline_rev = _merge_base(repo, base, head)
    for rel in _changed_source_files(repo, base, head):
        try:
            current_src = blob_at(repo, head, rel, max_bytes=max_bytes)
        except BlobUnreadable:
            continue
        if current_src is None:
            # Deleted by this diff: there is no current version to lint.
            continue
        try:
            # The archetype keys the whole comparison; a path in its place makes
            # the engine stub with "no ast_query for archetype", which reads as
            # a clean file rather than an unlinted one.
            arch_data = get_archetype(repo_id, str(repo / rel)).get("data") or {}
            # get_archetype reports an ungranted profile as data["status"], not
            # a trust_state field. Reading the wrong key made this early exit
            # dead code -- lint_file's own untrusted status still caught it one
            # step later, so the exit code was right for the wrong reason.
            if arch_data.get("status") == "untrusted":
                trusted = False
                break
            arch = arch_data.get("archetype") or "none"
            data = lint_file(repo_id, arch, current_src, str(repo / rel)).get("data") or {}
        except Exception:
            continue
        if data.get("status") == "untrusted":
            trusted = False
            break
        # A profile that is absent, torn, or on an unreadable schema makes the
        # engine STUB rather than fail: it returns zero violations, which is
        # indistinguishable from a clean file. Counting a stub as clean is how a
        # CI gate goes green on a repo it never linted.
        if data.get("stub"):
            # A stubbed envelope carries stub_reason; noop_reason is the sibling
            # field a NON-stub envelope uses to narrate a skipped AST pass, and
            # reading only that one left every stub reported as "no reason given"
            # -- the operator saw the exit code but not what to fix.
            why = data.get("stub_reason") or data.get("noop_reason") or "no reason given"
            raise GateUnusable(
                f"lint stubbed on {rel} ({why}); "
                "the profile is missing or unusable, so nothing was checked"
            )
        rows = actionable(data.get("violations") or [])
        # An empty baseline means "every row here is introduced", so it may only
        # be used when git CONFIRMS the path did not exist at the base commit.
        # An unreadable baseline -- over the size cap, git failed -- is unknown,
        # and treating it as absent scores the file's whole inherited load as
        # introduced. That is the 5.2-5.7x inflation this module exists to remove,
        # and under --strict it fails a build over violations the diff never made.
        try:
            baseline_src = blob_at(repo, baseline_rev, rel, max_bytes=max_bytes)
        except BlobUnreadable:
            continue
        baseline_rows: list[dict] = []
        if baseline_src is not None:
            try:
                prior = lint_file(repo_id, arch, baseline_src, str(repo / rel)).get("data") or {}
                baseline_rows = actionable(prior.get("violations") or [])
            except Exception:
                # The baseline lint itself failed, so its rows are unknown for the
                # same reason an unreadable blob's are. Skip rather than subtract
                # nothing from everything.
                continue
        new_rows, _ = net_new(rows, baseline_rows)
        for row in new_rows:
            row.setdefault("file", rel)
        introduced.extend(new_rows)
    return introduced, trusted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chameleon-gate",
        description=(
            "Report the convention violations a diff INTRODUCED. Advisory by "
            "default; --strict exits non-zero when the diff introduces any."
        ),
    )
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--base", required=True, help="base revision to compare against")
    parser.add_argument("--head", default="HEAD", help="revision under test (default: HEAD)")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when the diff introduces violations (default: report at exit 0)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    try:
        rows, trusted = collect(repo, args.base, args.head)
    except GateUnusable as exc:
        # Exit 2, never 0: a gate that could not run must not be mistaken for a
        # gate that ran and found nothing.
        print(f"chameleon gate: could not run -- {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        # And never exit 1 for a non-finding reason. An escaping exception would
        # exit 1, which under --strict is EXIT_FINDINGS -- "this diff introduced
        # violations". A CI step branching on the code cannot tell that apart
        # from a real result, so any unforeseen failure lands on 2 as well.
        print(f"chameleon gate: could not run -- {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    result = decide(rows, strict=args.strict, trusted=trusted)
    if args.json:
        print(
            json.dumps(
                {
                    "introduced": result.introduced,
                    "by_rule": result.by_rule,
                    "untrusted": result.untrusted,
                    "findings": result.findings,
                },
                separators=(",", ":"),
            )
        )
    else:
        print(render_text(result))
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover -- console-script entry
    sys.exit(main())
