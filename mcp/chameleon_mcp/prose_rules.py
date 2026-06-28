"""Offline prose-rule miner: doc-stated "use X not Y", corroborated against code.

chameleon derives conventions from ASTs, so the rules a team writes in prose --
"use our HTTP wrapper, never raw axios", "prefer date-fns over moment" -- are
invisible to it even though they are exactly the conventions new code should
match. This module reads a bounded allowlist of convention-bearing docs
(CONTRIBUTING / STYLE / AGENTS.md / docs), extracts high-precision
``use X not Y`` / ``prefer X over Y`` / ``use X instead of Y`` rules with file:line
provenance, and CORROBORATES each against the repo's own imports.

The corroboration gate is what keeps this on chameleon's low-false-positive
identity: a documented rule becomes ``teachable`` only when the repo's own code
already backs it (the preferred form is imported and the discouraged form is
absent). A rule the code contradicts is surfaced ``contested`` (the doc and the
code disagree -- a human decides), and one neither form supports is
``unsupported``. Nothing here writes the profile: the miner only PROPOSES, and a
corroborated candidate is handed to the existing ``teach_competing_import`` path
only on explicit approval.

Fully offline, no repo-code execution, bounded by file / byte caps. Tool-time
only, never a hook hot path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

# A module/import token: an identifier or path like ``axios``, ``os.path``,
# ``@/lib/http``, ``date-fns``. Optionally wrapped in backticks or quotes in
# prose. The two-token rule patterns capture (preferred, over) positionally.
_TOK = r"[`'\"]?([\w@][\w@/.\-]*)[`'\"]?"

_RULE_PATTERNS = (
    re.compile(rf"\buse\s+{_TOK}\s*,?\s+not\s+{_TOK}", re.IGNORECASE),
    re.compile(rf"\buse\s+{_TOK}\s+instead\s+of\s+{_TOK}", re.IGNORECASE),
    re.compile(rf"\bprefer\s+{_TOK}\s+over\s+{_TOK}", re.IGNORECASE),
)

# Tokens too generic to be a real import preference; a match on one of these is
# almost certainly an English sentence, not a convention. The corroboration gate
# would drop them anyway (they are not importable), but filtering here keeps the
# proposed candidate list clean.
_STOPWORDS = frozenset(
    {"the", "a", "an", "it", "this", "that", "them", "one", "us", "we", "you", "any", "all"}
)

# Convention-bearing doc filenames (exact, case-insensitive) and name prefixes
# scanned at the repo root, plus the docs/ tree.
_DOC_EXACT = frozenset(
    {
        "contributing.md",
        "contributing",
        "style.md",
        "styleguide.md",
        "style-guide.md",
        "conventions.md",
        "agents.md",
        "claude.md",
        "architecture.md",
        "readme.md",
    }
)
_DOC_PREFIXES = ("contributing", "style", "conventions", "adr-", "adr_")
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt")


def _clean_token(tok: str) -> str:
    """Strip prose wrapping (quotes/backticks) and trailing sentence punctuation,
    preserving an internal dot/hyphen (``os.path``, ``date-fns``)."""
    return tok.strip("`'\"").lstrip("(").rstrip(".,;:)")


def extract_prose_rules(text: str) -> list[tuple[str, str]]:
    """All ``(preferred, over)`` import-preference rules stated in ``text``.

    Matches only the high-precision ``use X not Y`` / ``use X instead of Y`` /
    ``prefer X over Y`` shapes. Deduped, first-occurrence order. A token that is
    an English stopword, empty after cleaning, or identical to its pair is
    dropped (the corroboration gate is the real filter; this just keeps the
    proposal list sane).
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pat in _RULE_PATTERNS:
        for m in pat.finditer(text):
            preferred = _clean_token(m.group(1))
            over = _clean_token(m.group(2))
            if not preferred or not over or preferred == over:
                continue
            # Drop single-character tokens: a real module name is never one char,
            # and "use X not Y" placeholders in docs would otherwise mine as rules.
            if len(preferred) < 2 or len(over) < 2:
                continue
            if preferred.lower() in _STOPWORDS or over.lower() in _STOPWORDS:
                continue
            key = (preferred, over)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _is_doc_file(name: str) -> bool:
    low = name.lower()
    if low in _DOC_EXACT:
        return True
    return low.startswith(_DOC_PREFIXES) and low.endswith(_DOC_SUFFIXES)


