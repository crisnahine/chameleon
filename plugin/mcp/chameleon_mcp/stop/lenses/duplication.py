"""The duplication lens: wraps ``duplication_review``'s body-hash + semantic
gather-and-confirm pipeline to return canonical ``core.finding.Finding``
objects instead of the standalone gate's rendered advisory lines.

This module does not replace the standalone duplication gate
(``hook_helper._duplication_advisory_lines``) yet -- that function stays a
live, callable turn-end gate until Task 7 deletes its last caller -- it is
the NEW consumer of the same pipeline (``build_candidate_index``,
``gather_findings`` -- which internally runs both
``gather_body_match_findings`` and ``gather_semantic_findings`` -- and
``judge_body_matches``), wired in the identical order and with the identical
fail-open contract, so a future caller (the Task-4 job runner) gets
canonical Findings out of the duplication review instead of the ad hoc
``duplication_review.Finding`` shape only the old gate's renderer understood.

Top-level imports stay stdlib-only; ``duplication_review``,
``core.finding.Finding``, and ``function_catalog`` are resolved via a
deferred import inside ``run``, mirroring the rest of the ``stop/``
package's pattern of deferring every non-stdlib import to call time so a
test that patches ``chameleon_mcp.duplication_review.<name>`` stays
effective for a call made from here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.stop.lenses import LensResult


def _claim_for(f) -> str:
    """The pinned "re-implements" advisory line, minus its sanitize wrapper
    and the header this Finding no longer carries -- byte-identical to the
    per-item line ``duplication_review.format_duplication_advisory`` renders
    (``test_duplication_review_format.py`` pins that exact template).
    """
    suffix = "reuse it"
    count = f.called_from_n_sites
    if isinstance(count, int) and count > 0:
        sites = "1 site" if count == 1 else f"{count} sites"
        suffix = f"reuse it; already called from {sites}"
    return f"{f.new_name} ({f.new_file}:{f.line}) re-implements {f.existing_name} ({f.existing_file}) — {suffix}."


def run(
    repo_root: Path,
    profile_dir: Path,
    files: list[str],
    archetype_for,
    *,
    intent_tokens: list[str] | None = None,
    budget: float | None = None,
    event_sink=None,
    model: str | None = None,
) -> LensResult:
    """Run the duplication review for one turn's edited ``files``.

    Mirrors the standalone gate's sequence exactly: infer the catalog
    language from the first edited file, build the candidate index over the
    edited files, gather body-hash + semantic matches against the committed
    catalog, then confirm each candidate with a bounded judge spawn. Every
    stage fails open to an empty finding list -- no edited files, no
    candidates, a spawn failure, or an unconfirmed candidate all return
    ``LensResult(findings=[], check_events=[...])``.

    ``profile_dir``, ``archetype_for``, ``budget``, and ``model`` are part
    of the shared lens signature (every registered lens is called
    uniformly) but are not used here: the duplication pipeline is per-
    catalog-language, not per-archetype, and neither ``gather_findings``
    nor ``judge_body_matches`` accepts a caller-supplied model or timeout --
    both are resolved internally the same way the standalone gate leaves
    them, so this lens does not thread values those functions cannot take.

    ``event_sink``, when given, receives every ``(kind, detail)`` event the
    same way ``correctness.run``'s sink does; the identical events are also
    collected into the returned ``LensResult.check_events``.
    """
    from chameleon_mcp import duplication_review as dr
    from chameleon_mcp.core.finding import Finding, compute_match_key, normalize_severity
    from chameleon_mcp.stop.lenses import LensResult

    events: list[tuple[str, str]] = []

    def _sink(kind: str, detail: str | None = None) -> None:
        events.append((kind, detail or ""))
        if event_sink is None:
            return
        try:
            event_sink(kind, detail)
        except Exception:
            pass

    try:
        edited = [p for p in files if Path(p).is_file()]
        if not edited:
            return LensResult(findings=[], check_events=events)

        lang = dr._lang_of(edited[0])
        index = dr.build_candidate_index(repo_root, edited)

        try:
            from chameleon_mcp.function_catalog import load_function_catalog

            catalog = load_function_catalog(repo_root)
        except Exception:
            catalog = None

        raw_findings = dr.gather_findings(
            repo_root, edited, index=index, catalog=catalog, lang=lang
        )
        if not raw_findings:
            return LensResult(findings=[], check_events=events)

        _sink("duplication_review", "ran")
        confirmed = dr.judge_body_matches(repo_root, raw_findings, semantic=True)
        if not confirmed:
            return LensResult(findings=[], check_events=events)

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        canonical: list[Finding] = []
        for f in confirmed:
            claim = _claim_for(f)
            canonical.append(
                Finding(
                    id=compute_match_key(claim, f.new_file, "duplication"),
                    kind="duplication",
                    severity=normalize_severity("high"),
                    confidence=1.0,
                    file=f.new_file,
                    span=(f.line, f.line),
                    claim=claim,
                    evidence="",
                    excerpt_sha="",
                    excerpt="",
                    source_lens="duplication",
                    status="pending",
                    created_at=created_at,
                    intent_tokens=tuple(intent_tokens or ()),
                )
            )
        return LensResult(findings=canonical, check_events=events)
    except Exception as exc:
        _sink("pipeline_error", repr(exc)[:200])
        return LensResult(findings=[], check_events=events)
