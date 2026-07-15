"""UserPromptSubmit / SessionStart finding delivery (spec sections 3.5, 5.4).

Three entry points, all built over ``review_ledger``'s canonical
finding-lifecycle rows and ``stop/assemble.py``'s renderer:

- ``deliver_pending_findings(cwd, session_id)`` -- the UserPromptSubmit
  handler's new path. Reuses the SAME multi-root discovery the Stop backstop
  uses (``hook_helper._discover_stop_roots``, the enforcement-state glob) so
  a coordinator/monorepo session's OTHER touched workspaces deliver too, not
  just ``find_repo_root(cwd)`` (the pre-phase-3 single-root leak). A single
  discovered root gets the fast cached-payload path (spec section 3.5's "the
  3s wrapper cap only ever covers a file read"); a genuinely multi-root
  session -- rare, and previously undelivered at all -- falls back to a live
  render combined across every root under ONE shared ceiling (spec section
  6: "ceilings are per emission, across all roots"), paying the staleness
  file-read cost a single job's cache would otherwise have amortized. That
  split is a deliberate, documented scope choice for this minimal
  implementation, not an oversight.

- ``deliver_dead_session_findings(repo_root, repo_id, repo_data)`` --
  SessionStart's age-bounded ledger query: a session that ended without a
  next prompt still surfaces its findings, at a LATER session's start.

- ``deliver_for_root`` -- the shared single-root primitive both the
  UserPromptSubmit fast path and ``stop/judge_wait.py`` call.

Staleness (spec section 5.4, "one policy at every delivery point"): a
finding's pinned excerpt is re-checked against the CURRENT on-disk excerpt at
every one of these entry points; a mismatch annotates ``[stale]``, it is
never dropped. Coexists with the legacy ``.judge_pending.<session>.json``
path (``hook_helper._pending_findings_block``, still callable, but no
longer written by the live Stop pipeline -- the sync/async judge gates that
used to write it are uncalled from ``stop/pipeline.py`` since the async-
first cutover) rather than replacing it: ``review_ledger.migrate_pending_queue``
is the one bridge, folding any leftover legacy rows (from a pre-cutover
session, or CHAMELEON_JUDGE_ASYNC=1 before the cutover) into the ledger so
they flow through this path too.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.finding import Finding

_WRAP_OPEN = "<chameleon-context>"
_WRAP_CLOSE = "</chameleon-context>"


def _annotate_staleness(ws_root, findings: list[Finding]) -> list[Finding]:
    """Re-check each pinned excerpt against the CURRENT file; annotate
    ``stale=True`` on a mismatch, never drop (spec section 5.4). A finding
    with no pinned excerpt_sha is left as-is -- staleness is never
    fabricated from data absence."""
    from chameleon_mcp.judge import _excerpt_sha_stale
    from chameleon_mcp.stop.verify import _excerpt_window

    root = Path(ws_root)
    out: list[Finding] = []
    for f in findings:
        if not f.excerpt_sha:
            out.append(f)
            continue
        line = f.span[0] if f.span else None
        current = _excerpt_window(root, f.file, line)
        stale = _excerpt_sha_stale(f.excerpt_sha, current)
        out.append(replace(f, stale=stale) if stale != f.stale else f)
    return out


def _delivery_header(n: int) -> str:
    return (
        f"chameleon: independent review flagged {n} possible issue{'s' if n != 1 else ''} "
        "from a previous turn"
    )


def _render_and_mark(repo_id: str, findings: list[Finding], *, header: str, ceiling_tokens: int):
    from chameleon_mcp.review_ledger import mark_delivered
    from chameleon_mcp.stop.assemble import render_findings

    if not findings:
        return None
    rendered = render_findings(findings, header=header, ceiling_tokens=ceiling_tokens)
    if not rendered.text:
        return None
    if rendered.delivered_match_keys:
        mark_delivered(repo_id, rendered.delivered_match_keys)
    return rendered.text


def _wrap(text: str) -> str:
    return f"{_WRAP_OPEN}\n{text}\n{_WRAP_CLOSE}"


def deliver_for_root(
    repo_id: str, repo_data: Path, ws_root, session_id, *, ceiling_tokens: int
) -> str | None:
    """One workspace's delivery: the cached job payload if one exists for
    this exact (repo_data, session_id), else a live render.

    Returns an already ``<chameleon-context>``-wrapped block (or None) --
    the shape every known caller needs: UserPromptSubmit's
    ``deliver_pending_findings`` composes a list of pre-wrapped blocks
    (mirroring the legacy ``_pending_findings_block``), and Stop's own
    per-root advisory list (``stop/pipeline.py``'s ``context_blocks``, which
    ``stop/judge_wait.py`` feeds into) is built from individually-wrapped
    blocks the same way.

    On a cache hit, ONLY the match_keys the cached payload actually
    represents (``DeliveryPayload.match_keys``, which is the render's own
    ``delivered_match_keys``) are marked delivered -- never the whole
    live-undelivered set. The job's render may have packed only a subset
    under the ceiling; marking the whole set delivered would silently retire
    an overflow finding that was never shown (permanent loss). Any un-rendered
    remainder is left ``pending``, so a later delivery point surfaces it.
    Returns None when there is nothing to show.
    """
    from chameleon_mcp.review_ledger import mark_delivered, undelivered_findings
    from chameleon_mcp.stop.assemble import clear_delivery_payload, read_delivery_payload

    live = undelivered_findings(repo_id, ws_roots=[str(ws_root)])
    cached = read_delivery_payload(repo_data, session_id)
    if cached is not None:
        clear_delivery_payload(repo_data, session_id)  # one-shot consumption
    if not live:
        return None
    if cached is not None and cached.text:
        # Mark ONLY what the cached text represents. mark_delivered transitions
        # a pending row and is a no-op for any other status (a key already
        # delivered elsewhere, or a terminal resurfaced/addressed one), so
        # marking is harmless; an overflow key absent from match_keys stays
        # pending for the next delivery point.
        if cached.match_keys:
            mark_delivered(repo_id, cached.match_keys)
        return _wrap(cached.text)
    live = _annotate_staleness(ws_root, live)
    text = _render_and_mark(
        repo_id, live, header=_delivery_header(len(live)), ceiling_tokens=ceiling_tokens
    )
    return _wrap(text) if text else None


def deliver_pending_findings(cwd: Path, session_id) -> str | None:
    """UserPromptSubmit's new ledger-based delivery, across every workspace
    the session touched. See the module docstring for the single-root vs
    multi-root split. Fails open to None on any error; a suppressed
    (disabled/paused) root is skipped individually, never blanket-skipping
    every other root."""
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.optouts import is_chameleon_suppressed
    from chameleon_mcp.review_ledger import (
        mark_delivered,
        migrate_pending_queue,
        undelivered_findings,
    )

    try:
        roots = hh._discover_stop_roots(Path(cwd), session_id)
    except Exception:
        roots = []
    if not roots:
        return None

    eligible = []
    for root in roots:
        try:
            if is_chameleon_suppressed(root["ws_root"], root["repo_id"], session_id) is not None:
                continue
            migrate_pending_queue(root["repo_id"], root["ws_root"])
            eligible.append(root)
        except Exception:
            continue
    if not eligible:
        return None

    ceiling = threshold_int("REVIEW_RENDER_TOKEN_CEILING")

    if len(eligible) == 1:
        root = eligible[0]
        try:
            return deliver_for_root(
                root["repo_id"],
                root["repo_data"],
                root["ws_root"],
                session_id,
                ceiling_tokens=ceiling,
            )
        except Exception:
            return None

    # Multi-root: one shared ceiling and one header across every workspace
    # (spec section 6), so the emission cannot grow with the root count.
    combined: list[Finding] = []
    repo_id_by_key: dict[str, str] = {}
    for root in eligible:
        try:
            rows = undelivered_findings(root["repo_id"], ws_roots=[str(root["ws_root"])])
            rows = _annotate_staleness(root["ws_root"], rows)
        except Exception:
            continue
        combined.extend(rows)
        for f in rows:
            repo_id_by_key[f.match_key] = root["repo_id"]
    if not combined:
        return None

    from chameleon_mcp.stop.assemble import render_findings

    rendered = render_findings(
        combined, header=_delivery_header(len(combined)), ceiling_tokens=ceiling
    )
    if not rendered.text:
        return None
    by_repo: dict[str, list[str]] = {}
    for key in rendered.delivered_match_keys:
        rid = repo_id_by_key.get(key)
        if rid:
            by_repo.setdefault(rid, []).append(key)
    for rid, keys in by_repo.items():
        try:
            mark_delivered(rid, keys)
        except Exception:
            pass
    return _wrap(rendered.text)


def deliver_dead_session_findings(repo_root: Path, repo_id: str, repo_data: Path) -> str | None:
    """SessionStart's age-bounded ledger query (spec section 3.5): a
    session that ended without a next prompt still surfaces its findings,
    here, at a later session's start. Age-bounded so a finding whose
    OWNING session is still live and about to deliver it through its own
    UserPromptSubmit is not raced (see ``SESSION_START_DEAD_FINDING_MIN_AGE_SECONDS``
    in ``_thresholds.py``). Fails open to None.

    Returns BARE text (no ``<chameleon-context>`` wrapper), unlike
    ``deliver_pending_findings`` -- SessionStart's other banners
    (``_judge_spawn_health_banner`` and siblings) are plain ``[🦎 ...]``
    lines folded into ONE shared outer wrapper the caller builds, not
    individually wrapped blocks.
    """
    import time

    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.review_ledger import undelivered_findings

    try:
        rows = undelivered_findings(repo_id, ws_roots=[str(repo_root)])
    except Exception:
        return None
    if not rows:
        return None

    min_age = threshold_int("SESSION_START_DEAD_FINDING_MIN_AGE_SECONDS")
    now = time.time()
    aged: list[Finding] = []
    for f in rows:
        age = _age_seconds(f.created_at, now)
        if age is None or age >= min_age:
            aged.append(f)
    if not aged:
        return None

    aged = _annotate_staleness(repo_root, aged)
    ceiling = threshold_int("SESSION_START_DELIVERY_TOKEN_CEILING")
    header = (
        f"chameleon: {len(aged)} unaddressed finding{'s' if len(aged) != 1 else ''} "
        "from a previous session's review"
    )
    return _render_and_mark(repo_id, aged, header=header, ceiling_tokens=ceiling)


def _age_seconds(created_at: str, now: float) -> float | None:
    """Seconds since ``created_at`` (an ISO-8601 UTC ``%Y-%m-%dT%H:%M:%SZ``
    timestamp, ``core.finding.Finding``'s own format), or None when it does
    not parse -- an unparseable timestamp is treated as "old enough" by the
    caller (``age is None`` packs into ``aged``) rather than silently
    withheld forever on a formatting fluke. ``calendar.timegm`` (not
    ``time.mktime``) interprets the parsed struct as UTC, matching how the
    timestamp was produced (``time.gmtime()`` everywhere ``created_at`` is
    stamped) -- ``mktime`` would misread it as local time and skew every
    age by the host's UTC offset.
    """
    import calendar
    import time as _time

    try:
        parsed = _time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
        epoch = calendar.timegm(parsed)
        return now - epoch
    except (ValueError, TypeError, OverflowError):
        return None
