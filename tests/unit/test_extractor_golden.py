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
