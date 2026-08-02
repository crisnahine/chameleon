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
from collections import Counter
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


def test_the_threshold_pin_excludes_secondary_and_generated_files():
    """Both halves of the pin, at the tier where they actually differ.

    The earlier version of this test used a 4+4 fixture and asserted the call
    site's kwarg equalled `_adaptive_sparse_threshold(4)`. Every count under 1000
    resolves to 3, so it could not tell a primary-only pin from a combined one
    and passed with the regression reinstated. The tiers only step at 1000, so
    the discrimination has to happen there.
    """
    from types import SimpleNamespace

    from chameleon_mcp.bootstrap.clustering import _adaptive_sparse_threshold
    from chameleon_mcp.bootstrap.orchestrator import _primary_sparse_threshold

    real = [SimpleNamespace(content_first_200_bytes="const x = 1;\n") for _ in range(995)]
    generated = [
        SimpleNamespace(content_first_200_bytes="// @generated by protoc\n") for _ in range(20)
    ]

    # Generated files must not count: 995 real stays under the boundary.
    assert _primary_sparse_threshold(real + generated) == 3
    # And the naive count crosses it, which is what makes the exclusion matter.
    assert _adaptive_sparse_threshold(len(real + generated)) == 4
    # Secondary files must not count either: the helper is only ever given the
    # primary list, so 995 primary + 10 secondary still resolves to 3.
    assert _primary_sparse_threshold(real) == 3
    assert _adaptive_sparse_threshold(len(real) + 10) == 4


def test_the_secondary_parse_honors_discovery_exclusions(tmp_path: Path):
    """Vendored third-party source must never reach clustering.

    `parse_repo` with no `paths=` falls to a raw glob over the whole tree, which
    skips `discover_files` and therefore `EXCLUDE_FROM_CLUSTERING_DIRS`
    (`vendor`, `node_modules`, `dist`, `.venv`), the gitignore filter and the
    repo size guard. Harmless while these files only reached index artifacts;
    not harmless once they reach canonical-witness selection, where a vendored
    file could become the witness injected per-edit as the shape to imitate --
    `EXCLUDE_FROM_CANONICAL_POOL_DIRS` covers test and legacy dirs, not vendor.
    """
    (tmp_path / "svc").mkdir()
    (tmp_path / "vendor" / "github.com" / "x").mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
    for i in range(3):
        (tmp_path / "svc" / f"s{i}.go").write_text(
            f"package s\n\nfunc F{i}() {{}}\n", encoding="utf-8"
        )
    for i in range(5):
        (tmp_path / "vendor" / "github.com" / "x" / f"v{i}.go").write_text(
            f"package x\n\nfunc V{i}() {{}}\n", encoding="utf-8"
        )

    rels = sorted(
        str(Path(f.path).relative_to(tmp_path))
        for f in _secondary_language_files(tmp_path, "typescript")
    )
    assert rels == ["svc/s0.go", "svc/s1.go", "svc/s2.go"], rels


def test_the_primary_sparse_threshold_excludes_secondaries_and_generated_files():
    """The two boundary crossings, tested at the tier where they differ.

    A 4-file fixture cannot discriminate (every tier under 1000 is 3), so this
    exercises the computation directly at the 1000 boundary instead.
    """
    from types import SimpleNamespace

    from chameleon_mcp.bootstrap.orchestrator import _primary_sparse_threshold

    real = [SimpleNamespace(content_first_200_bytes="const x = 1;\n") for _ in range(990)]
    generated = [
        SimpleNamespace(content_first_200_bytes="// @generated by protoc\n") for _ in range(20)
    ]

    # 990 real files stay under the boundary even with 20 generated ones present.
    assert _primary_sparse_threshold(real + generated) == 3, (
        "generated files were counted, pushing a single-language repo over 1000"
    )
    # And the raw count would have crossed it, which is what makes this load-bearing.
    from chameleon_mcp.bootstrap.clustering import _adaptive_sparse_threshold

    assert _adaptive_sparse_threshold(len(real + generated)) == 4


