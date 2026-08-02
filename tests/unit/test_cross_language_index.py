"""A mixed repo is one codebase, so the index covers every language in it.

Chameleon binds ONE extractor, and therefore one language, per profile:
`select_extractor` returns the first match and `parse_repo` filters to
`self._languages`. A real Go service living beside a TypeScript app was
therefore not merely ungraded, it was never parsed -- its symbols were absent
from `search_codebase`, and `describe_codebase` described a repo that did not
exist.

The primary language is still singular and still decides everything that
SHAPES output: archetypes, conventions, canonicals, the framework-aware layers,
every lint gate. Secondary languages feed only the three index builders that
carry no language gate. A second language enriches what the repo can ANSWER
without restyling what it enforces.

Each secondary language clears the same bar a primary does -- a build manifest
AND real source -- so a single vendored `.go` file cannot claim the repo is Go.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap.orchestrator import _secondary_language_files
from chameleon_mcp.extractors.spec_driven import SpecDrivenExtractor
from chameleon_mcp.extractors.treesitter.extractor import TreeSitterExtractor


@pytest.fixture
def mixed_repo(tmp_path: Path) -> Path:
    """A TypeScript app that also ships a real Go service."""
    (tmp_path / "src").mkdir()
    (tmp_path / "svc" / "handler").mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"mixed"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{}}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("export function boot(): void {}\n", encoding="utf-8")
    (tmp_path / "svc" / "handler" / "orders.go").write_text(
        'package handler\n\nfunc ServeOrders() string { return "ok" }\n', encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def dense_mixed_repo(tmp_path: Path) -> Path:
    """A polyglot repo with enough files per language to clear the sparse floor.

    `mixed_repo` is deliberately minimal and other tests assert its exact
    contents, but clustering drops clusters below an adaptive minimum, so one
    file per language yields no archetypes at all. Six each is what the
    derivation actually needs.
    """
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "svc" / "handler").mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"mixed"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{}}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n", encoding="utf-8")
    for i in range(6):
        (tmp_path / "web" / "src" / f"m{i}.ts").write_text(
            f"export function boot{i}(): void {{}}\n", encoding="utf-8"
        )
        (tmp_path / "svc" / "handler" / f"h{i}.go").write_text(
            f'package handler\n\nfunc Serve{i}() string {{ return "ok" }}\n', encoding="utf-8"
        )
    return tmp_path


def test_a_secondary_language_is_detected_only_with_its_build_manifest(tmp_path: Path):
    """The manifest is what makes the claim about the REPO, not about one file."""
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "stray.go").write_text("package v\n\nfunc F() {}\n", encoding="utf-8")
    assert SpecDrivenExtractor.languages_present(tmp_path) == ()

    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")
    assert SpecDrivenExtractor.languages_present(tmp_path) == ("go",)


def test_the_primary_language_is_excluded_from_its_own_secondary_set(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    assert SpecDrivenExtractor.languages_present(tmp_path, exclude="go") == ()


def test_the_extractor_carries_extra_languages_without_changing_its_own(mixed_repo: Path):
    """`.language` stays singular: every downstream branch reads it."""
    ext = TreeSitterExtractor("typescript", extra_languages=("go",))
    assert ext.language == "typescript"
    assert ext._languages == ("typescript", "go")


def test_unknown_and_duplicate_extra_languages_are_dropped_not_raised():
    """A secondary language is an enrichment; it may never fail a profile."""
    ext = TreeSitterExtractor("typescript", extra_languages=("go", "go", "typescript", "klingon"))
    assert ext._languages == ("typescript", "go")


def test_secondary_files_are_the_other_languages_only(mixed_repo: Path):
    """The primary's files are already in `parse_result`; re-parsing them here
    would double every row in the index."""
    files = _secondary_language_files(mixed_repo, "typescript")
    rels = sorted(Path(f.path).name for f in files)
    assert rels == ["orders.go"], rels


def test_a_single_language_repo_gains_nothing_and_pays_nothing(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name":"solo"}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    assert _secondary_language_files(tmp_path, "typescript") == []


def test_the_kill_switch_disables_the_whole_path(mixed_repo: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_CROSS_LANGUAGE_INDEX", "0")
    assert _secondary_language_files(mixed_repo, "typescript") == []


def test_the_index_covers_both_languages_end_to_end(mixed_repo: Path, monkeypatch, tmp_path: Path):
    """The claim that matters: after a real bootstrap, Go symbols are indexed."""
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac"))
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(argv, cwd=mixed_repo, check=True, capture_output=True)

    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo

    report = bootstrap_repo(mixed_repo)
    assert report.status == "success", report.status

    sig = json.loads((mixed_repo / ".chameleon" / "symbol_signatures.json").read_text())
    indexed = sig.get("files", {})
    assert "src/app.ts" in indexed, sorted(indexed)
    assert "svc/handler/orders.go" in indexed, (
        f"the Go service was never indexed; the repo answers as if it did not exist: {sorted(indexed)}"
    )
    assert "ServeOrders" in indexed["svc/handler/orders.go"]


def test_python_and_ruby_are_visible_as_secondary_languages(tmp_path: Path):
    """The detector is driven by the GRAMMAR table, not the spec table.

    Keying it on `EXTENSIONS_BY_LANGUAGE` covered only the five extraction-tier
    languages, so a polyglot repo's Python service was invisible AS A SECONDARY:
    never parsed, never indexed, absent from `search_codebase`. Python and Ruby
    are first-class as primaries and so never consult this path, which is
    exactly why the gap survived -- it only shows when they are not the winner.
    """
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="svc"\n', encoding="utf-8")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    found = SpecDrivenExtractor.languages_present(tmp_path, exclude="typescript")
    assert "python" in found, f"python invisible as a secondary language: {found}"


def test_a_secondary_language_still_needs_its_manifest(tmp_path: Path):
    """The manifest bar applies to the first-class languages too: loose `.py`
    scripts in a TS repo are not a Python service."""
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "oneoff.py").write_text("print(1)\n", encoding="utf-8")

    assert SpecDrivenExtractor.languages_present(tmp_path, exclude="typescript") == ()


def test_every_marked_language_has_a_grammar(tmp_path: Path):
    """A marker for a language tree-sitter cannot parse would detect a secondary
    the parser then silently drops."""
    from chameleon_mcp.extractors.spec_driven import _MARKERS
    from chameleon_mcp.extractors.treesitter.extractor import _TABLES

    assert set(_MARKERS) <= set(_TABLES), sorted(set(_MARKERS) - set(_TABLES))


def test_every_language_gets_its_own_archetype(dense_mixed_repo: Path, monkeypatch, tmp_path: Path):
    """The headline: a polyglot repo derives archetypes per language.

    Before, clustering saw the primary parse alone, so a TS+Go repo produced one
    TypeScript archetype and the Go service resolved to `archetype: None` with
    `match_quality: "none"` -- indexed, but no per-edit guidance at all.

    Safe to feed clustering every language because it already separates them
    structurally: `cluster_files` buckets with the extension attached, so
    `src:ts` and `svc/handler:go` are distinct keys no merge pass can join.
    """
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac"))
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(argv, cwd=dense_mixed_repo, check=True, capture_output=True)

    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo

    assert bootstrap_repo(dense_mixed_repo).status == "success"
    archetypes = json.loads((dense_mixed_repo / ".chameleon" / "archetypes.json").read_text())[
        "archetypes"
    ]

    patterns = {a.get("paths_pattern", "") for a in archetypes.values()}
    assert any(p.endswith(":go") for p in patterns), (
        f"the Go service produced no archetype of its own: {sorted(patterns)}"
    )
    assert any(p.endswith(":ts") or p.endswith(":tsx") for p in patterns), sorted(patterns)
    # Distinct keys per language: a merge would show as one archetype spanning both.
    assert len(archetypes) >= 2, sorted(archetypes)


def test_clustering_never_merges_two_languages(dense_mixed_repo: Path):
    """The property the change rests on, asserted directly rather than assumed."""
    from chameleon_mcp.bootstrap.clustering import cluster_files
    from chameleon_mcp.extractors.registry import select_extractor

    extractor = select_extractor(dense_mixed_repo)
    files = list(extractor.parse_repo(dense_mixed_repo).files) + _secondary_language_files(
        dense_mixed_repo, extractor.language
    )
    clusters = getattr(cluster_files(files, repo_root=dense_mixed_repo), "clusters", [])

    buckets = [c.key.path_pattern_bucket for c in clusters]
    assert len(buckets) == len(set(buckets)), buckets
    for cluster in clusters:
        exts = {Path(getattr(m, "path", m)).suffix for m in cluster.members}
        assert len(exts) == 1, f"cluster {cluster.key.path_pattern_bucket} mixes {exts}"


def test_adding_a_language_never_raises_the_sparse_bar_for_the_primary():
    """Covering a new language must not cost the primary its archetypes.

    `cluster_files` derives the sparse threshold from the TOTAL member count and
    the tiers are stepped (<1000 -> 3, <5000 -> 4, else 5). Feeding it secondary
    files therefore moves the bar for everyone: 995 TypeScript files plus 10 Go
    files crosses 1000, the threshold goes 3 -> 4, and every 3-member TypeScript
    cluster the repo used to get is dropped as sparse. Nothing in the suite would
    show it -- the archetypes simply stop existing on large repos.

    The orchestrator pins the threshold to the PRIMARY corpus, so this asserts
    the arithmetic that made the pin necessary.
    """
    from chameleon_mcp.bootstrap.clustering import _adaptive_sparse_threshold

    primary_only = _adaptive_sparse_threshold(995)
    with_secondaries = _adaptive_sparse_threshold(995 + 10)
    assert primary_only == 3 and with_secondaries == 4, (primary_only, with_secondaries)


def test_the_orchestrator_pins_the_threshold_to_the_primary_corpus(monkeypatch, tmp_path: Path):
    """The pin itself: `cluster_files` receives the primary-derived threshold."""
    from chameleon_mcp.bootstrap import orchestrator

    seen: dict = {}
    real = orchestrator.cluster_files

    def spy(files, *args, **kwargs):
        seen["min_cluster_size"] = kwargs.get("min_cluster_size")
        seen["n_files"] = len(list(files))
        return real(files, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "cluster_files", spy)
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac"))

    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    for i in range(4):
        (repo / "src" / f"m{i}.ts").write_text(f"export const v{i} = {i};\n", encoding="utf-8")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "i"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)

    orchestrator.bootstrap_repo(repo)
    assert seen.get("min_cluster_size") is not None, "threshold was left to the total count"
