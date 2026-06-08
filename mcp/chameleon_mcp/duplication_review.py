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
        for h, table in ((fn.body_hash, self.by_exact), (fn.body_hash_pnorm, self.by_pnorm)):
            if not h:
                continue
            hit = table.get(h)
            if hit is not None and hit.file != exclude_file:
                return hit
        return None


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


def _parse(repo_root: Path, path: str):
    """Indirection over parse_edited_functions so tests can stub one file."""
    from chameleon_mcp.tools import parse_edited_functions

    return parse_edited_functions(repo_root, path)


def _lang_of(path: str):
    from chameleon_mcp.function_catalog import _lang_from_path

    return _lang_from_path(path)


def gather_body_match_findings(repo_root: Path, edited_files: list[str], index, lang) -> list:
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
            hit = index.lookup(pf, exclude_file=rel)
            if hit is None:
                continue
            f = Finding(
                new_name=pf.name,
                new_file=rel,
                line=pf.start_line if pf.start_line is not None else 0,
                excerpt=pf.excerpt,
                existing_name=hit.name,
                existing_file=hit.file,
            )
            (exact if pf.body_hash else pnorm).append(f)
    return (exact + pnorm)[:max_findings]


# ---------------------------------------------------------------------------
# Task 7: Judge prompt, coercer, and judge_body_matches
# ---------------------------------------------------------------------------


def build_duplication_prompt(findings: list) -> str:
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    budget = threshold_int("DUPLICATION_REVIEW_MAX_PROMPT_BYTES")
    header = (
        "You are reviewing whether newly-edited functions re-implement existing "
        "ones. For each item, the NEW function body is shown with the EXISTING "
        "function it body-matched. Return ONLY a JSON array; one object per item "
        'that is a real re-implementation: {"new_name": "<name>", "is_duplicate": '
        "true}. Omit items that merely look similar but are not the same intent. "
        "No prose outside the JSON array.\n\n"
    )
    parts = [header]
    used = len(header)
    for f in findings:
        excerpt = sanitize_for_chameleon_context(f.excerpt)
        block = (
            f"### new: {f.new_name} ({f.new_file}:{f.line})\n"
            f"existing: {f.existing_name} ({f.existing_file})\n"
            f"new body:\n{excerpt}\n\n"
        )
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)


def _coerce_confirmed(arr, findings: list) -> list:
    by_name = {f.new_name: f for f in findings}
    confirmed: list = []
    seen: set = set()
    for item in arr or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_duplicate") is not True:
            continue
        name = item.get("new_name")
        if name in by_name and name not in seen:
            seen.add(name)
            confirmed.append(by_name[name])
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


def judge_body_matches(repo_root: Path, findings: list) -> list:
    if not findings:
        return []
    try:
        from chameleon_mcp import judge

        stdout = judge._spawn_reviewer(build_duplication_prompt(findings), Path(repo_root))
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


def _marker_path(repo_data: Path, session_id: str, file_rel: str, digest: str) -> Path:
    import hashlib

    key = hashlib.sha256(f"{session_id}\x00{file_rel}\x00{digest}".encode()).hexdigest()[:32]
    return Path(repo_data) / f".dup_judged.{key}"


def already_judged(repo_data: Path, session_id: str, file_rel: str, digest: str) -> bool:
    try:
        return _marker_path(repo_data, session_id, file_rel, digest).exists()
    except OSError:
        return False


def mark_judged(repo_data: Path, session_id: str, file_rel: str, digest: str) -> None:
    try:
        p = _marker_path(repo_data, session_id, file_rel, digest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass
