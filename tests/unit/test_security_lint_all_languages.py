"""The security checks reach every source language, not just the linted three.

`detect_language` answers "does this language have a dimension EXTRACTOR", and
gating the secret and eval-sink scans on it silently exempted every
extraction-tier language from the only two checks that are not style opinions:
an AWS key in a `.go` file was advisory where the identical key in a `.rb` file
was block-eligible. `security_language` is the wider gate those two checks use.

The split has to stay a split. Widening `detect_language` itself would hand the
heuristic extractors a language they have no arm for, and an EMPTY snapshot
compared against a real archetype query reports "file is missing top-level
constructs" on every well-formed file -- trading a silent gap for a loud false
positive on every edit. `test_the_narrow_gate_stays_narrow` pins that reasoning
to executable evidence so the next reader does not have to take it on faith.
"""

from __future__ import annotations

import pytest

from chameleon_mcp import language_support as ls
from chameleon_mcp.lint_engine import (
    _strip_c_family_strings_and_comments,
    detect_language,
    extract_dimensions,
    lint,
    scan_dangerous_sinks,
    scan_secrets,
    security_language,
)
from chameleon_mcp.violation_class import block_eligible_on_file, tag_secret_hardness

# AWS's own published documentation example key, and the fixture these tests
# exist to detect. The scanner flags it here exactly as it should, so the
# directive is required rather than a convenience.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # chameleon-ignore secret-detected-in-content

_SOURCES: dict[str, tuple[str, str]] = {
    "go": ("leak.go", 'package m\n\nconst k = "%s"\n'),
    "rust": ("leak.rs", 'pub const K: &str = "%s";\n'),
    "java": ("Leak.java", 'class Leak { static String k = "%s"; }\n'),
    "csharp": ("Leak.cs", 'class Leak { const string K = "%s"; }\n'),
    "php": ("leak.php", '<?php\n$k = "%s";\n'),
    "typescript": ("leak.ts", 'export const k = "%s";\n'),
    "ruby": ("leak.rb", 'K = "%s"\n'),
    "python": ("leak.py", 'K = "%s"\n'),
}


@pytest.mark.parametrize("language", sorted(_SOURCES), ids=sorted(_SOURCES))
def test_a_hardcoded_credential_is_block_eligible_in_every_language(language: str):
    """The check that matters most must not depend on which tier a language is."""
    filename, template = _SOURCES[language]
    found = scan_secrets(template % _AWS_KEY)
    assert found, f"{language}: the scanner did not see the credential"

    violations = [v.to_dict() for v in found]
    tag_secret_hardness(violations)
    eligible = block_eligible_on_file(violations, language=security_language(filename))
    assert eligible, (
        f"{language}: a hardcoded credential in {filename} is not block-eligible; "
        f"security_language({filename!r}) = {security_language(filename)!r}"
    )


def test_prose_and_unsupported_languages_stay_advisory():
    """A credential-shaped token in markdown or config text has no inline
    `chameleon-ignore` escape, so blocking on it would trap the turn with no way
    out. That is the distinction the narrow gate was really protecting, and
    widening the SECURITY gate must not lose it."""
    for filename in ("NOTES.md", "config.yaml", "data.json", "a.zig"):
        assert security_language(filename) is None, filename
        violations = [{"rule": "secret-detected-in-content", "severity": "error", "actual": "x"}]
        assert block_eligible_on_file(violations, language=security_language(filename)) == []


def test_the_narrow_gate_stays_narrow():
    """Widening `detect_language` instead would be the wrong fix, and this pins
    why: the heuristic extractor has no arm for these languages, so the snapshot
    comes back empty, and an empty snapshot against a real archetype query
    reports the file as structurally wrong."""
    for filename in ("a.go", "a.rs", "a.java", "a.cs", "a.php"):
        assert detect_language(filename) is None, filename

    empty = extract_dimensions("package m\n\nfunc X() {}\n", language="go", file_path="a.go")
    assert empty.top_level_node_kinds == []
    stored = {
        "top_level_node_kinds": ["FuncDecl", "PackageClause"],
        "default_export_kind": None,
        "named_export_count_bucket": "2",
        "jsx_present": False,
        "content_signal": None,
    }
    assert lint(empty, stored, language="go"), (
        "an empty snapshot no longer mismatches a real archetype query; if that "
        "changed, re-evaluate whether detect_language could now be widened directly"
    )


