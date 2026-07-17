"""G-025: pre-write reuse-before-create nudge.

The turn-end duplication catch fires after the model writes; a one-shot
generation has no next turn to act on it. This section surfaces a cross-file
name collision BEFORE the write, so the model reuses instead of duplicating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    got = {n for n, _ in hook_helper._extract_defined_functions(c, "app/x.py")}
    assert got == {"clean_url", "fetch_data"}


def test_extract_ruby_names():
    c = "class Foo\n  def clean_url(u)\n  end\n  def self.build_thing\n  end\nend\n"
    got = {n for n, _ in hook_helper._extract_defined_functions(c, "app/x.rb")}
    assert got == {"clean_url", "build_thing"}


def test_extract_ts_names():
    c = "export function cleanUrl(u: string) {}\nconst parseUser = (r: string) => r;\n"
    got = {n for n, _ in hook_helper._extract_defined_functions(c, "src/x.ts")}
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


def test_migration_change_override_not_flagged(tmp_path, monkeypatch):
    # `change` is a per-file Rails migration override every migration redefines,
    # not a unique reuse target -> the stopword list filters it before both passes.
    repo = tmp_path
    cat = _catalog([("change", "db/migrate/001_a.rb"), ("change", "db/migrate/002_b.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = (
        "class AddCol < ActiveRecord::Migration[8.1]\n"
        "  def change\n    add_column :t, :c, :string\n  end\nend\n"
    )
    out = hook_helper._prewrite_dedup_section(content, str(repo / "db/migrate/003_c.rb"), repo)
    assert out == ""


def test_ubiquitous_name_skipped_by_definer_frequency(tmp_path, monkeypatch):
    # `configure` defined across many files (> PREWRITE_DEDUP_MAX_DEFINERS) is a
    # per-file convention, not a reuse target -> the frequency guard skips it.
    # Single-token name stays below the 2-token semantic bar, isolating the guard.
    repo = tmp_path
    cat = _catalog([("configure", f"config/init/i{i}.rb") for i in range(8)])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "class Setup\n  def configure(app)\n    app\n  end\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "config/init/new.rb"), repo)
    assert "reuse-before-create" not in out


def test_low_frequency_duplicate_still_flagged(tmp_path, monkeypatch):
    # A genuine helper duplicated in only a couple of files stays below the
    # frequency bar and is still surfaced -- the guard must not over-suppress.
    repo = tmp_path
    cat = _catalog([("calculate_discount", "app/a.rb"), ("calculate_discount", "app/b.rb")])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    content = "class C\n  def calculate_discount(x)\n    x\n  end\nend\n"
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/c.rb"), repo)
    assert "reuse-before-create" in out
    assert "calculate_discount" in out


def test_frequency_guard_language_agnostic(tmp_path, monkeypatch):
    # The frequency guard keys on definer-file count, not language: a NestJS pipe
    # `transform` (TS) and a Django management command `handle` (Python) spread
    # across many files are both per-file contracts, not reuse targets.
    repo = tmp_path
    ts_cat = _catalog([("transform", f"src/pipes/p{i}.ts") for i in range(8)])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: ts_cat)
    ts_out = hook_helper._prewrite_dedup_section(
        "export class MyPipe {\n  transform(v: string) { return v; }\n}\n",
        str(repo / "src/pipes/new.ts"),
        repo,
    )
    assert "reuse-before-create" not in ts_out

    py_cat = _catalog([("handle", f"app/management/commands/c{i}.py") for i in range(8)])
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: py_cat)
    py_out = hook_helper._prewrite_dedup_section(
        "class Command:\n    def handle(self, *args):\n        pass\n",
        str(repo / "app/management/commands/new.py"),
        repo,
    )
    assert "reuse-before-create" not in py_out


def test_class_method_not_a_reuse_target(tmp_path, monkeypatch):
    # A class-bound method is not importable, so the same name on a DIFFERENT
    # class is not a reuse target (fixes the `expired`/`change`/`create` FP);
    # a free FUNCTION of the same name still is.
    from chameleon_mcp.function_catalog import CatalogedFunction, FunctionCatalog, name_tokens

    repo = tmp_path
    content = "class BuildQuerySet\n  def expired(days)\n    days\n  end\nend\n"

    method_cat = FunctionCatalog(
        [
            CatalogedFunction(
                name="expired",
                kind="method",
                file="app/domains/querysets.rb",
                arity=1,
                required=1,
                tokens=name_tokens("expired"),
                body_hash=None,
            )
        ]
    )
    monkeypatch.setattr(
        "chameleon_mcp.function_catalog.load_function_catalog", lambda r: method_cat
    )
    out = hook_helper._prewrite_dedup_section(content, str(repo / "app/builds/querysets.rb"), repo)
    assert "reuse-before-create" not in out

    # Non-vacuity control: a non-class-bound `function` kind DOES surface the
    # nudge, so it is the kind exclusion above that suppressed it, not an empty
    # extraction. (The `function` kind is synthetic for this Ruby content -- prism
    # emits only method/singleton_method -- so this isolates the exclusion branch;
    # Ruby's exact-name pass is fully disabled by design, see _CLASS_BOUND_METHOD_KINDS.)
    func_cat = FunctionCatalog(
        [
            CatalogedFunction(
                name="expired",
                kind="function",
                file="app/helpers/time_helper.rb",
                arity=1,
                required=1,
                tokens=name_tokens("expired"),
                body_hash=None,
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: func_cat)
    out2 = hook_helper._prewrite_dedup_section(content, str(repo / "app/builds/querysets.rb"), repo)
    assert "reuse-before-create" in out2
    assert "expired" in out2


@pytest.mark.parametrize(
    "kind,new_content,new_path,name",
    [
        # The real production collision the exact pass sees: the NEW content defines
        # a FREE function/def (the only shape _extract_defined_functions yields --
        # never TS class-member shorthand), whose name matches an EXISTING catalog
        # member of a class-bound kind. That member is not importable, so no reuse
        # nudge fires. (Names are >=5 chars and non-stopword so extraction keeps them.)
        ("singleton_method", "def build_thing(x)\n  x\nend\n", "app/b.rb", "build_thing"),
        ("constructor", "function makeThing(x) { return x; }\n", "src/b.ts", "makeThing"),
        ("getter", "function totalPrice(x) { return x; }\n", "src/b.ts", "totalPrice"),
        ("setter", "function displayName(x) { return x; }\n", "src/b.ts", "displayName"),
    ],
)
def test_class_bound_kinds_not_reuse_targets(
    tmp_path, monkeypatch, kind, new_content, new_path, name
):
    from chameleon_mcp.function_catalog import CatalogedFunction, FunctionCatalog, name_tokens

    suffix = Path(new_path).suffix

    def _cat(k):
        return FunctionCatalog(
            [
                CatalogedFunction(
                    name=name,
                    kind=k,
                    file="app/other" + suffix,
                    arity=1,
                    required=1,
                    tokens=name_tokens(name),
                    body_hash=None,
                )
            ]
        )

    # Class-bound kind -> the collision is NOT offered as a reuse target.
    monkeypatch.setattr(
        "chameleon_mcp.function_catalog.load_function_catalog", lambda r: _cat(kind)
    )
    out = hook_helper._prewrite_dedup_section(new_content, str(tmp_path / new_path), tmp_path)
    assert "reuse-before-create" not in out

    # Non-vacuity control: the SAME name/content against a non-class-bound
    # `function` kind DOES surface the nudge, so the name is genuinely extracted
    # and the collision path fires -- it is the kind exclusion, not an empty
    # `defined` set, that suppressed the assertion above. (For the Ruby case the
    # `function` kind is synthetic -- prism_dump emits only method/singleton_method
    # -- so this control isolates the exclusion branch rather than modelling a real
    # Ruby catalog; see _CLASS_BOUND_METHOD_KINDS on Ruby's fully-disabled exact pass.)
    monkeypatch.setattr(
        "chameleon_mcp.function_catalog.load_function_catalog", lambda r: _cat("function")
    )
    out2 = hook_helper._prewrite_dedup_section(new_content, str(tmp_path / new_path), tmp_path)
    assert "reuse-before-create" in out2
