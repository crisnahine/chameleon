"""Per-function catalog and the duplication-candidate prefilter.

The flat key_exports list catches a new ``formatDate`` colliding with an
existing ``formatDate`` by exact name, but it cannot see that a new
``toDisplayDate`` re-implements the existing ``formatDate`` under a different
name -- the single most common "this already exists, call X instead"
maintainability comment. Catching that needs the repo's functions cataloged by
more than name.

This module builds and reads a committed ``function_catalog.json`` recording,
per top-level/exported function or method, its name, kind, normalized signature
shape (positional arity + which slots are optional), and the file it lives in.
The signature is shape-only; no body is stored. The catalog is the cheap
candidate-narrowing layer for cross-file duplication: given the functions a file
defines, :func:`select_candidates` returns the handful of existing functions
whose signature shape and name tokens overlap, and the LLM caller (PR-review /
the turn-end judge) does the actual semantic-equivalence judging against those
candidates' real bodies read from disk. The prefilter never decides duplication;
it only bounds what the judge has to look at.

Plain Python throughout: arity comparison and name-token overlap, no MinHash.
Same-intent functions with different implementations share almost no token
shingles, so a syntactic near-duplicate index would miss exactly the renamed
re-implementations this targets; name tokens plus signature shape narrow far
better for that case.

Two halves live here so the build (bootstrap-time, populates the artifact) and
the read (tool-time, consumes it) share one schema and cannot drift:

- :func:`build_function_catalog` turns parsed files into the artifact payload.
- :func:`load_function_catalog` reads the committed artifact, cached on
  (mtime, size) so a mid-session refresh is picked up without re-reading.

Conservative and bounded by construction. The number of files and the functions
recorded per file are capped (see :mod:`chameleon_mcp._thresholds`) so one
generated file cannot bloat the artifact. Anonymous callables carry no stable
name and are never recorded (the dump scripts already skip them). Loading fails
open to None on any ambiguity -- missing, corrupt, future-schema, oversized, or
any I/O error -- so the duplication read simply does not fire rather than crash
or fabricate.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

FUNCTION_CATALOG_FILENAME = "function_catalog.json"
SCHEMA_VERSION = 1

# A camelCase / PascalCase / snake_case / kebab boundary splitter. A name is
# lowered and split into word tokens so toDisplayDate and formatDate compare on
# {to, display, date} vs {format, date} -- the overlap on "date" is the reuse
# hint. Single-character fragments are dropped as noise.
_TOKEN_BOUNDARY_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Generic name tokens carry no reuse signal: nearly every helper "gets",
# "builds", or "handles" something, so overlap on these would pair unrelated
# functions. They are stripped before the overlap test so a match must rest on a
# domain token (date, slug, total, price), not a verb every function shares.
_STOPWORD_TOKENS = frozenset(
    {
        "get",
        "set",
        "is",
        "has",
        "to",
        "of",
        "the",
        "a",
        "an",
        "do",
        "make",
        "build",
        "create",
        "new",
        "handle",
        "process",
        "run",
        "fn",
        "func",
        "method",
        "value",
        "val",
        "data",
        "item",
        "obj",
        "self",
        # Connector / preposition tokens carry no reuse signal on their own; a
        # match resting only on `in`/`on`/`for` (e.g. `shuffleDeckInPlace` vs
        # `updateAccountInCache`) is pure noise that crowds out the real
        # counterpart under the candidate cap.
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "into",
        "with",
        "and",
        "or",
        "as",
        "via",
    }
)


def name_tokens(name: str) -> frozenset[str]:
    """Lowered domain-word tokens of a callable name, stopwords removed.

    ``toDisplayDate`` -> {display, date}; ``format_date`` -> {date} (format is
    not a stopword, so actually {format, date}); ``getX`` -> {x}. Used to score
    name overlap between a new function and a catalog candidate. Single-character
    tokens and the generic-verb stopwords are dropped so overlap rests on a real
    domain word.
    """
    if not isinstance(name, str) or not name:
        return frozenset()
    raw = (t for t in _TOKEN_BOUNDARY_RE.split(name) if t)
    out = {t.lower() for t in raw if len(t) > 1}
    return frozenset(out - _STOPWORD_TOKENS)


def _signature_shape(params: object) -> tuple[int, int]:
    """Reduce a param list to (positional arity, required arity).

    Shape-only: parameter NAMES are intentionally discarded here (they feed the
    name-token test, not the arity test). Required arity is positional arity
    minus the optional slots, so two functions with the same total arity but a
    different required/optional split are distinguished. A rest/destructured slot
    counts toward arity like any positional.
    """
    if not isinstance(params, list):
        return (0, 0)
    arity = 0
    required = 0
    for p in params:
        if not isinstance(p, dict):
            continue
        arity += 1
        if not bool(p.get("optional")):
            required += 1
    return (arity, required)


@dataclass(frozen=True)
class CatalogedFunction:
    """One function recorded in the catalog.

    ``arity`` / ``required`` are the signature shape; ``tokens`` are the lowered
    domain-word tokens of the name, precomputed at load so the prefilter does not
    re-tokenize every candidate per query. ``body_hash`` is the normalized-body
    fingerprint (None for rows built before spans were recorded, or for bodies
    too short to be a meaningful identity).
    """

    name: str
    kind: str
    file: str
    arity: int
    required: int
    tokens: frozenset[str]
    body_hash: str | None = None


def normalized_body_hash(
    source_lines: list[str], start_line: object, end_line: object
) -> str | None:
    """Fingerprint a function body for the exact-clone fallback, or None.

    Slices the 1-based inclusive ``start_line``..``end_line`` span, DROPS the
    first line (it carries the function's name, which differs between a clone
    and its original), collapses all whitespace, and hashes. Bodies shorter
    than the minimum normalized length return None: trivial one-expression
    bodies collide across half a codebase and would flood the candidate list
    with noise rather than reuse leads.
    """
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    if start_line < 1 or end_line < start_line or start_line > len(source_lines):
        return None
    body_lines = source_lines[start_line : min(end_line, len(source_lines))]
    if not body_lines:
        return None
    normalized = " ".join("\n".join(body_lines).split())
    if len(normalized) < threshold_int("DUPLICATION_BODY_HASH_MIN_CHARS"):
        return None
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _function_rows(pf, root: Path) -> tuple[str | None, list[dict]]:
    """Turn one parsed file's callable_signatures into catalog rows.

    Returns (repo_relative_posix_path, rows). The path is None when the file
    cannot be made repo-relative (out-of-repo, I/O error); the caller drops it.
    Each row is the minimal record the artifact stores: name, kind, and the two
    arity numbers. Anonymous callables are already absent from the dump, and a
    row without a string name is skipped.
    """
    extras = getattr(pf, "extras", None) or {}
    raw = extras.get("callable_signatures")
    if not isinstance(raw, list) or not raw:
        return None, []
    try:
        rel = Path(pf.path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None, []

    per_file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FNS_PER_FILE")
    # Body hashing needs the file's lines; read them once per file, lazily, so
    # files whose dump predates body spans cost nothing extra.
    source_lines: list[str] | None = None
    rows: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for entry in raw:
        if len(rows) >= per_file_cap:
            break
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        arity, required = _signature_shape(entry.get("params"))
        # An overload set declares the same name+shape repeatedly in one file;
        # record each distinct (name, shape) once so a single overloaded helper
        # does not crowd out other functions under the per-file cap.
        key = (name, arity, required)
        if key in seen:
            continue
        seen.add(key)
        kind = entry.get("kind")
        body_hash: str | None = None
        if isinstance(entry.get("start_line"), int) and isinstance(entry.get("end_line"), int):
            if source_lines is None:
                try:
                    source_lines = (
                        Path(pf.path)
                        .read_bytes()[:1_000_000]
                        .decode("utf-8", errors="replace")
                        .splitlines()
                    )
                except OSError:
                    source_lines = []
            body_hash = normalized_body_hash(
                source_lines, entry.get("start_line"), entry.get("end_line")
            )
        row = {
            "name": name,
            "kind": kind if isinstance(kind, str) else "function",
            "arity": arity,
            "required": required,
        }
        if body_hash is not None:
            row["body_hash"] = body_hash
        rows.append(row)
    return rel, rows


def build_function_catalog(files, repo_root: Path | str) -> dict:
    """Build the ``function_catalog.json`` payload from parsed files.

    ``files`` is the bootstrap's parsed-file list; each entry's ``extras`` may
    carry ``callable_signatures`` (emitted for both TypeScript/JS and Ruby).
    Files with no recorded callable are omitted. Keys are repo-relative POSIX
    paths so the artifact is portable across checkouts and reproducible
    byte-for-byte (it is hashed into the trust SHA). The total number of files
    recorded is capped so a huge monorepo cannot bloat the artifact; files are
    taken in sorted-path order for a deterministic truncation.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FILES")
    collected: list[tuple[str, list[dict]]] = []
    for pf in files or ():
        rel, rows = _function_rows(pf, root)
        if rel is None or not rows:
            continue
        collected.append((rel, rows))

    collected.sort(key=lambda item: item[0])
    out: dict[str, list[dict]] = {rel: rows for rel, rows in collected[:file_cap]}
    return {"schema_version": SCHEMA_VERSION, "files": out}