def _iter_doc_files(repo_root: Path):
    """Yield convention-bearing doc files (root allowlist + docs/ tree), bounded.

    Deterministic order, skips symlinks, stops after ``PROSE_RULE_MAX_DOCS``.
    """
    root = Path(repo_root)
    max_docs = threshold_int("PROSE_RULE_MAX_DOCS")
    candidates: list[Path] = []
    try:
        for p in sorted(root.iterdir()):
            if p.is_file() and not p.is_symlink() and _is_doc_file(p.name):
                candidates.append(p)
    except OSError:
        return
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for dirpath, dirnames, filenames in os.walk(docs_dir):
            dirnames[:] = sorted(
                d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
            )
            for n in sorted(filenames):
                if n.lower().endswith(_DOC_SUFFIXES) and not os.path.islink(
                    os.path.join(dirpath, n)
                ):
                    candidates.append(Path(dirpath) / n)
    for p in candidates[:max_docs]:
        yield p


def _read_text(path: Path, max_bytes: int) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def mine_doc_rules(repo_root: Path | str) -> list[dict]:
    """Extract every doc-stated rule with provenance, before corroboration.

    Returns ``[{"preferred", "over", "source"}]`` where ``source`` is
    ``<repo-relative path>:<line>``. Deduped by (preferred, over), keeping the
    first doc location seen.
    """
    root = Path(repo_root)
    max_bytes = threshold_int("PROSE_RULE_MAX_DOC_BYTES")
    found: dict[tuple[str, str], dict] = {}
    for path in _iter_doc_files(root):
        text = _read_text(path, max_bytes)
        if not text:
            continue
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            rel = path.name
        for lineno, line in enumerate(text.splitlines(), start=1):
            for preferred, over in extract_prose_rules(line):
                key = (preferred, over)
                if key not in found:
                    found[key] = {
                        "preferred": preferred,
                        "over": over,
                        "source": f"{rel}:{lineno}",
                    }
    return list(found.values())


def _file_imports(text: str, token: str) -> bool:
    """Whether ``token`` appears as an import specifier somewhere in ``text``.

    A line that mentions an import/require/from is required, and the token must
    appear as a delimited unit (so ``axios`` does not match ``axios-retry`` and
    ``os.path`` keeps its dot). Heuristic by design -- it grounds an advisory
    corroboration, not a block.
    """
    tokre = re.compile(r"(?<![\w@/.\-])" + re.escape(token) + r"(?![\w@/.\-])")
    for line in text.splitlines():
        low = line.lower()
        if ("import" in low or "require" in low or "from " in low) and tokre.search(line):
            return True
    return False


def corroborate_rules(repo_root: Path | str, rules: list[dict]) -> list[dict]:
    """Tag each rule with its code-corroboration status from a single repo scan.

    Walks the repo's source files once (bounded, vendored/generated dirs pruned)
    and counts, per token, how many files import it. Each rule gains
    ``preferred_files`` / ``over_files`` / ``status`` / ``teachable``:
      - ``corroborated`` -- preferred imported, over absent. The code backs the
        documented rule, so it is teachable.
      - ``contested`` -- the discouraged form is still imported. Doc and code
        disagree; not teachable, surfaced for a human to reconcile.
      - ``unsupported`` -- neither form imported. Cannot verify from code; not
        teachable.
    """
    from chameleon_mcp.counterexamples import _iter_repo_source_files

    if not rules:
        return []
    tokens: set[str] = set()
    for r in rules:
        tokens.add(r["preferred"])
        tokens.add(r["over"])
    counts: dict[str, int] = dict.fromkeys(tokens, 0)
    max_bytes = threshold_int("PROSE_RULE_MAX_DOC_BYTES")
    for path in _iter_repo_source_files(repo_root):
        text = _read_text(path, max_bytes)
        if not text:
            continue
        for tok in tokens:
            if _file_imports(text, tok):
                counts[tok] += 1
    out: list[dict] = []
    for r in rules:
        pref = counts.get(r["preferred"], 0)
        over = counts.get(r["over"], 0)
        if pref >= 1 and over == 0:
            status = "corroborated"
        elif over >= 1:
            status = "contested"
        else:
            status = "unsupported"
        out.append(
            {
                **r,
                "preferred_files": pref,
                "over_files": over,
                "status": status,
                "teachable": status == "corroborated",
            }
        )
    return out


def mine_prose_rule_candidates(repo_root: Path | str) -> list[dict]:
    """Mine doc-stated import-preference rules and corroborate them against code.

    The full offline pipeline: extract rules from the doc allowlist, then tag each
    with its code-corroboration status. Returns the candidate list (corroborated
    first, then contested, then unsupported; stable by source within a status) so
    a caller surfaces the teachable ones first. Never writes the profile.
    """
    rules = corroborate_rules(repo_root, mine_doc_rules(repo_root))
    order = {"corroborated": 0, "contested": 1, "unsupported": 2}
    rules.sort(key=lambda r: (order.get(r["status"], 9), r["source"]))
    return rules
