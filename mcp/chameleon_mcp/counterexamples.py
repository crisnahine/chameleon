"""Per-archetype off-pattern counterexample index.

The canonical witness shows the model the RIGHT way to write an archetype. What
it does not show is the WRONG way the team has explicitly flagged. When a team
teaches a competing import ("prefer ``preferred`` over ``over``" via
``/chameleon-teach-competing-import``) and a real file in that archetype still
imports the discouraged module, that line is a grounded counterexample: a
"do NOT write it this way" form that exists in this repo, vouched for by the
team's own teaching rather than guessed. Paired with the witness at edit time it
forms a positive/negative contrast, the construction the in-context-learning
literature finds beats a positive example alone.

The signal is deliberately conservative. Only a TAUGHT competing pair is used
(``conventions.imports.<archetype>.competing`` is populated solely by teaching,
never auto-derived), and only when a cluster member actually still uses the
discouraged import. A clean archetype where nobody uses the discouraged form
yields no counterexample, which is correct: there is no real mistake to show. So
the index never fabricates an anti-pattern and never fires on a legitimate
variation.

Two halves share one schema so the build (bootstrap-time) and the read
(tool-time) cannot drift, mirroring :mod:`chameleon_mcp.symbol_signatures`:

- :func:`build_counterexamples` scans each archetype's members for a real use of
  a taught discouraged import and captures that line.
- :func:`load_counterexamples` reads the committed artifact, cached on
  (mtime, size) so a mid-session refresh is picked up without re-reading.

Loading fails open to None on any ambiguity -- missing, corrupt, future-schema,
oversized, or any I/O error -- so the injection simply does not fire rather than
crash or fabricate.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

COUNTEREXAMPLES_FILENAME = "counterexamples.json"
# v2 stores a LIST of off-patterns per archetype so a team that teaches several
# competing imports for one archetype (winston->logger AND moment->date) keeps a
# grounded counterexample for EACH, not just the last taught. v1 (a single row
# per archetype) is still read for back-compat and normalized to a 1-row list.
SCHEMA_VERSION = 2
_READABLE_SCHEMA_VERSIONS = (1, 2)

# Defensive upper bound on counterexample rows kept per archetype. The real bound
# is the number of competing imports a human has TAUGHT for the archetype (a
# naturally small, deliberate set), so this never bites a real team; it only caps
# a pathological teach loop from bloating the artifact and the edit spotlight.
_MAX_ROWS_PER_ARCHETYPE = 10

# A captured counterexample is a single import statement; anything longer than
# this is not a clean one-line import (a folded multi-name import, a false match
# inside a string) and is skipped rather than truncated -- a half-shown
# counterexample is worse than none.
_SNIPPET_MAX_CHARS = 400

# A markdown fence run (``` or ~~~). A real import line never contains one; a
# captured snippet that does is a code-fence-breakout smuggling attempt, since the
# snippet is rendered inside a ``` fence in the edit block. Rejected at capture and
# neutralized at render.
_FENCE_RE = re.compile(r"[`~]{3,}")


def neutralize_fences(text: str) -> str:
    """Break any markdown fence run so the text cannot close the code fence it is
    rendered inside (turns ``` into a space-separated, still-readable form)."""
    return _FENCE_RE.sub(lambda m: " ".join(m.group(0)), text)


# How many bytes of a member file to scan for the discouraged import. Imports sit
# at the top of a file; reading the whole file would be wasteful and lets a giant
# generated file slow the build.
_MEMBER_SCAN_BYTES = 64_000


# Cap on files visited by the repo-wide scan + the wall-clock budget (teach-time,
# never on a hook hot path). Read from _thresholds so an operator can tune them;
# the file cap is a high backstop and the budget is the real bound, so a huge
# monorepo whose off-pattern lives in a late-alphabetical dir is still found rather
# than the scan exhausting a low file cap inside app/. The scan stops early on a
# match, so the budget only binds when the taught module is absent.
def _scan_max_files() -> int:
    from chameleon_mcp._thresholds import threshold_int

    return threshold_int("COUNTEREXAMPLE_SCAN_MAX_FILES")


def _scan_budget_seconds() -> float:
    from chameleon_mcp._thresholds import threshold_float

    return threshold_float("COUNTEREXAMPLE_SCAN_BUDGET_SECONDS")


# A line whose stripped form opens with one of these is a comment, never a real
# import statement: a commented-out import (`// import x from "over"`, a JSDoc
# `* ...`, a Ruby `# ...`) satisfies the keyword+module heuristic but must not be
# captured as the team's off-pattern.
_COMMENT_PREFIX_RE = re.compile(r"""^(?://|/\*|\*/|\*|#|<!--)""")

# A Ruby heredoc opener. ``<<~ID`` / ``<<-ID`` (squiggly/dash) and ``<<"ID"`` /
# ``<<'ID'`` (quoted) are UNAMBIGUOUS heredocs. A bare ``<<ID`` is also captured
# but is only TREATED as a heredoc when it sits in heredoc position (see
# ``_heredoc_terminator``), so the append/shift operator (``arr << item``,
# ``arr<<CONST``, ``a << 2``) is not mistaken for one. The body lines until the
# terminator hold example code that must not be captured.
_HEREDOC_OPEN_RE = re.compile(r"""<<(?P<sq>[-~])?(?P<q>["'])?(?P<id>\w+)(?(q)["'])""")
# A char immediately before a bare ``<<`` that means heredoc, not append: an
# operand can never sit there (assignment, call/array open, comma, return-ish).
_HEREDOC_POS_RE = re.compile(r"""(?:^|[=(\[,:?])\s*$""")


def _advance_string(line: str, quote: str | None) -> str | None:
    """Return the open-quote char the ``line`` ENDS inside, starting already inside
    ``quote`` (or None when starting in code). Toggles only on the active quote
    type and skips ``\\`` escapes, so an apostrophe inside a ``"..."`` and an
    escaped backtick inside a template do not mis-toggle. This single scanner
    carries string/template state BOTH within and across lines."""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in ('"', "'", "`"):
            quote = c
        i += 1
    return quote


def _ends_inside_string(prefix: str) -> bool:
    """True if ``prefix`` (starting in code) ends inside an unterminated string."""
    return _advance_string(prefix, None) is not None


def _heredoc_terminator(raw: str) -> str | None:
    """The heredoc terminator opened on ``raw``, or None. ``<<~``/``<<-``/quoted
    forms are always heredocs; a bare ``<<ID`` only when in heredoc position (not
    an append/shift) and not inside a string."""
    m = _HEREDOC_OPEN_RE.search(raw)
    if not m or _ends_inside_string(raw[: m.start()]):
        return None
    if m.group("sq") or m.group("q"):
        return m.group("id")
    # bare <<ID: heredoc only if no operand precedes the <<
    return m.group("id") if _HEREDOC_POS_RE.search(raw[: m.start()]) else None


def _import_of(over: str) -> re.Pattern[str]:
    """A regex matching a real import of ``over``: the discouraged module quoted
    and IMMEDIATELY preceded by an import keyword (``from "over"``,
    ``import 'over'``, ``require("over")``, ``require 'over'``). The adjacency
    rules out a bare substring elsewhere on the line."""
    return re.compile(
        r"""\b(?:from|import|require|require_relative|load)\b\s*\(?\s*['"]"""
        + re.escape(over)
        + r"""['"]"""
    )


def _find_import_line(content: str, over: str) -> str | None:
    """The first import/require line in ``content`` that imports ``over``, or None.

    Real-import filters: skip comment lines (``//``, ``/* */``, JSDoc ``*``, Ruby
    ``#``); require an import keyword IMMEDIATELY before the quoted ``over``
    specifier (so ``react`` does not match ``react-dom``); and require that keyword
    to sit OUTSIDE a string literal. String state is tracked BOTH within a line
    (``_ends_inside_string``) and ACROSS lines, so an import-looking line buried in
    a multi-line construct -- a TS/JS template literal, a Ruby heredoc, or a
    ``/* */`` block comment -- is not mistaken for a real import. Returns the
    stripped line, or None.
    """
    if not over:
        return None
    pat = _import_of(over)
    open_quote: str | None = None  # template/string left open by a prior line
    in_block_comment = False  # inside an unclosed /* ... */
    heredoc_end: str | None = None  # Ruby heredoc terminator we are waiting for
    for raw in content.splitlines():
        line = raw.strip()
        # --- skip the body of any open multi-line construct ---
        if heredoc_end is not None:
            if line == heredoc_end:  # <<~ allows an indented terminator (stripped)
                heredoc_end = None
            continue
        if open_quote is not None:
            open_quote = _advance_string(raw, open_quote)
            continue
        if in_block_comment:
            if "*/" in raw:
                in_block_comment = False
            continue
        # --- a code line: try to match a real import ---
        if line and len(line) <= _SNIPPET_MAX_CHARS:
            if not _COMMENT_PREFIX_RE.match(line) and not _FENCE_RE.search(line):
                m = pat.search(line)
                if m and not _ends_inside_string(line[: m.start()]):
                    return line
        # --- update cross-line state opened BY this code line ---
        if "/*" in raw and "*/" not in raw.split("/*", 1)[1]:
            in_block_comment = True
            continue
        heredoc_end = _heredoc_terminator(raw)
        if heredoc_end is not None:
            continue
        # does this line end inside an open string / template literal?
        open_quote = _advance_string(raw, None)
    return None


def _read_member_text(path: Path) -> str | None:
    """Read the top of a member file for import scanning, or None on any error.

    Never follows a symlink: a teammate-planted ``*.ts`` link pointing outside the
    repo would otherwise have its content captured into a counterexample and shown
    in the edit block. This is the same cross-filesystem threat ``discover_files``
    drops symlinks for; the teach-time repo scan re-walks the tree, so it must
    re-apply the guard rather than inherit it.
    """
    try:
        if path.is_symlink():
            return None
        with open(path, "rb") as fh:
            raw = fh.read(_MEMBER_SCAN_BYTES)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def _make_entry(over: str, snippet: str, preferred: object) -> dict:
    """Assemble a counterexample row; ``preferred`` is included when it is a
    non-empty string."""
    entry = {"rule": "import-preference-violation", "over": over, "snippet": snippet}
    if isinstance(preferred, str) and preferred:
        entry["preferred"] = preferred
    return entry


def _iter_repo_source_files(repo_root: Path | str):
    """Yield repo source files in a deterministic order, bounded and pruned.

    Prunes the SAME directories the canonical clustering walk drops
    (``EXCLUDE_FROM_CLUSTERING_DIRS``) so the teach-time scan never captures a
    counterexample from a vendored/generated/cache file that is not hand-written
    team code, and the two exclude sets cannot drift. Skips symlinked dirs and
    files so a planted link out of the repo is never read. Stops after the
    ``COUNTEREXAMPLE_SCAN_MAX_FILES`` threshold. Tool-time only, never on a hook
    hot path.
    """
    from chameleon_mcp.bootstrap.discovery import EXCLUDE_FROM_CLUSTERING_DIRS
    from chameleon_mcp.conventions import _SOURCE_EXTENSIONS

    max_files = _scan_max_files()
    count = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # os.walk already does not descend symlinked dirs (followlinks=False);
        # prune them and the excluded names from the listing so they are not yielded.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in EXCLUDE_FROM_CLUSTERING_DIRS
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            if count >= max_files:
                return
            path = Path(dirpath) / name
            if path.suffix in _SOURCE_EXTENSIONS and not path.is_symlink():
                count += 1
                yield path


def normalize_archetype_rows(value: object) -> list[dict]:
    """Coerce a stored per-archetype counterexample value to a list of valid rows.

    Accepts a v2 list, a legacy v1 single dict, or junk, and returns a list of
    rows that each carry a string ``snippet``. The single normalizer is shared by
    :func:`load_counterexamples` and the teach/unteach read-modify-write so the on-
    disk shape (which may still be v1 until the next refresh) is handled one way.
    """
    rows = value if isinstance(value, list) else [value]
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("snippet"), str) and row.get("snippet"):
            out.append(row)
    return out[:_MAX_ROWS_PER_ARCHETYPE]


