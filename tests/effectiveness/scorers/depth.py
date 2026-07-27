"""Depth scorer: does conformance survive session depth, or decay with it?

Every other scorer reads the FINAL tree, which answers "was the end state good"
and cannot answer "did guidance hold". Those differ. A model that infers the
repo's conventions from context at turn 2 and drifts off them by turn 12 can
still land a passable final diff, and a whole-tree score reports that as a win
while the mechanism under test quietly failed.

This scorer replays the session's file-authoring events IN ORDER, lints each
file as it was actually written, and reports conformance as a function of
position. That is the measurement chameleon's design stands or falls on: a
convention delivered once at session start decays as context fills, while one
re-delivered before every edit should not. If the decay slope is the same with
the plugin off and on, per-edit injection is buying nothing a CLAUDE.md line
would not, which is the competitive question worth being able to answer.

Reported per cell:
  depth_files_scored      authored files that could be linted
  depth_early_viol        mean violations per file, FIRST half of authoring order
  depth_late_viol         mean violations per file, SECOND half
  depth_decay             late - early. POSITIVE = conformance degraded with depth
  depth_last_turn         turn index of the final authoring event (session reach)

Unscored (with reason) rather than zero whenever the order cannot be recovered
or too few files were authored to have halves -- a fabricated 0 decay would read
as "perfectly flat", the exact claim this scorer exists to test.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.effectiveness.scorers.base import ScoreContext, unscored
from tests.effectiveness.scorers.convention import (
    _LINTABLE_SUFFIXES,
    _lint,
    _pattern_context,
)

# Halves need at least this many files to mean anything. Below it the "trend"
# is one file against one file, which is noise wearing a slope's clothing.
_MIN_FILES_FOR_HALVES = 4


def _authoring_events(transcript_path: Path) -> list[tuple[int, str, str]]:
    """(turn_index, rel_or_abs_path, content) per Write, in transcript order.

    Write only, deliberately. A Write carries the whole authored file, so it can
    be linted exactly as the model produced it. An Edit carries a fragment whose
    surrounding file state would have to be reconstructed, and a wrong
    reconstruction would silently corrupt the very trend being measured -- so
    edits are counted toward the turn index but never scored.
    """
    events: list[tuple[int, str, str]] = []
    turn = 0
    try:
        raw = transcript_path.read_text(errors="replace")
    except OSError:
        return events
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        saw_tool = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            saw_tool = True
            if block.get("name") != "Write":
                continue
            inp = block.get("input") or {}
            path, body = inp.get("file_path"), inp.get("content")
            if isinstance(path, str) and isinstance(body, str):
                events.append((turn, path, body))
        if saw_tool:
            turn += 1
    return events


def score(ctx: ScoreContext) -> dict:
    events = _authoring_events(ctx.transcript_path)
    if not events:
        return unscored("no Write events recoverable from transcript")

    lintable = [(t, p, c) for (t, p, c) in events if str(p).lower().endswith(_LINTABLE_SUFFIXES)]
    if not lintable:
        return unscored("session authored no lintable source files")

    # One file authored several times counts at its LAST authoring: that is the
    # version the session stood behind, and counting a scratch first draft would
    # charge the early half for work the model itself superseded.
    latest: dict[str, tuple[int, str]] = {}
    for turn, path, body in lintable:
        latest[str(path)] = (turn, body)
    ordered = sorted(((t, p, c) for p, (t, c) in latest.items()), key=lambda e: e[0])

    if len(ordered) < _MIN_FILES_FOR_HALVES:
        return unscored(
            f"only {len(ordered)} authored file(s); need {_MIN_FILES_FOR_HALVES} for halves"
        )

    counts: list[int] = []
    for _turn, path, body in ordered:
        try:
            # get_pattern_context nests the name one level deeper than it looks:
            # data.archetype is an envelope whose own .archetype is the string.
            # Passing the envelope makes lint_file stub out with "archetype must
            # be a string", which reads as "nothing to score" rather than as the
            # type error it is -- so every cell came back unscored.
            arch = ((_pattern_context(str(path)).get("data") or {}).get("archetype") or {}).get(
                "archetype"
            ) or "none"
            data = (
                _lint(
                    repo=str(ctx.worktree), archetype=arch, content=body, file_path=str(path)
                ).get("data")
                or {}
            )
            if data.get("stub"):
                continue
            counts.append(len(data.get("violations") or []))
        except Exception:
            continue

    if len(counts) < _MIN_FILES_FOR_HALVES:
        return unscored(f"only {len(counts)} authored file(s) linted cleanly")

    mid = len(counts) // 2
    early = counts[:mid]
    late = counts[-mid:]  # trailing half; a middle file is dropped on odd counts
    early_mean = sum(early) / len(early)
    late_mean = sum(late) / len(late)

    return {
        "depth_files_scored": len(counts),
        "depth_early_viol": round(early_mean, 4),
        "depth_late_viol": round(late_mean, 4),
        "depth_decay": round(late_mean - early_mean, 4),
        "depth_last_turn": ordered[-1][0],
        # A cell where nothing was ever flagged has no range for decay to show
        # up in, so its 0.0 means "the task tripped no rules", not "conformance
        # held". Those read identically in a scoreboard and mean opposite things
        # about the instrument, so the distinction is carried in the row rather
        # than left for a reader to infer.
        "depth_floor": bool(sum(counts) == 0),
    }
