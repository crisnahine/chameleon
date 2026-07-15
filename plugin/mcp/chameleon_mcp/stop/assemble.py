"""Minimal output assembly for delivered review findings (spec section 6,
scoped to what Task 6 needs).

``render_findings`` is the whole renderer: one ``[🦎]`` header, one
disclaimer, one line per finding (``severity · claim · file:span`` plus
``[stale]``/``[unverified]``/``[confirmed]`` annotations), greedy-packed
whole under a provisional token ceiling (``core.budget.approx_tokens``). An
item that does not fit whole is OMITTED from the render -- never truncated
mid-item -- and the caller is told exactly which findings (by ``match_key``)
made it in, so it marks delivered only those; an omitted item stays
``pending`` for the next delivery point. It stays order-preserving only (no
priority re-ordering); delivery (UserPromptSubmit, SessionStart, and the
CHAMELEON_JUDGE_WAIT in-turn path) shares it via ``stop/delivery.py`` and
``stop/judge_wait.py``.

``assemble_stop_context`` is the ranked ``block > resurfaced HIGH >
delivered verified > delivered unverified > deterministic advisories >
idiom/nudge lines`` packer spec section 6 describes for the Stop emission: it
sorts a heterogeneous ``EmissionItem`` stream by priority and greedy-packs it
under one header/disclaimer, with a present block reason emitted alone.
Wiring ``stop/pipeline.py``'s ~12 independently-capped
``<chameleon-context>`` blocks through it is separate work; this module only
owns the packer itself.

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

# The durable off-switch disclosure the pre-cutover ``_idiom_review_gate``
# carried in its once-per-session BLOCK message ("Appendix F: port the
# disclosure, drop the block"). The idiom lens is no longer a turn-ending
# interrupt, so this rides the render itself instead -- the least-noisy
# honest surface: it appears only on a turn that actually shows the user an
# idiom finding, strictly narrower than the old gate (which fired on every
# session's first idiom-governed edit regardless of whether a real
# violation existed).
_IDIOM_DURABLE_OFF_HINT = (
    'Idiom review can be turned off durably for this repo: "enforcement": '
    '{"idiom_review": false} in .chameleon/config.json.'
)


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
    idiom_shown = False
    for f in items:
        line = _render_line(f)
        cost = approx_tokens(line)
        if spent + cost > ceiling:
            continue  # does not fit whole -- omitted, stays pending, never truncated
        lines.append(line)
        spent += cost
        packed_keys.append(f.match_key)
        if f.kind == "idiom":
            idiom_shown = True

    if not packed_keys:
        return RenderResult(text="", delivered_match_keys=())
    if idiom_shown:
        lines.append(_IDIOM_DURABLE_OFF_HINT)
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


# --- Ranked Stop assembler (spec section 6) ----------------------------------
#
# ``render_findings`` above stays the order-preserving finding-list renderer
# delivery/judge_wait already depend on. ``assemble_stop_context`` is the
# Stop-side entry point: it owns the ranked, ceiling-bounded packing across a
# heterogeneous stream (block reason, resurfaced findings, delivered findings,
# deterministic advisories, idiom nudges) into ONE budgeted emission. Its
# ``header`` argument selects finding-line mode (a str: one leading [🦎]
# header + disclaimer) vs Stop-emission mode (``None``: the items are already
# individually [🦎]-headered <chameleon-context> blocks, so NO extra
# top-level header is added and blocks are blank-line separated -- the
# pre-ranking additive "\n\n".join, now ranked and budgeted). pipeline.py's
# stop_gates funnels its resurface / review / deterministic-advisory blocks
# through this in Stop-emission mode.

# Lower number = higher priority = packed first.
PRIORITY_BLOCK = 0
PRIORITY_RESURFACED = 1
PRIORITY_DELIVERED_VERIFIED = 2
PRIORITY_DELIVERED_UNVERIFIED = 3
PRIORITY_ADVISORY = 4
PRIORITY_IDIOM = 5


@dataclass(frozen=True)
class EmissionItem:
    """One candidate block of text for a Stop emission.

    ``priority`` is one of the module-level ``PRIORITY_*`` constants (lower
    packs first). ``text`` is the fully-rendered block/line as it would
    appear in the final emission -- the packer never reformats or truncates
    it, only decides whether it fits whole. ``match_keys`` are the finding
    match_keys this item represents, if any (a deterministic advisory or a
    block reason typically carries none); when the item is packed, its keys
    join ``AssembledStop.packed_match_keys`` so the caller commits ledger
    delivered/resurfaced transitions only for what the human actually saw.
    ``droppable`` is reserved for a caller that must force an item into every
    emission regardless of ceiling (e.g. a block reason too large to defer,
    spec section 6) -- unused by the packer today; a future caller wires it.
    """

    priority: int
    text: str
    match_keys: tuple[str, ...] = ()
    droppable: bool = True


@dataclass(frozen=True)
class AssembledStop:
    """The result of ``assemble_stop_context``.

    ``text`` is what was actually emitted (``""`` when nothing packed).
    ``packed_match_keys`` is the union of the packed items' ``match_keys`` --
    exactly the findings the caller may now mark delivered/resurfaced. A
    block-present emission always returns an empty tuple here: a block does
    not "deliver" anything, so every finding it stood in front of stays
    ``pending``.
    """

    text: str
    packed_match_keys: tuple[str, ...]


def assemble_stop_context(
    items: list[EmissionItem], *, header: str | None, ceiling_tokens: int
) -> AssembledStop:
    """Rank-and-pack ``items`` into one Stop emission under ``ceiling_tokens``.

    Spec section 6's ranked order: block reason > resurfaced HIGH > delivered
    verified > delivered unverified > deterministic advisories > idiom/nudge
    lines (the ``PRIORITY_*`` constants above, lower packs first). Sorting is
    stable, so two items at the same priority keep their input order.

    A present ``PRIORITY_BLOCK`` item short-circuits everything else: its
    ``text`` is returned AS-IS (already the full, model-facing block reason
    with its own decision) and nothing else packs alongside it -- "a hard
    block emits only the block reason" (spec section 6). Because a block does
    not deliver findings, ``packed_match_keys`` is empty even if the block
    item itself carries ``match_keys``; every non-block item's findings stay
    ``pending``. When more than one block item is present (should not happen
    in practice), the first in input order wins.

    With no block present, items are greedy-packed whole in ranked order. An
    item that does not fit whole is omitted -- never truncated -- so it stays
    reachable at the next delivery point. Returns ``AssembledStop("", ())``
    when ``items`` is empty or nothing fits.

    ``header`` selects the rendering mode:

    - a string is finding-line mode (delivery-shaped): one ``[🦎 {header}]``
      line + one disclaimer lead the emission (both counted against the
      ceiling, exactly like ``render_findings``), then packed items follow,
      joined with ``"\\n"`` -- for callers whose items are bare finding lines.
    - ``None`` is Stop-emission mode: the items are ALREADY-wrapped,
      individually ``[🦎]``-headered ``<chameleon-context>`` blocks (the Stop
      pipeline's resurface / review / deterministic-advisory blocks), so NO
      extra top-level header is prepended -- the emission keeps each block's
      own pre-existing header, exactly as the pre-ranking additive
      ``"\\n\\n".join(context_blocks)`` did -- and blocks are separated by a
      blank line. Only the ranking + ceiling + packed-key accounting are new
      on this path; the header count is unchanged.
    """
    from chameleon_mcp.core.budget import approx_tokens

    entries = list(items or [])
    if not entries:
        return AssembledStop(text="", packed_match_keys=())

    block_items = [it for it in entries if it.priority == PRIORITY_BLOCK]
    if block_items:
        return AssembledStop(text=block_items[0].text, packed_match_keys=())

    ceiling = max(0, int(ceiling_tokens))
    if header is not None:
        prefix = [f"[\U0001f98e {header}]", _DISCLAIMER]
        separator = "\n"
    else:
        prefix = []
        separator = "\n\n"
    spent = approx_tokens("\n".join(prefix)) if prefix else 0
    packed_texts: list[str] = []
    packed_keys: list[str] = []
    for it in sorted(entries, key=lambda item: item.priority):
        cost = approx_tokens(it.text)
        if spent + cost > ceiling:
            continue  # does not fit whole -- omitted, stays pending, never truncated
        packed_texts.append(it.text)
        spent += cost
        for key in it.match_keys:
            if key not in packed_keys:
                packed_keys.append(key)

    if not packed_texts:
        return AssembledStop(text="", packed_match_keys=())
    body = separator.join(packed_texts)
    text = separator.join([*prefix, body]) if prefix else body
    return AssembledStop(text=text, packed_match_keys=tuple(packed_keys))
