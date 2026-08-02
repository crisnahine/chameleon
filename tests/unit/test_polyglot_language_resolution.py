"""A file's guidance comes from ITS language, never the profile's.

A profile carries one `language`, chosen by whichever extractor won detection.
The archetype read path used to fall back to that scalar whenever the edited
file's own extension was unrecognized:

    language = detect_language(str(p)) or loaded.profile.get("language")

which only fires for a file the profile's language does NOT own. On a modern
profile that is harmless, because `paths_pattern` carries a `:ext` tail and a
foreign file matches no archetype. On a LEGACY extension-blind profile -- still
a live compatibility path -- a foreign file exact-matches a first-class
archetype, and the fallback then extracted it under the wrong language.

That is not merely inaccurate. A non-empty snapshot can score a nonzero
`canonical_confidence`, which is what produces the `match_quality="ast"` plus
medium/high band that BOTH deny gates require, so a wrong-language file could
arm an enforcement block. `None` yields an empty snapshot, which scores nothing.

The measured asymmetry is why this needed a test rather than an argument: Go,
Java and C# read as TypeScript all yield `[]`, so a spot check on Go says
"harmless". PHP and Python yield `['ClassDeclaration']` -- the TS parser finds a
class-like construct and reports it. Testing only the quiet language would have
concluded there was no bug.

NOT GUARDED END TO END. Three attempts at a read-path guard were written and
all three were vacuous -- they passed with the fallback restored -- so they were
removed rather than shipped: an assertion on `match_quality` cannot discriminate
without a canonical witness in the fixture, and a spy on `tools.extract_dimensions`
never intercepts because the read path imports it locally. What is pinned below
is the extractor behaviour the fix rests on, measured, not the call site.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.lint_engine import detect_language, extract_dimensions

# Source in a language the profile does NOT own, paired with what the
# TypeScript extractor makes of it. The two that produce a node kind are the
# ones that could arm a block.
_FOREIGN_SOURCE: dict[str, str] = {
    "php": "<?php\nclass OrderService {\n  public function handle() {}\n}\n",
    "python": "def handler(request):\n    return {}\n\n\nclass Thing:\n    pass\n",
    "go": "package a\n\ntype S struct{}\n\nfunc H() error { return nil }\n",
    "java": "package a;\n\npublic class OrderService {\n  public void handle() {}\n}\n",
}


@pytest.mark.parametrize("language", sorted(_FOREIGN_SOURCE), ids=sorted(_FOREIGN_SOURCE))
def test_no_foreign_source_is_extracted_under_a_borrowed_language(language: str):
    """`None` is the honest answer for a language the extractor has no arm for."""
    snapshot = extract_dimensions(_FOREIGN_SOURCE[language], language=None, file_path="f.src")
    assert snapshot.top_level_node_kinds == [], (
        f"{language}: an unowned language must yield an empty snapshot, not "
        f"{snapshot.top_level_node_kinds}"
    )


def test_the_borrowed_language_really_did_invent_a_node_kind():
    """Pins the harm the fix removes, so the fix cannot be quietly reverted.

    If this ever stops holding, the TypeScript extractor changed and the comment
    on the fix needs re-checking -- but the fix itself stays right either way,
    since a file is still not the profile's language.
    """
    borrowed = extract_dimensions(_FOREIGN_SOURCE["php"], language="typescript", file_path="f.php")
    assert borrowed.top_level_node_kinds == ["ClassDeclaration"], borrowed.top_level_node_kinds


def test_a_first_class_file_is_unaffected_by_the_change():
    """The fallback never fired for these: `detect_language` already answers."""
    for path, expected in (("a.ts", "typescript"), ("a.py", "python"), ("a.rb", "ruby")):
        assert detect_language(path) == expected, path


@pytest.mark.parametrize("path", ["a.go", "a.rs", "a.java", "a.cs", "a.php", "Makefile"])
def test_the_narrow_gate_still_returns_none_for_everything_else(path: str):
    """The gate the fix now relies on alone, with no profile scalar behind it."""
    assert detect_language(path) is None, path
