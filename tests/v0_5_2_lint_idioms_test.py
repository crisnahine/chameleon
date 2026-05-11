"""v0.5.2 regression tests — lint engine + idiom filter.

Covers the two v0.5.2 dogfood bugs:

  Bug 1 — GitHub PAT bypassed by string-concat. `scan_secrets` now folds
  same-quote-style literal-to-literal `+` concatenation before invoking the
  underlying detect-secrets / fallback regex pass. Trivially obfuscated
  tokens like ``"ghp_" + "abcd…"`` now reach the scanner as
  ``"ghp_abcd…"`` and get flagged.

  Bug 2 — Idioms not language-scoped. A new ``chameleon_mcp.idiom_filter``
  module ships a markdown-frontmatter parser + filter. ``Language: ruby``
  idioms are hidden from JS edits in a Rails+JS repo, and vice versa.

Each bug section is structured as verify-before → verify-after so a future
regression flips the assertion back to FAIL on the unfixed branch.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_2_lint_idioms_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make in-repo modules importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Isolated plugin data dir so we don't bleed state into other test files.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_2_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA


PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


from chameleon_mcp.idiom_filter import (  # noqa: E402
    filter_idioms_by_language,
    language_for_path,
)
from chameleon_mcp.lint_engine import (  # noqa: E402
    _fold_string_concat,
    scan_secrets,
)


# ---------------------------------------------------------------------------
# Bug 1 — GitHub PAT bypassed by string-concat
# ---------------------------------------------------------------------------
# The underlying fallback regex is `\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b`
# (exactly 36 alnum chars after the prefix). We pick 36-char "rest" payloads
# in the synthetic fixtures so the existing scanner flags the folded result.
GHP_BODY_36 = "abcdef1234567890abcdef1234567890abcd"  # 36 chars exactly
assert len(GHP_BODY_36) == 36
GHP_FULL_DIRECT = f"ghp_{GHP_BODY_36}"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


# ---------- verify-before-style direct unit tests on the preprocessor ----------
section("Bug 1: _fold_string_concat — pure-function unit tests")

t(
    "fold double-quoted ghp prefix + rest → joined literal",
    _fold_string_concat(f'const t = "ghp_" + "{GHP_BODY_36}"')
    == f'const t = "ghp_{GHP_BODY_36}"',
)
t(
    "fold single-quoted ghp prefix + rest → joined literal",
    _fold_string_concat(f"const t = 'ghp_' + '{GHP_BODY_36}'")
    == f"const t = 'ghp_{GHP_BODY_36}'",
)
t(
    "fold AWS prefix + rest → joined literal",
    _fold_string_concat('t = "AKIA" + "IOSFODNN7EXAMPLE"')
    == 't = "AKIAIOSFODNN7EXAMPLE"',
)
t(
    "iterative fold collapses 4-way concat in one call",
    _fold_string_concat('chain = "a" + "b" + "c" + "d"') == 'chain = "abcd"',
)
t(
    "mixed-quote concat is NOT folded (different quote styles → leave alone)",
    _fold_string_concat('mix = "a" + \'b\'') == 'mix = "a" + \'b\'',
)
t(
    "no `+` operator → identity",
    _fold_string_concat('const t = "ghp_solo_literal_no_concat_here"')
    == 'const t = "ghp_solo_literal_no_concat_here"',
)
# Variable + literal concat — out of scope per spec
t(
    "variable + literal concat (`x + \"b\"`) NOT folded",
    _fold_string_concat('out = x + "b"') == 'out = x + "b"',
)
t(
    "literal + variable concat (`\"a\" + x`) NOT folded",
    _fold_string_concat('out = "a" + x') == 'out = "a" + x',
)

# Bounded substitutions — the preprocessor must not chew on 10k+ concats.
chain = '"x"' + (' + "y"' * 5)  # 5 folds = 5 + 1 → "xyyyyy"
folded_chain = _fold_string_concat(chain)
t(
    f"5-element same-quote chain folds to single literal (got {folded_chain!r})",
    folded_chain == '"xyyyyy"',
)
# Bound check — 1000-cap is the documented limit
huge_chain = '"a"' + (' + "b"' * 2000)
# We don't need to verify the exact output (CPU-bound); we just need:
#   1. The call returns in reasonable time (subject to test runner timeout)
#   2. The cap is honored (final string shouldn't be a single literal if cap
#      stopped us short — though here it actually finishes well under 2000).
result = _fold_string_concat(huge_chain, max_folds=10)
# After 10 folds we still see `+` operators
t(
    "cap on max_folds stops iteration short (operators remain at low cap)",
    " + " in result,
)
# Empty string passes through cleanly
t("empty content → empty fold result", _fold_string_concat("") == "")
t("whitespace-only content → identity", _fold_string_concat("   \n  ") == "   \n  ")


# ---------- verify-after end-to-end: scan_secrets catches concat tokens ----------
section("Bug 1: scan_secrets flags concat-obfuscated secrets")


def _hits(content: str) -> list:
    return scan_secrets(content)


# verify-before snapshot — without the fix, every "concat" line below would
# return 0 violations. With the fix, they should all match.

# ghp_ via double-quote concat
v = _hits(f'const t = "ghp_" + "{GHP_BODY_36}"')
t(
    "ghp_ via double-quote concat → at least 1 violation",
    len(v) >= 1,
    f"got {len(v)} violations",
)
t(
    "ghp_ concat violation carries 'github_token' kind in actual field",
    any("github_token" in (x.actual or "") for x in v),
    f"got actuals={[x.actual for x in v]}",
)
t(
    "ghp_ concat violation surfaces '[after string-concat fold]' marker",
    any("[after string-concat fold]" in x.actual for x in v),
)

# ghp_ via single-quote concat
v = _hits(f"const t = 'ghp_' + '{GHP_BODY_36}'")
t(
    "ghp_ via single-quote concat → at least 1 violation",
    len(v) >= 1,
    f"got {len(v)} violations",
)

# AKIA via double-quote concat (the exact dogfood example)
v = _hits('t = "AKIA" + "IOSFODNN7EXAMPLE"')
t(
    "AKIA via concat → at least 1 violation",
    len(v) >= 1,
    f"got {[x.actual for x in v]}",
)

# Direct (non-concat) match — regression check that we didn't break the
# happy path by introducing the preprocessor.
v = _hits(f'const t = "{GHP_FULL_DIRECT}"')
t(
    "ghp_ direct (no concat) → still flagged (regression check)",
    len(v) >= 1,
    f"got {len(v)} violations",
)
v = _hits(f'const t = "{AWS_KEY}"')
t(
    "AKIA direct (no concat) → still flagged (regression check)",
    len(v) >= 1,
    f"got {len(v)} violations",
)

# Negative — variable not literal: out of scope per spec
v = _hits('const t = "ghp_" + foo()')
t(
    "ghp_ + variable (NOT literal-to-literal) → not flagged (out of scope)",
    len(v) == 0,
    f"got {[x.actual for x in v]}",
)

# Negative — substring inside a path
v = _hits('const t = "/path/to/ghp_/file"')
t(
    "ghp_ as path substring (no trailing 36-char body) → not flagged",
    len(v) == 0,
    f"got {[x.actual for x in v]}",
)

# Python-style concat (same operator) — must also work
v = _hits(f't = "ghp_" + "{GHP_BODY_36}"\n')
t(
    "Python-style concat (same `+` operator) → at least 1 violation",
    len(v) >= 1,
)


# ---------------------------------------------------------------------------
# Bug 2 — Idioms not language-scoped
# ---------------------------------------------------------------------------

section("Bug 2: language_for_path extension mapping")

t("language_for_path('foo.rb') == 'ruby'", language_for_path("foo.rb") == "ruby")
t("language_for_path('a/b/foo.rb') == 'ruby'", language_for_path("a/b/foo.rb") == "ruby")
t("language_for_path('foo.ts') == 'typescript'", language_for_path("foo.ts") == "typescript")
t("language_for_path('foo.tsx') == 'typescript'", language_for_path("foo.tsx") == "typescript")
t("language_for_path('foo.js') == 'typescript'", language_for_path("foo.js") == "typescript")
t("language_for_path('foo.jsx') == 'typescript'", language_for_path("foo.jsx") == "typescript")
t("language_for_path('foo.mjs') == 'typescript'", language_for_path("foo.mjs") == "typescript")
t("language_for_path('foo.cjs') == 'typescript'", language_for_path("foo.cjs") == "typescript")
t("language_for_path('foo.py') == 'unknown'", language_for_path("foo.py") == "unknown")
t("language_for_path('foo.txt') == 'unknown'", language_for_path("foo.txt") == "unknown")
t("language_for_path(None) == 'unknown'", language_for_path(None) == "unknown")
t("language_for_path('') == 'unknown'", language_for_path("") == "unknown")


# ----------- filter_idioms_by_language ------------
section("Bug 2: filter_idioms_by_language — frontmatter parsing + filtering")

MIXED_IDIOMS = """# idioms