def capture_counterexamples_in_repo(repo_root: Path | str, competing_pairs: list) -> list[dict]:
    """All real off-pattern lines for the taught competing pairs, one row per pair.

    Walks the repo ONCE and, for each distinct discouraged (``over``) module, keeps
    the first real import of it found anywhere in the tree -- ``archetypes.json``
    records no per-archetype member paths, and the off-pattern usage may sit in an
    outlier file that did not cluster into the archetype. Returns one entry per pair
    whose discouraged import is actually present (deduped by ``over``, in pair
    order); a pair nobody violates yields nothing, so a clean archetype produces an
    empty list. This is the single capture path shared by teach and the
    bootstrap/refresh rebuild, so the two cannot diverge. Tool-time only.

    Bounded by a wall-clock budget (``COUNTEREXAMPLE_SCAN_BUDGET_SECONDS``) as well
    as the file cap: the scan breaks early once every pair is found, so the budget
    only binds when a taught module is absent (nothing to capture), keeping the
    teach on the largest monorepos snappy without missing an off-pattern that sits
    in a late-scanned directory.
    """
    if not isinstance(competing_pairs, list) or not competing_pairs:
        return []
    overs: list[tuple[str, object]] = []
    seen_over: set[str] = set()
    for p in competing_pairs:
        if isinstance(p, dict) and isinstance(p.get("over"), str) and p.get("over"):
            over = p["over"]
            if over not in seen_over:
                seen_over.add(over)
                overs.append((over, p.get("preferred")))
    if not overs:
        return []

    deadline = time.monotonic() + _scan_budget_seconds()
    found: dict[str, dict] = {}
    for path in _iter_repo_source_files(repo_root):
        if len(found) == len(overs):
            break
        if time.monotonic() > deadline:
            break
        text = _read_member_text(path)
        if text is None:
            continue
        for over, preferred in overs:
            if over in found:
                continue
            snippet = _find_import_line(text, over)
            if snippet:
                found[over] = _make_entry(over, snippet, preferred)
    # Preserve the taught pair order, capped.
    return [found[over] for over, _ in overs if over in found][:_MAX_ROWS_PER_ARCHETYPE]