class FunctionCatalog:
    """Repo-wide function records, loaded from the committed artifact.

    Holds the flat list of every cataloged function so the prefilter can scan it
    once per query. ``functions`` is the public read; the list is small relative
    to a repo because only named top-level/exported callables are recorded and
    both the file count and per-file function count are capped at build time.
    """

    def __init__(self, functions: list[CatalogedFunction]) -> None:
        self._functions = functions

    @property
    def functions(self) -> list[CatalogedFunction]:
        return self._functions

    def __len__(self) -> int:
        return len(self._functions)


# Process-global cache of parsed catalogs, keyed on the artifact path, carrying
# the (mtime, size) the catalog was parsed at so a refresh that rewrites the
# artifact is picked up without re-reading on every call.
_CATALOG_CACHE: dict[str, tuple[tuple[int, int], FunctionCatalog]] = {}


def load_function_catalog(repo_root: Path | str | None) -> FunctionCatalog | None:
    """Load the committed ``function_catalog.json`` for ``repo_root``, or None.

    Returns None (no candidates, no finding) on any ambiguity: no repo_root, no
    artifact, a corrupt or future-schema payload, an oversized file, or any I/O
    error. The duplication read only ADDS context; failing open here means it
    simply does not fire -- never a crash, never a fabricated candidate.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    artifact = root / ".chameleon" / FUNCTION_CATALOG_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        # Empty or implausibly large (a real catalog is well under this); skip
        # rather than read a pathological file.
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CATALOG_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        return None

    functions: list[CatalogedFunction] = []
    for rel, rows in raw_files.items():
        if not isinstance(rel, str) or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name:
                continue
            kind = row.get("kind")
            arity = row.get("arity")
            required = row.get("required")
            body_hash = row.get("body_hash")
            functions.append(
                CatalogedFunction(
                    name=name,
                    kind=kind if isinstance(kind, str) else "function",
                    file=rel,
                    arity=int(arity) if isinstance(arity, int) else 0,
                    required=int(required) if isinstance(required, int) else 0,
                    tokens=name_tokens(name),
                    body_hash=body_hash if isinstance(body_hash, str) and body_hash else None,
                )
            )

    catalog = FunctionCatalog(functions)
    _CATALOG_CACHE[key] = (token, catalog)
    return catalog


def _arity_close(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when two signature shapes are close enough to be reuse candidates.

    A duplicate re-implementation usually keeps the same call shape, but a
    rename can add or drop a defaulted argument, so the positional arity may
    differ by one. Require the arities within 1 of each other. Zero-arity is
    matched only against zero-arity: a no-arg getter and a 3-arg builder are
    never the same intent.
    """
    arity_a, _req_a = a
    arity_b, _req_b = b
    if arity_a == 0 or arity_b == 0:
        return arity_a == arity_b
    return abs(arity_a - arity_b) <= 1


