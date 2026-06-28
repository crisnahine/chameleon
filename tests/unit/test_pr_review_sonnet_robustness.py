"""pr-review must be faithfully executable by a Sonnet-class driving model.

A live Sonnet skill-execution run (Sonnet agents actually ran the procedure on a
real diff and self-reported what they dropped) surfaced three concrete drops, all
in chameleon-pr-review:

  1. Fan-out had no "Task tool unavailable" fallback, so a Sonnet subagent that
     could not dispatch reviewers rationalized a bypass instead of running inline.
  2. The "run lint_file on every changed file" rule was buried mid-step with no
     forcing function, and the word "source" let Sonnet sample out doc files, so
     the pre-archetype secret scan silently missed files.
  3. Step 2b had no branch for a null archetype, so Sonnet improvised the string
     "none".

These tests pin the fixes (a documented inline fallback, a coverage ledger +
N/N accounting line, and an explicit null-archetype branch). Assertions run on a
whitespace-normalized copy so they match regardless of line wrapping.
"""

from __future__ import annotations

from pathlib import Path

PR = Path(__file__).resolve().parents[2] / "skills" / "chameleon-pr-review" / "SKILL.md"


def _pr() -> str:
    return " ".join(PR.read_text(encoding="utf-8").split())


def test_fanout_has_task_unavailable_inline_fallback():
    t = _pr()
    assert "cannot dispatch in-session Task reviewers" in t
    assert "run the review single-pass inline" in t
    assert "fan-out-recommended-but-unavailable" in t
    # It must be framed as "do not skip / do not rationalize a bypass".
    assert "do NOT rationalize a bypass" in t


def test_step2_has_coverage_ledger_forcing_function():
    t = _pr()
    assert "Coverage ledger (forcing function)" in t
    assert "run the per-file passes on EVERY one" in t
    # The output must carry an explicit N/N accounting line.
    assert "lint_file run on N/N changed files" in t


def test_2b_runs_on_every_file_and_handles_null_archetype():
    t = _pr()
    # Reworded from "every changed source file" so doc/config files are not sampled out.
    assert "Run this on every changed FILE (source or not)" in t
    # The explicit null-archetype branch: pass a NON-NULL placeholder string so
    # the secret/sink scans still run (passing null/omitting returns early before
    # them -- archetype is a required str). Sonnet's improvised "none" was correct.
    assert "`archetype` null/none" in t
    assert "pass a non-null placeholder archetype STRING" in t
    assert "Do NOT pass `null` and do NOT omit the `archetype` argument" in t
    assert "return early BEFORE the secret and sink scans run" in t