## active

### ruby-only-idiom
Status: active (added 2026-05-11)
Language: ruby
Always use Strong Params in Rails controllers.

### typescript-only-idiom
Status: active (added 2026-05-11)
Language: typescript
Prefer absolute imports via path aliases.

### universal-idiom
Status: active (added 2026-05-11)
Language: any
Always rotate secrets via env vars.

### legacy-no-frontmatter
Status: active (added 2025-12-01)
This idiom was captured before Language frontmatter existed.

## deprecated
"""


# verify-before sanity: confirm the input has all 4 expected slugs.
t(
    "fixture has all four idiom slugs (sanity)",
    "ruby-only-idiom" in MIXED_IDIOMS
    and "typescript-only-idiom" in MIXED_IDIOMS
    and "universal-idiom" in MIXED_IDIOMS
    and "legacy-no-frontmatter" in MIXED_IDIOMS,
)

# Ruby target — keeps ruby + any + legacy (legacy defaults to `any`)
filtered_ruby = filter_idioms_by_language(MIXED_IDIOMS, "ruby")
t("ruby filter keeps ruby-only-idiom", "ruby-only-idiom" in filtered_ruby)
t("ruby filter drops typescript-only-idiom", "typescript-only-idiom" not in filtered_ruby)
t("ruby filter keeps universal-idiom (Language: any)", "universal-idiom" in filtered_ruby)
t(
    "ruby filter keeps legacy-no-frontmatter (defaults to any)",
    "legacy-no-frontmatter" in filtered_ruby,
)
t(
    "ruby filter preserves section structure (## active heading)",
    "## active" in filtered_ruby and "## deprecated" in filtered_ruby,
)

# TypeScript target — keeps typescript + any + legacy
filtered_ts = filter_idioms_by_language(MIXED_IDIOMS, "typescript")
t("ts filter keeps typescript-only-idiom", "typescript-only-idiom" in filtered_ts)
t("ts filter drops ruby-only-idiom", "ruby-only-idiom" not in filtered_ts)
t("ts filter keeps universal-idiom", "universal-idiom" in filtered_ts)
t("ts filter keeps legacy-no-frontmatter", "legacy-no-frontmatter" in filtered_ts)

# Unknown target — only `any`-tagged + legacy (which defaults to any) pass
filtered_unknown = filter_idioms_by_language(MIXED_IDIOMS, "unknown")
t(
    "unknown filter drops both language-specific idioms",
    "ruby-only-idiom" not in filtered_unknown
    and "typescript-only-idiom" not in filtered_unknown,
)
t("unknown filter keeps universal-idiom", "universal-idiom" in filtered_unknown)
t(
    "unknown filter keeps legacy-no-frontmatter (defaults to any)",
    "legacy-no-frontmatter" in filtered_unknown,
)

# Filter annotation surfaces when something was hidden
t(
    "ruby filter appends '[N filtered]' comment when dropping idioms",
    "filtered" in filtered_ruby
    and ("idiom" in filtered_ruby.lower() or "idioms" in filtered_ruby.lower()),
)
t(
    "no-filter case (only any-idioms in input) produces no annotation",
    "filtered" not in filter_idioms_by_language(
        "# idioms\n\n## active\n\n### any-only\nLanguage: any\ntext\n",
        "ruby",
    ),
)

# Empty / minimal input
t("empty input → empty output", filter_idioms_by_language("", "ruby") == "")
t(
    "input without idioms (only headers) → preserved",
    filter_idioms_by_language("# idioms\n\n## active\n\n## deprecated\n", "ruby")
    == "# idioms\n\n## active\n\n## deprecated\n",
)

# Case-insensitive `Language:` key recognition
case_test = """## active

