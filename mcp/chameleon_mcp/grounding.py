"""Execution-grounding helpers (R4): tie a review finding to a runnable check.

The single highest-leverage technique across the 2026 evidence for a trustworthy
machine review gate is to let a finding *block* only when a runnable check backs
it (a type error, a failing test, a lint rule) and to keep every free-floating
LLM opinion advisory. An execution-grounded finding has a measured false-positive
rate near zero by construction: the check either failed or it did not.

This module parses the runnable-check outputs into a structured, per-file form so
a consumer (the auto-pass router, the PR-review gate) can ask "is this file's
finding grounded in a real failure?" rather than trusting a model's say-so. Pure
functions only; running the checks (the sandbox, the subprocess) lives in the
tool layer, the same split the auto-pass router uses for git.
"""

from __future__ import annotations

import re

# `tsc --noEmit` default (non-pretty) diagnostic line:
#   path(line,col): error TS2322: message
# The path may contain spaces; the `(line,col): error TSxxxx:` shape anchors the
# parse, so everything before the trailing `(L,C):` is the file path.
_TSC_LINE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<message>.*)$"
)


def parse_tsc_output(text: str) -> list[dict]:
    """Parse ``tsc --noEmit`` output into per-diagnostic rows.

    Each error line yields ``{file, line, col, code, message}``. Summary lines
    ("Found N errors..."), blank lines, and anything not matching the diagnostic
    shape are skipped, so a noisy or partial compiler run still fails open to the
    errors it could parse rather than raising.
    """
    rows: list[dict] = []
    for raw in (text or "").splitlines():
        m = _TSC_LINE.match(raw.rstrip())
        if not m:
            continue
        rows.append(
            {
                "file": m.group("file"),
                "line": int(m.group("line")),
                "col": int(m.group("col")),
                "code": m.group("code"),
                "message": m.group("message"),
            }
        )
    return rows


def files_with_type_errors(text: str) -> set[str]:
    """The set of files ``tsc`` reported at least one error in.

    This is the grounding signal the gate consumes: a finding on a file in this
    set is backed by a real compiler failure, not a model's guess.
    """
    return {row["file"] for row in parse_tsc_output(text)}