def _overlap_score(new_tokens: frozenset[str], cand: CatalogedFunction) -> int:
    """Count of shared domain tokens between a new function and a candidate."""
    return len(new_tokens & cand.tokens)


def _jaccard(new_tokens: frozenset[str], cand_tokens: frozenset[str]) -> float:
    """Token-set Jaccard similarity, used as the candidate ranking tiebreak.

    When several candidates share the same raw overlap count (commonly a single
    very-frequent token like ``name``), the one whose whole token set is closest
    to the query is the better reuse lead. Ranking purely by overlap then
    alphabetically buried the real counterpart (``getFullName`` for
    ``buildDisplayName``) below same-overlap noise like ``EventName`` and longer
    multi-token names. Jaccard pushes the closest-shaped names up so they land
    inside the candidate cap.
    """
    union = new_tokens | cand_tokens
    if not union:
        return 0.0
    return len(new_tokens & cand_tokens) / len(union)


@dataclass(frozen=True)
class NewFunction:
    """A function defined in the file under review, the prefilter's query side."""

    name: str
    kind: str
    arity: int
    required: int
    body_hash: str | None = None


def select_candidates(
    catalog: FunctionCatalog,
    new_functions: list[NewFunction],
    *,
    exclude_file: str | None = None,
) -> list[dict]:
    """Prefilter the catalog to likely duplication candidates per new function.

    For each function in ``new_functions``, score every cataloged function by
    name-token overlap and keep those that (a) share at least the minimum number
    of domain tokens AND (b) have a close signature shape, EXCLUDING the file
    under review itself (a function never duplicates itself) and exact same-name
    matches in OTHER files (an exact-name collision is the flat key_exports
    signal's job, not the near-duplicate prefilter's). Candidates are ranked by
    overlap, then required-arity closeness, then name, and capped.

    Returns one entry per new function that has any candidate:
    ``{"function": {...}, "candidates": [{name, file, kind, arity, required,
    shared_tokens}, ...]}``. The caller reads each candidate's real body from
    disk and judges semantic equivalence; this list only narrows the search.
    """
    min_tokens = threshold_int("DUPLICATION_MIN_SHARED_TOKENS")
    max_candidates = threshold_int("DUPLICATION_MAX_CANDIDATES_PER_FN")

    results: list[dict] = []
    for nf in new_functions:
        new_tokens = name_tokens(nf.name)
        if not new_tokens:
            continue
        new_shape = (nf.arity, nf.required)
        scored: list[tuple[int, int, float, int, CatalogedFunction]] = []
        for cand in catalog.functions:
            if exclude_file is not None and cand.file == exclude_file:
                continue
            if cand.name == nf.name:
                # Exact-name collision is the flat key_exports / name-collision
                # check's responsibility; the near-duplicate prefilter targets
                # the DIFFERENT-name re-implementation case.
                continue
            # Identical normalized bodies pair regardless of name tokens: a
            # body-exact clone renamed with zero shared tokens is exactly the
            # LLM-duplication case the name prefilter cannot see.
            body_match = bool(nf.body_hash) and nf.body_hash == cand.body_hash
            overlap = _overlap_score(new_tokens, cand)
            if not body_match:
                if overlap < min_tokens:
                    continue
                if not _arity_close(new_shape, (cand.arity, cand.required)):
                    continue
            req_distance = abs(nf.required - cand.required)
            similarity = _jaccard(new_tokens, cand.tokens)
            scored.append((1 if body_match else 0, overlap, similarity, req_distance, cand))

        if not scored:
            continue
        # Rank body-identical matches first (strongest possible reuse lead),
        # then raw token overlap, then token-set similarity (so the closest-
        # shaped name wins the tie instead of the alphabetically-first one),
        # then required-arity closeness, then a stable name/file tiebreak.
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3], t[4].name, t[4].file))
        candidates = [
            {
                "name": cand.name,
                "file": cand.file,
                "kind": cand.kind,
                "arity": cand.arity,
                "required": cand.required,
                "shared_tokens": sorted(new_tokens & cand.tokens),
                "body_match": bool(body_flag),
            }
            for body_flag, _overlap, _sim, _dist, cand in scored[:max_candidates]
        ]
        results.append(
            {
                "function": {
                    "name": nf.name,
                    "kind": nf.kind,
                    "arity": nf.arity,
                    "required": nf.required,
                },
                "candidates": candidates,
            }
        )
    return results