### case-test
Status: active
language: RUBY
body
"""
t(
    "Language: key is case-insensitive (lowercase key, uppercase value)",
    "case-test" in filter_idioms_by_language(case_test, "ruby")
    and "case-test" not in filter_idioms_by_language(case_test, "typescript"),
)

# Invalid / unknown language tag → defaults to `any` (defensive)
bad_lang = """## active

### bad-lang-tag
Status: active
Language: cobol
body
"""
t(
    "invalid Language: tag falls back to 'any' (kept across all targets)",
    "bad-lang-tag" in filter_idioms_by_language(bad_lang, "ruby")
    and "bad-lang-tag" in filter_idioms_by_language(bad_lang, "typescript"),
)

# Language: line mentioned in body (not frontmatter) — should NOT be treated as frontmatter
body_mention = """## active

### body-mention
Status: active

This idiom applies to the team. Language: ruby (in the body, not frontmatter)
"""
# After a blank line, we exit frontmatter window → block defaults to `any`
t(
    "Language: mentioned in body (after blank line) does NOT count as frontmatter — block defaults to any",
    "body-mention" in filter_idioms_by_language(body_mention, "ruby")
    and "body-mention" in filter_idioms_by_language(body_mention, "typescript"),
)

# Multiple Language: lines — first valid wins
multi_lang = """## active

