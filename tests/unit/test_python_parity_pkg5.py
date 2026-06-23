"""PKG-5: Python style lint (black/ruff/flake8 config -> indent/quote/line-length).

The style baseline reads ONLY declared formatter config (no inferred rule), like
the TS/Ruby paths. For Python that config lives in pyproject.toml ([tool.black],
[tool.ruff], [tool.ruff.format]) and setup.cfg/.flake8/tox.ini ([flake8]).
"""

from __future__ import annotations

from chameleon_mcp.bootstrap.tool_config import read_tool_configs
from chameleon_mcp.lint_engine import _declared_max_line_length, _declared_quote, scan_style_rules


def _rules(**python_format):
    return {"rules": {"python_format": {"source": "pyproject.toml", "rules": python_format}}}


# --------------------------------------------------------------------------- #
# tool_config reader
# --------------------------------------------------------------------------- #


def test_reads_black_and_ruff_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.black]\nline-length = 100\n\n[tool.ruff.format]\nquote-style = "single"\n',
        encoding="utf-8",
    )
    res = read_tool_configs(tmp_path)
    assert res.python_format == {"line_length": 100, "quote_style": "single"}


def test_black_defaults_to_double_quotes(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n", encoding="utf-8")
    res = read_tool_configs(tmp_path)
    assert res.python_format["quote_style"] == "double"


def test_black_skip_string_normalization_no_quote_pref(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.black]\nskip-string-normalization = true\n", encoding="utf-8"
    )
    res = read_tool_configs(tmp_path)
    assert (res.python_format or {}).get("quote_style") is None


def test_reads_flake8_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 120\n", encoding="utf-8")
    res = read_tool_configs(tmp_path)
    assert res.python_format == {"line_length": 120}


def test_no_python_config(tmp_path):
    assert read_tool_configs(tmp_path).python_format is None


# --------------------------------------------------------------------------- #
# _declared_* read the python_format section
# --------------------------------------------------------------------------- #


def test_declared_max_line_length_python():
    assert _declared_max_line_length(_rules(line_length=88), "python") == 88


def test_declared_quote_python():
    assert _declared_quote(_rules(quote_style="double"), "python") == "double"


# --------------------------------------------------------------------------- #
# scan_style_rules — line length + quote, on real Python content
# --------------------------------------------------------------------------- #


def test_line_length_flagged():
    rules = _rules(line_length=20)
    content = "x = 1\nresult = some_really_long_function_name(argument_one, argument_two)\n"
    v = scan_style_rules(content, language="python", rules=rules)
    assert any(x.rule == "style-rule-violation" and "cols" in x.actual for x in v)


def test_line_length_clean_within_limit():
    rules = _rules(line_length=200)
    v = scan_style_rules("x = 1\ny = 2\n", language="python", rules=rules)
    assert not any("cols" in x.actual for x in v)


def test_quote_flagged_single_when_double_preferred():
    rules = _rules(quote_style="double")
    v = scan_style_rules("x = 'hello'\n", language="python", rules=rules)
    assert any(x.rule == "style-rule-violation" and "single-quoted" in x.actual for x in v)


def test_quote_clean_double():
    rules = _rules(quote_style="double")
    v = scan_style_rules('x = "hello"\n', language="python", rules=rules)
    assert not any("quoted string" in x.actual for x in v)


def test_quote_skips_docstrings_and_fstrings():
    rules = _rules(quote_style="double")
    # A triple-quoted docstring and an f-string must not be quote-flagged.
    content = "'''module docstring'''\nx = f'{value}'\n"
    v = scan_style_rules(content, language="python", rules=rules)
    assert not any("quoted string" in x.actual for x in v)


def test_quote_skips_literal_needing_the_other_quote():
    rules = _rules(quote_style="double")
    # 'it"s' would need escaping if switched to double -> not flagged.
    v = scan_style_rules("x = 'say \"hi\"'\n", language="python", rules=rules)
    assert not any("quoted string" in x.actual for x in v)
