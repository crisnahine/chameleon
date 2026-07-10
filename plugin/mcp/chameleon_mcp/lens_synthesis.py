"""Decorrelated multi-lens synthesis (R3): merge N review lenses without the
raw-union false-positive blowup.

The 2026 evidence is sharp on this: running several decorrelated review lenses
(correctness, security, cross-file, intent) over a diff raises recall a lot
(measured ~32.8% single-agent to ~72.4% combined), but shipping their *raw union*
carries a ~50% false-positive rate -- the union inherits every lens's mistakes.
The value is entirely in the synthesis: dedup identical findings, treat
independent agreement across lenses as the strong trust signal, and surface a
lone-lens finding only when it is confident on its own. This module is that
synthesis as a pure function; spawning the lenses (the model calls) lives in the
judge layer, the same pure-core / thin-IO split the rest of the engine uses.
"""

from __future__ import annotations


def _claim_key(claim: str) -> str:
    return " ".join(str(claim).lower().split())


def synthesize_lens_findings(lens_findings, *, min_confidence: float = 0.7) -> list[dict]:
    """Merge per-lens findings into one deduped, agreement-annotated set.

    Each input finding is ``{file, line, claim, lens, confidence}``. Findings with
    the same ``(file, line, normalized-claim)`` collapse to one, carrying the set
    of lenses that raised it (``lenses``/``agreement``) and the max confidence any
    lens assigned. ``surface`` is the anti-raw-union gate: a finding surfaces when
    two or more lenses independently raised it (cross-lens agreement) OR a single
    lens raised it at/above ``min_confidence``. Order is stable by first
    appearance, so output is deterministic for a fixed input.
    """
    order: list[tuple] = []
    merged: dict[tuple, dict] = {}
    for f in lens_findings or ():
        if not isinstance(f, dict):
            continue
        key = (f.get("file"), f.get("line"), _claim_key(f.get("claim", "")))
        entry = merged.get(key)
        if entry is None:
            entry = {
                "file": f.get("file"),
                "line": f.get("line"),
                "claim": f.get("claim", ""),
                "lenses": set(),
                "confidence": 0.0,
            }
            merged[key] = entry
            order.append(key)
        lens = f.get("lens")
        if lens is not None:
            entry["lenses"].add(lens)
        try:
            entry["confidence"] = max(entry["confidence"], float(f.get("confidence", 0.0)))
        except (TypeError, ValueError):
            pass

    out: list[dict] = []
    for key in order:
        entry = merged[key]
        agreement = len(entry["lenses"])
        surface = agreement >= 2 or entry["confidence"] >= min_confidence
        out.append(
            {
                "file": entry["file"],
                "line": entry["line"],
                "claim": entry["claim"],
                "lenses": sorted(entry["lenses"]),
                "agreement": agreement,
                "confidence": entry["confidence"],
                "surface": surface,
            }
        )
    return out