def capture_counterexample_in_repo(repo_root: Path | str, competing_pairs: list) -> dict | None:
    """First real off-pattern instance for any of the competing pairs, or None.

    Back-compatible singular wrapper over :func:`capture_counterexamples_in_repo`
    for callers that want a single row.
    """
    rows = capture_counterexamples_in_repo(repo_root, competing_pairs)
    return rows[0] if rows else None


def build_counterexamples(
    competing_by_archetype: dict[str, list],
    repo_root: Path | str,
) -> dict:
    """Build the ``counterexamples.json`` payload.

    For each archetype that has a taught competing pair, capture the first real
    use of a discouraged (``over``) import via the SAME repo-wide scan teach uses
    (:func:`capture_counterexample_in_repo`), so a full bootstrap/refresh cannot
    drop a counterexample whose usage sits in a non-member outlier file. An
    archetype with no present discouraged import yields no entry.

    ``competing_by_archetype`` maps each archetype to its taught competing pairs
    (``[{"preferred", "over"}, ...]``). Keys in the output are archetype names so
    the edit-time reader can look up by the edited file's archetype. Each value is
    a LIST: one row per taught pair whose discouraged import is still present.
    """
    out: dict[str, list[dict]] = {}
    for archetype in sorted(competing_by_archetype):
        rows = capture_counterexamples_in_repo(
            repo_root, competing_by_archetype.get(archetype) or []
        )
        if rows:
            out[archetype] = rows

    return {"schema_version": SCHEMA_VERSION, "archetypes": out}


