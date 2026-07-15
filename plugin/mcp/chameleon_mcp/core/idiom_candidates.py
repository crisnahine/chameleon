"""Idiom CANDIDATES: proposals the self-learning miner (``stop/miner.py``)
derives from real turn-end usage signals, never adopted automatically.

A candidate lives under ``.chameleon/idiom-candidates/<slug>.json``, one file
per slug, IdiomRecord-shaped plus an evidence trail (``occurrences``,
``session_ids``, cumulative ``evidence``). It is deliberately a SEPARATE
directory from ``core.idiom_store``'s ``idioms/`` -- a candidate is unapproved
by definition, so nothing here ever calls ``upsert_idiom`` or otherwise
touches the live idiom store, and this directory is NOT part of
``profile/trust.py``'s ``_HASHED_ARTIFACTS``: hashing it would arm the trust
gate on the miner's own unreviewed output, which defeats the point of a
proposal a human has not yet seen. A candidate becomes a real idiom only
through the same ``/chameleon-teach`` (or ``/chameleon-auto-idiom``) path a
hand-taught idiom uses.

Writes are atomic per file (``write_candidate`` copies ``upsert_idiom``'s
tmp-write + ``os.replace`` pattern) and MERGE rather than clobber: a second
write of the same slug takes ``occurrences`` to the larger of what's on disk
and what the caller just passed, unions ``session_ids``, and appends new
``evidence`` onto whatever the file already held, so repeated sightings of
the same pattern across turns/sessions accumulate into one richer proposal
instead of forking into duplicate files. New slugs are bounded by
``IDIOM_CANDIDATE_MAX`` (a merge into an EXISTING slug is never refused by
the cap -- only minting a brand-new file is).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

CANDIDATES_DIRNAME = "idiom-candidates"
CANDIDATE_SCHEMA = "chameleon-idiom-candidate-1"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SOURCES = ("auto", "learned")


def candidates_dir(profile_dir: Path) -> Path:
    return profile_dir / CANDIDATES_DIRNAME


def _read_candidate(path: Path) -> dict | None:
    """Best-effort read of one candidate file; ``None`` on any failure."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _merge_str_list(existing: object, new: Iterable[str]) -> list[str]:
    out = [str(x) for x in existing] if isinstance(existing, list) else []
    for item in new:
        item = str(item)
        if item and item not in out:
            out.append(item)
    return out


def write_candidate(
    profile_dir: Path,
    *,
    slug: str,
    title: str,
    rationale: str,
    source: str,
    evidence: str,
    languages: Iterable[str] = (),
    archetypes: Iterable[str] = (),
    occurrences: int = 1,
    session_ids: Iterable[str] = (),
) -> None:
    """Write (or merge into) one candidate file, atomically.

    ``title``/``rationale`` fall back to whatever the file already holds when
    called with an empty string (the reinforcement signal deliberately passes
    both empty so it never overwrites a fuller proposal with a stub) and
    finally to ``slug`` itself if the file is brand new and both are empty.
    ``occurrences`` is the AUTHORITATIVE running total, not a delta: the
    stored value is ``max(prior_occurrences, occurrences)``, so a caller
    passes its own current total sighting count and re-submitting that same
    total on an unchanged state is idempotent (never inflates), while a
    genuinely higher total raises the stored value to match. ``session_ids``
    unions in insertion order, and ``evidence`` appends the new line onto the
    existing trail (a byte-identical repeat is not duplicated). Silently
    declines to create a file for a brand-new slug once
    ``IDIOM_CANDIDATE_MAX`` files already exist -- the cap only ever blocks
    new proposals, never a merge into one already on disk.

    Raises ``ValueError`` for an invalid ``slug``/``source`` -- both are
    caller-controlled (derived deterministically, never raw model text), so a
    validation failure here is a real caller bug, not something to fail open
    over silently.
    """
    if source not in _SOURCES:
        raise ValueError(f"invalid candidate source: {source!r}")
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid candidate slug: {slug!r}")

    cdir = candidates_dir(profile_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cdir, 0o700)
    except OSError:
        pass

    path = cdir / f"{slug}.json"
    existing = _read_candidate(path)

    if existing is None:
        from chameleon_mcp._thresholds import threshold_int

        try:
            current_count = sum(1 for _ in cdir.glob("*.json"))
        except OSError:
            current_count = 0
        if current_count >= threshold_int("IDIOM_CANDIDATE_MAX"):
            return

    prior_title = str(existing.get("title") or "") if existing else ""
    prior_rationale = str(existing.get("rationale") or "") if existing else ""
    prior_evidence = str(existing.get("evidence") or "") if existing else ""
    prior_occurrences = int(existing.get("occurrences") or 0) if existing else 0

    merged_title = (title or "").strip() or prior_title or slug
    merged_rationale = (rationale or "").strip() or prior_rationale
    merged_evidence = prior_evidence
    new_evidence = (evidence or "").strip()
    if new_evidence and new_evidence not in prior_evidence:
        merged_evidence = f"{prior_evidence}\n{new_evidence}" if prior_evidence else new_evidence

    body = {
        "schema": CANDIDATE_SCHEMA,
        "slug": slug,
        "title": merged_title,
        "rationale": merged_rationale,
        "languages": _merge_str_list(existing.get("languages") if existing else None, languages),
        "archetypes": _merge_str_list(existing.get("archetypes") if existing else None, archetypes),
        "source": source,
        "evidence": merged_evidence,
        "occurrences": max(prior_occurrences, max(0, int(occurrences))),
        "session_ids": _merge_str_list(
            existing.get("session_ids") if existing else None, session_ids
        ),
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_candidates(profile_dir: Path) -> list[dict]:
    """All candidate rows, fail-open ``[]``.

    Skips a corrupt file (bad JSON, non-object root) with the rest of the
    directory still loading, mirroring ``core.idiom_store.load_store``'s
    per-file fail-open discipline. Never trust-hashed, never scanned for
    injection: a candidate is unreviewed model output that reaches a human
    only through explicit browsing (``/chameleon-status`` /
    ``/chameleon-explain`` / ``/chameleon-auto-idiom``), never injected back
    into a live session's context the way an adopted idiom is.
    """
    out: list[dict] = []
    cdir = candidates_dir(profile_dir)
    try:
        paths = sorted(cdir.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        row = _read_candidate(path)
        if row is not None:
            out.append(row)
    return out
