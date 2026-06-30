"""Read-back of the would_block metrics log for the shadow -> enforce decision.

``metrics.jsonl`` is written by every hook call but nothing reads it back. A
lead deciding whether to flip a repo from ``shadow`` to ``enforce`` needs the
live, accumulating real-edit record: over the last N days of editing, how often
would each block rule have fired, on how many distinct files and sessions, and
which specific file:line instances. This module aggregates that.

It deliberately reports only would-block FREQUENCY plus a sampled file:line list
for human spot-check. It does NOT compute a false-positive fraction: an inline
``chameleon-ignore`` override IS logged (as its own per-rule ``overrides`` tally,
kept distinct from advisory-only emissions), but the rows still carry no
accept/fix outcome signal, so whether a would-block was a genuine off-pattern
edit stays a human judgement on the sample, not a number this module invents.

Rotation is the correctness trap. ``metrics.jsonl`` rotates by size into
``.1``..``.5`` and then deletes the oldest, so a reader of only the current file
silently undercounts once rotation fires. This reader globs every retained
segment and merges them; if the oldest retained timestamp is younger than the
requested window, it flags the window as truncated rather than asserting that
"0 would-blocks" covers the whole period.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.metrics import _metrics_path

# The classes of defect chameleon's structural rules cannot see. Both the
# conformance score and the enforcement-outcome rates are derived from
# archetype-shape matching and a fixed set of block rules, so neither moves when
# an edit has the right shape but wrong behaviour. This line is the guardrail
# against reading a low score or an all-zeros result as a correctness guarantee;
# every honest-signal surface prints it above any otherwise-green output.
SIGNAL_BLIND_SPOTS = ("logic", "dataflow", "cross-file", "auth checks")

# One-line summary of what the conformance score does and does NOT mean, reused
# verbatim by the status surface and the drift banner so the framing never
# drifts between them.
CONFORMANCE_DISCLAIMER = (
    "Structural conformance measures how closely edits match their archetype's "
    "shape. It is NOT a quality bar and does NOT cover: " + ", ".join(SIGNAL_BLIND_SPOTS) + "."
)

# Hook names that emit a per-rule would_block row. The idiom-review gate is
# intentionally excluded: it has no single rule, so it is reported separately as
# a turn-level counter, never as a per-rule promotion candidate.
_RULE_BEARING_HOOKS = frozenset({"preflight-and-advise", "posttool-verify", "stop-backstop"})

# Hook name for the once-per-session idiom/principle self-review nudge.
_IDIOM_REVIEW_HOOK = "stop-idiom-review"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(value: object) -> float | None:
    """Epoch seconds for a metric row's UTC timestamp, or None if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return time.mktime(time.strptime(value, _TS_FORMAT)) - time.timezone
    except (ValueError, OverflowError):
        return None


def _segments(base: Path) -> list[Path]:
    """Every retained metrics segment: the live file plus rotated backups.

    Globbing ``metrics.jsonl*`` would also match an unrelated sibling; instead
    we name the live file and the numbered rotations explicitly so a stray file
    in the data dir cannot be parsed as metrics.
    """
    out: list[Path] = []
    if base.is_file():
        out.append(base)
    parent = base.parent
    name = base.name
    for entry in sorted(parent.glob(f"{name}.*")):
        suffix = entry.name[len(name) + 1 :]
        if suffix.isdigit():
            out.append(entry)
    return out


def _has_rotated_segment(base: Path) -> bool:
    """True when at least one numbered rotation backup exists.

    Truncation can only have happened if rotation has fired: a repo whose whole
    history is younger than the window is simply young, not truncated. The flag
    must distinguish "rotation dropped the older tail" from "no older tail
    exists yet", so it keys off the presence of a rotated segment.
    """
    return any(p != base for p in _segments(base))