def test_the_secondary_corpus_is_bounded(tmp_path: Path, monkeypatch):
    """A secondary language may not mint unbounded archetypes.

    The primary corpus has REPO_SIZE_GUARD; a secondary had no ceiling at all,
    so a repo whose primary is small and whose secondary is huge (a Go monorepo
    beside a small TS tooling dir) would derive an archetype per 3-file Go
    directory into the trust-hashed profile, with nothing bounding the count.
    """
    monkeypatch.setenv("CHAMELEON_CROSS_LANGUAGE_MAX_SECONDARY_FILES", "5")
    (tmp_path / "svc").mkdir()
    (tmp_path / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
    for i in range(30):
        (tmp_path / "svc" / f"s{i}.go").write_text(
            f"package s\n\nfunc F{i}() {{}}\n", encoding="utf-8"
        )

    files = _secondary_language_files(tmp_path, "typescript")
    assert len(files) == 5, f"secondary corpus unbounded: {len(files)}"


def test_the_cap_is_applied_by_spreading_not_truncating(tmp_path: Path, monkeypatch):
    """Guards the CALL SITE, not just the sampler.

    A single-directory fixture cannot tell round-robin from `[:cap]`, so this
    lays the secondary files across several directories: sorted truncation keeps
    only the alphabetically-first ones and starves the rest, which is exactly the
    invisible, arbitrary gap the sampler exists to prevent.
    """
    monkeypatch.setenv("CHAMELEON_CROSS_LANGUAGE_MAX_SECONDARY_FILES", "6")
    (tmp_path / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
    for d in ("alpha", "beta", "gamma"):
        (tmp_path / d).mkdir()
        for i in range(5):
            (tmp_path / d / f"s{i}.go").write_text(
                f"package {d}\n\nfunc F{i}() {{}}\n", encoding="utf-8"
            )

    first = _secondary_language_files(tmp_path, "typescript")
    assert len(first) == 6, len(first)

    dirs = Counter(Path(f.path).parent.name for f in first)
    assert set(dirs) == {"alpha", "beta", "gamma"}, (
        f"the cap starved whole directories instead of spreading: {dict(dirs)}"
    )

    second = [f.path for f in _secondary_language_files(tmp_path, "typescript")]
    assert [f.path for f in first] == second, "sampling is not deterministic"


def test_the_cap_spreads_across_directories_instead_of_starving_them():
    """Alphabetical truncation is worse than thinner uniform coverage.

    Taking the first `cap` sorted paths gives one directory full coverage and
    later ones none, so a developer working in a starved area gets no archetype
    and no way to see why the gap exists. Round-robin keeps every area
    represented, and stays deterministic so two runs agree.
    """
    from chameleon_mcp.bootstrap.orchestrator import _sample_across_dirs

    paths = [f"{d}/f{i}.go" for d in ("alpha", "beta", "gamma", "zeta") for i in range(10)]

    kept = _sample_across_dirs(paths, 12)
    assert len(kept) == 12
    per_dir = Counter(p.split("/")[0] for p in kept)
    assert set(per_dir) == {"alpha", "beta", "gamma", "zeta"}, per_dir
    assert max(per_dir.values()) - min(per_dir.values()) <= 1, per_dir
    # Sorted truncation is what this replaces, and it starves two whole dirs.
    starved = Counter(p.split("/")[0] for p in sorted(paths)[:12])
    assert len(starved) < 4, starved

    assert _sample_across_dirs(paths, 12) == kept, "sampling is not deterministic"
    assert len(_sample_across_dirs(paths[:5], 12)) == 5, "cap larger than input"
    assert _sample_across_dirs([], 5) == []


def test_conventions_never_receive_a_secondary_language_file(
    dense_mixed_repo: Path, monkeypatch, tmp_path: Path
):
    """The asymmetry the design rests on, asserted on the committed artifact.

    `extract_all_conventions` takes ONE repo-wide language and gates a dozen
    passes on it, so a Go file arriving under an archetype key would be measured
    with TypeScript semantics. A secondary-language archetype must therefore
    carry a shape and a witness but NO convention rules -- absent guidance
    rather than wrong guidance.
    """
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac"))
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "i"]):
        subprocess.run(argv, cwd=dense_mixed_repo, check=True, capture_output=True)

    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo

    assert bootstrap_repo(dense_mixed_repo).status == "success"
    profile = dense_mixed_repo / ".chameleon"
    archetypes = json.loads((profile / "archetypes.json").read_text())["archetypes"]
    conventions = json.loads((profile / "conventions.json").read_text())["conventions"]

    go_archetypes = {
        n for n, a in archetypes.items() if str(a.get("paths_pattern", "")).endswith(":go")
    }
    assert go_archetypes, sorted(archetypes)

    # Every per-archetype conventions section must be silent about the Go ones.
    for section, by_archetype in conventions.items():
        if not isinstance(by_archetype, dict):
            continue
        leaked = go_archetypes & set(by_archetype)
        assert not leaked, f"conventions section {section!r} carries Go archetypes {leaked}"


