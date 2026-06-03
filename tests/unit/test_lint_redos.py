"""The lint dimension extractor must not blow up on whitespace-heavy files.

The line-start anchors were written `^\\s*` / `^\\s+` under re.MULTILINE. Because
`\\s` matches `\\n`, every line-start anchor scanned forward across all following
blank lines, giving O(n^2) on leading-whitespace input: ~20s on a 100KB file,
which stalls the PreToolUse hook and lint_file. The leading anchors must match
in-line indentation only (`[ \\t]`), not newlines.
"""

from __future__ import annotations

import time

import pytest

from chameleon_mcp import lint_engine


@pytest.mark.parametrize("language", ["typescript", "ruby"])
def test_extract_dimensions_is_linear_on_whitespace_heavy_input(language):
    # 100KB of blank, indented lines — the pathological case for `^\s*` MULTILINE.
    content = "   \n" * 25_000
    start = time.perf_counter()
    lint_engine.extract_dimensions(content, language=language)
    elapsed = time.perf_counter() - start
    # The O(n^2) version took ~20s; the linear version is well under a second.
    assert elapsed < 2.0, f"{language} extract took {elapsed:.2f}s (regex blowup not fixed)"
