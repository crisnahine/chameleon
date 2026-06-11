"""Tests for call_sites, namespace_imports, and enclosing_class extraction in ts_dump.mjs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE_MODULES = Path(__file__).resolve().parents[2] / "mcp" / "node_modules" / "typescript"
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
  const r = new Repo();
  r.save();
  helper();
}
function helper() {}
"""


def _dump(tmp_path: Path) -> dict:
    f = tmp_path / "main.ts"
    f.write_text(FIXTURE, encoding="utf-8")
    out = subprocess.run(
        ["node", "scripts/ts_dump.mjs"],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


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
        ["node", "scripts/ts_dump.mjs"],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(rec["call_sites"]) == 2000
    assert rec["call_sites_truncated"] is True
    assert rec["call_sites_total"] >= 2500


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_namespace_imports(tmp_path):
    rec = _dump(tmp_path)
    # The fixture has `import * as svc from './svc'` on line 3.
    assert rec["namespace_imports"] == [{"alias": "svc", "module": "./svc", "line": 3}]
