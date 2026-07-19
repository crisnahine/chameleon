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


def _body_hash_of(content, file_path):
    hs = hook_helper._extract_method_body_hashes(content, file_path)
    return hs[0] if hs else (None, None)


def test_body_identical_class_method_dup_fires(tmp_path, monkeypatch):
    # A method written verbatim, whose body exactly matches an existing CLASS-BOUND
    # method elsewhere, surfaces an extract-to-shared nudge (the OO-framework reuse
    # gap the exact/semantic passes miss). Uses the hot-path extractor to compute
    # the catalog body_hash, so index and query agree by construction.
    content = (
        "class BuildQuerySet\n  def concurrent(project)\n"
        "    self.filter(project=project)\n        .exclude(state='finished')\n"
        "        .count()\n  end\nend\n"
    )
    name, bh = _body_hash_of(content, "app/a.rb")
    assert bh  # body clears the hash floor
    cat = FunctionCatalog(
        [
            CatalogedFunction(
                name=name,
                kind="method",
                file="app/other/querysets.rb",
                arity=1,
                required=1,
                tokens=name_tokens(name),
                body_hash_lax=bh,
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    out = hook_helper._prewrite_dedup_section(content, str(tmp_path / "app/a.rb"), tmp_path)
    assert "reuse-before-create" in out
    assert "extract a shared" in out
    assert "app/other/querysets.rb" in out
    assert "import and reuse" not in out  # a method is not importable


def test_body_dup_short_name_still_fires(tmp_path, monkeypatch):
    # A short method name (`show`, <5 chars) is filtered out of the name-based
    # passes but must still reach the body-based pass.
    content = "class Ctrl\n  def show\n    respond_with(@record)\n    log_access(@record.id)\n  end\nend\n"
    name, bh = _body_hash_of(content, "app/a.rb")
    assert name == "show" and bh
    cat = FunctionCatalog(
        [
            CatalogedFunction(
                name="show",
                kind="method",
                file="app/other/ctrl.rb",
                arity=0,
                required=0,
                tokens=name_tokens("show"),
                body_hash_lax=bh,
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    out = hook_helper._prewrite_dedup_section(content, str(tmp_path / "app/a.rb"), tmp_path)
    assert "reuse-before-create" in out and "extract a shared" in out


def test_body_dup_novel_method_no_fire(tmp_path, monkeypatch):
    # A method with a unique body must NOT fire (collision-resistant hash -> no
    # false nudge).
    content = "class Foo\n  def unique_thing(a, b)\n    a * 7 + b * 13 - 42\n  end\nend\n"
    cat = FunctionCatalog(
        [
            CatalogedFunction(
                name="something_else",
                kind="method",
                file="app/x.rb",
                arity=1,
                required=1,
                tokens=name_tokens("something_else"),
                body_hash="deadbeefcafe0000",
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    out = hook_helper._prewrite_dedup_section(content, str(tmp_path / "app/foo.rb"), tmp_path)
    assert "reuse-before-create" not in out


def test_lax_body_hash_context_parity():
    # The acceptance gate: the SAME method body must produce the SAME lax hash
    # regardless of surrounding context (class body, after a sibling, top level) so
    # bootstrap (committed source) and the hot path (proposed content) agree by
    # construction. Ruby, Python, TS.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    rb_body = "  def compute(project)\n    self.filter(project: project).count\n    log_it(project)\n  end"
    h_a = dict(E("class A\n" + rb_body + "\nend\n", "a.rb")).get("compute")
    h_b = dict(E("class B\n  def other; end\n" + rb_body + "\nend\n", "a.rb")).get("compute")
    assert h_a and h_a == h_b

    py_body = "    def compute(self, project):\n        rows = self.filter(project=project)\n        return rows.count()"
    p_a = dict(E("class A:\n" + py_body + "\n", "a.py")).get("compute")
    p_b = dict(E("class B:\n    x = 1\n" + py_body + "\n    def z(self): pass\n", "a.py")).get(
        "compute"
    )
    assert p_a and p_a == p_b

    ts_body = (
        "  compute(x: number): number {\n    const y = x * 2;\n    return y + this.offset;\n  }"
    )
    t_a = dict(E("class A {\n" + ts_body + "\n}\n", "a.ts")).get("compute")
    t_b = dict(E("export class A {\n  other() {}\n" + ts_body + "\n}\n", "a.ts")).get("compute")
    assert t_a and t_a == t_b


def test_body_dup_matches_on_lax_field(tmp_path, monkeypatch):
    # Pass 3 keys the index on body_hash_lax (the reproducible field) when present.
    content = (
        "class BuildQuerySet\n  def concurrent(project)\n"
        "    self.filter(project: project).where.not(state: :done).count\n  end\nend\n"
    )
    name, bh = _body_hash_of(content, "app/a.rb")
    cat = FunctionCatalog(
        [
            CatalogedFunction(
                name=name,
                kind="method",
                file="app/other/qs.rb",
                arity=1,
                required=1,
                tokens=name_tokens(name),
                body_hash=None,
                body_hash_lax=bh,
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    out = hook_helper._prewrite_dedup_section(content, str(tmp_path / "app/a.rb"), tmp_path)
    assert "reuse-before-create" in out and "extract a shared" in out and "app/other/qs.rb" in out


def test_python_block_structure_not_collapsed(tmp_path, monkeypatch):
    # REGRESSION: two Python methods differing ONLY in a statement's block
    # membership (inside vs after an `if`) have DIFFERENT behavior and must NOT
    # produce the same lax fingerprint -- a full whitespace collapse would merge
    # them and draw a false "same body" nudge on conforming code.
    in_block = (
        "class Pricing:\n    def apply_discount(self):\n        total = self.subtotal\n"
        "        if self.member:\n            total = total * 0.9\n            self.log('m')\n"
        "        return total\n"
    )
    after_block = (
        "class Checkout:\n    def compute_total(self):\n        total = self.subtotal\n"
        "        if self.member:\n            total = total * 0.9\n        self.log('m')\n"
        "        return total\n"
    )
    _, bh_existing = _body_hash_of(in_block, "a.py")
    cat = FunctionCatalog(
        [
            CatalogedFunction(
                name="apply_discount",
                kind="method",
                file="billing/pricing.py",
                arity=0,
                required=0,
                tokens=name_tokens("apply_discount"),
                body_hash_lax=bh_existing,
            )
        ]
    )
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)
    out = hook_helper._prewrite_dedup_section(
        after_block, str(tmp_path / "billing/checkout.py"), tmp_path
    )
    assert "reuse-before-create" not in out  # different control flow -> no false match


def test_flush_left_string_span_not_truncated_into_false_match():
    # REGRESSION: a flush-left (column-0) line inside a triple-quoted string or a
    # heredoc must not truncate the body span, which would collide two methods that
    # share only their pre-string prefix. Such methods are skipped (no hash), so no
    # false "same body" nudge -- the safe direction.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    rev = (
        "class R:\n    def monthly_revenue(self):\n        scope = self.records.all()\n"
        '        sql = """\nSELECT sum(amount) FROM sales\n"""\n        return scope.raw(sql).total\n'
    )
    ref = (
        "class R:\n    def monthly_refunds(self):\n        scope = self.records.all()\n"
        '        sql = """\nSELECT sum(amount) FROM refunds\n"""\n        return scope.raw(sql).count\n'
    )
    a = dict(E(rev, "a.py")).get("monthly_revenue")
    b = dict(E(ref, "b.py")).get("monthly_refunds")
    assert not (a is not None and a == b)  # no false collision (both skipped)

    # A properly-INDENTED multi-line string body is NOT over-skipped -- it hashes.
    described = (
        'class D:\n    def describe(self):\n        text = """line one\n'
        '        line two\n        """\n        return text.strip()\n'
    )
    assert dict(E(described, "a.py")).get("describe") is not None


def test_ts_brace_in_string_not_truncated():
    # REGRESSION: a `}` inside a TS string/template/comment must not close the
    # method's brace span early, which would collide two methods sharing a prefix.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    a = (
        "class A {\n  buildQuery(): string {\n    const base = this.scope();\n"
        '    const tpl = "a}b";\n    return base + " WHERE revenue > 0";\n  }\n}\n'
    )
    b = (
        "class B {\n  buildFilter(): string {\n    const base = this.scope();\n"
        '    const tpl = "a}b";\n    return base + " WHERE refunded = true";\n  }\n}\n'
    )
    ha = dict(E(a, "a.ts")).get("buildQuery")
    hb = dict(E(b, "b.ts")).get("buildFilter")
    assert ha is not None and hb is not None
    assert ha != hb  # full bodies captured -> different WHERE clauses differ


def test_flush_left_comment_does_not_truncate_span():
    # REGRESSION: a flush-left comment INSIDE a method body must not close the
    # indent span early, which would collide two methods sharing a prefix.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    a = (
        "class A:\n    def foo(self):\n"
        "        x = self.compute_base_amount(self.order)\n"
        "# stray flush-left comment\n"
        "        y = self.apply_discount(x)\n        return y\n"
    )
    b = (
        "class B:\n    def bar(self):\n"
        "        x = self.compute_base_amount(self.order)\n"
        "# stray flush-left comment\n"
        "        y = self.apply_surcharge(x)\n        return y\n"
    )
    ha = dict(E(a, "a.py")).get("foo")
    hb = dict(E(b, "b.py")).get("bar")
    assert ha is not None and hb is not None
    assert ha != hb  # full bodies captured -> divergent tails differ

    r1 = (
        "class A\n  def foo\n    x = compute_base_amount(order)\n"
        "# stray comment\n    y = apply_discount(x)\n    y\n  end\nend\n"
    )
    r2 = (
        "class B\n  def bar\n    x = compute_base_amount(order)\n"
        "# stray comment\n    y = apply_surcharge(x)\n    y\n  end\nend\n"
    )
    assert dict(E(r1, "a.rb")).get("foo") != dict(E(r2, "b.rb")).get("bar")


def test_ts_regex_literal_brace_not_truncated():
    # REGRESSION: an unbalanced brace inside a regex literal (/}/, /[}]/) must not
    # close the method's brace span early. Two methods sharing a >40-char prefix
    # before such a regex would otherwise collide despite divergent tails.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    a = (
        "class A {\n  parseA(): void {\n"
        "    const base = this.resolveTenantScopeForRequest(this.ctx);\n"
        "    const re = /}/;\n    this.applyAlphaTransform(base);\n"
        "    return this.finalizeAlpha(base);\n  }\n}\n"
    )
    b = (
        "class B {\n  parseB(): void {\n"
        "    const base = this.resolveTenantScopeForRequest(this.ctx);\n"
        "    const re = /}/;\n    this.applyBetaTransform(base);\n"
        "    return this.finalizeBeta(base);\n  }\n}\n"
    )
    ha = dict(E(a, "a.ts")).get("parseA")
    hb = dict(E(b, "b.ts")).get("parseB")
    assert ha is not None and hb is not None
    assert ha != hb

    # division is not misread as a regex (full body still captured)
    d = (
        "class D {\n  calc(): number {\n    const ratio = this.total / this.count;\n"
        "    const pct = ratio * 100;\n    return Math.round(pct);\n  }\n}\n"
    )
    assert dict(E(d, "d.ts")).get("calc") is not None


def test_ruby_lowercase_heredoc_delimiter_truncation_skipped():
    # REGRESSION: a lowercase heredoc delimiter (<<-query, <<~sql) with flush-left
    # content must be recognized so the truncated span is skipped, not hashed into a
    # false collision between two methods sharing a pre-heredoc prefix.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    a = (
        "class R\n  def alpha_totals(scope)\n"
        "    base = scope.where(status: :active).includes(:line_items)\n"
        "    sql = <<-query\nSELECT SUM(amount) FROM sales\nquery\n"
        "    base.first\n  end\nend\n"
    )
    b = (
        "class R\n  def beta_totals(scope)\n"
        "    base = scope.where(status: :active).includes(:line_items)\n"
        "    sql = <<-query\nSELECT SUM(amount) FROM refunds\nquery\n"
        "    base.last\n  end\nend\n"
    )
    assert dict(E(a, "a.rb")).get("alpha_totals") is None
    assert dict(E(b, "b.rb")).get("beta_totals") is None

    # a spaced `<<` append is NOT a heredoc -> the method still hashes normally
    app = (
        "class A\n  def build_rows(items)\n"
        "    out = []\n    items.each { |i| out << i.to_h.merge(kind: :row) }\n"
        "    out.sort_by { |r| r[:created_at] }\n  end\nend\n"
    )
    assert dict(E(app, "app.rb")).get("build_rows") is not None


def test_backslash_continuation_truncation_skipped():
    # REGRESSION: a flush-left backslash line-continuation truncates the indent span;
    # a span ending on a dangling `\` must be skipped, not hashed into a false match.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    a = (
        "class C:\n    def alpha(self):\n"
        "        base = self.resolve_tenant_scope_for_request(self.ctx)\n"
        '        q = "SELECT * FROM sales WHERE x \\\n'
        'AND y = 1"\n        return self.finalize_alpha(base)\n'
    )
    b = (
        "class C:\n    def beta(self):\n"
        "        base = self.resolve_tenant_scope_for_request(self.ctx)\n"
        '        q = "SELECT * FROM refunds WHERE x \\\n'
        'AND y = 1"\n        return self.finalize_beta(base)\n'
    )
    # Both spans end on a dangling `\` -> skipped (None), so the divergent tails can
    # never be hashed into a false "same body" match.
    assert dict(E(a, "a.py")).get("alpha") is None
    assert dict(E(b, "b.py")).get("beta") is None

    # a normal method whose statements complete on their own line still hashes
    ok = (
        "class C:\n    def calc(self):\n"
        "        ratio = self.total_amount_collected / self.count_of_orders\n"
        "        pct = ratio * 100\n        return round(pct)\n"
    )
    assert dict(E(ok, "ok.py")).get("calc") is not None


def test_unbalanced_bracket_span_truncation_skipped():
    # REGRESSION: any multi-line construct whose closing bracket sits flush-left
    # (%w[], %q{}, an implicit paren continuation, a flush-left block `}`) truncates
    # the indent span. The general bracket-balance guard skips such a span instead of
    # hashing the shared prefix into a false collision. One guard for the whole family.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    # Ruby %w[] with flush-left content
    rw = (
        "class R\n  def alpha_cols(scope)\n"
        "    base = scope.where(active: true).includes(:line_items).order(:id)\n"
        "    cols = %w[\nname email phone\n]\n    base.pluck(*cols).first\n  end\nend\n"
    )
    assert dict(E(rw, "rw.rb")).get("alpha_cols") is None

    # Ruby %q{} with flush-left content
    rq = (
        "class R\n  def alpha_sql(scope)\n"
        "    base = scope.where(active: true).includes(:line_items).order(:id)\n"
        "    q = %q{\nSELECT * FROM sales\n}\n    base.first\n  end\nend\n"
    )
    assert dict(E(rq, "rq.rb")).get("alpha_sql") is None

    # Python implicit paren continuation with flush-left content
    py = (
        "class C:\n    def alpha(self):\n"
        "        base = self.resolve_tenant_scope_for_request(self.ctx)\n"
        "        vals = func(\nfirst_arg, second_arg,\n        )\n"
        "        return self.finalize_alpha(base)\n"
    )
    assert dict(E(py, "py.py")).get("alpha") is None

    # a normal method with BALANCED brackets (multi-line dict, call) still hashes
    ok = (
        "class C:\n    def calc(self):\n"
        "        ratio = self.compute_ratio(self.total_amount, self.count)\n"
        "        adjusted = ratio * self.factor_for(self.region)\n"
        "        return round(adjusted, 2)\n"
    )
    assert dict(E(ok, "ok.py")).get("calc") is not None


def test_ruby_flush_left_continuation_truncation_skipped():
    # REGRESSION: Ruby continues a statement across a bare newline (trailing binary
    # operator, leading-dot method chain) and through a `=begin`..`=end` block comment
    # forced to column 0. Each truncates the indent span with no bracket/heredoc/
    # backslash signal; the terminating line or a dangling trailing operator reveals it
    # so the span is skipped, not hashed into a false collision.
    from chameleon_mcp.function_catalog import extract_method_body_hashes as E

    # =begin block comment mid-method (deterministic column-0 truncation)
    beg = (
        "class R\n  def alpha_calc(scope)\n"
        "    total = scope.where(active: true).sum(:amount_cents)\n"
        "=begin\nnote\n=end\n    total / 100\n  end\nend\n"
    )
    assert dict(E(beg, "beg.rb")).get("alpha_calc") is None

    # trailing-operator continuation, flush-left
    top = (
        "class R\n  def alpha_sum(scope)\n"
        "    total = scope.first_amount_value_here +\nsecond_amount_value_here\n"
        "    total.round(2)\n  end\nend\n"
    )
    assert dict(E(top, "top.rb")).get("alpha_sum") is None

    # leading-dot method-chain continuation, flush-left
    dot = (
        "class R\n  def alpha_map(scope)\n"
        "    result = scope.where(active: true).order(:id)\n"
        ".map { |x| x.transform_alpha }\n    result.first\n  end\nend\n"
    )
    assert dict(E(dot, "dot.rb")).get("alpha_map") is None

    # a normal Ruby method with a balanced multi-line block still hashes
    ok = (
        "class R\n  def compute_total(scope)\n"
        "    rows = scope.where(active: true).includes(:items)\n"
        "    rows.map { |r| r.amount_cents }.sum\n  end\nend\n"
    )
    assert dict(E(ok, "ok.rb")).get("compute_total") is not None


def _patch_catalog(monkeypatch, cat):
    monkeypatch.setattr(hook_helper, "load_function_catalog", lambda r: cat, raising=False)
    monkeypatch.setattr("chameleon_mcp.function_catalog.load_function_catalog", lambda r: cat)


def test_editing_a_test_file_never_nudges(tmp_path, monkeypatch):
    # Every observed false positive was test-on-test: a new test function paired
    # with an existing one on the shared `test` prefix plus a common word. A test
    # is authored to exercise one specific thing and is never an importable reuse
    # target, so the name-based reuse nudge must not fire when editing a test.
    # A pair that genuinely triggers the semantic pass: 3 shared tokens, same
    # (zero) arity. Without the gate this fires "looks like the existing ...".
    cat = _catalog([("test_validate_email_shape", "tests/unit/test_validators.py", 0)])
    _patch_catalog(monkeypatch, cat)
    content = "def test_validate_email_format():\n    assert True\n"
    out = hook_helper._prewrite_dedup_section(
        content, str(tmp_path / "tests/unit/test_function_catalog.py"), tmp_path
    )
    assert out == ""


def test_test_functions_are_never_offered_as_candidates(tmp_path, monkeypatch):
    # Editing PRODUCTION code must not be told to reuse a TEST function: a test
    # is not importable-into-production reuse material.
    cat = _catalog([("test_validate_email_format", "tests/unit/test_validators.py")])
    _patch_catalog(monkeypatch, cat)
    content = "def validate_email_format(addr):\n    return '@' in addr\n"
    out = hook_helper._prewrite_dedup_section(
        content, str(tmp_path / "src/validators.py"), tmp_path
    )
    assert "tests/unit/test_validators.py" not in out


def test_production_reuse_still_fires_from_production_edit(tmp_path, monkeypatch):
    # The real signal is unaffected: a production helper re-implemented in
    # another production file still nudges.
    cat = _catalog([("clean_url_slug", "app/helpers/url_helper.rb")])
    _patch_catalog(monkeypatch, cat)
    content = "class Account\n  def clean_url_slug(u)\n    u.strip\n  end\nend\n"
    out = hook_helper._prewrite_dedup_section(
        content, str(tmp_path / "app/models/account.rb"), tmp_path
    )
    assert "reuse-before-create" in out
    assert "clean_url_slug" in out
