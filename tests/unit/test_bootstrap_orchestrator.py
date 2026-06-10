"""Unit tests for bootstrap.orchestrator PURE-LOGIC helpers.

These exercise the orchestrator's non-extraction machinery: path-pattern
display re-derivation, distribution-key stringification, same-pattern
archetype collapsing, sparse/bimodal warning assembly, the user-rename
overlay loader, extractor selection / hybrid-language detection, monorepo
workspace fanout detection, ad-hoc discovery hints, file counters, the
heuristic archetype summary, the generation counter, and BootstrapReport
serialization.

No node/prism subprocess is spawned. Where a full assembly path is
reached, clusters are produced from synthetic ParsedFiles via the real
cluster_files (pure Python, on-disk files only) instead of mocking.

Isolation: this module has no conftest. Each helper that touches the data
dir is given an explicit tmp_path; an autouse fixture pins
CHAMELEON_PLUGIN_DATA at tmp_path so nothing leaks into the developer's
real ~/.local/share/chameleon/.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap import orchestrator as o
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.extractors._base import ParsedFile


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch):
    """Replicate the per-file isolation other unit tests use.

    No connection cache to reset for the orchestrator, but pinning the
    data dir keeps record_bootstrap_baseline / index writes off the real
    home dir if any test reaches an assembly path.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    yield


# --------------------------------------------------------------------------
# helpers


def _pf(path: Path, *, kinds=("ClassNode",), named=0, jsx=False, default_kind=None) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=kinds,
        default_export_kind=default_kind,
        named_export_count=named,
        import_specifiers=(),
        has_jsx=jsx,
    )


