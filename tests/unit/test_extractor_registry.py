"""A2 seam: the extractor registry.

A new language should be a registry entry plus a signature mapping, not an edit
to bootstrap's hardcoded selection loop. This is a pure structural seam: the
selection order and behavior are identical to the previous hardcoded
``(TypeScript, Ruby)`` loop, so no profile re-clusters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.extractors import registry


@pytest.fixture(autouse=True)
def _restore_registry():
    saved = list(registry.EXTRACTORS)
    yield
    registry.EXTRACTORS[:] = saved


def test_default_registry_order_is_typescript_then_ruby():
    langs = [e().language for e in registry.EXTRACTORS]
    assert langs[:2] == ["typescript", "ruby"]


def test_select_typescript_repo(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "typescript"


def test_select_ruby_repo(tmp_path):
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "ruby"


def test_select_python_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "python"


def test_typescript_precedes_python_for_hybrid(tmp_path):
    # A repo with both a tsconfig and .py files routes to TS (registry order),
    # so Python's liberal detection never steals a TS repo.
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "script.py").write_text("x = 1\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "typescript"


def test_select_none_for_unknown_repo(tmp_path):
    assert registry.select_extractor(tmp_path) is None


def test_register_appends_and_dedupes():
    class FakeExtractor:
        language = "fake"

        def can_handle(self, repo_root: Path) -> bool:
            return (repo_root / "FAKE_MARKER").exists()

        def parse_repo(self, repo_root, glob="**/*", limit=None):
            raise NotImplementedError

    before = len(registry.EXTRACTORS)
    registry.register(FakeExtractor)
    registry.register(FakeExtractor)  # idempotent
    assert len(registry.EXTRACTORS) == before + 1
    assert FakeExtractor in registry.EXTRACTORS


def test_registered_extractor_is_selectable(tmp_path):
    class FakeExtractor:
        language = "fake"

        def can_handle(self, repo_root: Path) -> bool:
            return (repo_root / "FAKE_MARKER").exists()

        def parse_repo(self, repo_root, glob="**/*", limit=None):
            raise NotImplementedError

    registry.register(FakeExtractor)
    (tmp_path / "FAKE_MARKER").write_text("", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "fake"


def test_orchestrator_select_extractor_unchanged_for_typescript(tmp_path):
    # The behavior-preservation guard: bootstrap's selector still returns TS.
    from chameleon_mcp.bootstrap.orchestrator import _select_extractor

    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    ext = _select_extractor(tmp_path)
    assert ext is not None and ext.language == "typescript"


def test_select_plain_javascript_repo(tmp_path):
    # A pure-JS ESM service (root package.json + .js sources, zero TypeScript
    # anywhere) is first-class: it must select the TypeScript-family extractor
    # instead of failing as unsupported.
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text("export function boot() {}\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "typescript"


def test_js_sources_do_not_claim_a_django_repo(tmp_path):
    # A backend repo's asset bundle (root package.json + static .js) must keep
    # its backend language: manage.py marks Django, so Python wins.
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.js").write_text("function f() {}\n", encoding="utf-8")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "views.py").write_text("def index(request): ...\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "python"


def test_js_sources_do_not_claim_a_gem_repo(tmp_path):
    # Same guard for Ruby: a gemspec keeps the repo Ruby even with a root
    # package.json and docs-site JS lying around.
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "widget.gemspec").write_text("Gem::Specification.new\n", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "widget.rb").write_text("module Widget; end\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "site.js").write_text("function f() {}\n", encoding="utf-8")
    ext = registry.select_extractor(tmp_path)
    assert ext is not None and ext.language == "ruby"
