"""Multi-lens runner (G3): run N review lenses, normalize, synthesize.

The pure orchestration core that activates ``lens_synthesis``. A lens is a named
callable returning raw findings (``{file, line, claim, confidence}``); the runner
tags each finding with its lens name, flattens across lenses, and merges through
``synthesize_lens_findings`` so the surfaced set is deduped and agreement-gated
rather than a raw union (the union inherits every lens's false positives).

Spawning the lenses (the model calls) lives in the callables -- the same
pure-core / thin-IO split the rest of the engine uses, so this core is fully
testable without a subprocess. Bounded by ``max_lenses``; fails open per lens (a
lens that raises contributes nothing rather than sinking the whole pass).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chameleon_mcp.lens_synthesis import synthesize_lens_findings


@dataclass(frozen=True)
class Lens:
    """A named review lens: ``run()`` returns its raw findings, or [] / raises."""

    name: str
    run: Callable[[], list]


def correctness_lens(run_judge: Callable[[], list]) -> Lens:
    """Adapt the correctness judge into a lens.

    ``run_judge`` is a thunk returning the judge's ``Finding`` objects
    (``message``/``confidence``/``file``/``line``); the lens maps each to the
    synthesis shape. The thunk does the spawn -- run_lenses is the fail-open
    boundary, so a raising thunk drops just this lens.
    """

    def run() -> list[dict]:
        return [
            {
                "file": getattr(f, "file", None),
                "line": getattr(f, "line", None),
                "claim": getattr(f, "message", ""),
                "confidence": getattr(f, "confidence", 0.0),
            }
            for f in (run_judge() or [])
        ]

    return Lens(name="correctness", run=run)


def duplication_lens(run_dup: Callable[[], list]) -> Lens:
    """Adapt the turn-end duplication gate into a lens.

    ``run_dup`` is a thunk returning confirmed duplication ``Finding`` objects
    (``new_name``/``new_file``/``line``/``existing_name``/``existing_file``); the
    lens phrases each as a reuse claim. Confidence is 1.0 -- these are already
    judge-confirmed re-implementations, not raw model guesses.
    """

    def run() -> list[dict]:
        out: list[dict] = []
        for f in run_dup() or []:
            out.append(
                {
                    "file": getattr(f, "new_file", None),
                    "line": getattr(f, "line", None),
                    "claim": (
                        f"{getattr(f, 'new_name', '?')} re-implements "
                        f"{getattr(f, 'existing_name', '?')} "
                        f"({getattr(f, 'existing_file', '?')}) — reuse it"
                    ),
                    "confidence": 1.0,
                }
            )
        return out

    return Lens(name="duplication", run=run)


def run_lenses(lenses, *, max_lenses: int = 4, min_confidence: float = 0.7) -> list[dict]:
    """Run up to ``max_lenses`` lenses and return their synthesized findings.

    Each surfaced entry carries ``{file, line, claim, lenses, agreement,
    confidence, surface}`` per ``synthesize_lens_findings``: a finding two lenses
    independently raised (agreement >= 2) or one lens raised at/above
    ``min_confidence`` has ``surface = True``. A lens that raises is skipped.

    The lenses run CONCURRENTLY. Each lens thunk typically spawns its own
    reviewer subprocess with an independent wall-clock budget, so running them
    sequentially sums those budgets — two ~45s spawns can blow past the Stop
    hook's hard ``timeout`` cap, get SIGKILLed, and lose the whole review. Run
    them in parallel so the pass costs the slowest single lens, not the sum.
    Results are collected in lens order so synthesis (and tests) stay
    deterministic; a lens that raises contributes nothing.
    """
    selected = list(lenses)[:max_lenses]

    def _safe_run(lens: Lens) -> list:
        try:
            return lens.run() or []
        except Exception:
            return []

    if len(selected) <= 1:
        # Single (or zero) lens: run inline, no thread, no executor overhead.
        results = [(lens, _safe_run(lens)) for lens in selected]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(selected)) as ex:
            # ex.map preserves input order and yields one result per input, so
            # `results` stays lens-ordered and the zip lengths always match.
            results = list(zip(selected, ex.map(_safe_run, selected), strict=True))

    raw: list[dict] = []
    for lens, findings in results:
        for f in findings:
            if not isinstance(f, dict):
                continue
            raw.append(
                {
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "claim": f.get("claim", ""),
                    "lens": lens.name,
                    "confidence": f.get("confidence", 0.0),
                }
            )
    return synthesize_lens_findings(raw, min_confidence=min_confidence)
