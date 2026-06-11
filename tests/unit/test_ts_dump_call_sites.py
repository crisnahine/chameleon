"""Tests for call_sites, namespace_imports, and enclosing_class extraction in ts_dump.mjs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE_MODULES = Path(__file__).resolve().parents[2] / "mcp" / "node_modules" / "typescript"
_TS_DUMP = Path(__file__).resolve().parents[2] / "scripts" / "ts_dump.mjs"
_HAVE_TS = shutil.which("node") is not None and _NODE_MODULES.is_dir()

FIXTURE = """
import { getUser } from './api';
import * as svc from './svc';
class Repo {
  save() { return this.flush(); }
  flush() { return 1; }
}
export function main() {
  const u = getUser();
  svc.sync(u);
  svc.api.deep.sync2(u);
  const r = new Repo();
  r.save();
  helper();
}
function helper() {}
"""


def _dump_src(tmp_path: Path, src: str, name: str = "mod.ts") -> dict:
    f = tmp_path / name
    f.write_text(src, encoding="utf-8")
    out = subprocess.run(
        ["node", str(_TS_DUMP)],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _dump(tmp_path: Path) -> dict:
    return _dump_src(tmp_path, FIXTURE, "main.ts")


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_call_sites_extracted(tmp_path):
    rec = _dump(tmp_path)
    sites = {(s["name"], s["kind"], s["caller"]) for s in rec["call_sites"]}
    assert ("getUser", "bare", "main") in sites
    assert ("sync", "member", "main") in sites
    assert ("Repo", "new", "main") in sites
    assert ("save", "member", "main") in sites
    assert ("helper", "bare", "main") in sites
    assert ("flush", "this", "save") in sites
    member = next(s for s in rec["call_sites"] if s["name"] == "sync")
    assert member["receiver"] == "svc"
    assert isinstance(member["line"], int)


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_member_chain_deeper_than_one_hop_records_no_receiver(tmp_path):
    # svc.api.deep.sync2() dispatches through properties of svc, not svc
    # itself; collapsing the chain to its leftmost identifier made it dump
    # identically to svc.sync2(), which fabricated namespace-import edges.
    # The receiver carries the identifier only for a depth-1 chain.
    rec = _dump(tmp_path)
    deep = next(s for s in rec["call_sites"] if s["name"] == "sync2")
    assert deep["kind"] == "member"
    assert deep["receiver"] is None
    shallow = next(s for s in rec["call_sites"] if s["name"] == "sync")
    assert shallow["receiver"] == "svc"


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_enclosing_class_on_methods(tmp_path):
    rec = _dump(tmp_path)
    save = next(s for s in rec["callable_signatures"] if s["name"] == "save")
    assert save["enclosing_class"] == "Repo"
    main = next(s for s in rec["callable_signatures"] if s["name"] == "main")
    assert main.get("enclosing_class") is None


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_call_sites_cap(tmp_path):
    many = "export function f() {\n" + "g();\n" * 2500 + "}\nfunction g() {}\n"
    f = tmp_path / "many.ts"
    f.write_text(many, encoding="utf-8")
    out = subprocess.run(
        ["node", str(_TS_DUMP)],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(rec["call_sites"]) == 2000
    assert rec["call_sites_truncated"] is True
    assert rec["call_sites_total"] >= 2500


_FIXTURE_ALIASED_IMPORTS = """\
import { getUser } from './api';
import { getUser as fetchLegacy } from './legacy';
export function go() { getUser(); fetchLegacy(); }
"""


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_import_symbols_carry_local_binding(tmp_path):
    # `name` stays the source-exported name (the reverse index keys on it);
    # `local` is the binding the importer's call sites actually use.
    rec = _dump_src(tmp_path, _FIXTURE_ALIASED_IMPORTS, "alias.ts")
    rows = {(r["name"], r["local"], r["module"]) for r in rec["import_symbols"]}
    assert ("getUser", "getUser", "./api") in rows
    assert ("getUser", "fetchLegacy", "./legacy") in rows


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_namespace_imports(tmp_path):
    rec = _dump(tmp_path)
    # The fixture has `import * as svc from './svc'` on line 3.
    assert rec["namespace_imports"] == [{"alias": "svc", "module": "./svc", "line": 3}]


# type-only namespace imports must not appear in namespace_imports

_FIXTURE_TYPE_NS = """\
import * as svc from './svc';
import type * as tns from './types';
export function run() { svc.go(); }
"""


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_type_only_namespace_import_excluded(tmp_path):
    rec = _dump_src(tmp_path, _FIXTURE_TYPE_NS)
    aliases = {entry["alias"] for entry in rec["namespace_imports"]}
    assert "svc" in aliases, "runtime namespace import must be recorded"
    assert "tns" not in aliases, "type-only namespace import must not be recorded"


# anonymous class expressions and object literals must shadow the enclosing class

_FIXTURE_ANON_CLASS = """\
class Outer {
  m() {
    const Anon = class {
      inner() { return 1; }
    };
  }
  named() {
    class Inner {
      go() { return 2; }
    }
  }
  build() {
    const obj = {
      onClick() { return 3; }
    };
  }
}
"""


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_anonymous_class_expression_method_enclosing_null(tmp_path):
    rec = _dump_src(tmp_path, _FIXTURE_ANON_CLASS, "anon.ts")
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    # Method of an anonymous class expression must not inherit "Outer".
    assert "inner" in sigs, "inner method must be recorded"
    assert sigs["inner"]["enclosing_class"] is None


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_object_literal_method_enclosing_null(tmp_path):
    rec = _dump_src(tmp_path, _FIXTURE_ANON_CLASS, "anon.ts")
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    # Method of an object literal inside a class method must not inherit "Outer".
    assert "onClick" in sigs, "onClick method must be recorded"
    assert sigs["onClick"]["enclosing_class"] is None


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_named_inner_class_enclosing_preserved(tmp_path):
    rec = _dump_src(tmp_path, _FIXTURE_ANON_CLASS, "anon.ts")
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    # Method of a named inner class must record that class, not the outer.
    assert "go" in sigs, "go method must be recorded"
    assert sigs["go"]["enclosing_class"] == "Inner"
