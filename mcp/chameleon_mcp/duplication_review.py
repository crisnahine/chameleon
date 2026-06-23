"""Turn-end duplication gate: body-hash match against the catalog + session union.

Advisory-only Stop pass. Matches each turn's new functions by body_hash /
body_hash_pnorm equality against the committed function catalog and the functions
added earlier this session, confirms real re-implementations with a bounded judge,
and returns sanitized advisory lines. Never blocks; fails open everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class IndexEntry:
    name: str
    file: str


@dataclass
class CandidateIndex:
    by_exact: dict[str, IndexEntry] = field(default_factory=dict)
    by_pnorm: dict[str, IndexEntry] = field(default_factory=dict)

    def add_function(self, file: str, name: str, *, body_hash, body_hash_pnorm) -> None:
        entry = IndexEntry(name=name, file=file)
        if body_hash:
            self.by_exact.setdefault(body_hash, entry)
        if body_hash_pnorm:
            self.by_pnorm.setdefault(body_hash_pnorm, entry)

    def lookup(self, fn, *, exclude_file: str):
        """Return (IndexEntry, match_type) or (None, None).

        Tries the exact body-hash index first, then the pnorm index.
        match_type is "exact" or "pnorm" depending on which index produced the hit.
        """
        for h, table, match_type in (
            (fn.body_hash, self.by_exact, "exact"),
            (fn.body_hash_pnorm, self.by_pnorm, "pnorm"),
        ):
            if not h:
                continue
            hit = table.get(h)
            if hit is not None and hit.file != exclude_file:
                return hit, match_type
        return None, None


def build_candidate_index(repo_root: Path, session_files: list[str]) -> CandidateIndex:
    """Catalog rows + this session's parsed functions, indexed by body hash.

    Fail-open: a missing/unreadable catalog or an unparseable session file simply
    contributes nothing. The session side reuses parse_edited_functions.
    """
    idx = CandidateIndex()
    try:
        from chameleon_mcp.function_catalog import load_function_catalog

        catalog = load_function_catalog(repo_root)
        if catalog is not None:
            for cf in catalog.functions:
                idx.add_function(
                    cf.file, cf.name, body_hash=cf.body_hash, body_hash_pnorm=cf.body_hash_pnorm
                )
    except Exception:
        pass
    try:
        from chameleon_mcp.tools import parse_edited_functions

        for path in session_files:
            rel = _repo_rel(repo_root, path)
            for pf in parse_edited_functions(repo_root, path):
                idx.add_function(
                    rel, pf.name, body_hash=pf.body_hash, body_hash_pnorm=pf.body_hash_pnorm
                )
    except Exception:
        pass
    return idx


def _repo_rel(repo_root: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).name


# ---------------------------------------------------------------------------
# Task 6: gather_body_match_findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    new_name: str
    new_file: str
    line: int
    excerpt: str
    existing_name: str
    existing_file: str
    # Body of the matched existing function, read from disk at gather time.
    # The judge is told to omit "merely similar" items, so without the
    # existing body to compare against it conservatively omits everything —
    # even byte-for-byte copies.
    existing_excerpt: str = ""


def _parse(repo_root: Path, path: str):
    """Indirection over parse_edited_functions so tests can stub one file."""
    from chameleon_mcp.tools import parse_edited_functions

    return parse_edited_functions(repo_root, path)


def _lang_of(path: str):
    from chameleon_mcp.function_catalog import _lang_from_path

    return _lang_from_path(path)


def gather_body_match_findings(repo_root: Path, edited_files: list[str], index, lang) -> list:
    try:
        from chameleon_mcp._thresholds import threshold_int

        max_files = threshold_int("DUPLICATION_REVIEW_MAX_FILES")
        max_findings = threshold_int("DUPLICATION_REVIEW_MAX_FINDINGS")
        exact: list = []
        pnorm: list = []
        for path in edited_files[:max_files]:
            if lang is not None and _lang_of(path) != lang:
                continue
            rel = _repo_rel(repo_root, path)
            try:
                parsed = _parse(repo_root, path)
            except Exception:
                continue
            for pf in parsed:
                hit, match_type = index.lookup(pf, exclude_file=rel)
                if hit is None:
                    continue
                try:
                    from chameleon_mcp._thresholds import threshold_int as _ti
                    from chameleon_mcp.tools import _candidate_body_excerpt

                    existing_excerpt = _candidate_body_excerpt(
                        Path(repo_root),
                        hit.file,
                        hit.name,
                        _ti("DUPLICATION_BODY_EXCERPT_LINES"),
                    )
                except Exception:
                    existing_excerpt = ""
                f = Finding(
                    new_name=pf.name,
                    new_file=rel,
                    line=pf.start_line if pf.start_line is not None else 0,
                    excerpt=pf.excerpt,
                    existing_name=hit.name,
                    existing_file=hit.file,
                    existing_excerpt=existing_excerpt,
                )
                (exact if match_type == "exact" else pnorm).append(f)
        return (exact + pnorm)[:max_findings]
    except Exception:
        return []


def gather_semantic_findings(repo_root: Path, edited_files: list[str], catalog, lang) -> list:
    """Name/shape-prefiltered duplication candidates from the committed catalog.

    The body-hash gate only sees byte-identical (or param-renamed) clones. This
    pass reuses the pr-review prefilter (``select_candidates``: name-token overlap
    + signature shape, with body-identical matches ranked first) so a helper that
    re-implements an existing one with a DIFFERENT body is surfaced too. One
    Finding per new function, against its top-ranked candidate. Fails open to [].

    Scoped to the committed catalog only (not this session's earlier functions);
    within-session re-implementations stay the body-hash gate's job.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.function_catalog import NewFunction, select_candidates

        if catalog is None:
            return []
        max_files = threshold_int("DUPLICATION_REVIEW_MAX_FILES")
        max_findings = threshold_int("DUPLICATION_REVIEW_MAX_FINDINGS")
        excerpt_lines = threshold_int("DUPLICATION_BODY_EXCERPT_LINES")
        min_shared = threshold_int("DUPLICATION_SEMANTIC_MIN_SHARED_TOKENS")
        out: list = []
        for path in edited_files[:max_files]:
            if lang is not None and _lang_of(path) != lang:
                continue
            rel = _repo_rel(repo_root, path)
            try:
                parsed = _parse(repo_root, path)
            except Exception:
                continue
            # Map ParsedFn -> NewFunction, deduplicating overload sets on (name,
            # arity, required) exactly as get_duplication_candidates does; keep
            # the first ParsedFn per name so the Finding can cite its line/body.
            new_functions: list = []
            by_name: dict = {}
            seen: set = set()
            for pf in parsed:
                by_name.setdefault(pf.name, pf)
                key = (pf.name, pf.arity, pf.required)
                if key in seen:
                    continue
                seen.add(key)
                new_functions.append(
                    NewFunction(
                        name=pf.name,
                        kind=pf.kind,
                        arity=pf.arity,
                        required=pf.required,
                        body_hash=pf.body_hash,
                        body_hash_pnorm=pf.body_hash_pnorm,
                    )
                )
            if not new_functions:
                continue
            for match in select_candidates(catalog, new_functions, exclude_file=rel):
                candidates = match.get("candidates") or []
                if not candidates:
                    continue
                pf = by_name.get(match["function"]["name"])
                if pf is None:
                    continue
                top = candidates[0]
                # Precision gate: turn-end nags mid-edit, so a body-identical
                # clone always qualifies but a name-only lead must clear a higher
                # shared-token bar than the looser pr-review prefilter. A single
                # shared token (state, address, sales) is overwhelmingly noise.
                if not top.get("body_match") and len(top.get("shared_tokens") or []) < min_shared:
                    continue
                try:
                    from chameleon_mcp.tools import _candidate_body_excerpt

                    existing_excerpt = _candidate_body_excerpt(
                        Path(repo_root), top["file"], top["name"], excerpt_lines
                    )
                except Exception:
                    existing_excerpt = ""
                out.append(
                    Finding(
                        new_name=pf.name,
                        new_file=rel,
                        line=pf.start_line if pf.start_line is not None else 0,
                        excerpt=pf.excerpt,
                        existing_name=top["name"],
                        existing_file=top["file"],
                        existing_excerpt=existing_excerpt,
                    )
                )
        return out[:max_findings]
    except Exception:
        return []


def gather_findings(repo_root: Path, edited_files: list[str], *, index, catalog, lang) -> list:
    """Body-hash matches UNION name/shape semantic candidates, deduped and capped.

    The body-hash pass (catalog + this session's earlier functions) keeps the
    within-session and byte-identical detection; the semantic pass adds the
    different-body / same-intent case from the committed catalog. Body-hash
    findings come first so an exact match is preferred over a looser candidate
    for the same pair. Deduped on (new_name, new_file, existing_name,
    existing_file); capped at DUPLICATION_REVIEW_MAX_FINDINGS. Fails open to [].
    """
    try:
        from chameleon_mcp._thresholds import threshold_int

        body = gather_body_match_findings(repo_root, edited_files, index, lang)
        semantic = gather_semantic_findings(repo_root, edited_files, catalog, lang)
        merged: list = []
        seen: set = set()
        for f in [*body, *semantic]:
            key = (f.new_name, f.new_file, f.existing_name, f.existing_file)
            if key in seen:
                continue
            seen.add(key)
            merged.append(f)
        return merged[: threshold_int("DUPLICATION_REVIEW_MAX_FINDINGS")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Task 7: Judge prompt, coercer, and judge_body_matches
# ---------------------------------------------------------------------------


def build_duplication_prompt(findings: list, semantic: bool = False) -> str:
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    budget = threshold_int("DUPLICATION_REVIEW_MAX_PROMPT_BYTES")
    if semantic:
        # The semantic candidates were surfaced by name/shape similarity (or an
        # identical body), so their bodies may DIFFER. Ask the judge to confirm
        # only those that re-implement the existing function's intent.
        header = (
            "You are reviewing whether newly-edited functions re-implement "
            "existing ones. Each item has an id and pairs a NEW function with an "
            "EXISTING candidate surfaced by name/shape similarity; their bodies "
            "may differ. Return ONLY a JSON array; one object per item that "
            're-implements the same intent: {"id": <id>, "is_duplicate": true}. '
            "Omit items that do a different job. No prose outside the JSON "
            "array.\n\n"
        )
    else:
        header = (
            "You are reviewing whether newly-edited functions re-implement existing "
            "ones. Each item has an id; the NEW function body is shown with the "
            "EXISTING function it body-matched. Return ONLY a JSON array; one "
            'object per item that is a real re-implementation: {"id": <id>, '
            '"is_duplicate": true}. Omit items that are not the same intent. No '
            "prose outside the JSON array.\n\n"
        )
    parts = [header]
    used = len(header)
    # The id is the finding's position in this list; _coerce_confirmed maps it
    # back the same way. Echoing a stable integer keeps two functions that share
    # a name in different files distinct.
    for idx, f in enumerate(findings):
        excerpt = sanitize_for_chameleon_context(f.excerpt)
        existing_excerpt = sanitize_for_chameleon_context(f.existing_excerpt or "")
        block = (
            f"### id {idx}: {f.new_name} ({f.new_file}:{f.line})\n"
            f"new body:\n{excerpt}\n"
            f"existing: {f.existing_name} ({f.existing_file})\n"
            f"existing body:\n{existing_excerpt or '(source unavailable)'}\n\n"
        )
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)


def _coerce_confirmed(arr, findings: list) -> list:
    """Map the judge's confirmation echo back to Finding objects.

    The judge echoes the per-item integer id assigned by build_duplication_prompt;
    that id is the finding's position in this same list, so it disambiguates two
    findings sharing a new_name. A new_name echo is still honored as a fallback
    for a judge that omits the id, and the fallback resolves to the first
    not-yet-confirmed finding with that name so several same-named findings each
    get their own confirmation.
    """
    confirmed: list = []
    confirmed_idx: set = set()

    def _confirm(idx: int) -> None:
        if 0 <= idx < len(findings) and idx not in confirmed_idx:
            confirmed_idx.add(idx)
            confirmed.append(findings[idx])

    for item in arr or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_duplicate") is not True:
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, bool):
            raw_id = None
        if isinstance(raw_id, int):
            _confirm(raw_id)
            continue
        if isinstance(raw_id, str) and raw_id.strip().lstrip("-").isdigit():
            _confirm(int(raw_id.strip()))
            continue
        name = item.get("new_name")
        if name is None:
            continue
        for idx, f in enumerate(findings):
            if f.new_name == name and idx not in confirmed_idx:
                _confirm(idx)
                break
    return confirmed