def _write(repo: Path, rel: str, body: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# _displayed_paths_pattern — Rails-honest bucket re-derivation


class TestDisplayedPathsPattern:
    def test_rails_models_witness_rederives_app_second_tail(self):
        # bucket dropped the load-bearing "models" segment; witness restores it
        assert (
            o._displayed_paths_pattern(
                "app/rule/action_executor",
                "app/models/rule/action_executor/auto_categorize.rb",
            )
            == "app/models/action_executor"
        )

    def test_rails_controllers_witness(self):
        assert (
            o._displayed_paths_pattern(
                "app/admin/dashboards",
                "app/controllers/admin/dashboards/foo.rb",
            )
            == "app/controllers/dashboards"
        )

    def test_bucket_already_correct_is_unchanged(self):
        # witness only 3 parts -> guard bails, bucket returned verbatim
        assert o._displayed_paths_pattern("app/models", "app/models/user.rb") == "app/models"

    def test_non_rails_witness_unchanged(self):
        assert (
            o._displayed_paths_pattern("src/components/base", "src/components/base/Button.tsx")
            == "src/components/base"
        )

    def test_empty_witness_returns_bucket(self):
        assert o._displayed_paths_pattern("app/models", "") == "app/models"

    def test_first_segment_not_app_unchanged(self):
        assert o._displayed_paths_pattern("x/y/z", "lib/models/rule/foo.rb") == "x/y/z"

    def test_second_segment_not_load_bearing_unchanged(self):
        # "vendor" is not in _RAILS_LOAD_BEARING_SECOND_SEGS -> unchanged
        assert (
            o._displayed_paths_pattern("app/vendor/x", "app/vendor/sub/dir/file.rb")
            == "app/vendor/x"
        )

    def test_bucket_already_contains_segment_unchanged(self):
        # "models" already present in the bucket -> no re-derivation
        assert (
            o._displayed_paths_pattern("app/models/foo", "app/models/foo/bar/baz.rb")
            == "app/models/foo"
        )


# --------------------------------------------------------------------------
# _stringify_distribution_key — JSON-safe dict keys


class TestStringifyDistributionKey:
    def test_true_false_lowercase(self):
        assert o._stringify_distribution_key(True) == "true"
        assert o._stringify_distribution_key(False) == "false"

    def test_none_is_null(self):
        assert o._stringify_distribution_key(None) == "null"

    def test_str_and_int_passthrough(self):
        assert o._stringify_distribution_key("hi") == "hi"
        assert o._stringify_distribution_key(5) == "5"

    def test_int_zero_is_not_false(self):
        # identity check (value is False) means 0 must NOT collapse to "false"
        assert o._stringify_distribution_key(0) == "0"

    def test_int_one_is_not_true(self):
        assert o._stringify_distribution_key(1) == "1"


# --------------------------------------------------------------------------
# _rel_or_abs


class TestRelOrAbs:
    def test_path_inside_repo_is_relative(self):
        assert o._rel_or_abs(Path("/repo/a/b.rb"), Path("/repo")) == "a/b.rb"

    def test_path_outside_repo_falls_back_to_absolute(self):
        assert o._rel_or_abs(Path("/other/x.rb"), Path("/repo")) == "/other/x.rb"


# --------------------------------------------------------------------------
# _generation_counter


class TestGenerationCounter:
    def test_truncates_float_to_int(self):
        assert o._generation_counter(123.9) == 123

    def test_none_returns_int(self):
        assert isinstance(o._generation_counter(), int)


# --------------------------------------------------------------------------
# _collapse_same_pattern_archetypes


class TestCollapseSamePattern:
    def test_largest_cluster_keeps_and_absorbs_canonicals(self):
        arches = {
            "alpha": {"paths_pattern": "app/models", "cluster_size": 3},
            "beta": {"paths_pattern": "app/models", "cluster_size": 7},
            "gamma": {"paths_pattern": "app/services", "cluster_size": 2},
        }
        canon = {"alpha": [{"w": "a"}], "beta": [{"w": "b"}], "gamma": [{"w": "g"}]}
        na, nc = o._collapse_same_pattern_archetypes(arches, canon)

        assert sorted(na.keys()) == ["beta", "gamma"]
        # cluster sizes accumulate onto the keeper
        assert na["beta"]["cluster_size"] == 10
        # keeper's primary canonical stays at index 0, loser appended after
        assert nc["beta"] == [{"w": "b"}, {"w": "a"}]
        # untouched single-pattern archetype is preserved
        assert nc["gamma"] == [{"w": "g"}]
        # loser fully removed from both maps
        assert "alpha" not in na
        assert "alpha" not in nc

    def test_size_tie_breaks_alphabetically(self):
        arches = {
            "zeta": {"paths_pattern": "p", "cluster_size": 5},
            "apex": {"paths_pattern": "p", "cluster_size": 5},
        }
        canon = {"zeta": [{"w": "z"}], "apex": [{"w": "a"}]}
        na, nc = o._collapse_same_pattern_archetypes(arches, canon)
        assert list(na.keys()) == ["apex"]
        assert nc["apex"] == [{"w": "a"}, {"w": "z"}]

    def test_empty_paths_pattern_never_merges(self):
        arches = {
            "x": {"paths_pattern": "", "cluster_size": 1},
            "y": {"paths_pattern": "", "cluster_size": 1},
        }
        canon = {"x": [], "y": []}
        na, _ = o._collapse_same_pattern_archetypes(arches, canon)
        assert sorted(na.keys()) == ["x", "y"]

    def test_does_not_mutate_inputs(self):
        arches = {
            "a": {"paths_pattern": "p", "cluster_size": 1},
            "b": {"paths_pattern": "p", "cluster_size": 2},
        }
        canon = {"a": [1], "b": [2]}
        o._collapse_same_pattern_archetypes(arches, canon)
        assert sorted(arches.keys()) == ["a", "b"]
        assert canon == {"a": [1], "b": [2]}


# --------------------------------------------------------------------------
# _build_sparse_warnings (real clusters, on-disk files)


class TestBuildSparseWarnings:
    def test_one_singleton_per_bucket(self, tmp_path: Path):
        repo = tmp_path / "repo"
        files = [
            _pf(_write(repo, "app/lonely/a.rb", "class A; end"), kinds=("ClassNode",)),
            _pf(_write(repo, "app/other/b.rb", "module B; end"), kinds=("ModuleNode",)),
        ]
        res = cluster_files(files, repo, min_cluster_size=2)
        assert not res.dense_clusters
        warnings = o._build_sparse_warnings(res.sparse_clusters, repo)
        assert len(warnings) == 2
        patterns = {w["paths_pattern"] for w in warnings}
        assert patterns == {"app/lonely:rb", "app/other:rb"}
        for w in warnings:
            assert w["kind"] == "sparse_cluster"
            assert w["size"] == 1
            assert "threshold 2" in w["reason"]
            assert len(w["sample_paths"]) == 1

    def test_same_bucket_singletons_aggregate(self, tmp_path: Path):
        repo = tmp_path / "repo"
        files = [
            _pf(_write(repo, f"app/same/f{i}.rb", f"class C{i}; end"), kinds=(f"Kind{i}",))
            for i in range(3)
        ]
        res = cluster_files(files, repo, min_cluster_size=2)
        warnings = o._build_sparse_warnings(res.sparse_clusters, repo)
        assert len(warnings) == 1
        g = warnings[0]
        assert g["paths_pattern"] == "app/same:rb"
        assert g["cluster_count"] == 3
        assert g["total_members"] == 3
        assert g["min_size"] == 1 and g["max_size"] == 1
        assert "3 sparse clusters" in g["reason"]
        # sample paths capped at _WARNING_SAMPLE_PATHS (3)
        assert len(g["sample_paths"]) <= o._WARNING_SAMPLE_PATHS

    def test_empty_input_produces_no_warnings(self):
        assert o._build_sparse_warnings([], Path("/repo")) == []


# --------------------------------------------------------------------------
# _build_bimodal_warnings (fake cluster object — pure formatting logic)


class _FakeKey:
    path_pattern_bucket = "app/models"


class _FakeBimodalCluster:
    key = _FakeKey()
    size = 8
    bimodal_dimensions = ["content_signal"]
    members: list = []

    def dimension_distribution(self, dim):  # noqa: ARG002
        return {True: 5, False: 3}


class TestBuildBimodalWarnings:
    def test_distribution_keys_are_stringified(self):
        warnings = o._build_bimodal_warnings([_FakeBimodalCluster()], Path("/repo"))
        assert len(warnings) == 1
        w = warnings[0]
        assert w["kind"] == "bimodal_cluster"
        assert w["paths_pattern"] == "app/models"
        assert w["size"] == 8
        assert w["dimensions"] == ["content_signal"]
        # bool keys collapsed to "true"/"false" strings for JSON safety
        assert w["distributions"] == {"content_signal": {"true": 5, "false": 3}}
        assert "content_signal" in w["reason"]

    def test_empty_input(self):
        assert o._build_bimodal_warnings([], Path("/repo")) == []


# --------------------------------------------------------------------------
# _load_user_renames — overlay loader hardening


class TestLoadUserRenames:
    def _profile_dir(self, tmp_path: Path) -> Path:
        p = tmp_path / ".chameleon"
        p.mkdir()
        return p

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert o._load_user_renames(self._profile_dir(tmp_path)) == {}

    def test_valid_overlay(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text(
            json.dumps({"schema_version": 1, "renames": {"cluster_abc": "user-models"}})
        )
        assert o._load_user_renames(pdir) == {"cluster_abc": "user-models"}

    def test_future_schema_version_refused(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text(
            json.dumps({"schema_version": 99, "renames": {"a": "b"}})
        )
        assert o._load_user_renames(pdir) == {}

    def test_invalid_target_name_filtered_out(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        # "Bad_Name" violates ARCHETYPE_NAME_RE (uppercase + underscore)
        (pdir / "renames.json").write_text(
            json.dumps({"schema_version": 1, "renames": {"good": "Bad_Name", "ok": "fine-name"}})
        )
        assert o._load_user_renames(pdir) == {"ok": "fine-name"}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text("{not json")
        assert o._load_user_renames(pdir) == {}

    def test_renames_not_a_dict_returns_empty(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text(json.dumps({"schema_version": 1, "renames": ["x"]}))
        assert o._load_user_renames(pdir) == {}

    def test_schema_version_not_int_returns_empty(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text(
            json.dumps({"schema_version": "1", "renames": {"a": "b"}})
        )
        assert o._load_user_renames(pdir) == {}

    def test_empty_key_or_value_skipped(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        (pdir / "renames.json").write_text(
            json.dumps({"schema_version": 1, "renames": {"": "x", "y": ""}})
        )
        assert o._load_user_renames(pdir) == {}

    def test_over_cap_returns_empty(self, tmp_path: Path):
        pdir = self._profile_dir(tmp_path)
        from chameleon_mcp._thresholds import threshold_int

        cap = threshold_int("RENAMES_OVERLAY_CAP")
        big = {f"c{i}": f"name-{i}" for i in range(cap + 1)}
        (pdir / "renames.json").write_text(json.dumps({"schema_version": 1, "renames": big}))
        assert o._load_user_renames(pdir) == {}


# --------------------------------------------------------------------------
# _select_extractor / _is_rails_with_frontend / _rails_frontend_dir


class TestSelectExtractor:
    def _rails_frontend(self, repo: Path):
        repo.mkdir(parents=True)
        (repo / "Gemfile").write_text("source 'x'")
        (repo / "config").mkdir()
        (repo / "config" / "application.rb").write_text("# rails")
        (repo / "app" / "javascript").mkdir(parents=True)
        # also drop a TS signal that would otherwise win precedence
        (repo / "package.json").write_text('{"dependencies":{"typescript":"5"}}')
        return repo

    def test_rails_with_frontend_picks_ruby(self, tmp_path: Path):
        repo = self._rails_frontend(tmp_path / "repo")
        assert o._is_rails_with_frontend(repo) is True
        assert o._select_extractor(repo).language == "ruby"
        assert o._rails_frontend_dir(repo).name == "javascript"

    def test_ts_wins_over_ruby_without_rails_signal(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("source 'x'")
        (repo / "tsconfig.json").write_text("{}")
        assert o._select_extractor(repo).language == "typescript"

    def test_pure_ruby_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("source 'x'")
        assert o._select_extractor(repo).language == "ruby"

    def test_no_signals_returns_none(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert o._select_extractor(repo) is None

    def test_rails_needs_application_rb(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("x")
        (repo / "app" / "javascript").mkdir(parents=True)
        # no config/application.rb -> not classified as rails-with-frontend
        assert o._is_rails_with_frontend(repo) is False

    def test_rails_frontend_dir_none_when_absent(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert o._rails_frontend_dir(repo) is None

    def test_rails_frontend_dir_legacy_sprockets_layout(self, tmp_path: Path):
        repo = tmp_path / "repo"
        (repo / "app" / "assets" / "javascripts").mkdir(parents=True)
        # search-order: app/javascript first (absent) then assets/javascripts
        assert o._rails_frontend_dir(repo) == repo / "app" / "assets" / "javascripts"


# --------------------------------------------------------------------------
# _detect_workspace_ts_monorepo / _is_ts_workspace


class TestDetectWorkspaceTsMonorepo:
    def test_turborepo_style_root_fans_out(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"scripts":{"build":"turbo"}}')
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "apps" / "web" / "tsconfig.json").write_text("{}")
        (repo / "packages" / "ui").mkdir(parents=True)
        (repo / "packages" / "ui" / "package.json").write_text(
            '{"devDependencies":{"typescript":"5"}}'
        )
        (repo / "apps" / "empty").mkdir(parents=True)  # no TS signal -> excluded
        roots, capped = o._detect_workspace_ts_monorepo(repo)
        assert roots == ["apps/web", "packages/ui"]
        assert capped is False

    def test_root_tsconfig_short_circuits(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        (repo / "tsconfig.json").write_text("{}")
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "apps" / "web" / "tsconfig.json").write_text("{}")
        assert o._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_root_ts_token_short_circuits(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"dependencies":{"typescript":"5"}}')
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "apps" / "web" / "tsconfig.json").write_text("{}")
        assert o._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_no_root_package_json(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert o._detect_workspace_ts_monorepo(repo) == ([], False)

    def test_is_ts_workspace_tsconfig(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "tsconfig.json").write_text("{}")
        assert o._is_ts_workspace(ws) is True

    def test_is_ts_workspace_pkg_token(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text('{"devDependencies":{"vite":"5"}}')
        assert o._is_ts_workspace(ws) is True

    def test_is_ts_workspace_pkg_without_token(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text('{"name":"x"}')
        assert o._is_ts_workspace(ws) is False

    def test_is_ts_workspace_no_manifest(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        assert o._is_ts_workspace(ws) is False


# --------------------------------------------------------------------------
# _ad_hoc_discovery_hints


class TestAdHocDiscoveryHints:
    def test_detects_mixed_language_subprojects(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "apps" / "web" / "package.json").write_text("{}")
        (repo / "apps" / "api").mkdir(parents=True)
        (repo / "apps" / "api" / "Gemfile").write_text("x")
        (repo / "packages" / "lib").mkdir(parents=True)
        (repo / "packages" / "lib" / "tsconfig.json").write_text("{}")
        (repo / "apps" / "nolang").mkdir(parents=True)  # no manifest -> skipped
        hints = {h["subdir"]: h["language"] for h in o._ad_hoc_discovery_hints(repo)}
        assert hints == {
            "apps/web": "typescript",
            "apps/api": "ruby",
            "packages/lib": "typescript",
        }

    def test_each_hint_carries_abs_path(self, tmp_path: Path):
        repo = tmp_path / "repo"
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "apps" / "web" / "package.json").write_text("{}")
        hints = o._ad_hoc_discovery_hints(repo)
        assert len(hints) == 1
        assert hints[0]["abs_path"] == str(repo / "apps" / "web")

    def test_no_parent_dirs_returns_empty(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert o._ad_hoc_discovery_hints(repo) == []


# --------------------------------------------------------------------------
# file counters


class TestFileCounters:
    def test_count_ts_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.ts").write_text("x")
        (repo / "b.tsx").write_text("x")
        (repo / "sub").mkdir()
        (repo / "sub" / "c.js").write_text("x")
        assert o._count_ts_files_under(repo) == 3

    def test_count_ts_missing_dir_is_zero(self, tmp_path: Path):
        assert o._count_ts_files_under(tmp_path / "nope") == 0

    def test_count_ruby_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.rb").write_text("x")
        (repo / "sub").mkdir()
        (repo / "sub" / "b.rb").write_text("x")
        assert o._count_ruby_files_under(repo) == 2

    def test_count_ruby_missing_dir_is_zero(self, tmp_path: Path):
        assert o._count_ruby_files_under(tmp_path / "nope") == 0


# --------------------------------------------------------------------------
# _generate_archetype_summary


class TestGenerateArchetypeSummary:
    def test_ruby_namespaced_inheritance(self, tmp_path: Path):
        rb = tmp_path / "user.rb"
        rb.write_text("class Api::V1::User < Api::V1::Base\nend\n")
        entry = {
            "paths_pattern": "app/models",
            "top_level_node_kinds": ["ClassNode", "ModuleNode", "Method", "Extra"],
            "content_signal": "active_record",
        }
        summary = o._generate_archetype_summary(entry, rb, "ruby")
        # node kinds humanized and capped at 3 labels; inheritance + signal appended
        assert summary == (
            "app/models. typical shape: classes, modules, Method. "
            "active_record. inherits Api::V1::Base."
        )

    def test_ts_extends_and_use_client(self, tmp_path: Path):
        ts = tmp_path / "Comp.tsx"
        ts.write_text("'use client'\nexport class Comp extends React.Component {}\n")
        entry = {
            "paths_pattern_display": "src/components",
            "top_level_node_kinds": ["ClassDeclaration"],
            "content_signal": "none",
        }
        summary = o._generate_archetype_summary(entry, ts, "typescript")
        # content_signal "none" suppressed; both inherit + client component shown
        assert (
            summary == "src/components. typical shape: classes. inherits React.Component. "
            "client component."
        )

    def test_empty_entry_no_witness(self):
        assert o._generate_archetype_summary({}, None, "ruby") == ""

    def test_pattern_only(self):
        assert o._generate_archetype_summary({"paths_pattern": "lib/x"}, None, "ruby") == "lib/x."

    def test_node_kinds_truncated_to_three(self):
        entry = {"paths_pattern": "p", "top_level_node_kinds": ["A", "B", "C", "D", "E"]}
        assert o._generate_archetype_summary(entry, None, "ruby") == "p. typical shape: A, B, C."

    def test_display_pattern_preferred_over_raw(self):
        entry = {"paths_pattern": "raw", "paths_pattern_display": "shown"}
        assert o._generate_archetype_summary(entry, None, "ruby") == "shown."

    def test_witness_path_that_is_a_directory_degrades_gracefully(self, tmp_path: Path):
        # GAP #5: a canonical witness path that EXISTS but is NOT a regular
        # file (here a directory) must not crash. is_file() returns False so
        # the file-read branch is skipped and the summary is built from the
        # entry fields alone — no "inherits"/"client component" suffix.
        a_dir = tmp_path / "looks_like_a_witness"
        a_dir.mkdir()
        assert a_dir.exists() and not a_dir.is_file()
        entry = {
            "paths_pattern": "app/models",
            "top_level_node_kinds": ["ClassNode"],
            "content_signal": "active_record",
        }
        summary = o._generate_archetype_summary(entry, a_dir, "ruby")
        assert summary == "app/models. typical shape: classes. active_record."
        assert "inherits" not in summary
        assert "client component" not in summary

    def test_empty_entry_with_directory_witness_returns_empty(self, tmp_path: Path):
        # No entry fields and a directory witness -> still no crash, "" result.
        a_dir = tmp_path / "dir_witness"
        a_dir.mkdir()
        assert o._generate_archetype_summary({}, a_dir, "ruby") == ""


# --------------------------------------------------------------------------
# bootstrap_repo / _bootstrap_single — cleanly-reachable early-return branches
#
# GAP #6: drive the two no-source-files error returns without spawning
# node/prism. A repo with a Ruby signal (Gemfile) but no .rb files reaches
# discovery, finds nothing, and returns failed_unsupported_language before
# any extractor subprocess runs.


class TestBootstrapNoSourceFiles:
    def test_ruby_signal_but_no_rb_files_fails_unsupported(self, tmp_path: Path):
        # GAP #6(a): Gemfile present -> RubyExtractor selected; zero .rb files
        # -> discover_files returns [] -> the no-paths_glob branch fires with
        # the generic discovery-glob message.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
        # confirm the extractor really is Ruby (so we exercise the **/*.rb glob)
        assert o._select_extractor(repo).language == "ruby"

        report = o.bootstrap_repo(repo)
        assert report.status == "failed_unsupported_language"
        assert report.error == "No source files found matching the discovery glob"
        assert report.profile_path is None
        assert report.archetypes_detected == 0
        assert report.files_processed == 0
        # no profile dir was committed
        assert not (repo / ".chameleon").exists()

    def test_explicit_paths_glob_matching_nothing_fails_with_glob_message(self, tmp_path: Path):
        # GAP #6(b): a Ruby repo that DOES have a .rb file, but an explicit
        # paths_glob that matches nothing, takes the other branch of the same
        # no-candidates guard -> the paths_glob-specific error message.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
        (repo / "app").mkdir()
        (repo / "app" / "user.rb").write_text("class User; end\n")

        report = o.bootstrap_repo(repo, paths_glob="**/*.no_such_ext")
        assert report.status == "failed_unsupported_language"
        assert report.error == (
            "No source files found matching paths_glob '**/*.no_such_ext'. "
            "Verify the pattern (brace expansion is supported in both "
            "directory and basename) and that the chosen extensions "
            "actually exist under the repo."
        )
        assert report.profile_path is None
        assert not (repo / ".chameleon").exists()

    def test_no_language_signal_fails_unsupported_with_no_signals_message(self, tmp_path: Path):
        # GAP #6 (extra cleanly-reachable branch): an empty repo with neither
        # TS nor Ruby signals -> extractor is None -> the earliest
        # failed_unsupported_language return with the no-signals message.
        repo = tmp_path / "repo"
        repo.mkdir()
        report = o.bootstrap_repo(repo)
        assert report.status == "failed_unsupported_language"
        assert report.error == (
            "No TypeScript signals (tsconfig.json / package.json TS deps) "
            "and no Ruby signals (Gemfile / *.gemspec) detected"
        )
        assert report.profile_path is None
        assert report.discovery_hints == []

    def test_misconfigured_workspace_glob_surfaced_in_report(self, tmp_path: Path):
        # A pnpm workspace whose only glob matches nothing must surface a
        # diagnostic on the report instead of vanishing. The root has no TS
        # signal so bootstrap returns early, but the glob warning still flows.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/{ui,api}'\n")
        report = o.bootstrap_repo(repo)
        assert any("packages/{ui,api}" in w for w in report.workspace_glob_warnings)

    def test_manifestless_workspace_dir_surfaced_as_potential(self, tmp_path: Path):
        # A directory matching the glob but lacking package.json is reported as
        # a potential workspace so the user can add the missing manifest.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
        (repo / "apps" / "docs").mkdir(parents=True)
        report = o.bootstrap_repo(repo)
        assert "apps/docs" in report.workspace_potential_paths


# --------------------------------------------------------------------------
# BootstrapReport.to_dict


class TestBootstrapReportToDict:
    def _report(self, **kw):
        base = dict(
            status="success",
            archetypes_detected=2,
            rules_extracted=1,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=10,
            files_skipped_generated=1,
            files_skipped_parse=0,
            duration_ms=5,
            profile_path=Path("/x/.chameleon"),
        )
        base.update(kw)
        return o.BootstrapReport(**base)

    def test_archetype_totals_exclude_failed_workspaces(self):
        rep = self._report()
        rep.workspace_reports = [
            {"status": "success", "archetypes_detected": 3, "workspace_path": "apps/web"},
            {"status": "failed", "archetypes_detected": 9, "workspace_path": "apps/bad"},
        ]
        d = rep.to_dict()
        # 2 (root) + 3 (only the successful workspace)
        assert d["archetypes_detected"] == 5
        assert d["archetypes_detected_root"] == 2
        assert d["archetypes_per_workspace"] == {"apps/web": 3}
        # full workspace list (including failures) preserved under "workspaces"
        assert len(d["workspaces"]) == 2

    def test_profile_path_serialized_as_str(self):
        d = self._report().to_dict()
        assert d["profile_path"] == "/x/.chameleon"
        assert d["clustered_files"] == 10

    def test_none_profile_path_serializes_to_none(self):
        d = self._report(profile_path=None).to_dict()
        assert d["profile_path"] is None
        assert d["language_hint"] is None

    def test_no_workspaces_means_empty_per_workspace(self):
        d = self._report().to_dict()
        assert d["archetypes_detected"] == 2
        assert d["archetypes_per_workspace"] == {}
        assert d["workspaces"] == []

    def test_scalar_fields_coerced(self):
        rep = self._report(
            fanout_capped=True,
            discovered_files_pre_exclusion=12,
            discovered_files_post_exclusion=8,
            sparse_dropped_files=4,
        )
        d = rep.to_dict()
        assert d["fanout_capped"] is True
        assert d["discovered_files_pre_exclusion"] == 12
        assert d["discovered_files_post_exclusion"] == 8
        assert d["sparse_dropped_files"] == 4

    def test_workspace_glob_diagnostics_serialized(self):
        rep = self._report(
            workspace_glob_warnings=["'packages/{a,b}': matched no directories"],
            workspace_potential_paths=["apps/docs"],
        )
        d = rep.to_dict()
        assert d["workspace_glob_warnings"] == ["'packages/{a,b}': matched no directories"]
        assert d["workspace_potential_paths"] == ["apps/docs"]

    def test_workspace_glob_diagnostics_default_empty(self):
        d = self._report().to_dict()
        assert d["workspace_glob_warnings"] == []
        assert d["workspace_potential_paths"] == []


# --------------------------------------------------------------------------
# schema-version stamping (behavioral, beyond the const equality test)


class TestSchemaVersionStamping:
    def test_reload_reflects_env_independent_constant(self):
        # PROFILE_SCHEMA_VERSION is a module constant, not env-driven.
        # importlib.reload must yield the same value (regression guard that
        # nothing snuck an env read into the const definition).
        reloaded = importlib.reload(o)
        assert reloaded.PROFILE_SCHEMA_VERSION == 8
        assert reloaded.RENAMES_SCHEMA_VERSION == 1

    def test_engine_min_version_is_nonempty_string(self):
        assert isinstance(o.ENGINE_MIN_VERSION, str)
        assert o.ENGINE_MIN_VERSION


# --------------------------------------------------------------------------
# degraded-parse gate — a dying extractor child must never commit a thin
# profile over a healthy one under a success status (qa25 P1)


class TestKilledExtractorNeverCommitsThinProfile:
    def _ruby_repo(self, tmp_path: Path, n_files: int) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
        for i in range(n_files):
            _write(repo, f"app/models/model_{i}.rb", f"class Model{i}; end\n")
        return repo

    def test_mass_parse_skips_fail_the_run_and_leave_profile_untouched(
        self, tmp_path: Path, monkeypatch
    ):
        # Simulate a ruby child killed after the first record: 1 parsed, the
        # rest marked skipped as never-reached-stdout. The run must fail with
        # failed_extractor_degraded and must not write a profile.
        repo = self._ruby_repo(tmp_path, 12)
        files = sorted((repo / "app" / "models").glob("*.rb"))

        from chameleon_mcp.extractors._base import ParseResult
        from chameleon_mcp.extractors.ruby import RubyExtractor

        def _truncated(self, repo_root, glob="**/*.rb", limit=None, paths=None):
            return ParseResult(
                files=[_pf(files[0])],
                skipped=[(p, "extractor exited before emitting a record") for p in files[1:]],
            )

        monkeypatch.setattr(RubyExtractor, "parse_repo", _truncated)
        # A pre-existing profile must survive byte-identical.
        marker = repo / ".chameleon"
        marker.mkdir()
        (marker / "sentinel.txt").write_text("healthy\n")

        report = o.bootstrap_repo(repo)
        assert report.status == "failed_extractor_degraded"
        assert "11 of 12 files failed to parse" in (report.error or "")
        assert report.files_skipped_parse == 11
        assert (marker / "sentinel.txt").read_text() == "healthy\n"
        assert not (marker / "archetypes.json").exists()

    def test_zero_parsed_files_fail_even_below_the_skip_floor(self, tmp_path: Path, monkeypatch):
        # 3 candidates, all skipped: below EXTRACTOR_DEGRADED_MIN_SKIPPED but
        # nothing parsed at all — still a failed run, never an empty profile.
        repo = self._ruby_repo(tmp_path, 3)
        files = sorted((repo / "app" / "models").glob("*.rb"))

        from chameleon_mcp.extractors._base import ParseResult
        from chameleon_mcp.extractors.ruby import RubyExtractor

        monkeypatch.setattr(
            RubyExtractor,
            "parse_repo",
            lambda self, repo_root, glob="**/*.rb", limit=None, paths=None: ParseResult(
                files=[], skipped=[(p, "boom") for p in files]
            ),
        )
        report = o.bootstrap_repo(repo)
        assert report.status == "failed_extractor_degraded"
        assert not (repo / ".chameleon").exists()

    def test_ruby_toolchain_missing_degrades_to_clean_report(self, tmp_path: Path, monkeypatch):
        # ruby/prism_dump.rb unavailable must yield a failed report through the
        # normal envelope, not an exception escaping to the MCP boundary.
        repo = self._ruby_repo(tmp_path, 2)

        from chameleon_mcp.extractors.ruby import RubyExtractor, RubyUnavailableError

        def _raise(self, repo_root, glob="**/*.rb", limit=None, paths=None):
            raise RubyUnavailableError("`ruby` not found on PATH")

        monkeypatch.setattr(RubyExtractor, "parse_repo", _raise)
        report = o.bootstrap_repo(repo)
        assert report.status == "failed_ruby_unavailable"
        assert "ruby" in (report.error or "")
        assert not (repo / ".chameleon").exists()

    def test_missing_ts_dump_script_is_extractor_unavailable_not_crash(self, tmp_path: Path):
        # The TS extractor's missing-script guard must raise the catchable
        # NodeUnavailableError, not a bare FileNotFoundError.
        from chameleon_mcp.extractors.typescript import NodeUnavailableError, TypeScriptExtractor

        ext = TypeScriptExtractor(ts_dump_script=tmp_path / "nope" / "ts_dump.mjs")
        src = _write(tmp_path, "src/a.ts", "export const a = 1\n")
        with pytest.raises(NodeUnavailableError):
            ext.parse_repo(tmp_path, paths=[src])


class TestParseLooksDegraded:
    def test_clean_parse_is_not_degraded(self):
        assert not o._parse_looks_degraded(100, 0)

    def test_handful_of_skips_below_floor_is_not_degraded(self):
        assert not o._parse_looks_degraded(5, 9)

    def test_ratio_above_half_with_floor_met_is_degraded(self):
        assert o._parse_looks_degraded(9, 11)

    def test_large_repo_minority_skips_not_degraded(self):
        assert not o._parse_looks_degraded(900, 100)

    def test_zero_attempted_is_not_degraded(self):
        assert not o._parse_looks_degraded(0, 0)

    def test_zero_parsed_any_skipped_is_degraded(self):
        assert o._parse_looks_degraded(0, 1)


# --------------------------------------------------------------------------
# idioms.md carry-forward — taught idioms are user-authored and cannot be
# regenerated, so a damaged idioms.md must survive byte-identical and be
# flagged loudly, never silently templated away or committed as healthy
# (qa25 P2)


TAUGHT_IDIOMS = """# Team idioms

## active

### http-via-request-tuple

All HTTP goes through the request tuple helper.

Example:

```ts
const [data, error] = await request(url)
```
"""


class TestDamagedIdiomsCarryForward:
    def _ts_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tsconfig.json").write_text("{}")
        for i in range(3):
            _write(repo, f"src/util_{i}.ts", f"export const u{i} = {i}\n")
        return repo

    def _bootstrap_with_fake_parse(self, repo: Path, monkeypatch):
        from chameleon_mcp.extractors.typescript import TypeScriptExtractor

        files = sorted((repo / "src").glob("*.ts"))
        monkeypatch.setattr(
            TypeScriptExtractor,
            "parse_repo",
            lambda self, repo_root, glob="**/*", limit=None, paths=None: __import__(
                "chameleon_mcp.extractors._base", fromlist=["ParseResult"]
            ).ParseResult(files=[_pf(p, kinds=("VariableStatement",)) for p in files], skipped=[]),
        )
        return o.bootstrap_repo(repo)

    def test_non_utf8_idioms_md_survives_byte_identical_with_warning(
        self, tmp_path: Path, monkeypatch
    ):
        repo = self._ts_repo(tmp_path)
        pd = repo / ".chameleon"
        pd.mkdir()
        damaged = b"# Team idioms\n\xff\xfe taught content here\n"
        (pd / "idioms.md").write_bytes(damaged)

        report = self._bootstrap_with_fake_parse(repo, monkeypatch)
        assert report.status == "success"
        assert (pd / "idioms.md").read_bytes() == damaged
        assert any("not valid UTF-8" in w for w in report.idiom_warnings)

    def test_garbage_idioms_md_carried_verbatim_with_warning(self, tmp_path: Path, monkeypatch):
        repo = self._ts_repo(tmp_path)
        pd = repo / ".chameleon"
        pd.mkdir()
        garbage = "completely unstructured text, no idiom blocks at all\n"
        (pd / "idioms.md").write_text(garbage, encoding="utf-8")

        report = self._bootstrap_with_fake_parse(repo, monkeypatch)
        assert report.status == "success"
        assert (pd / "idioms.md").read_text(encoding="utf-8") == garbage
        assert any("no parseable idiom blocks" in w for w in report.idiom_warnings)

    def test_healthy_idioms_md_carried_with_count_and_no_warning(self, tmp_path: Path, monkeypatch):
        repo = self._ts_repo(tmp_path)
        pd = repo / ".chameleon"
        pd.mkdir()
        (pd / "idioms.md").write_text(TAUGHT_IDIOMS, encoding="utf-8")

        report = self._bootstrap_with_fake_parse(repo, monkeypatch)
        assert report.status == "success"
        assert report.idiom_warnings == []
        assert report.idioms_collected == 1
        assert "http-via-request-tuple" in (pd / "idioms.md").read_text(encoding="utf-8")

    def test_fresh_repo_without_idioms_gets_template_and_no_warning(
        self, tmp_path: Path, monkeypatch
    ):
        repo = self._ts_repo(tmp_path)
        report = self._bootstrap_with_fake_parse(repo, monkeypatch)
        assert report.status == "success"
        assert report.idiom_warnings == []
        assert report.idioms_collected == 0