def test_no_secondary_file_is_handed_to_convention_extraction(
    dense_mixed_repo: Path, monkeypatch, tmp_path: Path
):
    """Guards the FILTER, not just its outcome.

    The sibling test asserts no Go archetype reaches `conventions.json`, and that
    holds even unfiltered -- the TypeScript-gated extractors simply yield nothing
    for Go source, so the contract is satisfied by accident. What must be pinned
    is that the Go files never arrive at all: an extractor that started returning
    a value for foreign input (or a primary language whose passes are less
    selective) would otherwise publish it under a Go archetype key silently.
    """
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac"))
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "i"]):
        subprocess.run(argv, cwd=dense_mixed_repo, check=True, capture_output=True)

    from chameleon_mcp.bootstrap import orchestrator

    seen: dict = {}
    real = orchestrator.extract_all_conventions

    def spy(*args, **kwargs):
        by_archetype = kwargs.get("files_by_archetype") or {}
        seen["suffixes"] = {
            Path(getattr(m, "path", m)).suffix for members in by_archetype.values() for m in members
        }
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "extract_all_conventions", spy)
    orchestrator.bootstrap_repo(dense_mixed_repo)

    assert seen.get("suffixes"), "extract_all_conventions was never reached"
    assert ".go" not in seen["suffixes"], (
        f"Go files were handed to convention extraction: {sorted(seen['suffixes'])}"
    )


def test_typescript_is_visible_as_a_secondary_language(tmp_path: Path):
    """The most common polyglot shape there is, and it derived nothing.

    `_MARKERS` gained python and ruby for the secondary case but not typescript,
    because typescript is the usual PRIMARY -- so the omission cost nothing on a
    TS repo and everything on a Rails app with `app/javascript/` or a Django app
    with a `frontend/` React tree, which derived zero TypeScript archetypes.
    """
    (tmp_path / "app" / "javascript").mkdir(parents=True)
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\n', encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    for i in range(6):
        (tmp_path / "app" / "models" / f"m{i}.rb").write_text(
            f"class M{i}; end\n", encoding="utf-8"
        )
        (tmp_path / "app" / "javascript" / f"c{i}.ts").write_text(
            f"export const c{i} = {i};\n", encoding="utf-8"
        )

    assert "typescript" in SpecDrivenExtractor.languages_present(tmp_path, exclude="ruby")


def test_the_user_scope_override_applies_to_secondary_languages_too(tmp_path: Path):
    """A `paths_glob` that scoped only the primary would contradict the profile.

    The profile records the narrow scope in `discovery.paths_glob`, so pulling
    secondary-language files from the whole tree into clustering and
    canonical-witness selection makes that record false.
    """
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    (tmp_path / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "apps" / "web" / "in.go").write_text(
        "package w\n\nfunc In() {}\n", encoding="utf-8"
    )
    (tmp_path / "other" / "out.go").write_text("package o\n\nfunc Out() {}\n", encoding="utf-8")

    scoped = _secondary_language_files(tmp_path, "typescript", paths_glob="apps/web/**/*")
    names = sorted(Path(f.path).name for f in scoped)
    assert "out.go" not in names, f"scope override ignored for secondary languages: {names}"