def test_an_eval_sink_is_detected_in_an_extraction_tier_language():
    """The sink scan must be reached THROUGH the gate, not handed a language.

    Passing `language="go"` directly would prove nothing: `scan_dangerous_sinks`
    already returned eval-call for any unrecognized value (including None) via
    its raw-content branch, so such a test passes with the gate reverted. The
    gate is what decides whether the scan runs at all, so the gate is what the
    call has to go through.
    """
    for filename, source in (
        ("sink.go", "package m\nfunc f(){ eval(x) }\n"),
        ("sink.php", '<?php\neval($_GET["c"]);\n'),
        ("sink.rs", "fn f() { eval(x) }\n"),
    ):
        language = security_language(filename)
        assert language is not None, filename
        found = [
            v for v in scan_dangerous_sinks(source, language=language) if v.rule == "eval-call"
        ]
        assert found, f"{filename}: no eval-call via security_language -> {language!r}"


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("c.go", "package m\n// TODO: stop using eval(x)\nfunc f(){}\n"),
        ("c.go", "package m\nvar s = `eval(`\n"),
        ("C.java", 'class C { String s = "eval("; }\n'),
        ("C.cs", 'class C { const string S = @"eval("; }\n'),
        ("c.php", "<?php\n# eval($x) is banned here\n"),
        ("c.rs", 'fn f() { let s = r#"eval("#; }\n'),
        ("c.rs", "// eval(x)\nfn f() {}\n"),
    ],
)
def test_a_commented_or_quoted_eval_is_not_a_sink(filename: str, source: str):
    """Widening the gate without a stripper would have made this a BLOCK.

    `scan_dangerous_sinks` had string/comment strippers for ruby/typescript/
    python only and fell through to raw content otherwise, so every case here
    would have flagged eval-call at error severity on well-formed code.
    """
    language = security_language(filename)
    found = [v for v in scan_dangerous_sinks(source, language=language) if v.rule == "eval-call"]
    assert not found, f"{filename}: false eval-call on {source!r}"


def test_a_rust_lifetime_does_not_swallow_the_code_after_it():
    """The char-literal arm must not treat `'a` in `&'a str` as an open quote.

    A general single-quoted-string pattern would match from that `'` to the next
    one in the file, blanking real code (and any `eval(` in it) as if it were a
    string literal -- a false NEGATIVE hiding a live sink.

    Asserted on the STRIPPER's own output, not on whether a finding survives:
    with the stripper disabled `scan_dangerous_sinks` falls back to raw content
    and still reports the sink, so a findings-based assertion here passes no
    matter what the stripper does. What actually needs pinning is that the text
    between the lifetimes is left alone.
    """
    source = "fn f<'a>(x: &'a str) -> &'a str { eval(y); x }\n"
    stripped = _strip_c_family_strings_and_comments(source, "rust")
    assert stripped is not None
    assert "eval(y)" in stripped, f"the lifetime blanked live code: {stripped!r}"
    assert stripped == source, "nothing in this line is a literal, so nothing should be blanked"


def test_the_registry_agrees_with_the_security_wiring():
    """`language_support` must not claim a check the wiring does not deliver.

    Graded against OBSERVED block-eligibility, not against `security_language`'s
    return value: asserting the declaration matches the helper only restates the
    helper, and would still pass in a world where every gate downstream of it
    was narrow and no credential ever blocked. A capability table that drifts
    from the wiring is the vacuous-silence bug this module exists to prevent,
    one layer up.
    """
    for language in ls.supported_languages():
        caps = ls.capabilities_for(language)
        assert caps is not None
        filename, template = _SOURCES[language]
        violations = [v.to_dict() for v in scan_secrets(template % _AWS_KEY)]
        tag_secret_hardness(violations)
        blocks = bool(block_eligible_on_file(violations, language=security_language(filename)))
        assert caps.security_lint == blocks, (
            f"{language}: registry says security_lint={caps.security_lint} but a "
            f"hardcoded credential in {filename} is block-eligible={blocks}"
        )
