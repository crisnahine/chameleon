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


def test_empty_and_malformed_inputs(tmp_path):
    assert build_constant_index([], tmp_path)["constants"] == {}
    assert build_constant_index(None, tmp_path)["constants"] == {}
    assert constants_defined_in(None, "x.rb") == []
    assert referencing_files(None, "Foo") == []
    # a record with garbage extras must not crash
    bad = SimpleNamespace(path=tmp_path / "z.rb", extras={"call_sites": "notalist"})
    assert build_constant_index([bad], tmp_path)["constants"] == {}
