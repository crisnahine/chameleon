"""Golden-corpus tests for the real AST extractors (ts_dump.mjs / prism_dump.rb).

These subprocess extractors are the actual parsing core the whole product
depends on, and had ZERO test coverage — a grammar/version drift or a malformed
ParsedFile would surface only in a live session. These pin the extracted
dimensions for known inputs. Skipped (not failed) when the node/ruby toolchain
is absent, so a CI runner without it stays green; a runner with setup-node +
setup-ruby exercises them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import TypeScriptExtractor

_NODE_MODULES = Path(__file__).resolve().parents[2] / "mcp" / "node_modules" / "typescript"
_HAVE_TS = shutil.which("node") is not None and _NODE_MODULES.is_dir()


def _have_prism() -> bool:
    if not shutil.which("ruby"):
        return False
    try:
        return (
            subprocess.run(
                ["ruby", "-e", "require 'prism'"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_typescript_extractor_golden(tmp_path):
    (tmp_path / "Foo.tsx").write_text(
        "export default class Foo {\n  render() { return <div/>; }\n}\n"
    )
    (tmp_path / "bar.ts").write_text("export const a = 1;\nexport function b() {}\n")
    pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.{ts,tsx}")
    assert pr.skipped == []
    by_name = {f.path.name: f for f in pr.files}
    assert set(by_name) == {"Foo.tsx", "bar.ts"}

    foo = by_name["Foo.tsx"]
    assert "ClassDeclaration" in foo.top_level_node_kinds
    assert foo.default_export_kind == "ClassDeclaration"
    assert foo.has_jsx is True

    bar = by_name["bar.ts"]
    assert "FunctionDeclaration" in bar.top_level_node_kinds
    assert bar.named_export_count == 2
    assert bar.has_jsx is False
    assert bar.default_export_kind is None


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_typescript_function_scopes(tmp_path):
    (tmp_path / "shape.ts").write_text(
        "export function flat(a, b, c) {\n"
        "  const t = [1, 2, 3];\n"
        "  return t;\n"
        "}\n"
        "export function deep(x) {\n"
        "  if (x > 0) {\n"
        "    for (let i = 0; i < x; i++) {\n"
        "      if (i % 2 === 0) {\n"
        "        return i;\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "  return x;\n"
        "}\n"
    )
    pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.ts")
    scopes = pr.files[0].extras["function_scopes"]
    by_span = {s["line_span"]: s for s in scopes}
    flat = next(s for s in scopes if s["param_count"] == 3)
    assert flat["branch_count"] == 0
    assert flat["max_depth"] == 0
    deep = next(s for s in scopes if s["param_count"] == 1)
    assert deep["branch_count"] >= 2
    assert deep["max_depth"] >= 2
    assert by_span  # span recorded for each function


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_typescript_callable_signatures(tmp_path):
    (tmp_path / "sig.ts").write_text(
        "export function renderData(serializer, status = 200) { return 1; }\n"
        "export const fetchUser = (id, opts) => id;\n"
        "export default class Widget {\n"
        "  constructor(props) { this.props = props; }\n"
        "  render({ title }, ...rest) { return title; }\n"
        "}\n"
    )
    pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.ts")
    sigs = {s["name"]: s for s in pr.files[0].extras["callable_signatures"]}
    assert sigs["renderData"]["params"][0]["name"] == "serializer"
    # A default value makes the slot droppable.
    assert sigs["renderData"]["params"][1]["optional"] is True
    # An arrow assigned to a const reads its name off the binding.
    assert sigs["fetchUser"]["kind"] == "function"
    assert [p["name"] for p in sigs["fetchUser"]["params"]] == ["id", "opts"]
    assert sigs["constructor"]["kind"] == "constructor"
    # A destructured param keeps the positional slot with a stable marker.
    assert sigs["render"]["params"][0]["kind"] == "destructured"
    assert sigs["render"]["params"][1]["kind"] == "rest"


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism not available")
def test_ruby_callable_signatures(tmp_path):
    (tmp_path / "sig.rb").write_text(
        "class FoosController < ApplicationController\n"
        "  def create(name, status: 200, **opts)\n"
        "    name\n"
        "  end\n"
        "\n"
        "  def self.build(id)\n"
        "    new(id)\n"
        "  end\n"
        "end\n"
    )
    pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
    sigs = {s["name"]: s for s in pr.files[0].extras["callable_signatures"]}
    create = sigs["create"]
    assert create["enclosing_class"] == "FoosController"
    assert create["base_class"] == "ApplicationController"
    kinds = {p["name"]: p for p in create["params"]}
    assert kinds["name"]["optional"] is False
    assert kinds["status"]["kind"] == "keyword"
    # A class method carries an explicit self receiver.
    assert sigs["build"]["kind"] == "singleton_method"


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism not available")
def test_ruby_extractor_golden(tmp_path):
    (tmp_path / "svc.rb").write_text(
        "class Svc < ApplicationRecord\n  validates :name\n  def call; end\nend\n"
    )
    pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
    assert pr.skipped == []
    files = {f.path.name: f for f in pr.files}
    assert "svc.rb" in files
    assert "ClassNode" in files["svc.rb"].top_level_node_kinds


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism not available")
def test_ruby_function_scopes(tmp_path):
    (tmp_path / "shape.rb").write_text(
        "class Svc\n"
        "  def flat(a, b)\n"
        "    [1, 2, 3]\n"
        "  end\n"
        "\n"
        "  def deep(x)\n"
        "    if x > 0\n"
        "      [1, 2].each do |i|\n"
        "        return i if i.even?\n"
        "      end\n"
        "    end\n"
        "    x\n"
        "  end\n"
        "end\n"
    )
    pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
    scopes = pr.files[0].extras["function_scopes"]
    flat = next(s for s in scopes if s["param_count"] == 2)
    assert flat["branch_count"] == 0
    assert flat["max_depth"] == 0
    deep = next(s for s in scopes if s["param_count"] == 1)
    assert deep["branch_count"] >= 2
    assert deep["max_depth"] >= 1


@pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
def test_ts_destructured_export_names_recorded(tmp_path):
    # `export const { a, b: c, ...rest } = f()` binds names through a binding
    # pattern whose node has no `.text`. Unwalked, those names vanish from the
    # export set while the file stays authoritative, so the phantom-symbol check
    # flags the (real) imports of them as hallucinated. Every bound name must be
    # recorded and the set must stay closed (it is fully enumerable).
    (tmp_path / "auth.ts").write_text(
        "export const { useUser, useLogout, AuthLoader } = configureAuth({});\n"
        "export const { a: renamed, ...rest } = obj;\n"
        "export const [first, , third] = makeTuple();\n"
        "export const PLAIN = 1;\n"
    )
    pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.{ts,tsx}")
    by_name = {f.path.name: f for f in pr.files}
    names = set(by_name["auth.ts"].extras.get("named_export_names", []))
    assert {
        "useUser",
        "useLogout",
        "AuthLoader",
        "renamed",
        "rest",
        "first",
        "third",
        "PLAIN",
    } <= names
    assert by_name["auth.ts"].extras.get("export_set_open") is not True
