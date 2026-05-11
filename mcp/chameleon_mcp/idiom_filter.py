"""Language-scoped idiom filter for `.chameleon/idioms.md`.

v0.5.2 (forem/maybe/mastodon dogfood gap — "Idioms not language-scoped"):
the original `get_pattern_context` returns repo-wide idioms regardless of
which file the model is about to edit. In a Rails + JS hybrid (forem,
mastodon) a `.js` edit would receive Ruby idioms like "always use Strong
Params" — context that's at best confusing and at worst actively misleading.

This module ships:

1. A markdown frontmatter convention: each idiom block under `## active`
   (or `## deprecated`) may carry a single `Language: <lang>` line directly
   beneath its `### slug` heading. Values are restricted to:

       Language: ruby
       Language: typescript
       Language: any

   Idioms with no `Language:` line are treated as `any` (backward compatible
   with every idiom captured before v0.5.2).

2. `filter_idioms_by_language(text, target)` — a pure-function loader-side
   filter. Pass the entire idioms.md text plus the target language; the
   returned text is the same markdown structure with non-matching idiom
   blocks excised (and an `[N idioms filtered]` annotation appended so
   trust-review surfaces still see *something*).

3. `language_for_path(file_path)` — convenience: maps a file path's
   extension to one of `"ruby"`, `"typescript"`, `"unknown"`. Matches the
   same extension sets `lint_engine.detect_language` uses.

The API surface (`filter_idioms_by_language`, `language_for_path`) is
intentionally minimal so `tools.py:get_pattern_context` can adopt it with
a one-line call: `idioms_text = filter_idioms_by_language(idioms_text,
language_for_path(file_path))`. We don't depend on tools.py here.

Pure functions, no I/O, no globals. Hostile-input safe: the parse is bounded
by line count and never compiles a regex from user input.
"""

from __future__ import annotations

import re

# Extension → language mapping. Mirrors lint_engine.detect_language exactly
# so a `.tsx` file lints AND filters under the same language. We don't import
# lint_engine here to keep this module dependency-light (loader can call us
# without dragging in the AST extractors).
_RUBY_EXTENSIONS: frozenset[str] = frozenset({".rb"})
_TS_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
})

# Recognized language tags inside `Language: <lang>`. Anything outside this
# set falls back to "any" so a typo doesn't accidentally hide a legitimate
# idiom from every target language.
_VALID_LANGUAGES: frozenset[str] = frozenset({"ruby", "typescript", "any"})

# Heading regex for an idiom block (`### slug` at column 0 — same shape that
# teach_profile.py produces and that the chameleon-teach skill documents).
_IDIOM_HEADING_RE = re.compile(r"^###\s+\S")

# Section markers (`## active` / `## deprecated`) — used to recognise section
# boundaries so we don't accidentally treat a section header as an idiom.
_SECTION_HEADING_RE = re.compile(r"^##\s+\S")

# Inline `Language: <lang>` frontmatter line. Case-insensitive on the key
# (matches existing `Status:` / `Archetype:` style); value is normalized to
# lowercase before comparison.
_LANGUAGE_LINE_RE = re.compile(
    r"^\s*Language\s*:\s*(\S+)\s*$",
    re.IGNORECASE,
)


def language_for_path(file_path: str | None) -> str:
    """Map a file path to one of ``ruby`` / ``typescript`` / ``unknown``.

    Used by callers that need to derive the filter's target language from a
    file the model is about to edit. ``None`` / empty input → ``unknown``,
    which is the most conservative filter result (only `any`-tagged idioms
    pass).
    """
    if not file_path:
        return "unknown"
    lower = file_path.lower()
    for ext in _RUBY_EXTENSIONS:
        if lower.endswith(ext):
            return "ruby"
    for ext in _TS_EXTENSIONS:
        if lower.endswith(ext):
            return "typescript"
    return "unknown"