def _iter_rows(base: Path):
    """Yield parsed metric rows from all segments, skipping malformed lines.

    A truncated tail (a half-written line from a crashed append) or a corrupt
    rotated file must not abort the read, so each line is parsed defensively and
    non-dict / undecodable lines are dropped.
    """
    for segment in _segments(base):
        try:
            # errors='replace' so an undecodable byte (a hook SIGKILLed mid-write
            # of a non-ASCII path) corrupts only its own line -- json.loads then
            # drops it -- instead of UnicodeDecodeError aborting the whole read and
            # silently zeroing would-block history (a false high_override_rate flag).
            with open(segment, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            continue


def build_shadow_report(
    repo_id: str | None,
    window_days: int | None = None,
    *,
    now: float | None = None,
    metrics_path: Path | None = None,
) -> dict:
    """Aggregate would_block rows for ``repo_id`` within the lookback window.

    Returns a plain dict (the MCP tool wraps it in the standard envelope):

    - ``window_days`` / ``window_start`` — the lookback applied.
    - ``window_truncated`` — True when the oldest retained row is younger than
      the window start, so the per-rule counts cannot claim full coverage of the
      requested period (rotation dropped the older tail).
    - ``total_edits`` — clean ``posttool-verify`` rows in the window, the
      denominator the promotion verdict reads.
    - ``rules`` — per rule: ``would_blocks``, ``distinct_files``,
      ``distinct_sessions`` (count of distinct sessions whose rows carried a
      session id, or None when no would_block row for the rule carried one — it
      is never a relabel of ``distinct_files``), ``advisory_only``
      (advisory-emitted rows for the same rule that did not would-block),
      ``overrides`` (inline ``chameleon-ignore`` bypasses of this rule, counted
      apart from ``advisory_only``), and a ``verdict``.
    - ``idiom_review`` — turn-level would-block count for the idiom/principle
      gate, with no per-rule verdict.
    - ``sample`` — up to SHADOW_REPORT_SAMPLE_CAP ``{rule, file, line, ts}``
      entries for human spot-check.

    The verdict is a threshold over would-block COUNT only. It never asserts a
    false-positive fraction: the data has no outcome signal for that.
    """
    if window_days is None:
        window_days = threshold_int("SHADOW_REPORT_WINDOW_DAYS")
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        window_days = threshold_int("SHADOW_REPORT_WINDOW_DAYS")
    if window_days <= 0:
        window_days = threshold_int("SHADOW_REPORT_WINDOW_DAYS")

    now = time.time() if now is None else now
    window_start = now - window_days * 86400
    base = metrics_path or _metrics_path()
    sample_cap = threshold_int("SHADOW_REPORT_SAMPLE_CAP")
    min_edits = threshold_int("SHADOW_PROMOTION_MIN_EDITS")

    # Per-rule accumulators.
    would_blocks: dict[str, int] = defaultdict(int)
    files: dict[str, set[str]] = defaultdict(set)
    sessions: dict[str, set[str]] = defaultdict(set)
    advisory_only: dict[str, int] = defaultdict(int)
    overrides: dict[str, int] = defaultdict(int)
    total_edits = 0
    idiom_review_blocks = 0
    sample: list[dict] = []

    # Track the oldest retained row across ALL segments (any repo): if the
    # oldest row we still have is younger than the window start, rotation
    # dropped the older tail and the window is truncated.
    oldest_retained: float | None = None
    saw_any_row = False

    for row in _iter_rows(base):
        saw_any_row = True
        ts = _parse_ts(row.get("ts"))
        if ts is not None and (oldest_retained is None or ts < oldest_retained):
            oldest_retained = ts

        if repo_id is not None and row.get("repo_id") != repo_id:
            continue
        if ts is None or ts < window_start:
            continue

        hook = row.get("hook")
        rule = row.get("rule")
        is_would_block = bool(row.get("would_block"))

        if hook == _IDIOM_REVIEW_HOOK:
            if is_would_block:
                idiom_review_blocks += 1
            continue

        # An override row records an inline `chameleon-ignore` that dropped a
        # block-eligible rule. It carries would_block=False but is NOT an
        # advisory-only emission: counting it in advisory_only would inflate the
        # promotion-floor denominator with bypasses. Bucket it separately so the
        # report can show "fired but overridden" without conflating the two.
        if hook == "override" or row.get("override"):
            if rule:
                overrides[rule] += 1
            continue

        if hook == "posttool-verify" and not is_would_block:
            # A baseline verify row: one real edit reached the verifier. This is
            # the edit-volume denominator the promotion floor reads.
            total_edits += 1

        if not is_would_block:
            if rule:
                advisory_only[rule] += 1
            continue

        if hook not in _RULE_BEARING_HOOKS:
            continue

        # A would_block row whose rule is null (e.g. a backstop file that
        # re-linted blockable but yielded no rule name) is bucketed under a
        # stable placeholder so its file:line sample is not lost.
        rule_key = rule or "(unattributed)"
        would_blocks[rule_key] += 1
        file_rel = row.get("file_rel")
        if file_rel:
            files[rule_key].add(file_rel)
        sess = row.get("repo_id_session") or row.get("session_id")
        # Only a real session id counts toward distinct sessions. A row without
        # one is left unattributed rather than proxied to file_rel: the proxy
        # made distinct_sessions a silent relabel of distinct_files, which reads
        # as a second dimension that does not exist. With no real id present the
        # rule's distinct_sessions surfaces as None (unknown), not a file count.
        if sess:
            sessions[rule_key].add(sess)
        if len(sample) < sample_cap:
            # The sampled path traces back to a repo file path, which is
            # attacker-influenceable and can cross-encode a tag-boundary token
            # across the separator, so the displayed value is sanitized before
            # it reaches the model. The raw file_rel still keys the distinct
            # accounting sets above (those are dedup counts, never displayed).
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context

            sample.append(
                {
                    "rule": rule_key,
                    "file": sanitize_for_chameleon_context(file_rel) if file_rel else file_rel,
                    "line": row.get("line"),
                    "ts": row.get("ts"),
                }
            )

    # The window is truncated only when rotation actually dropped older rows:
    # a rotated backup exists AND the oldest row we still hold is younger than
    # the window start, so the requested period's older tail is gone. A young
    # repo with no rotation has its full history present and is NOT truncated,
    # even though its oldest row is younger than the window.
    window_truncated = (
        bool(saw_any_row)
        and _has_rotated_segment(base)
        and (oldest_retained is None or oldest_retained > window_start)
    )

    rules_out: dict[str, dict] = {}
    for rule_key in sorted(set(would_blocks) | set(advisory_only) | set(overrides)):
        count = would_blocks.get(rule_key, 0)
        # None (unknown) when no would_block row carried a session id, so the
        # field is never a silent copy of distinct_files. An integer here means
        # real session ids were observed and counted.
        known_sessions = sessions.get(rule_key)
        rules_out[rule_key] = {
            "would_blocks": count,
            "distinct_files": len(files.get(rule_key, ())),
            "distinct_sessions": len(known_sessions) if known_sessions else None,
            "advisory_only": advisory_only.get(rule_key, 0),
            "overrides": overrides.get(rule_key, 0),
            "verdict": _promotion_verdict(
                would_blocks=count,
                total_edits=total_edits,
                window_truncated=window_truncated,
                min_edits=min_edits,
            ),
        }

    return {
        "repo_id": repo_id,
        "window_days": window_days,
        "window_start": time.strftime(_TS_FORMAT, time.gmtime(window_start)),
        "window_truncated": window_truncated,
        "total_edits": total_edits,
        "rules": rules_out,
        "idiom_review": {"would_blocks": idiom_review_blocks},
        "sample": sample,
        "sample_truncated": sum(would_blocks.values()) > len(sample),
    }


def _promotion_verdict(
    *, would_blocks: int, total_edits: int, window_truncated: bool, min_edits: int
) -> str:
    """Promotion readiness for one rule, by would-block COUNT alone.

    - ``would_block`` — the rule fired at least once; a human must read the
      sample to judge whether those instances were genuine off-pattern code
      before enforcing.
    - ``insufficient_data`` — zero would-blocks but the window is truncated or
      saw too few edits to trust "never fires"; gather more shadow time.
    - ``safe_to_enforce`` — zero would-blocks across enough real edits in a
      non-truncated window.

    This never reports a false-positive fraction; the data has no outcome signal
    for one.
    """
    if would_blocks > 0:
        return "would_block"
    if window_truncated or total_edits < min_edits:
        return "insufficient_data"
    return "safe_to_enforce"


def build_longitudinal_signals(
    repo_id: str | None,
    window_days: int | None = None,
    *,
    now: float | None = None,
    metrics_path: Path | None = None,
) -> dict:
    """The two honestly-labelled longitudinal health tracks for a repo.

    A lead watching a rollout has historically had one trailing number: the
    drift score. That number is structural mimicry, not correctness, so it
    over-reads as a quality bar. This combines the two signals chameleon
    actually records, each labelled for what it measures, and keeps the
    blind-spots disclaimer attached so neither track is read as a correctness
    guarantee:

    - ``structural_conformance`` — Track 1. ``score`` is the observed drift
      score (1 - mean structural-match confidence) over the same window, with
      ``conformance`` = 1 - score as the "how on-shape are recent edits"
      reading, plus the disclaimer. None when there are no observations yet.
    - ``enforcement_outcomes`` — Track 2. Aggregate would-block rates over the
      window, derived from the same ``would_block`` rows the shadow report
      reads: ``block_rate`` (would-blocking rule fires / real edits) and
      ``idiom_review_rate`` (turn-level idiom/principle would-blocks / real
      edits). These count how often chameleon's OWN shape/idiom rules fired;
      an all-zeros result means those rules never caught anything, which is NOT
      the same as the code being safe.

    ``blind_spots`` / ``disclaimer`` are surfaced at the top level too so a
    caller that only renders the headline still carries the caveat. Fail-open:
    a missing drift.db or unreadable metrics log degrades the affected track to
    None / zeros rather than raising.
    """
    if window_days is None:
        window_days = threshold_int("LONGITUDINAL_WINDOW_DAYS")
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        window_days = threshold_int("LONGITUDINAL_WINDOW_DAYS")
    if window_days <= 0:
        window_days = threshold_int("LONGITUDINAL_WINDOW_DAYS")

    conformance = _structural_conformance(repo_id, window_days)
    outcomes = _enforcement_outcomes(repo_id, window_days, now=now, metrics_path=metrics_path)

    return {
        "repo_id": repo_id,
        "window_days": window_days,
        "blind_spots": list(SIGNAL_BLIND_SPOTS),
        "disclaimer": CONFORMANCE_DISCLAIMER,
        "structural_conformance": conformance,
        "enforcement_outcomes": outcomes,
    }


def _structural_conformance(repo_id: str | None, window_days: int) -> dict | None:
    """Track 1: the drift score relabelled as structural conformance.

    Returns the raw drift ``score`` (higher = more off-shape), its complement
    ``conformance`` (higher = more on-shape), the ``count`` it was measured
    over, and the disclaimer. None when no observations exist or the drift store
    is unreadable -- never raises.
    """
    if not repo_id:
        return None
    try:
        from chameleon_mcp.drift.observations import compute_drift_stats

        stats = compute_drift_stats(repo_id, window_days=window_days)
    except Exception:
        return None
    if not stats:
        return None
    score = float(stats.get("score", 0.0))
    return {
        "score": round(score, 4),
        "conformance": round(max(0.0, min(1.0, 1.0 - score)), 4),
        "observations": int(stats.get("count", 0)),
        "is_quality_bar": False,
        "disclaimer": CONFORMANCE_DISCLAIMER,
    }


def _enforcement_outcomes(
    repo_id: str | None,
    window_days: int,
    *,
    now: float | None = None,
    metrics_path: Path | None = None,
) -> dict:
    """Track 2: aggregate would-block rates over the window.

    Reuses the shadow report's per-rule aggregation so the rates rest on the
    same ``would_block`` rows. ``block_rate`` / ``idiom_review_rate`` are the
    would-block counts divided by real edits in the window; both are None when
    no edits were seen (a rate over zero edits is undefined, not zero). Fail-open
    to all-zeros on any read failure -- the caller still gets a usable shape.
    """
    empty = {
        "total_edits": 0,
        "would_block_edits": 0,
        "idiom_review_blocks": 0,
        "block_rate": None,
        "idiom_review_rate": None,
        "window_truncated": False,
        "measures": "how often chameleon's own shape/idiom rules fired",
    }
    if not repo_id:
        return empty
    try:
        report = build_shadow_report(repo_id, window_days, now=now, metrics_path=metrics_path)
    except Exception:
        return empty

    total_edits = int(report.get("total_edits", 0))
    rules = report.get("rules") or {}
    would_block_edits = sum(int(meta.get("would_blocks", 0)) for meta in rules.values())
    idiom_blocks = int((report.get("idiom_review") or {}).get("would_blocks", 0))

    def _rate(n: int) -> float | None:
        if total_edits <= 0:
            return None
        return round(n / total_edits, 4)

    return {
        "total_edits": total_edits,
        "would_block_edits": would_block_edits,
        "idiom_review_blocks": idiom_blocks,
        "block_rate": _rate(would_block_edits),
        "idiom_review_rate": _rate(idiom_blocks),
        "window_truncated": bool(report.get("window_truncated")),
        "measures": "how often chameleon's own shape/idiom rules fired",
    }
