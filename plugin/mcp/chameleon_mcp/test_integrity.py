"""Turn-end test-integrity advisory: deterministic, zero-LLM, fail-open.

The auto-pass router already computes test-weakening signals (added skip markers,
an assertion-count drop, net test-line deletion) from a diff, but only the router
consumes them. This surfaces the same signals to the author at turn end -- when
the turn ALSO changed live source -- so the "why did you skip/delete this test?"
reviewer comment is fixed before the PR. No model spawn, no per-turn cost beyond
one ``git diff``; fails open to no advisory on any error.
"""

from __future__ import annotations

from pathlib import Path

# Kept in sync with scan_diff_signals' default so the reasons reported match the
# threshold that actually fired the weakening marker.
_ASSERTION_DELTA_FLOOR = -3


def build_turn_diff(repo_root: Path, edited_files: list[str]) -> str:
    """Combined ``git diff HEAD`` over the turn's edited files, or "". Fail-open.

    Scoped to the files the turn touched so a weakening left uncommitted on an
    untouched file does not re-fire on every later turn.
    """
    try:
        from chameleon_mcp import judge

        root = Path(repo_root)
        if not edited_files or not judge._git_available(root):
            return ""
        rels: list[str] = []
        for f in edited_files:
            try:
                rels.append(Path(f).resolve().relative_to(root.resolve()).as_posix())
            except (ValueError, OSError):
                continue
        if not rels:
            return ""
        result = judge._run_git(["diff", "HEAD", "--", *rels], cwd=root)
        if result is not None and result.returncode == 0:
            return result.stdout or ""
        return ""
    except Exception:
        return ""


def assess_test_weakening(diff_text: str, edited_files: list[str]) -> dict | None:
    """Fired weakening signals when the turn weakened tests AND changed live
    (non-test) source, else None. Pure test cleanup (no source change) -> None.
    """
    try:
        from chameleon_mcp.autopass import _is_test_file, scan_diff_signals

        if not diff_text:
            return None
        signals = scan_diff_signals(diff_text, assertion_delta_floor=_ASSERTION_DELTA_FLOOR)
        if not signals.get("test_weakening_markers"):
            return None
        if not any(not _is_test_file(f) for f in edited_files):
            return None

        reasons: list[str] = []
        skips = signals.get("added_skip_markers") or 0
        if skips:
            reasons.append(f"added {skips} test skip/todo marker{'s' if skips != 1 else ''}")
        delta = signals.get("assertion_delta", 0)
        if delta <= _ASSERTION_DELTA_FLOOR:
            reasons.append(f"assertion count dropped by {abs(delta)}")
        # The marker fired; if neither explicit arm explains it, net test-line
        # deletion did.
        if not reasons:
            reasons.append("net test lines removed")
        # Name the test files the turn touched so the advisory points at WHERE to
        # restore coverage instead of only WHAT was weakened. The diff was scoped
        # to the turn's edited files, so the test files among them are the ones
        # carrying the weakening signal. Bounded so a large diff cannot bloat the
        # line; the caller sanitizes each name at render.
        weakened = [f for f in edited_files if _is_test_file(f)][:5]
        return {"signals": signals, "reasons": reasons, "test_files": weakened}
    except Exception:
        return None


def format_test_integrity_advisory(assessment: dict | None) -> list[str]:
    if not assessment:
        return []
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    reasons = "; ".join(assessment.get("reasons") or [])
    test_files = assessment.get("test_files") or []
    where = ""
    if test_files:
        names = ", ".join(sanitize_for_chameleon_context(Path(f).name) for f in test_files)
        where = f" in {names}"
    return [
        "[\U0001f98e chameleon: test integrity]",
        sanitize_for_chameleon_context(
            f"this turn changed source and weakened tests{where} ({reasons}) — "
            "restore the coverage or justify why it is safe."
        ),
    ]
