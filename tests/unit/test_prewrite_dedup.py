"""G-025: pre-write reuse-before-create nudge.

The turn-end duplication catch fires after the model writes; a one-shot
generation has no next turn to act on it. This section surfaces a cross-file
name collision BEFORE the write, so the model reuses instead of duplicating.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import hook_helper
from chameleon_mcp.function_catalog import CatalogedFunction, FunctionCatalog, name_tokens


def _catalog(entries):
    # entries: (name, file) or (name, file, arity); tokens computed like the real
    # catalog so the semantic (token-overlap) pass has real tokens to match on.
    fns = []
    for e in entries:
        n, f = e[0], e[1]
        arity = e[2] if len(e) > 2 else 1
        fns.append(
            CatalogedFunction(
                name=n,
                kind="function",
                file=f,
                arity=arity,
                required=arity,
                tokens=name_tokens(n),
                body_hash=None,
            )
        )
    return FunctionCatalog(fns)


def test_extract_python_names():
    c = "import os\n\ndef clean_url(u):\n    return u\n\nasync def fetch_data():\n    pass\n"
    assert hook_helper._extract_defined_names(c, "app/x.py") == {"clean_url", "fetch_data"}


def test_extract_ruby_names():
    c = "class Foo\n  def clean_url(u)\n  end\n  def self.build_thing\n  end\nend\n"
    assert hook_helper._extract_defined_names(c, "app/x.rb") == {"clean_url", "build_thing"}


def test_extract_ts_names():
    c = "export function cleanUrl(u: string) {}\nconst parseUser = (r: string) => r;\n"
    got = hook_helper._extract_defined_names(c, "src/x.ts")
    assert "cleanUrl" in got and "parseUser" in got


def test_cross_file_name_collision_fires(tmp_path, monkeypatch):
    repo = tmp_path
    cat = _catalog([("clean_url", "app/helpers/url_helper.rb")])
    monkeypatch.setattr(hook_helper, "load_function_catalog", lambda r: cat, raising=False)
    # patch the imported symbol inside the function's local import
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "class Account\n  def clean_url(u)\n    u.strip\n  end\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert "reuse-before-create" in out
    assert "clean_url" in out
    assert "app/helpers/url_helper.rb" in out


def test_same_file_match_does_not_fire(tmp_path, monkeypatch):
    repo = tmp_path
    # the only catalog entry is in the SAME file being edited -> not a duplicate
    cat = _catalog([("clean_url", "app/models/account.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "def clean_url(u)\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_generic_name_is_stopworded(tmp_path, monkeypatch):
    repo = tmp_path
    cat = _catalog([("render", "app/other.rb"), ("index", "app/z.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "def render\nend\ndef index\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_short_name_skipped(tmp_path, monkeypatch):
    repo = tmp_path
    cat = _catalog([("id", "app/other.rb"), ("run", "app/z.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "def id\nend\ndef run\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_kill_switch(tmp_path, monkeypatch):
    repo = tmp_path
    cat = _catalog([("clean_url", "app/helpers/url_helper.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    monkeypatch.setenv("CHAMELEON_PREWRITE_DEDUP", "0")
    content = "def clean_url(u)\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_no_catalog_fails_open(tmp_path, monkeypatch):
    repo = tmp_path
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: None)
    content = "def clean_url(u)\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_empty_content_and_missing_args():
    assert hook_helper._prewrite_dedup_section("", "a.rb", Path("/x")) == ""
    assert hook_helper._prewrite_dedup_section("def clean_url\nend", "", Path("/x")) == ""
    assert hook_helper._prewrite_dedup_section("def clean_url\nend", "a.rb", None) == ""


def test_hits_bounded(tmp_path, monkeypatch):
    repo = tmp_path
    entries = [(f"helper_func_{i}", f"app/lib/f{i}.rb") for i in range(20)]
    cat = _catalog(entries)
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "\n".join(f"def helper_func_{i}\nend" for i in range(20))
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    # capped at PREWRITE_DEDUP_MAX_HITS (5) list items
    assert out.count("- `") == 5


def test_extract_functions_with_arity():
    py = "def format_display_date(d, tz=None):\n    pass\n"
    got = dict(hook_helper._extract_defined_functions(py, "app/x.py"))
    assert got["format_display_date"] == 2  # d, tz (self not present)
    rb = "def parse_money_amount(raw, opts = {})\nend\n"
    gotr = dict(hook_helper._extract_defined_functions(rb, "app/x.rb"))
    assert gotr["parse_money_amount"] == 2
    ts = "function toDisplayDate(d: Date, fmt: string) { return d; }\n"
    gott = dict(hook_helper._extract_defined_functions(ts, "src/x.ts"))
    assert gott["toDisplayDate"] == 2


def test_semantic_different_name_shared_tokens_fires(tmp_path, monkeypatch):
    # existing `format_display_date(d, tz)`; model writes `render_display_date(d, tz)`
    # -> different name, shares {display, date} (>= 2 tokens), close shape -> nudge.
    repo = tmp_path
    cat = _catalog([("format_display_date", "app/helpers/date_helper.rb", 2)])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "class Account\n  def render_display_date(d, tz)\n    d\n  end\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert "format_display_date" in out
    assert "app/helpers/date_helper.rb" in out
    assert "looks like the existing" in out


def test_semantic_single_shared_token_does_not_fire(tmp_path, monkeypatch):
    # `save_record(x)` vs existing `delete_record(y)` share only {record} (1 token)
    # -> below the >= 2 pre-write bar -> no nudge (precision guard).
    repo = tmp_path
    cat = _catalog([("delete_record", "app/other.rb", 1)])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "def save_record(x)\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/models/account.rb"), repo)
    assert out == ""


def test_rough_arity_nested_defaults():
    assert hook_helper._rough_arity("a, b=[1, 2], c", drop_self=False) == 3
    assert hook_helper._rough_arity("self, x, y", drop_self=True) == 2
    assert hook_helper._rough_arity("", drop_self=False) == 0
