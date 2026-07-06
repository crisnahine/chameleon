"""Read-only surfacing of hook degraded-delivery events.

Chameleon hooks fail open. When guidance is not delivered there are three
classes, all already persisted, none surfaced as a cumulative count until now:

1. In-process advisor failure -- Python ran but the advisor raised. Recorded as
   a ``fail_open: true`` row in ``metrics.jsonl`` by ``metrics.emit_hook_metric``.
2. No-interpreter -- no Python >=3.11 / uv resolved, so Python never ran. The
   shell hook writes ``[ts] <hook> no-interpreter (...)`` to ``.hook_errors.log``.
3. Spawn-failed -- the interpreter resolved but the helper exited non-zero. The
   shell hook writes ``[ts] <hook> failed (python=...)`` to ``.hook_errors.log``.

This module owns READING those sources and combining them. It never writes. The
``parse_degradations`` line parser is shared with the SessionStart degraded
banner so the marker grammar has a single home.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from pathlib import Path

from chameleon_mcp import plugin_paths

# Matches the ISO-8601 Z timestamp the shell hooks prefix each fail-open line
# with: ``[2026-06-19T12:34:56Z] <hook> <reason>``.
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]")

# Bytes read from the tail of .hook_errors.log when summarizing. Degraded lines
# are short and rare on a healthy install, so a 256 KiB tail covers the window
# with margin; a broken install reads "many" either way. Tool-time read.
_LOG_TAIL_BYTES = 256 * 1024


def plugin_data_dir() -> Path:
    """Resolve the plugin data dir (override-aware via CHAMELEON_PLUGIN_DATA).

    Delegates to the canonical resolver so this module, ``metrics.jsonl``'s
    writer, and the SessionStart banner all agree on one location.
    """
    return plugin_paths.plugin_data_dir()


def hook_error_log_path() -> Path:
    """Path the shell hooks append fail-open lines to (override-aware).

    Mirrors ``hook_helper._hook_error_log_path``: ``CHAMELEON_HOOK_ERROR_LOG``
    when set, else ``<plugin_data>/.hook_errors.log`` -- the file the shell hooks
    write their no-interpreter and spawn-failed lines to in the common case.
    """
    override = os.environ.get("CHAMELEON_HOOK_ERROR_LOG")
    if override:
        return Path(override).expanduser()
    return plugin_data_dir() / ".hook_errors.log"


def _ts_in_window(ts: str, since_epoch: float) -> float | None:
    """Epoch seconds for an ISO-Z ``ts`` if at/after ``since_epoch``, else None."""
    try:
        when = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
    if when < since_epoch:
        return None
    return float(when)


def count_failopen_metrics(since_epoch: float) -> tuple[int, str | None]:
    """Count in-process advisor fail-opens in ``metrics.jsonl`` within the window.

    Returns ``(count, last_ts)`` over rows with ``fail_open is True`` whose ``ts``
    parses and is at/after ``since_epoch``. Missing/unreadable file -> ``(0, None)``;
    malformed lines skipped. Best-effort: never raises.
    """
    path = plugin_data_dir() / "metrics.jsonl"
    count = 0
    last_when = -1.0
    last_ts: str | None = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("fail_open") is not True:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, str):
                    continue
                when = _ts_in_window(ts, since_epoch)
                if when is None:
                    continue
                count += 1
                if when > last_when:
                    last_when = when
                    last_ts = ts
    except OSError:
        return 0, None
    return count, last_ts


def parse_degradations(text: str, since_epoch: float) -> tuple[int, int, str | None]:
    """Count no-interpreter and spawn-failed hook fail-opens in ``text``.

    ``text`` is a slice of ``.hook_errors.log``. Only lines whose ``[ts]`` prefix
    parses and is at or after ``since_epoch`` are counted; raw python stderr (no
    timestamp prefix) and out-of-window lines are ignored. Returns
    ``(no_interpreter, spawn_failed, last_ts)`` where ``last_ts`` is the ISO
    string of the most recent counted event (None if nothing counted).

    A contiguous run of byte-identical countable lines collapses to one incident.
    One broken session writes a burst of the same ``[ts] <hook> <reason>`` line at
    a single second; counting each raw line reads as chronic. Adjacency is over the
    previous *counted* line, so interleaved non-matching noise (raw stderr) does not
    split a run. The full-line identity keys on the second, hook, and reason, so
    distinct seconds, hooks, or reasons stay separate incidents.
    """
    no_interpreter = 0
    spawn_failed = 0
    last_when = -1.0
    last_ts: str | None = None
    prev_counted: str | None = None
    for line in text.splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        ts = m.group(1)
        try:
            when = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if when < since_epoch:
            continue
        if "no-interpreter" in line:
            kind = "no_interpreter"
        elif "failed (python=" in line:
            kind = "spawn_failed"
        else:
            continue
        if line == prev_counted:
            continue
        prev_counted = line
        if kind == "no_interpreter":
            no_interpreter += 1
        else:
            spawn_failed += 1
        if when > last_when:
            last_when = when
            last_ts = ts
    return no_interpreter, spawn_failed, last_ts


def _read_log_tail(path: Path, max_bytes: int) -> str:
    """Last ``max_bytes`` of ``path`` decoded as utf-8 (errors replaced).

    Empty string on any read error. The leading partial line after a mid-file
    seek is harmless: ``parse_degradations`` only counts lines whose ``[ts]``
    prefix matches, so a truncated head line is ignored.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _max_ts(a: str | None, b: str | None) -> str | None:
    """Most recent of two ISO-8601 Z timestamps (lexicographic == chronological)."""
    candidates = [t for t in (a, b) if t]
    return max(candidates) if candidates else None


def read_degraded_summary(window_days: int) -> dict:
    """Combined cumulative degraded-delivery summary over the recent window.

    Folds the in-process ``fail_open`` rows (``metrics.jsonl``) with the
    no-interpreter and spawn-failed lines (``.hook_errors.log``) over
    ``now - window_days``. Best-effort: any failure yields the all-zero summary,
    never raises (a status read must not crash on a corrupt log).

    ``scope`` is ``"user-global"``: both sources are per-user, not per-repo (a
    no-interpreter failure happens before any repo resolves, so it carries no
    repo id), so these counts span every repo this user touched. get_status
    embeds the summary in a per-repo envelope, which read as per-repo without
    this marker -- three different repos returned byte-identical degraded blocks.
    """
    try:
        since = time.time() - max(0, int(window_days)) * 86400.0
        log_text = _read_log_tail(hook_error_log_path(), _LOG_TAIL_BYTES)
        no_interpreter, spawn_failed, log_last = parse_degradations(log_text, since)
        advisor_unavailable, metrics_last = count_failopen_metrics(since)
        total = advisor_unavailable + no_interpreter + spawn_failed
        return {
            "window_days": int(window_days),
            "scope": "user-global",
            "advisor_unavailable": advisor_unavailable,
            "no_interpreter": no_interpreter,
            "spawn_failed": spawn_failed,
            "total": total,
            "last_ts": _max_ts(log_last, metrics_last),
        }
    except Exception:
        return {
            "window_days": int(window_days) if isinstance(window_days, int) else 0,
            "scope": "user-global",
            "advisor_unavailable": 0,
            "no_interpreter": 0,
            "spawn_failed": 0,
            "total": 0,
            "last_ts": None,
        }
