"""Minimal output assembly for delivered review findings (spec section 6,
scoped to what Task 6 needs).

``render_findings`` is the whole renderer: one ``[🦎]`` header, one
disclaimer, one line per finding (``severity · claim · file:span`` plus
``[stale]``/``[unverified]``/``[confirmed]`` annotations), greedy-packed
whole under a provisional token ceiling (``core.budget.approx_tokens``). An
item that does not fit whole is OMITTED from the render -- never truncated
mid-item -- and the caller is told exactly which findings (by ``match_key``)
made it in, so it marks delivered only those; an omitted item stays
``pending`` for the next delivery point. This is NOT yet the full ranked
``block > resurfaced HIGH > delivered verified > delivered unverified >
deterministic advisories > idiom/nudge lines`` assembler spec section 6
describes for every Stop/SessionStart emission -- that whole-surface
unification is later work; this module only owns finding-list rendering for
delivery (UserPromptSubmit, SessionStart, and the CHAMELEON_JUDGE_WAIT
in-turn path all share it via ``stop/delivery.py`` and ``stop/judge_wait.py``).

``write_delivery_payload``/``read_delivery_payload`` are the pre-rendered
delivery-payload cache: the detached job (``stop/job.py``) renders its
repo's undelivered findings once, at job end, and stashes the text here so a
later UserPromptSubmit read under the callout-detector wrapper's 3s cap only
ever pays for a file read, never a re-render (which -- unlike rendering
itself -- can involve a per-finding staleness file read). One payload per
(repo_data, session_id): the session whose Stop launched the job is the one
whose very next UserPromptSubmit is overwhelmingly likely to consume it
first (spec section 3.5's "reads one prebuilt file" path); a different or
later-arriving session falls back to a live ledger query instead (see
``stop/delivery.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.finding import Finding

_DISCLAIMER = "Advisory; verify each before acting -- they may be wrong."


@dataclass(frozen=True)
class RenderResult:
    """One assembled render.

    ``text`` is what was actually emitted (``""`` when nothing packed).
    ``delivered_match_keys`` is exactly the packed subset, in the order they
    were packed -- never the full input list -- so the caller marks
    delivered ONLY what the human actually saw this turn; an item omitted
    for space stays reachable (still `pending`) at the next delivery point.
    """

    text: str
    delivered_match_keys: tuple[str, ...]


def _annotation_tags(f: Finding) -> str:
    tags = []
    if f.stale:
        tags.append("[stale]")
    tags.append("[confirmed]" if f.verified == "confirmed" else "[unverified]")
    return " ".join(tags)


def _render_line(f: Finding) -> str:
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    loc = f.file or "?"
    span = f.span if isinstance(f.span, (tuple, list)) else None
    if span and isinstance(span[0], int) and not isinstance(span[0], bool) and span[0] > 0:
        loc = f"{loc}:{span[0]}"
    claim = sanitize_for_chameleon_context(str(f.claim or ""))
    loc = sanitize_for_chameleon_context(loc)
    severity = sanitize_for_chameleon_context(str(f.severity or ""))
    return f"- {severity} · {claim} · {loc} {_annotation_tags(f)}"


def render_findings(findings: list[Finding], *, header: str, ceiling_tokens: int) -> RenderResult:
    """Render ``findings`` in INPUT order under ``ceiling_tokens``.

    No priority re-ordering happens here (a caller that wants severity-first
    packing sorts before calling this -- see ``stop/delivery.py``): this
    function stays a small, deterministic, order-preserving packer. The
    header and disclaimer are always emitted together and count against the
    ceiling like any other line, so a ceiling too small even for them packs
    zero findings rather than raising -- a caller-side misconfiguration, not
    a crash. Returns ``RenderResult(text="", delivered_match_keys=())`` when
    ``findings`` is empty or nothing fits.
    """
    from chameleon_mcp.core.budget import approx_tokens

    items = list(findings or [])
    if not items:
        return RenderResult(text="", delivered_match_keys=())

    ceiling = max(0, int(ceiling_tokens))
    lines = [f"[\U0001f98e {header}]", _DISCLAIMER]
    spent = approx_tokens("\n".join(lines))
    packed_keys: list[str] = []
    for f in items:
        line = _render_line(f)
        cost = approx_tokens(line)
        if spent + cost > ceiling:
            continue  # does not fit whole -- omitted, stays pending, never truncated
        lines.append(line)
        spent += cost
        packed_keys.append(f.match_key)

    if not packed_keys:
        return RenderResult(text="", delivered_match_keys=())
    return RenderResult(text="\n".join(lines), delivered_match_keys=tuple(packed_keys))


@dataclass(frozen=True)
class DeliveryPayload:
    """A pre-rendered delivery payload plus the EXACT set of findings its text
    represents.

    ``match_keys`` is the load-bearing half: the cached ``text`` shows only
    the subset that fit under the ceiling when the job rendered it, so a
    cache-hit consumer MUST mark delivered only these keys -- never the whole
    live-undelivered set, which would silently mark an overflow finding
    delivered without ever showing it (permanent loss). It is exactly
    ``RenderResult.delivered_match_keys`` from the render that produced
    ``text``.
    """

    text: str
    match_keys: tuple[str, ...]


def _payload_path(repo_data: Path, session_id) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker

    marker = _safe_session_marker(session_id)
    return Path(repo_data) / f".delivery_payload.{marker}.json"


def write_delivery_payload(repo_data: Path, session_id, text: str, match_keys=()) -> None:
    """Atomically stage ``text`` + the ``match_keys`` it represents as the
    pre-rendered delivery payload.

    The payload is a small JSON object ``{"text": ..., "match_keys": [...]}``
    rather than raw text so a cache-hit reader can mark delivered ONLY the
    findings the text actually shows (spec: never lose a finding). Best-effort:
    a write failure here never crashes the job -- the finding stays reachable
    via a live ledger query at the next delivery point, just without the
    fast-path cache. An empty ``text`` unlinks any stale payload instead of
    writing an empty file, so a job that ends with nothing left to deliver
    never leaves a prior render lying around to be served as if still fresh.
    """
    path = _payload_path(repo_data, session_id)
    if not text:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {"text": text, "match_keys": [str(k) for k in match_keys]},
            separators=(",", ":"),
        )
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        pass


def read_delivery_payload(repo_data: Path, session_id) -> DeliveryPayload | None:
    """The cached ``DeliveryPayload`` (text + the match_keys it represents),
    or None when absent/unreadable/empty/malformed.

    A read-only peek: it does NOT unlink the file. The caller consumes and
    clears it via ``clear_delivery_payload`` once it actually emits the text,
    so a read-only caller (a health check, a test) can look without a side
    effect. A corrupt or non-conforming file fails open to None (the caller
    falls back to a live render) rather than raising.
    """
    import json

    path = _payload_path(repo_data, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text:
        return None
    keys = data.get("match_keys")
    match_keys = tuple(str(k) for k in keys) if isinstance(keys, list) else ()
    return DeliveryPayload(text=text, match_keys=match_keys)


def clear_delivery_payload(repo_data: Path, session_id) -> None:
    """Unlink the payload file -- one-shot consumption after it is read and
    actually emitted to the user."""
    path = _payload_path(repo_data, session_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