class Counterexamples:
    """Archetype -> list of counterexample rows, loaded from the artifact."""

    def __init__(self, entries: dict[str, list[dict]]) -> None:
        self._entries = entries

    def for_archetype(self, archetype: str) -> list[dict]:
        """The counterexample rows for ``archetype`` (empty list when none)."""
        if not archetype:
            return []
        return self._entries.get(archetype, [])

    def __len__(self) -> int:
        return len(self._entries)


# Process-global cache keyed on the artifact path, carrying the (mtime, size) the
# index was parsed at so a refresh that rewrites the artifact is picked up.
_CACHE: dict[str, tuple[tuple[int, int], Counterexamples]] = {}


def load_counterexamples(repo_root: Path | str | None) -> Counterexamples | None:
    """Load the committed ``counterexamples.json`` for ``repo_root``, or None.

    Returns None on any ambiguity: no repo_root, no artifact, a corrupt or
    future-schema payload, an oversized file, or any I/O error. The injection
    only ADDS a counterexample, so failing open here means it simply does not
    fire.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    artifact = root / ".chameleon" / COUNTEREXAMPLES_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") not in _READABLE_SCHEMA_VERSIONS:
        return None
    raw = data.get("archetypes")
    if not isinstance(raw, dict):
        return None

    # Each value may be a v2 list or a legacy v1 single dict; normalize_archetype_rows
    # handles both and drops rows without a string snippet. An archetype that
    # normalizes to no valid rows is omitted.
    entries: dict[str, list[dict]] = {}
    for archetype, value in raw.items():
        if not isinstance(archetype, str):
            continue
        rows = normalize_archetype_rows(value)
        if rows:
            entries[archetype] = rows

    index = Counterexamples(entries)
    _CACHE[key] = (token, index)
    return index
