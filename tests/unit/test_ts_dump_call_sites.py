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
def test_member_chain_deeper_than_one_hop_is_dropped(tmp_path):
    # svc.api.deep.sync2() is statically unresolvable: the callee is a property
    # of a property, so no receiver identifier names the direct namespace.
    # Emitting receiver=null made it byte-identical to a true receiver-less site
    # and let the builder fabricate import edges. The fix: drop such sites at
    # extraction, the same stance as computed access.
    rec = _dump(tmp_path)
    names = {s["name"] for s in rec["call_sites"]}
    assert "sync2" not in names, "multi-hop member chain must be dropped, not emitted"
    # Depth-1 member call is still recorded with its receiver.
    shallow = next(s for s in rec["call_sites"] if s["name"] == "sync")
    assert shallow["receiver"] == "svc"


_FIXTURE_MULTI_HOP = """\
import * as api from './m';
import { Klass } from './k';
export function go() {
  api.utils.helper();
  new api.utils.Klass();
  api.helper();
  new api.Klass();
}
"""


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_multi_hop_call_and_new_are_dropped(tmp_path):
    # api.utils.helper() and new api.utils.Klass() both require chaining through
    # at least two receiver hops before reaching the callee; neither can resolve
    # deterministically against the import set.
    rec = _dump_src(tmp_path, _FIXTURE_MULTI_HOP, "multi.ts")
    names = {s["name"] for s in rec["call_sites"]}
    assert "helper" not in names or all(
        s["receiver"] == "api" for s in rec["call_sites"] if s["name"] == "helper"
    ), "multi-hop api.utils.helper() must be dropped; depth-1 api.helper() may remain"
    # The multi-hop new must not appear at all; the depth-1 new should appear
    # with receiver 'api'.
    klass_sites = [s for s in rec["call_sites"] if s["name"] == "Klass"]
    for site in klass_sites:
        assert site["receiver"] == "api", (
            f"only depth-1 new api.Klass() should appear; got receiver={site['receiver']!r}"
        )


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_multi_hop_call_produces_no_row(tmp_path):
    # Precise: api.utils.helper() emits NO row for 'helper'; only the depth-1
    # api.helper() row (receiver='api') survives.
    rec = _dump_src(tmp_path, _FIXTURE_MULTI_HOP, "multi.ts")
    helper_sites = [s for s in rec["call_sites"] if s["name"] == "helper"]
    assert len(helper_sites) == 1, (
        f"expected exactly 1 helper row (depth-1 api.helper()); got {helper_sites}"
    )
    assert helper_sites[0]["receiver"] == "api"


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_multi_hop_new_produces_no_row(tmp_path):
    # Precise: new api.utils.Klass() emits NO row for 'Klass'; only the
    # depth-1 new api.Klass() row (receiver='api') survives.
    rec = _dump_src(tmp_path, _FIXTURE_MULTI_HOP, "multi.ts")
    klass_sites = [s for s in rec["call_sites"] if s["name"] == "Klass"]
    assert len(klass_sites) == 1, (
        f"expected exactly 1 Klass row (depth-1 new api.Klass()); got {klass_sites}"
    )
    assert klass_sites[0]["receiver"] == "api"
    assert klass_sites[0]["kind"] == "new"


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


_FIXTURE_AMBIENT_MODULE = """\
declare module "express" {
  export class Foo {
    m(): void {}
  }
}
export namespace RealNs {
  export class Bar {
    n(): void {}
  }
}
"""


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_string_literal_module_name_not_in_enclosing_path(tmp_path):
    # A string-literal ambient module name (`declare module "express"`) carries no
    # useful path segment, so a method inside it keeps the bare class path. A real
    # Identifier namespace still contributes its segment.
    rec = _dump_src(tmp_path, _FIXTURE_AMBIENT_MODULE, "ambient.ts")
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    assert sigs["m"]["enclosing_class_path"] == "Foo"
    assert sigs["n"]["enclosing_class_path"] == "RealNs.Bar"
