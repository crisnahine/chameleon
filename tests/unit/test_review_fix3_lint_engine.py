"""Review fix: commented-out-code detection now fires for Python.

extract_comment_spans gained a Python arm (`#` line comments), and the bootstrap
comment scan already had `_ext_for`/`_span_is_code` Python branches, so a block of
commented-out Python code is now detected end-to-end where it was a silent no-op.
"""

from __future__ import annotations

from chameleon_mcp.bootstrap.comment_scan import detect_commented_out_code_by_group
from chameleon_mcp.extractors.python import PythonExtractor
from chameleon_mcp.lint_engine import extract_comment_spans


def _detect(contents, *, language, extractor) -> int:
    counts = detect_commented_out_code_by_group(
        {"g": contents}, language=language, extractor=extractor
    )
    return counts.get("g", 0)


def test_extract_comment_spans_python_stitches_hash_lines():
    content = "# def handler(req):\n# return process(req)\nx = 1\n"
    spans = extract_comment_spans(content, language="python")
    assert any("def handler" in s and "return process" in s for s in spans)


def test_extract_comment_spans_python_no_block_comment_crash():
    # Python has no block comment; a triple-quoted string is NOT a comment span.
    spans = extract_comment_spans('"""not a comment"""\n# real = comment()\n', language="python")
    assert all("not a comment" not in s for s in spans)


def test_detect_commented_out_code_python_flags_real_code():
    # A commented-out function definition parses as real Python code.
    commented = "# def process(data):\n#     return transform(data)\n\nactive = 1\n"
    n = _detect([commented], language="python", extractor=PythonExtractor())
    assert n >= 1


def test_detect_commented_out_code_python_ignores_prose():
    prose = "# this function processes the data and returns it\n# see the docs for details\n"
    n = _detect([prose], language="python", extractor=PythonExtractor())
    assert n == 0
