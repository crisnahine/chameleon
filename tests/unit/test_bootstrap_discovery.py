"""Unit coverage for bootstrap source-file discovery.

Targets ``chameleon_mcp.bootstrap.discovery``: the repo walk, path-based
exclusion sets (clustering denylist dirs, leaf-name globs, exact relpaths),
canonical-pool eligibility, the brace-expansion glob layer, symlink/traversal
guards, and the post-glob file-count ceiling.

The target module reads no env vars at import time and holds no DB / connection
caches, so isolation is just the per-test ``tmp_path`` sandbox. Each test that
needs to walk the tree resolves ``tmp_path`` so the on-disk paths line up with
``Path.resolve()`` (on macOS ``/tmp`` is a symlink to ``/private/tmp``; the
walker resolves workspace bases, so an unresolved root would mismatch).

All synthetic fixtures live entirely under ``tmp_path``. No network, no node,
no prism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.bootstrap import discovery as d

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Return a resolved, empty repo root inside tmp_path."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _w(repo: Path, rel: str, body: str = "x") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _rels(repo: Path, paths) -> list[str]:
    return sorted(p.relative_to(repo).as_posix() for p in paths)


# ---------------------------------------------------------------------------
# discover_files: file-type filtering by glob
# ---------------------------------------------------------------------------


def test_default_glob_picks_only_ts_js_variants(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/a.ts")
    _w(repo, "src/b.tsx")
    _w(repo, "src/c.js")
    _w(repo, "src/d.jsx")
    _w(repo, "src/e.mjs")
    _w(repo, "src/f.cjs")
    _w(repo, "src/g.rb")  # not in default TS/JS glob
    _w(repo, "src/h.py")  # not in default TS/JS glob

    got = _rels(repo, d.discover_files(repo))
    assert got == [
        "src/a.ts",
        "src/b.tsx",
        "src/c.js",
        "src/d.jsx",
        "src/e.mjs",
        "src/f.cjs",
    ]


def test_ruby_glob_overrides_default(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "app/models/user.rb")
    _w(repo, "app/services/svc.rb")
    _w(repo, "src/a.ts")  # excluded: not matched by *.rb glob

    got = _rels(repo, d.discover_files(repo, glob="**/*.rb"))
    assert got == ["app/models/user.rb", "app/services/svc.rb"]


def test_results_are_sorted_lexicographically(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/zeta.ts")
    _w(repo, "src/alpha.ts")
    _w(repo, "src/mid.ts")

    got = d.discover_files(repo)
    assert [p.relative_to(repo).as_posix() for p in got] == [
        "src/alpha.ts",
        "src/mid.ts",
        "src/zeta.ts",
    ]


# ---------------------------------------------------------------------------
# discover_files: excluded directories
# ---------------------------------------------------------------------------


def test_excluded_dirs_are_dropped(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/keep.ts")
    _w(repo, "node_modules/dep/index.js")
    _w(repo, "dist/out.js")
    _w(repo, "build/out.js")
    _w(repo, ".next/page.js")
    _w(repo, "coverage/lcov.js")
    _w(repo, ".git/hooks/x.js")
    _w(repo, "__generated__/api.ts")
    _w(repo, ".venv/lib/site.py")  # also wrong type, doubly excluded

    got = _rels(repo, d.discover_files(repo))
    assert got == ["src/keep.ts"]


def test_excluded_dir_match_is_any_path_component(tmp_path):
    repo = _make_repo(tmp_path)
    # node_modules nested several levels down is still excluded.
    _w(repo, "packages/a/node_modules/dep.ts")
    _w(repo, "packages/a/src/keep.ts")

    got = _rels(repo, d.discover_files(repo))
    assert got == ["packages/a/src/keep.ts"]


def test_dir_name_substring_is_not_excluded(tmp_path):
    repo = _make_repo(tmp_path)
    # "node_modules_helper" only contains the excluded name as a substring,
    # not as a full path component, so it must survive.
    _w(repo, "node_modules_helper/x.ts")
    _w(repo, "node_modules/dep.ts")

    got = _rels(repo, d.discover_files(repo))
    assert got == ["node_modules_helper/x.ts"]


def test_vendor_dir_excluded_for_ruby(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "vendor/bundle/gem.rb")
    _w(repo, "app/models/user.rb")

    got = _rels(repo, d.discover_files(repo, glob="**/*.rb"))
    assert got == ["app/models/user.rb"]


# ---------------------------------------------------------------------------
# discover_files: leaf-name globs and exact relpaths
# ---------------------------------------------------------------------------


def test_minified_and_bundle_and_lock_files_dropped(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/foo.min.js")
    _w(repo, "src/style.min.css")  # wrong type anyway, but glob-excluded
    _w(repo, "src/app.bundle.js")
    _w(repo, "src/keep.js")
    _w(repo, "src/.DS_Store")

    got = _rels(repo, d.discover_files(repo))
    assert got == ["src/keep.js"]


def test_exact_relpath_exclusions_for_rails_schema(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "db/schema.rb")
    _w(repo, "db/structure.sql")  # wrong type, but in exact set
    _w(repo, "db/migrate/001.rb")  # NOT in the exact set -> kept
    _w(repo, "app/models/user.rb")

    got = _rels(repo, d.discover_files(repo, glob="**/*.rb"))
    assert got == ["app/models/user.rb", "db/migrate/001.rb"]


def test_schema_rb_only_excluded_at_repo_root_relpath(tmp_path):
    repo = _make_repo(tmp_path)
    # The exact-relpath set keys on the repo-relative posix path "db/schema.rb".
    # A schema.rb in a different dir is NOT in the set.
    _w(repo, "lib/db/schema.rb")
    _w(repo, "db/schema.rb")

    got = _rels(repo, d.discover_files(repo, glob="**/*.rb"))
    assert got == ["lib/db/schema.rb"]


# ---------------------------------------------------------------------------
# discover_files: symlink and path-traversal guards
# ---------------------------------------------------------------------------


def test_symlinks_are_dropped(tmp_path):
    repo = _make_repo(tmp_path)
    real = _w(repo, "src/real.ts")
    _w(repo, "src/normal.ts")
    link = repo / "src" / "linked.ts"
    link.symlink_to(real)

    got = _rels(repo, d.discover_files(repo))
    assert got == ["src/normal.ts", "src/real.ts"]


def test_paths_glob_escaping_repo_is_blocked(tmp_path):
    base = tmp_path.resolve()
    repo = (base / "repo").resolve()
    repo.mkdir(parents=True, exist_ok=True)
    _w(repo, "a.ts")
    # A sibling "secrets" dir outside the repo root.
    secrets = base / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "key.ts").write_text("TOPSECRET", encoding="utf-8")

    got = d.discover_files(repo, paths_glob="../secrets/*.ts")
    # The lexical "../"-prefixed relpath is rejected by the traversal guard.
    assert got == []


def test_paths_glob_overrides_default_glob(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/a.ts")
    _w(repo, "src/b.ts")
    _w(repo, "lib/c.ts")

    got = _rels(repo, d.discover_files(repo, paths_glob="src/*.ts"))
    assert got == ["src/a.ts", "src/b.ts"]


# ---------------------------------------------------------------------------
# discover_files: workspace roots (monorepo path-down)
# ---------------------------------------------------------------------------


def test_workspace_roots_scope_walk_to_named_subdirs(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "apps/web/x.ts")
    _w(repo, "apps/web/sub/deep.ts")
    _w(repo, "apps/api/y.ts")
    _w(repo, "packages/ignored/z.ts")  # outside the workspace roots

    got = _rels(repo, d.discover_files(repo, workspace_roots=["apps/web", "apps/api"]))
    assert got == ["apps/api/y.ts", "apps/web/sub/deep.ts", "apps/web/x.ts"]


def test_workspace_roots_still_apply_exclusions(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "apps/web/keep.ts")
    _w(repo, "apps/web/node_modules/dep.ts")  # excluded dir inside a workspace

    got = _rels(repo, d.discover_files(repo, workspace_roots=["apps/web"]))
    assert got == ["apps/web/keep.ts"]


def test_workspace_root_missing_dir_is_skipped(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "apps/web/x.ts")

    # "apps/api" does not exist; it must be silently skipped, not error.
    got = _rels(repo, d.discover_files(repo, workspace_roots=["apps/web", "apps/api"]))
    assert got == ["apps/web/x.ts"]


# ---------------------------------------------------------------------------
# discover_files: file-count ceiling
# ---------------------------------------------------------------------------


def test_over_ceiling_raises_too_many_files(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for i in range(6):
        _w(repo, f"src/f{i}.ts")

    monkeypatch.setattr(d, "REPO_SIZE_GUARD", 3)
    with pytest.raises(d.TooManyFilesError) as exc:
        d.discover_files(repo)
    # Count reflects the post-exclusion survivor count, not the guard.
    assert exc.value.count == 6


def test_exactly_at_ceiling_does_not_raise(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for i in range(6):
        _w(repo, f"src/f{i}.ts")

    # The check is strict-greater (> guard), so exactly-at-guard is allowed.
    monkeypatch.setattr(d, "REPO_SIZE_GUARD", 6)
    got = d.discover_files(repo)
    assert len(got) == 6


def test_too_many_files_error_message_and_attrs():
    err = d.TooManyFilesError(7, ceiling=5)
    assert err.count == 7
    assert err.ceiling == 5
    assert str(err) == ("repo has 7 files (ceiling 5); use explicit paths_glob to scope analysis")


def test_too_many_files_error_default_ceiling_is_repo_size_guard():
    err = d.TooManyFilesError(999)
    assert err.ceiling == d.REPO_SIZE_GUARD == 200_000


# ---------------------------------------------------------------------------
# discover_files: empty / missing inputs
# ---------------------------------------------------------------------------


def test_missing_repo_root_returns_empty(tmp_path):
    missing = (tmp_path / "does_not_exist").resolve()
    assert d.discover_files(missing) == []


def test_empty_repo_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    assert d.discover_files(repo) == []


# ---------------------------------------------------------------------------
# discovery_stats: counter semantics
# ---------------------------------------------------------------------------


def test_discovery_stats_pre_and_post_counts(tmp_path):
    repo = _make_repo(tmp_path)
    _w(repo, "src/a.ts")
    _w(repo, "src/b.tsx")
    _w(repo, "src/c.js")
    _w(repo, "node_modules/dep/index.js")  # excluded dir
    _w(repo, "dist/out.js")  # excluded dir
    _w(repo, "src/foo.min.js")  # excluded leaf glob

    stats = d.discovery_stats(repo)
    # pre counts every is_file/non-symlink candidate the glob matched.
    assert stats["pre_exclusion"] == 6
    # post counts only those surviving the EXCLUDE_FROM_CLUSTERING_* sets.
    assert stats["post_exclusion"] == 3


def test_discovery_stats_drops_symlinks_before_either_counter(tmp_path):
    repo = _make_repo(tmp_path)
    real = _w(repo, "src/a.ts")
    link = repo / "src" / "b.ts"
    link.symlink_to(real)

    stats = d.discovery_stats(repo)
    assert stats == {"pre_exclusion": 1, "post_exclusion": 1}


def test_discovery_stats_never_raises_on_oversize(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for i in range(5):
        _w(repo, f"src/f{i}.ts")

    monkeypatch.setattr(d, "REPO_SIZE_GUARD", 1)
    # discover_files would raise here; discovery_stats must not.
    stats = d.discovery_stats(repo)
    assert stats == {"pre_exclusion": 5, "post_exclusion": 5}


def test_discovery_stats_missing_repo(tmp_path):
    missing = (tmp_path / "nope").resolve()
    assert d.discovery_stats(missing) == {"pre_exclusion": 0, "post_exclusion": 0}


def test_discovery_stats_honors_paths_glob_traversal_guard(tmp_path):
    base = tmp_path.resolve()
    repo = (base / "repo").resolve()
    repo.mkdir(parents=True, exist_ok=True)
    _w(repo, "a.ts")
    secrets = base / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "key.ts").write_text("x", encoding="utf-8")

    stats = d.discovery_stats(repo, paths_glob="../secrets/*.ts")
    # The escaping file is counted in pre (it is a real file matched by glob)
    # but dropped from post by the "../" traversal guard.
    assert stats == {"pre_exclusion": 1, "post_exclusion": 0}


# ---------------------------------------------------------------------------
# is_eligible_as_canonical: canonical-pool exclusions
# ---------------------------------------------------------------------------


def test_canonical_excludes_nested_test_dirs():
    assert d.is_eligible_as_canonical("src/__tests__/a.ts") is False
    assert d.is_eligible_as_canonical("src/tests/a.ts") is False
    assert d.is_eligible_as_canonical("app/spec/user.rb") is False
    assert d.is_eligible_as_canonical("packages/x/legacy/old.ts") is False
    assert d.is_eligible_as_canonical("a/b/deprecated/c.ts") is False


def test_canonical_excludes_test_suffix_files():
    assert d.is_eligible_as_canonical("src/Button.test.tsx") is False
    assert d.is_eligible_as_canonical("src/api.spec.ts") is False
    assert d.is_eligible_as_canonical("src/Button.stories.tsx") is False
    assert d.is_eligible_as_canonical("src/data.fixture.ts") is False


def test_canonical_includes_ordinary_source():
    assert d.is_eligible_as_canonical("src/util.ts") is True
    assert d.is_eligible_as_canonical("app/models/user.rb") is True
    # "_spec.rb" has no ".spec." segment, so it is not matched by *.spec.*
    assert d.is_eligible_as_canonical("app/models/user_spec.rb") is True


def test_canonical_excludes_top_level_test_dirs():
    # Component matching excludes a test/e2e/cypress dir whether it is at the
    # repo root (the most common TS/JS and Rails layout) or nested under any
    # parent. Ordinary source outside those dirs stays eligible.
    assert d.is_eligible_as_canonical("tests/a.ts") is False
    assert d.is_eligible_as_canonical("e2e/flow.ts") is False
    assert d.is_eligible_as_canonical("cypress/x.ts") is False
    assert d.is_eligible_as_canonical("src/tests/a.ts") is False
    assert d.is_eligible_as_canonical("src/e2e/flow.ts") is False
    assert d.is_eligible_as_canonical("src/cypress/x.ts") is False
    assert d.is_eligible_as_canonical("src/util.ts") is True


# ---------------------------------------------------------------------------
# is_likely_generated: marker heuristics
# ---------------------------------------------------------------------------


def test_generated_markers_detected():
    assert d.is_likely_generated("// Code generated by protoc. DO NOT EDIT.") is True
    assert d.is_likely_generated("// @generated SignedSource<<abc>>") is True
    assert d.is_likely_generated("# This file was generated automatically") is True
    assert d.is_likely_generated("/* auto-generated, do not touch */") is True
    assert d.is_likely_generated("// autogenerated stub") is True


def test_generated_marker_is_case_insensitive():
    assert d.is_likely_generated("CODE GENERATED BY TOOL") is True
    assert d.is_likely_generated("Do Not Edit This File") is True


def test_ordinary_source_not_flagged_generated():
    assert d.is_likely_generated("export const x = 1;") is False
    assert d.is_likely_generated("class User < ApplicationRecord\nend") is False
    assert d.is_likely_generated("") is False


# ---------------------------------------------------------------------------
# Brace-expansion glob layer
# ---------------------------------------------------------------------------


def test_brace_expansion_cartesian_product():
    assert d._expand_brace_groups("{src,cypress}/**/*.{ts,tsx}") == [
        "src/**/*.ts",
        "src/**/*.tsx",
        "cypress/**/*.ts",
        "cypress/**/*.tsx",
    ]


def test_brace_expansion_nested_groups():
    assert d._expand_brace_groups("a{b,{c,d}}e") == ["abe", "ace", "ade"]


def test_brace_expansion_no_braces_unchanged():
    assert d._expand_brace_groups("src/**/*.ts") == ["src/**/*.ts"]


def test_brace_expansion_unbalanced_returns_raw():
    assert d._expand_brace_groups("src/{a,b") == ["src/{a,b"]


def test_brace_expansion_empty_body_returns_raw():
    assert d._expand_brace_groups("src/{}x") == ["src/{}x"]


def test_brace_expansion_dedups_identical_alternatives():
    assert d._expand_brace_groups("{a,a}x") == ["ax"]


def test_brace_expansion_capped_at_limit():
    a = "{" + ",".join(str(i) for i in range(40)) + "}"
    res = d._expand_brace_groups(a + a)
    assert len(res) == d._BRACE_EXPANSION_CAP == 512


def test_split_top_alternatives_respects_nesting():
    assert d._split_top_alternatives("a,{b,c},d") == ["a", "{b,c}", "d"]


def test_find_matching_brace_pairs_outermost():
    # "{a,{b,c}}" — index 0 must pair with index 8, not the inner brace.
    assert d._find_matching_brace("{a,{b,c}}", 0) == 8


def test_find_matching_brace_unbalanced_returns_minus_one():
    assert d._find_matching_brace("{a,b", 0) == -1


# ---------------------------------------------------------------------------
# Component / glob matcher primitives
# ---------------------------------------------------------------------------


def test_has_excluded_component_matches_any_part():
    excluded = d.EXCLUDE_FROM_CLUSTERING_DIRS
    assert d._has_excluded_component(Path("packages/a/node_modules/x.ts"), excluded) is True
    assert d._has_excluded_component(Path("src/app/main.ts"), excluded) is False
    assert d._has_excluded_component(Path("dist/bundle.js"), excluded) is True


def test_matches_filename_glob_leaf_only():
    globs = d.EXCLUDE_FROM_CLUSTERING_FILE_GLOBS
    assert d._matches_filename_glob("foo.min.js", globs) is True
    assert d._matches_filename_glob("yarn.lock", globs) is True
    assert d._matches_filename_glob(".DS_Store", globs) is True
    assert d._matches_filename_glob("foo.js", globs) is False


def test_matches_any_canonical_file_globs():
    globs = d.EXCLUDE_FROM_CANONICAL_POOL_FILE_GLOBS
    assert d._matches_any("Button.test.tsx", globs) is True
    assert d._matches_any("api.spec.ts", globs) is True
    assert d._matches_any("util.ts", globs) is False