def _stream_texts(stdout: str):
    """Yield candidate text payloads from claude -p stream-json output.

    Mirrors judge._parse_findings' extraction logic: collects both
    ``type=result`` result strings and ``type=assistant`` text blocks,
    then returns them newest-first so the caller can stop at the first
    parseable JSON array.
    """
    import json as _json

    texts: list = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            texts.append(obj["result"])
        elif obj.get("type") == "assistant":
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            texts.append(t)
    return list(reversed(texts))


def judge_body_matches(repo_root: Path, findings: list, semantic: bool = False) -> list:
    if not findings:
        return []
    try:
        from chameleon_mcp import judge

        stdout = judge._spawn_reviewer(
            build_duplication_prompt(findings, semantic=semantic), Path(repo_root)
        )
        if not stdout:
            return []
        arr = None
        for text in _stream_texts(stdout):
            arr = judge._extract_json_array(text)
            if arr is not None:
                break
        return _coerce_confirmed(arr, findings)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Task 8: format_duplication_advisory
# ---------------------------------------------------------------------------


def format_duplication_advisory(confirmed: list) -> list:
    if not confirmed:
        return []
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    n = len(confirmed)
    lines = [f"[\U0001f98e chameleon: {n} possible duplicate{'s' if n != 1 else ''}]"]
    for f in confirmed:
        lines.append(
            sanitize_for_chameleon_context(
                f"{f.new_name} ({f.new_file}:{f.line}) re-implements "
                f"{f.existing_name} ({f.existing_file}) — reuse it."
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Task 9: Per-(file,digest) dedup marker + spawn-cap helpers
# ---------------------------------------------------------------------------


def _marker_path(
    repo_data: Path,
    session_id: str,
    file_rel: str,
    digest: str,
    *,
    prefix: str = ".dup_judged.",
) -> Path:
    import hashlib

    key = hashlib.sha256(f"{session_id}\x00{file_rel}\x00{digest}".encode()).hexdigest()[:32]
    return Path(repo_data) / f"{prefix}{key}"


def already_judged(
    repo_data: Path,
    session_id: str,
    file_rel: str,
    digest: str,
    *,
    prefix: str = ".dup_judged.",
) -> bool:
    try:
        return _marker_path(repo_data, session_id, file_rel, digest, prefix=prefix).exists()
    except OSError:
        return False


def mark_judged(
    repo_data: Path,
    session_id: str,
    file_rel: str,
    digest: str,
    *,
    prefix: str = ".dup_judged.",
) -> None:
    """Record a (session, file, digest) as judged under a marker namespace.

    ``prefix`` selects the namespace: the duplication gate uses the default
    ``.dup_judged.`` (existing markers stay valid byte-for-byte) and the
    correctness gate passes ``.corr_judged.`` so the two judged-sets never
    collide.
    """
    try:
        p = _marker_path(repo_data, session_id, file_rel, digest, prefix=prefix)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass
