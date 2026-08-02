"""Unterminated strings and comments must not become false security findings.

The lint path scans a CLIPPED copy of large files (~100k chars), so a cut
landing inside a docstring, block comment or template literal leaves the opener
unpaired. The strippers used to give up on that construct and leave its body
visible, so every `eval(` written in prose became an `eval-call` finding -- and
`eval-call` is block-eligible, so a large file could be BLOCKED over its own
documentation.

This was found the hard way: chameleon blocked an edit to its own
`lint_engine.py` citing "dynamic eval() at line 2184", a line that is prose
inside the `scan_dangerous_sinks` docstring. The 99900-char clip fell in the
middle of that docstring.

The same patterns also carried a quadratic: `/\\*.*?\\*/` rescans to EOF for
every unclosed `/*`, which measured ~950ms on a 100KB file -- on a hook that
budgets tens of milliseconds.
"""

from __future__ import annotations

import time

import pytest

from chameleon_mcp.lint_engine import scan_dangerous_sinks


def _evals(source: str, *, language: str) -> list[str]:
    return [
        v.actual for v in scan_dangerous_sinks(source, language=language) if v.rule == "eval-call"
    ]


@pytest.mark.parametrize(
    ("language", "source"),
    [
        # Each source is CUT mid-construct, exactly as a clip would leave it.
        ("python", 'def f():\n    """\n    Docs that mention eval(x) in prose.\n'),
        ("python", "def f():\n    '''\n    Docs that mention eval(x) in prose.\n"),
        ("typescript", "const a = 1\n/* docs: call eval(x) here\n"),
        ("typescript", "const t = `prose about eval(x)\n"),
        ("go", "package m\n/* docs: eval(x)\n"),
        ("rust", "fn f() {}\n/* docs: eval(x)\n"),
        ("java", "class A {}\n/* docs: eval(x)\n"),
        ("csharp", "class A {}\n/* docs: eval(x)\n"),
        ("php", "<?php\n/* docs: eval($x)\n"),
    ],
)
def test_an_unterminated_construct_does_not_leak_a_false_sink(language: str, source: str):
    """A clip that cuts mid-construct must blank to EOF, not give up."""
    assert _evals(source, language=language) == [], (
        f"{language}: prose inside an unterminated construct was read as a real "
        f"sink -- that is a false BLOCK on {source!r}"
    )


@pytest.mark.parametrize(
    ("language", "source"),
    [
        ("python", "def f():\n    eval(x)\n"),
        ("typescript", "function f(){ eval(x) }\n"),
        ("ruby", "def f; eval(x); end\n"),
        ("go", "package m\nfunc f(){ eval(x) }\n"),
        ("rust", "fn f() { eval(x) }\n"),
        ("java", "class A { void f(){ eval(x); } }\n"),
        ("csharp", "class A { void F(){ eval(x); } }\n"),
        ("php", "<?php\neval($x);\n"),
    ],
)
def test_a_real_sink_still_fires(language: str, source: str):
    """The other direction: blanking to EOF must not swallow live code.

    A stripper that over-blanks hides real sinks, which is the worse failure --
    a false negative in a security check reads as a clean file.
    """
    assert _evals(source, language=language), f"{language}: real eval( no longer detected"


@pytest.mark.parametrize("language", ["typescript", "go", "java", "csharp", "rust", "php"])
def test_unclosed_block_comments_do_not_blow_the_latency_budget(language: str):
    """Guards the quadratic, not the exact timing.

    The lazy `/\\*.*?\\*/` form took ~950ms here; the linear form takes single-
    digit milliseconds. The bound is deliberately loose (a CI box under load is
    still nowhere near 950ms), so this fails on a reintroduced quadratic rather
    than on ordinary timing noise.
    """
    body = "x\n/* never closed\n" * 4000
    started = time.perf_counter()
    scan_dangerous_sinks(body, language=language)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 400, f"{language}: {elapsed_ms:.0f}ms -- the quadratic scan is back"


@pytest.mark.parametrize(
    ("language", "source"),
    [
        # Each cut mid multi-line LITERAL, the form each language spans lines with.
        ("go", "package m\nvar s = `SELECT\n  eval(x)\n"),
        ("java", 'class C { String s = """\n  eval(x)\n'),
        ("csharp", 'class C { const string S = """\n  eval(x)\n'),
        ("php", "<?php\n$a = <<<EOT\n eval($x)\n"),
        ("rust", 'fn f() { let s = r#"\n eval(x)\n'),
    ],
)
def test_an_unterminated_multiline_literal_does_not_leak_a_sink(language: str, source: str):
    """The C-family half of the clipped-content problem.

    The TS and Python strippers carry unterminated arms for BOTH their comment
    and their multi-line string forms; the C-family originally got one only for
    block comments. A >100KB Go backtick literal, Java or C# text block, PHP
    heredoc or Rust raw string cut by the ~100k clip therefore left its tail
    scanned as live code, and a dynamic-execution token inside it fired at error
    severity on a file that merely got truncated.
    """
    assert _evals(source, language=language) == [], (
        f"{language}: a clipped multi-line literal leaked a false sink"
    )