### multi-lang
Status: active
Language: ruby
Language: typescript
body
"""
# First wins (ruby)
t(
    "multiple Language: lines — first valid value wins (kept for ruby)",
    "multi-lang" in filter_idioms_by_language(multi_lang, "ruby"),
)
t(
    "multiple Language: lines — first valid value wins (dropped for ts)",
    "multi-lang" not in filter_idioms_by_language(multi_lang, "typescript"),
)


# ---------- Backward compatibility: full legacy idioms.md text shape ----------
section("Bug 2: backward compatibility with pre-v0.5.2 idioms.md")

# An idioms.md captured by the v0.5.1 free-form teach_profile path has the
# exact wire shape teach_profile writes — no `Language:` line.
LEGACY_ONLY = """# idioms

## active

### idiom-2025-12-01-12345
Status: active (added 2025-12-01)
Always use absolute imports.

### idiom-2025-12-02-67890
Status: active (added 2025-12-02)
Wrap all API responses in a Result envelope.

## deprecated
"""

# All legacy idioms (no frontmatter) pass through for every target.
for target in ("ruby", "typescript", "unknown"):
    fout = filter_idioms_by_language(LEGACY_ONLY, target)
    t(
        f"legacy-only idioms.md preserved for target={target!r}",
        "idiom-2025-12-01-12345" in fout
        and "idiom-2025-12-02-67890" in fout,
    )

# Section headings preserved
fout = filter_idioms_by_language(LEGACY_ONLY, "ruby")
t(
    "section headings (## active / ## deprecated) preserved on legacy filter",
    "## active" in fout and "## deprecated" in fout,
)

# No annotation appended when nothing filtered (legacy idioms all default to `any`)
t(
    "legacy-only filter produces no '[filtered]' comment (nothing dropped)",
    "filtered" not in fout,
)


# ---------------------------------------------------------------------------
section("Summary")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
if FAIL:
    sys.exit(1)
sys.exit(0)