def _idiom_language(block_lines: list[str]) -> str:
    """Return the idiom block's declared language, defaulting to ``any``.

    Scans the lines *after* the `### slug` heading for a `Language:` line.
    Stops at the first blank line that's followed by non-frontmatter content
    so a "Language:" mention in the idiom body (e.g., "this rule applies to
    the Language: Ruby team") doesn't get mistaken for frontmatter.

    Robustness: case-insensitive key, lowercase value comparison, unknown
    values fall back to ``any``. Block_lines should start with the `###` line.
    """
    # Frontmatter window: from the line after `###` until the first blank
    # line. teach_profile.py renders frontmatter as consecutive `Key: value`
    # lines (Status, Language, Archetype) followed by the rationale body.
    for line in block_lines[1:]:
        if not line.strip():
            break
        m = _LANGUAGE_LINE_RE.match(line)
        if m:
            value = m.group(1).strip().lower()
            return value if value in _VALID_LANGUAGES else "any"
    return "any"


def _passes_filter(idiom_lang: str, target_language: str) -> bool:
    """Decide whether `idiom_lang` should be kept for `target_language`.

    Rules (per spec):
    - target ``ruby`` → keep ``ruby`` + ``any``
    - target ``typescript`` → keep ``typescript`` + ``any``
    - target ``unknown`` → keep only ``any``

    `any` idioms ALWAYS pass — they're the language-agnostic baseline
    (e.g., "always use absolute imports", "rotate secrets via env vars")
    that every file edit should respect.
    """
    if idiom_lang == "any":
        return True
    if target_language == "ruby":
        return idiom_lang == "ruby"
    if target_language == "typescript":
        return idiom_lang == "typescript"
    # target_language == "unknown" or any other value → only `any` passes.
    return False


def filter_idioms_by_language(idioms_md_text: str, target_language: str) -> str:
    """Filter ``idioms.md`` text to idioms relevant to ``target_language``.

    Parsing model (deliberately simple):

    - Split on lines matching ``^### `` (the canonical idiom slug heading).
      Everything before the first `###` is "preamble" (top-level title +
      `## active` section header) and is preserved verbatim.
    - Each idiom block runs from its `###` line up to (but not including)
      the next `###` line OR the next `## ` section heading. The trailing
      section heading is re-attached to the preamble of the next section.
    - For each block, find a `Language: <lang>` line within the frontmatter
      window (first blank line ends the window). Default = ``any``.
    - Keep blocks where the language passes ``_passes_filter``.

    Backward compatibility: an idiom captured before v0.5.2 (no `Language:`
    line) defaults to ``any`` and is preserved for *every* target language.
    This is the safe direction: we'd rather show a few cross-language idioms
    to the user than silently hide legitimate team conventions.

    Hostile input safe: regex is fixed-compile, no eval. The parse is O(N)
    in line count.

    Returns markdown text (str). The structure (section headings, top-level
    title, deprecation markers) is preserved; only idiom blocks are removed.
    When *every* idiom is filtered out we still return a non-empty preamble
    so the trust-review surface ("the user's idioms") doesn't go blank.
    """
    if not idioms_md_text:
        return idioms_md_text

    target = (target_language or "unknown").lower()

    lines = idioms_md_text.split("\n")
    out_lines: list[str] = []
    block_lines: list[str] | None = None
    kept = 0
    filtered = 0

    def _flush_block() -> None:
        nonlocal kept, filtered
        if block_lines is None:
            return
        lang = _idiom_language(block_lines)
        if _passes_filter(lang, target):
            out_lines.extend(block_lines)
            kept += 1
        else:
            filtered += 1

    for line in lines:
        if _IDIOM_HEADING_RE.match(line):
            # Boundary: close any in-progress block, then start a new one.
            _flush_block()
            block_lines = [line]
        elif _SECTION_HEADING_RE.match(line):
            # `## active` / `## deprecated` — close the current block (the
            # section header belongs in the preamble, not in an idiom block).
            _flush_block()
            block_lines = None
            out_lines.append(line)
        else:
            if block_lines is None:
                out_lines.append(line)
            else:
                block_lines.append(line)

    # Flush the trailing block (file ended inside an idiom).
    _flush_block()

    # Annotate when we filtered something so the trust-review surface
    # surfaces "this isn't all of your idioms — N were hidden for the active
    # target language". Keep it discreet: a trailing comment, no headings.
    result = "\n".join(out_lines)
    if filtered > 0:
        if result and not result.endswith("\n"):
            result += "\n"
        result += (
            f"\n<!-- chameleon: filtered {filtered} idiom(s) not matching "
            f"target language '{target}' (kept {kept}) -->\n"
        )

    return result
