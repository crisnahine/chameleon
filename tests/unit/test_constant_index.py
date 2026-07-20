"""Ruby constant-reference reverse index: build, load, and query helpers."""

import json
from types import SimpleNamespace

from chameleon_mcp.constant_index import (
    SCHEMA_VERSION,
    build_constant_index,
    constants_defined_in,
    load_constant_index,
    referencing_files,
)


def _pf(path, *, classes=None, const_calls=None):
    """A parse record: classes -> callable_signatures rows (enclosing_class_path);
    const_calls -> call_sites rows with kind=constant (receiver name)."""
    extras = {}
    if classes:
        extras["callable_signatures"] = [
            {"name": f"m{i}", "enclosing_class_path": c} for i, c in enumerate(classes)
        ]
    if const_calls:
        extras["call_sites"] = [
            {"name": "call", "receiver": r, "kind": "constant"} for r in const_calls
        ]
    return SimpleNamespace(path=path, extras=extras)


def test_defined_in_and_referenced_by(tmp_path):
    files = [
        _pf(tmp_path / "app/services/foo.rb", classes=["Foo"]),
        _pf(tmp_path / "app/controllers/x_controller.rb", const_calls=["Foo"]),
        _pf(tmp_path / "app/controllers/y_controller.rb", const_calls=["Foo"]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    foo = idx["constants"]["Foo"]
    assert foo["defined_in"] == ["app/services/foo.rb"]
    assert foo["referenced_by"] == [
        "app/controllers/x_controller.rb",
        "app/controllers/y_controller.rb",
    ]
    # query helpers
    assert constants_defined_in(idx, "app/services/foo.rb") == ["Foo"]
    assert referencing_files(idx, "Foo") == [
        "app/controllers/x_controller.rb",
        "app/controllers/y_controller.rb",
    ]


def test_namespaced_constant_exact_match_only(tmp_path):
    # A bare `Foo` receiver does NOT join to the namespaced `App::Foo` definition
    # (call sites carry no lexical nesting) -- the accepted undercoverage.
    files = [
        _pf(tmp_path / "app/services/app/foo.rb", classes=["App::Foo"]),
        _pf(tmp_path / "a.rb", const_calls=["Foo"]),
        _pf(tmp_path / "b.rb", const_calls=["App::Foo"]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["App::Foo"]["referenced_by"] == ["b.rb"]
    # bare Foo is its own (undefined) constant, referenced by a.rb
    assert idx["constants"]["Foo"]["defined_in"] == []
    assert idx["constants"]["Foo"]["referenced_by"] == ["a.rb"]
    assert referencing_files(idx, "App::Foo") == ["b.rb"]


def test_ambiguous_definition_kept(tmp_path):
    # A constant defined in two files lists both in defined_in (the consumer
    # decides what to do with ambiguity, mirroring class_defs).
    files = [
        _pf(tmp_path / "a.rb", classes=["Dup"]),
        _pf(tmp_path / "b.rb", classes=["Dup"]),
        _pf(tmp_path / "c.rb", const_calls=["Dup"]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["Dup"]["defined_in"] == ["a.rb", "b.rb"]
    assert constants_defined_in(idx, "a.rb") == ["Dup"]
    assert constants_defined_in(idx, "b.rb") == ["Dup"]


def test_non_ruby_is_empty(tmp_path):
    files = [_pf(tmp_path / "a.ts", classes=["Foo"], const_calls=["Foo"])]
    idx = build_constant_index(files, tmp_path, language="typescript")
    assert idx["constants"] == {}


def test_load_roundtrip_and_schema(tmp_path):
    files = [
        _pf(tmp_path / "foo.rb", classes=["Foo"]),
        _pf(tmp_path / "bar.rb", const_calls=["Foo"]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    ch = tmp_path / ".chameleon"
    ch.mkdir()
    (ch / "constant_index.json").write_text(json.dumps(idx))
    loaded = load_constant_index(tmp_path)
    assert loaded is not None
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert referencing_files(loaded, "Foo") == ["bar.rb"]


def test_load_missing_and_future_schema(tmp_path):
    assert load_constant_index(tmp_path) is None  # no .chameleon
    assert load_constant_index(None) is None
    ch = tmp_path / ".chameleon"
    ch.mkdir()
    (ch / "constant_index.json").write_text(json.dumps({"schema_version": 999, "constants": {}}))
    assert load_constant_index(tmp_path) is None  # future schema -> None
    (ch / "constant_index.json").write_text("{ not json")
    assert load_constant_index(tmp_path) is None  # corrupt -> None


def test_ruby_constant_importers_helper(tmp_path):
    # The query_symbol_importers Ruby branch: constants defined in the edited file
    # mapped to their referencing files (blast radius); broken is always empty.
    from chameleon_mcp.tools import _ruby_constant_importers

    files = [
        _pf(tmp_path / "app/services/foo.rb", classes=["Foo"]),
        _pf(tmp_path / "a.rb", const_calls=["Foo"]),
        _pf(tmp_path / "b.rb", const_calls=["Foo"]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    ch = tmp_path / ".chameleon"
    ch.mkdir()
    (ch / "constant_index.json").write_text(json.dumps(idx))

    out = _ruby_constant_importers(tmp_path, tmp_path / "app/services/foo.rb")
    assert out["found"] is True
    assert out["module"] == "app/services/foo.rb"
    assert out["broken"] == []
    assert len(out["importers"]) == 1
    imp = out["importers"][0]
    assert imp["name"] == "Foo"
    assert imp["count"] == 2
    assert sorted(s["path"] for s in imp["sites"]) == ["a.rb", "b.rb"]

    # a file that defines nothing referenced -> found True, no importers (eligible)
    out2 = _ruby_constant_importers(tmp_path, tmp_path / "a.rb")
    assert out2["found"] is True
    assert out2["importers"] == []

    # no constant index on disk -> found False with a reason (routes to review)
    import shutil

    shutil.rmtree(ch)
    out3 = _ruby_constant_importers(tmp_path, tmp_path / "app/services/foo.rb")
    assert out3["found"] is False
    assert "refresh" in out3.get("reason", "")


def test_ruby_constant_existence_break_helper(tmp_path):
    # get_crossfile_context Ruby: a constant the index says is defined here but
    # the file no longer defines, with a referencer that still names it.
    from chameleon_mcp.tools import _ruby_constant_existence_breaks

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "foo.rb").write_text("class Renamed\nend\n")  # used to be `class Foo`
    (tmp_path / "app" / "caller.rb").write_text("Foo.bar\n")  # still references Foo
    ch = tmp_path / ".chameleon"
    ch.mkdir()
    (ch / "constant_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "constants": {
                    "Foo": {"defined_in": ["app/foo.rb"], "referenced_by": ["app/caller.rb"]}
                },
            }
        )
    )
    out = _ruby_constant_existence_breaks(tmp_path)
    assert out["found"] is True
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["symbol"] == "Foo"
    assert f["module"] == "app/foo.rb"
    assert f["high_confidence"] is True
    assert [s["path"] for s in f["sites"]] == ["app/caller.rb"]

    # Still defined on disk -> no break.
    (tmp_path / "app" / "foo.rb").write_text("class Foo\nend\n")
    assert _ruby_constant_existence_breaks(tmp_path)["findings"] == []

    # Removed but no live referencer -> no break.
    (tmp_path / "app" / "foo.rb").write_text("class Renamed\nend\n")
    (tmp_path / "app" / "caller.rb").write_text("Bar.bar\n")
    assert _ruby_constant_existence_breaks(tmp_path)["findings"] == []

    # Ambiguous (two defining files) -> skipped even when removed.
    (ch / "constant_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "constants": {
                    "Foo": {"defined_in": ["app/foo.rb", "app/foo2.rb"], "referenced_by": ["x.rb"]}
                },
            }
        )
    )
    assert _ruby_constant_existence_breaks(tmp_path)["findings"] == []

    # No index -> _index_missing flag (caller falls back to the reason).
    import shutil

    shutil.rmtree(ch)
    assert _ruby_constant_existence_breaks(tmp_path).get("_index_missing") is True


def test_empty_and_malformed_inputs(tmp_path):
    assert build_constant_index([], tmp_path)["constants"] == {}
    assert build_constant_index(None, tmp_path)["constants"] == {}
    assert constants_defined_in(None, "x.rb") == []
    assert referencing_files(None, "Foo") == []
    # a record with garbage extras must not crash
    bad = SimpleNamespace(path=tmp_path / "z.rb", extras={"call_sites": "notalist"})
    assert build_constant_index([bad], tmp_path)["constants"] == {}


def _pf_nested(path, *, classes=None, refs=None):
    """A parse record whose reference sites may carry lexical nesting:
    refs -> (receiver, nesting) pairs, nesting None for a top-level site."""
    extras = {}
    if classes:
        extras["callable_signatures"] = [
            {"name": f"m{i}", "enclosing_class_path": c} for i, c in enumerate(classes)
        ]
    if refs:
        extras["call_sites"] = [
            {"name": "call", "receiver": r, "kind": "constant"}
            | ({"nesting": n} if n is not None else {})
            for r, n in refs
        ]
    return SimpleNamespace(path=path, extras=extras)


def test_gem_nesting_reference_unifies_onto_qualified_entry(tmp_path):
    # A bare `DuplicateScanner` written inside Ledgermatch::Commands joins the
    # Ledgermatch::DuplicateScanner entry instead of dangling as a disjoint
    # bare-name entry with no defined_in.
    files = [
        _pf_nested(
            tmp_path / "lib/ledgermatch/duplicate_scanner.rb",
            classes=["Ledgermatch::DuplicateScanner"],
        ),
        _pf_nested(
            tmp_path / "lib/ledgermatch/commands/reconcile.rb",
            refs=[("DuplicateScanner", ["Ledgermatch", "Commands"])],
        ),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    entry = idx["constants"]["Ledgermatch::DuplicateScanner"]
    assert entry["defined_in"] == ["lib/ledgermatch/duplicate_scanner.rb"]
    assert entry["referenced_by"] == ["lib/ledgermatch/commands/reconcile.rb"]
    assert "DuplicateScanner" not in idx["constants"]
    assert referencing_files(idx, "Ledgermatch::DuplicateScanner") == [
        "lib/ledgermatch/commands/reconcile.rb"
    ]


def test_ambiguous_nesting_levels_reference_recorded_nowhere(tmp_path):
    # Bare `Scanner` inside Ledgermatch::Commands matches two different files
    # at two nesting levels: recording any join would pick a maybe-wrong
    # winner, so the reference is dropped entirely.
    files = [
        _pf_nested(
            tmp_path / "lib/ledgermatch/commands/scanner.rb",
            classes=["Ledgermatch::Commands::Scanner"],
        ),
        _pf_nested(tmp_path / "lib/ledgermatch/scanner.rb", classes=["Ledgermatch::Scanner"]),
        _pf_nested(
            tmp_path / "lib/ledgermatch/commands/audit.rb",
            refs=[("Scanner", ["Ledgermatch", "Commands"])],
        ),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert "Scanner" not in idx["constants"]
    assert idx["constants"]["Ledgermatch::Commands::Scanner"]["referenced_by"] == []
    assert idx["constants"]["Ledgermatch::Scanner"]["referenced_by"] == []


def test_flat_top_level_reference_with_nesting_still_unifies(tmp_path):
    # A top-level class referenced from inside a namespace: only the outermost
    # candidate matches, so the flat entry keeps its blast radius.
    files = [
        _pf_nested(tmp_path / "app/models/billing.rb", classes=["Billing"]),
        _pf_nested(tmp_path / "app/services/admin/pay.rb", refs=[("Billing", ["Admin"])]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["Billing"]["referenced_by"] == ["app/services/admin/pay.rb"]


def test_root_anchored_reference_resolves_to_top_level_entry(tmp_path):
    # `::Audit` inside `module Ledgermatch` is absolute: it joins the
    # top-level Audit entry, never the nested Ledgermatch::Audit the lexical
    # walk would otherwise prefer.
    files = [
        _pf_nested(tmp_path / "lib/audit.rb", classes=["Audit"]),
        _pf_nested(tmp_path / "lib/ledgermatch/audit.rb", classes=["Ledgermatch::Audit"]),
        _pf_nested(tmp_path / "lib/ledgermatch/cli.rb", refs=[("::Audit", ["Ledgermatch"])]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["Audit"]["referenced_by"] == ["lib/ledgermatch/cli.rb"]
    assert idx["constants"]["Ledgermatch::Audit"]["referenced_by"] == []


def test_reopened_class_reference_keeps_multi_file_entry(tmp_path):
    # A constant reopened across two files still lists its referencers under
    # the one matching key; the consumer sees the multi-file defined_in and
    # treats that ambiguity itself.
    files = [
        _pf_nested(tmp_path / "a.rb", classes=["Dup"]),
        _pf_nested(tmp_path / "b.rb", classes=["Dup"]),
        _pf_nested(tmp_path / "c.rb", refs=[("Dup", None)]),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["Dup"]["defined_in"] == ["a.rb", "b.rb"]
    assert idx["constants"]["Dup"]["referenced_by"] == ["c.rb"]


def test_unresolved_framework_reference_keeps_literal_key(tmp_path):
    # No candidate is defined in the repo: the literal receiver keeps its
    # entry (empty defined_in, harmless) so the reference is still visible.
    files = [
        _pf_nested(
            tmp_path / "lib/ledgermatch/report.rb",
            classes=["Ledgermatch::Report"],
            refs=[("ActiveRecord::Base", ["Ledgermatch"])],
        ),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    assert idx["constants"]["ActiveRecord::Base"]["defined_in"] == []
    assert idx["constants"]["ActiveRecord::Base"]["referenced_by"] == ["lib/ledgermatch/report.rb"]


def test_gem_nesting_blast_radius_reaches_query_helper(tmp_path):
    # The query_symbol_importers Ruby branch sees the unified entry: the gem
    # file's qualified constant now lists its bare-reference caller.
    from chameleon_mcp.tools import _ruby_constant_importers

    files = [
        _pf_nested(
            tmp_path / "lib/ledgermatch/duplicate_scanner.rb",
            classes=["Ledgermatch::DuplicateScanner"],
        ),
        _pf_nested(
            tmp_path / "lib/ledgermatch/commands/reconcile.rb",
            refs=[("DuplicateScanner", ["Ledgermatch", "Commands"])],
        ),
    ]
    idx = build_constant_index(files, tmp_path, language="ruby")
    ch = tmp_path / ".chameleon"
    ch.mkdir()
    (ch / "constant_index.json").write_text(json.dumps(idx))

    out = _ruby_constant_importers(tmp_path, tmp_path / "lib/ledgermatch/duplicate_scanner.rb")
    assert out["found"] is True
    assert [i["name"] for i in out["importers"]] == ["Ledgermatch::DuplicateScanner"]
    assert [s["path"] for s in out["importers"][0]["sites"]] == [
        "lib/ledgermatch/commands/reconcile.rb"
    ]
