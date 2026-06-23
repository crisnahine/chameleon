"""Regression tests for the conventions.py review fixes.

Covers three behavioral fixes:

* ``format_directory_listing`` caps the sibling list to the alphabetically-first
  ``max_files`` deterministically, regardless of filesystem iteration order.
* ``compute_doc_coverage_from_content`` detects Python/Ruby declarations on a
  strings/comments-stripped copy (so a def inside a template is not counted as
  public surface) while still recognising a genuine docstring/leading comment on
  the original lines.
* ``_py_decl_has_docstring`` finds the docstring after a signature whose
  parameter list spans more than 15 physical lines.
"""

from __future__ import annotations

from chameleon_mcp.conventions import (
    compute_doc_coverage_from_content,
    format_directory_listing,
)


def test_directory_listing_returns_alphabetical_first_k(tmp_path):
    # Create more siblings than the cap, named so alphabetical order differs
    # from any plausible filesystem creation order.
    for name in ["z.ts", "m.ts", "a.ts", "q.ts", "b.ts"]:
        (tmp_path / name).write_text("export const x = 1;\n", encoding="utf-8")
    target = tmp_path / "target.ts"
    target.write_text("export const t = 1;\n", encoding="utf-8")

    out = format_directory_listing(str(target), max_files=2)

    # Cap applied to the alphabetically-first two, with an accurate overflow tail.
    assert "Nearby: a.ts, b.ts (+3 more)" in out


def test_directory_listing_no_overflow_tail_when_under_cap(tmp_path):
    (tmp_path / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("export const b = 1;\n", encoding="utf-8")
    target = tmp_path / "target.ts"
    target.write_text("export const t = 1;\n", encoding="utf-8")

    out = format_directory_listing(str(target), max_files=10)

    assert "Nearby: a.ts, b.ts -- check before creating a new file." == out


def test_python_doc_coverage_excludes_declarations_inside_template():
    # A documented public function, plus a triple-quoted template that contains
    # text resembling def/class declarations. The template ones must not count
    # toward the public surface.
    content = (
        "def render(value):\n"
        '    """Render the value."""\n'
        "    return TEMPLATE\n"
        "\n"
        'TEMPLATE = """\n'
        "def fake_one(x):\n"
        "    pass\n"
        "class FakeTwo:\n"
        "    pass\n"
        '"""\n'
    )
    documented, public = compute_doc_coverage_from_content(content, language="python")

    # Only ``render`` is real public surface, and it is documented.
    assert public == 1
    assert documented == 1


def test_python_doc_coverage_counts_genuine_docstring():
    content = 'def real(a, b):\n    """Adds two values."""\n    return a + b\n'
    documented, public = compute_doc_coverage_from_content(content, language="python")
    assert (documented, public) == (1, 1)


def test_python_doc_coverage_flags_undocumented_real_function():
    content = "def bare(a):\n    return a\n"
    documented, public = compute_doc_coverage_from_content(content, language="python")
    assert (documented, public) == (0, 1)


def test_ruby_doc_coverage_excludes_def_inside_heredoc():
    content = (
        "# Documented method.\n"
        "def real\n"
        "  TEMPLATE\n"
        "end\n"
        "\n"
        "TEMPLATE = <<~RUBY\n"
        "  def fake_one\n"
        "    noop\n"
        "  end\n"
        "RUBY\n"
    )
    documented, public = compute_doc_coverage_from_content(content, language="ruby")

    # Only ``real`` is real public surface, and it carries a leading comment.
    assert public == 1
    assert documented == 1


def test_ruby_doc_coverage_counts_leading_comment():
    content = "# A documented method.\ndef foo\n  bar\nend\n"
    documented, public = compute_doc_coverage_from_content(content, language="ruby")
    assert (documented, public) == (1, 1)


def test_python_long_signature_still_finds_docstring():
    # A def whose parameter list spans well past the old 15-line backstop, with a
    # docstring as the first body statement. The old bound returned False; the
    # widened scan must reach the colon and find the docstring.
    params = "".join(f"    arg_{n},\n" for n in range(40))
    content = (
        f'def big(\n{params}):\n    """Documented despite the long signature."""\n    return None\n'
    )
    documented, public = compute_doc_coverage_from_content(content, language="python")
    assert (documented, public) == (1, 1)


def test_typescript_doc_coverage_unchanged_by_strip_fix():
    # TS naming derivation does not strip, so the doc-coverage TS branch must stay
    # raw — a documented exported function counts, an undocumented one does not.
    content = "/** Does a thing. */\nexport function documented() {}\nexport function bare() {}\n"
    documented, public = compute_doc_coverage_from_content(content, language="typescript")
    assert (documented, public) == (1, 2)
