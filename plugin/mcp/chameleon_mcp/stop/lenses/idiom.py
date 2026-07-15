"""The idiom lens: a NEW review pass (spec 2026-07-14 section 5.2) that
replaces ``_idiom_review_gate`` -- not by deleting it (Task 7 does that once
every caller has moved) but by giving the job runner a scoped, citation-
required detector to call instead.

The legacy gate dumps every taught idiom (reordered, char-capped) and forces
a once-per-session Stop block regardless of whether any of it applies to the
turn's edit. This lens instead scopes the idiom STORE to the turn's own diff
-- the languages, archetypes, and paths of the files actually touched
(``core.idiom_store.idioms_for_scope``; an idiom's empty dimension is a
wildcard) -- and spawns a reviewer only over THOSE idioms plus the relevant
diff hunks. A turn with no scoped idioms produces no findings and never
spawns a reviewer at all: compliant and out-of-scope turns are silent,
the opposite of the legacy gate's guaranteed interrupt.

Every surviving claim must cite the violated idiom's slug AND the offending
diff line numbers -- a claim naming neither is not a grounded citation of a
real taught idiom against a real changed line, so it is dropped rather than
trusted on the reviewer's prose alone.

Top-level imports stay stdlib-only; ``judge``, ``core.finding.Finding``,
``core.idiom_store``, and ``lint_engine.detect_language`` are resolved via a
deferred import inside the functions that need them, mirroring the rest of
the ``stop/`` package's pattern of deferring every non-stdlib import to call
time so a test that patches ``chameleon_mcp.judge.<name>`` stays effective
for a call made from here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.finding import Finding
    from chameleon_mcp.core.idiom_store import IdiomRecord
    from chameleon_mcp.judge import FileDiff
    from chameleon_mcp.stop.lenses import LensResult


_HEADER = (
    "You are reviewing a code change against this team's taught idioms -- "
    "conventions specific to this repository that generic style rules would "
    "not catch. Each idiom below has a slug, a directive, and (when taught) "
    "an example of the right pattern and a counterexample of the wrong one. "
    "Check the CHANGED lines in the diffs below against each idiom in "
    "scope.\n\n"
    "Return ONLY a JSON array (no prose, no code fence). Each element is an "
    'object: {"slug": "<the violated idiom\'s slug, exactly as listed '
    'below>", "file": "<repo-relative path>", "lines": [<diff line numbers '
    'that violate it>], "message": "<one-sentence explanation>", '
    '"confidence": <float 0..1, optional>}. Every object MUST include a '
    "real slug from the list below AND at least one line number, or it is "
    "discarded -- do not cite an idiom without pointing at the offending "
    "lines. Omit idioms the change does not violate. Return [] if the "
    "change violates none of them.\n\n"
)


def _governed_language(rel_path: str) -> str | None:
    """Map a file to its idiom-governed language, or None.

    A notebook cell is Python source (``detect_language('.ipynb')`` is
    None), so a notebook-only turn stays governed instead of silently
    skipping idiom review.
    """
    from chameleon_mcp.lint_engine import detect_language

    if rel_path.lower().endswith(".ipynb"):
        return "python"
    return detect_language(rel_path)


def _render_idiom_for_prompt(rec: IdiomRecord) -> str:
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    lines = [f"### {rec.slug}: {sanitize_for_chameleon_context(rec.title)}"]
    lines.append(sanitize_for_chameleon_context(rec.rationale.strip()))
    for label, items in (("Example:", rec.examples), ("Counterexample:", rec.counterexamples)):
        for code in items:
            lines.append(f"{label}\n{sanitize_for_chameleon_context(code.rstrip())}")
    return "\n".join(lines) + "\n\n"


def _build_prompt(scoped: list[IdiomRecord], diffs: list[FileDiff]) -> str:
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    budget = threshold_int("IDIOM_LENS_MAX_PROMPT_BYTES")
    max_idioms = threshold_int("IDIOM_LENS_MAX_IDIOMS")
    parts: list[str] = [_HEADER, "Idioms in scope:\n\n"]
    used = len(parts[0]) + len(parts[1])
    for rec in scoped[:max_idioms]:
        block = _render_idiom_for_prompt(rec)
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    tail = "\nChanged files:\n\n"
    parts.append(tail)
    used += len(tail)
    for fd in diffs:
        header = f"=== {fd.rel_path}"
        header += (
            " (full file; no diff available)" if fd.is_whole_file else " (unified diff vs HEAD)"
        )
        header += " ===\n"
        block = header + sanitize_for_chameleon_context(fd.diff_text) + "\n\n"
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)


def _parse_idiom_claims(stdout: str) -> tuple[list, bool]:
    """Parse the reviewer's stream-json output into ``(claims, parsed_ok)``.

    Reuses ``judge``'s stream-json text extraction and JSON-array scan (the
    same primitives ``judge._parse_findings_status`` uses) but does NOT
    reuse ``judge._coerce_findings``: a judge Finding has no ``slug``/
    ``lines`` fields, so the claim schema this lens requires needs its own
    coercion (``_coerce_claim``).
    """
    from chameleon_mcp import judge

    for text in reversed(judge._stream_json_texts(stdout)):
        arr = judge._extract_json_array(text)
        if arr is not None:
            return arr, True
    return [], False


def _coerce_claim(
    item, slug_by: dict[str, IdiomRecord], *, intent_tokens, created_at: str
) -> Finding | None:
    """Validate one raw claim and adapt it into a canonical Finding, or None.

    A claim survives only when it names a REAL scoped idiom's slug and at
    least one positive offending line number -- both required by the citation
    contract (spec section 5.2: "must cite the violated slug AND offending
    diff lines in every claim"). ``file``/``message``/``confidence`` are all
    best-effort: a missing file falls back to "", a missing message to a
    generic label, and a missing/invalid confidence to a neutral 0.6 (the
    same "own the uncertainty rather than fabricate precision" default the
    rest of the reviewer pipeline uses).
    """
    from chameleon_mcp.core.finding import Finding, compute_match_key, normalize_severity

    if not isinstance(item, dict):
        return None
    raw_slug = item.get("slug")
    slug = raw_slug.strip() if isinstance(raw_slug, str) else ""
    lines_raw = item.get("lines")
    offending = (
        [n for n in lines_raw if isinstance(n, int) and not isinstance(n, bool) and n > 0]
        if isinstance(lines_raw, list)
        else []
    )
    if not slug or slug not in slug_by or not offending:
        return None

    rec = slug_by[slug]
    raw_file = item.get("file")
    file = raw_file.strip() if isinstance(raw_file, str) and raw_file.strip() else ""
    raw_message = item.get("message")
    message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else "violates this idiom"
    )
    raw_conf = item.get("confidence")
    if isinstance(raw_conf, bool):
        confidence = 0.6
    else:
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    severity = normalize_severity("high" if confidence >= 0.7 else "medium")
    span = (min(offending), max(offending))
    claim = f"idiom '{slug}' ({rec.title}): {message}"
    return Finding(
        id=compute_match_key(claim, file, "idiom"),
        kind="idiom",
        severity=severity,
        confidence=confidence,
        file=file,
        span=span,
        claim=claim,
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="idiom",
        status="pending",
        created_at=created_at,
        intent_tokens=tuple(intent_tokens or ()),
    )


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
    """Run the scoped idiom review for one turn's edited ``files``.

    Diffs are reconstructed first (``judge.collect_file_diffs``, the same
    evidence builder the correctness lens uses -- it already resolves each
    file's archetype via ``archetype_for`` and fails open per file), then
    filtered to idiom-governed files (a recognized source language). No
    governed files, or a scoped idiom set that comes back empty once the
    store is filtered through ``idioms_for_scope``, both return
    ``LensResult([], [("idiom_lens", "no_scoped_idioms")])`` WITHOUT
    spawning a reviewer -- computing diffs is cheap (no model call); only
    the spawn is the expensive step this lens is careful to skip.

    Every check event this lens records is namespaced ``("idiom_lens",
    <detail>)`` (unlike the correctness lens's flat per-outcome kind
    strings) so a caller folding events from every active lens into one
    stream can still tell which lens produced which event.
    """
    from chameleon_mcp import judge
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.core.idiom_store import idioms_for_scope, load_store
    from chameleon_mcp.stop.lenses import LensResult

    events: list[tuple[str, str]] = []

    def _sink(detail: str) -> None:
        events.append(("idiom_lens", detail))
        if event_sink is None:
            return
        try:
            event_sink("idiom_lens", detail)
        except Exception:
            pass

    try:
        diffs = judge.collect_file_diffs(repo_root, files, archetype_for)
        governed = [fd for fd in diffs if _governed_language(fd.rel_path) is not None]
        if not governed:
            _sink("no_scoped_idioms")
            return LensResult(findings=[], check_events=events)

        languages = {_governed_language(fd.rel_path) for fd in governed}
        archetypes = {fd.archetype for fd in governed if fd.archetype}
        rel_paths = [fd.rel_path for fd in governed]

        records = load_store(profile_dir)
        scoped = idioms_for_scope(
            records, languages=languages, archetypes=archetypes, paths=rel_paths
        )
        # idioms_for_scope treats an empty set on EITHER side of a dimension as
        # a wildcard. That is right for a record's own empty declaration, but
        # wrong for the caller side of archetypes: governed files that resolve
        # NO archetype at all (ordinary -- utility/script files the detector
        # does not classify) yield an empty caller set, which would let an
        # archetype-TAGGED idiom back into scope with no matching file. The
        # spec's languages/archetypes/paths intersection requires a declared
        # archetype to be matched by a touched file, so drop archetype-specific
        # records here; wildcard records (empty rec.archetypes) rightly stay.
        # languages has no symmetric hole: `governed` is BY CONSTRUCTION the
        # files whose _governed_language is not None, so a non-empty governed
        # set always yields a non-empty languages set; and rel_paths is one
        # entry per governed file, so the paths dimension cannot be empty here
        # either.
        if not archetypes:
            scoped = [rec for rec in scoped if not rec.archetypes]
        if not scoped:
            _sink("no_scoped_idioms")
            return LensResult(findings=[], check_events=events)

        prompt = _build_prompt(scoped, governed)

        timeout_s = int(budget) if isinstance(budget, (int, float)) and budget > 0 else None
        stdout, fail_reason = judge._spawn_reviewer_status(
            prompt, repo_root, model=model, timeout_s=timeout_s
        )
        if stdout is None:
            _sink(fail_reason or "spawn_exec_error")
            return LensResult(findings=[], check_events=events)

        raw_claims, parsed_ok = _parse_idiom_claims(stdout)
        if not parsed_ok:
            _sink("unparseable_output")

        slug_by = {r.slug: r for r in scoped}
        max_findings = threshold_int("IDIOM_LENS_MAX_FINDINGS")
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        findings = []
        for item in raw_claims:
            if len(findings) >= max_findings:
                break
            finding = _coerce_claim(
                item, slug_by, intent_tokens=intent_tokens, created_at=created_at
            )
            if finding is None:
                _sink("claim_missing_citation")
                continue
            findings.append(finding)
        return LensResult(findings=findings, check_events=events)
    except Exception as exc:
        _sink(f"pipeline_error:{repr(exc)[:200]}")
        return LensResult(findings=[], check_events=events)
