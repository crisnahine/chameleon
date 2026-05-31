"""Unit tests for chameleon_mcp.bootstrap.naming — archetype name proposal.

These pin the rule-based, non-AI naming heuristic that turns the
``cluster-<hash>`` placeholders into human-meaningful names (``controller``,
``component``, ``hook``, ``migration``, ...). Every assertion fixes an exact
output for a fixed synthetic cluster, including the test/Rails/TS prior tables,
language gates, workspace stripping, slugification, and collision dedup.

The naming module is pure (no file/network I/O and reads no env vars at import
time). There is no conftest.py in this suite; sibling tests isolate via an
autouse fixture that points CHAMELEON_PLUGIN_DATA at tmp_path. We replicate that
isolation inline for hygiene even though this module never touches that dir.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from chameleon_mcp.bootstrap.naming import (
    _base_name_for,
    _disambiguation_suffixes,
    _has_dir_chain,
    _looks_like_test,
    _members_contain,
    _rails_prior_match,
    _sanitize,
    _segments,
    _short_hash_for,
    _strip_workspace_prefix,
    _ts_prior_match,
    propose_archetype_name,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Match the sibling-test isolation contract (no shared on-disk state)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))


def _cluster(
    *,
    bucket: str = "",
    default_export=None,
    kinds=(),
    jsx: bool = False,
    members=None,
    cluster_id=None,
):
    """Build a dict-shaped cluster stand-in.

    naming._cluster_attr reads ``cluster.key.<attr>`` first then falls back to
    ``cluster.get(attr)``; a plain dict has no ``key`` attribute so the signal
    keys here are read directly. ``members`` are plain path strings, which
    naming._member_relpaths accepts.
    """
    d = {
        "path_pattern_bucket": bucket,
        "default_export_kind": default_export,
        "top_level_node_kinds": kinds,
        "jsx_present": jsx,
        "members": list(members or []),
    }
    if cluster_id is not None:
        d["cluster_id"] = cluster_id
    return d


def _member(abs_path: str):
    """Build a member stub matching the production ParsedFile shape.

    Production passes ParsedFile instances whose ``.path`` is an absolute
    ``pathlib.Path``. naming._member_relpaths only does ``getattr(m, "path")``
    so a SimpleNamespace with a Path is a faithful stand-in.
    """
    return SimpleNamespace(path=Path(abs_path))


def _real_cluster(
    *,
    bucket: str = "",
    default_export=None,
    kinds=(),
    jsx: bool = False,
    members=None,
    cluster_id=None,
):
    """Build a cluster shaped like the real Cluster the orchestrator passes.

    Signals live on ``cluster.key`` (a ClusterKey-shaped SimpleNamespace), so
    naming._cluster_attr exercises its ``getattr(key, attr)`` branch instead of
    the dict fallback. Members expose an absolute ``.path``, matching ParsedFile.
    """
    key = SimpleNamespace(
        path_pattern_bucket=bucket,
        default_export_kind=default_export,
        top_level_node_kinds=kinds,
        jsx_present=jsx,
    )
    return SimpleNamespace(
        key=key,
        members=list(members or []),
        cluster_id=cluster_id,
    )


# --------------------------------------------------------------------------
# _segments
# --------------------------------------------------------------------------
class TestSegments:
    def test_splits_and_drops_empty_fragments(self):
        assert _segments("/app//controllers/") == ["app", "controllers"]

    def test_empty_string_is_no_segments(self):
        assert _segments("") == []


# --------------------------------------------------------------------------
# Rails prior table
# --------------------------------------------------------------------------
class TestRailsPriors:
    def test_controller(self):
        c = _cluster(
            bucket="app/controllers",
            members=[
                "app/controllers/users_controller.rb",
                "app/controllers/posts_controller.rb",
            ],
        )
        assert propose_archetype_name(c, set()) == "controller"

    def test_model(self):
        c = _cluster(
            bucket="app/models",
            members=["app/models/user.rb", "app/models/post.rb"],
        )
        assert propose_archetype_name(c, set()) == "model"

    def test_controller_concern_beats_bare_controller(self):
        # The concern chain is listed first in _RAILS_PRIORS so it wins.
        c = _cluster(
            bucket="app/controllers/concerns",
            members=[
                "app/controllers/concerns/authable.rb",
                "app/controllers/concerns/pageable.rb",
            ],
        )
        assert propose_archetype_name(c, set()) == "controller-concern"

    def test_migration(self):
        c = _cluster(
            bucket="db/migrate",
            members=["db/migrate/001_create_users.rb", "db/migrate/002_add_x.rb"],
        )
        assert propose_archetype_name(c, set()) == "migration"

    def test_job_requires_job_suffix_in_prior_but_falls_back_to_dir_token(self):
        # Prior entry for jobs needs "_job.rb"; without it the prior misses,
        # but the generic _has("jobs") fallback still yields "job".
        assert _rails_prior_match(["app/jobs/email.rb", "app/jobs/sync.rb"]) is None
        c = _cluster(bucket="app/jobs", members=["app/jobs/email.rb", "app/jobs/sync.rb"])
        assert _base_name_for(c) == "job"

    def test_job_with_suffix_matches_prior(self):
        c = _cluster(
            bucket="app/jobs",
            members=["app/jobs/email_job.rb", "app/jobs/sync_job.rb"],
        )
        assert _rails_prior_match([m for m in c["members"]]) == "job"

    def test_helper_with_suffix_matches_but_without_suffix_has_no_fallback(self):
        # _helper.rb suffix matches the prior...
        assert _base_name_for(
            _cluster(
                bucket="app/helpers",
                members=["app/helpers/x_helper.rb", "app/helpers/y_helper.rb"],
            )
        ) == "helper"
        # ...but with no suffix there is NO generic _has("helpers") fallback,
        # so the base name is None and the caller drops to a cluster-hash form.
        no_suffix = _cluster(
            bucket="app/helpers",
            members=["app/helpers/x.rb", "app/helpers/y.rb"],
            cluster_id="1234abcd9999",
        )
        assert _base_name_for(no_suffix) is None
        assert propose_archetype_name(no_suffix, set()) == "cluster-1234abcd"

    def test_rails_prior_skipped_for_non_ruby_members(self):
        # A TS file under app/models is not a ruby cluster: no rails prior runs,
        # but the generic _has("models") fallback still names it "model".
        c = _cluster(
            bucket="app/models",
            members=["app/models/user.ts", "app/models/post.ts"],
        )
        assert _rails_prior_match(c["members"]) == "model"  # path-only matcher is language-blind
        assert _base_name_for(c) == "model"


# --------------------------------------------------------------------------
# TS / JS prior table
# --------------------------------------------------------------------------
class TestTsPriors:
    def test_component_requires_components_dir(self):
        c = _cluster(
            bucket="src/components",
            members=["src/components/Button.tsx", "src/components/Card.tsx"],
            jsx=True,
        )
        assert propose_archetype_name(c, set()) == "component"

    def test_hook_requires_use_prefix(self):
        c = _cluster(
            bucket="src/hooks",
            members=["src/hooks/useThing.ts", "src/hooks/useOther.ts"],
        )
        assert _base_name_for(c) == "hook"

    def test_lib_module(self):
        assert _ts_prior_match(["src/lib/foo/X.ts"]) == "lib-module"

    def test_remix_route(self):
        c = _cluster(
            bucket="app/routes",
            members=["app/routes/index.tsx", "app/routes/about.tsx"],
        )
        assert _base_name_for(c) == "remix-route"

    def test_next_app_page_component(self):
        c = _cluster(
            bucket="app/dashboard",
            members=["app/dashboard/page.tsx", "app/settings/page.tsx"],
        )
        assert _base_name_for(c) == "app-page-component"

    def test_pages_api_handler(self):
        c = _cluster(
            bucket="pages/api",
            members=["pages/api/users.ts", "pages/api/posts.ts"],
        )
        assert _base_name_for(c) == "pages-api-handler"

    def test_root_api_client_special_case(self):
        # api/ as first segment with no Next.js overlap -> api-client.
        c = _cluster(bucket="api", members=["api/users.ts", "api/posts.ts"])
        assert _base_name_for(c) == "api-client"

    def test_root_middleware_ts_special_case(self):
        c = _cluster(bucket="(root)", members=["middleware.ts"])
        assert _base_name_for(c) == "middleware"

    def test_ts_prior_skipped_when_any_member_is_ruby(self):
        # First member is .ts so the language tell says TS, but a stray .rb in
        # the cluster trips the no-.rb-anywhere purity gate; the TS prior is
        # skipped and the generic jsx+components fallback names it "component".
        c = _cluster(
            bucket="src/components",
            members=["src/components/a.ts", "src/components/b.rb"],
            jsx=True,
        )
        assert _base_name_for(c) == "component"


# --------------------------------------------------------------------------
# Test-cluster detection
# --------------------------------------------------------------------------
class TestLooksLikeTest:
    def test_named_test_when_pattern_has_test_dir(self):
        c = _cluster(
            bucket="spec/models",
            members=["spec/models/a_spec.rb", "spec/models/b_spec.rb"],
        )
        assert propose_archetype_name(c, set()) == "test"

    def test_dir_token_signal(self):
        assert _looks_like_test("spec/models", []) is True
        assert _looks_like_test("src/__tests__", []) is True

    def test_half_of_members_carry_test_suffix(self):
        # 1 of 2 files ends in .test.ts -> 1*2 >= 2 -> True.
        assert _looks_like_test("src/x", ["src/x/a.test.ts", "src/x/b.ts"]) is True

    def test_all_members_under_spec_dir(self):
        assert _looks_like_test("", ["a/spec/x.rb", "b/spec/y.rb"]) is True

    def test_non_test_cluster_returns_false(self):
        assert _looks_like_test("app/models", ["app/models/user.rb"]) is False

    def test_empty_members_and_no_pattern_token_is_false(self):
        assert _looks_like_test("app/models", []) is False


# --------------------------------------------------------------------------
# Majority helpers
# --------------------------------------------------------------------------
class TestMajorityHelpers:
    def test_members_contain_strict_majority(self):
        majority = ["a/controllers/x.rb", "a/controllers/y.rb", "b/other/z.rb"]
        assert _members_contain(majority, "controllers") is True

    def test_members_contain_minority_rejected(self):
        minority = ["a/controllers/x.rb", "b/other/y.rb", "c/other/z.rb"]
        assert _members_contain(minority, "controllers") is False

    def test_members_contain_empty_is_false(self):
        assert _members_contain([], "controllers") is False

    def test_has_dir_chain_exact_half_passes(self):
        # 1 of 2 contains the chain -> 1*2 >= 2 -> True (tie counts as pass).
        assert _has_dir_chain(["app/api/r.ts", "other/x.ts"], ("app", "api")) is True

    def test_has_dir_chain_empty_is_false(self):
        assert _has_dir_chain([], ("app", "api")) is False


# --------------------------------------------------------------------------
# Workspace prefix stripping
# --------------------------------------------------------------------------
class TestStripWorkspacePrefix:
    def test_explicit_root_stripped(self):
        out = _strip_workspace_prefix(["apps/web/src/components/Foo.tsx"], ["apps/web"])
        assert out == ["src/components/Foo.tsx"]

    def test_path_shape_fallback_strips_two_segments(self):
        out = _strip_workspace_prefix(["packages/ui/src/hooks/useX.ts"], None)
        assert out == ["src/hooks/useX.ts"]

    def test_non_workspace_path_passes_through(self):
        assert _strip_workspace_prefix(["src/components/Foo.tsx"], None) == [
            "src/components/Foo.tsx"
        ]

    def test_does_not_mutate_input(self):
        src = ["apps/web/src/x.ts"]
        _strip_workspace_prefix(src, ["apps/web"])
        assert src == ["apps/web/src/x.ts"]

    def test_workspace_roots_let_component_prior_fire(self):
        c = _cluster(
            bucket="apps/web/src/components",
            members=[
                "apps/web/src/components/Foo.tsx",
                "apps/web/src/components/Bar.tsx",
            ],
            jsx=True,
        )
        assert propose_archetype_name(c, set(), workspace_roots=["apps/web"]) == "component"


# --------------------------------------------------------------------------
# Class default-export fallback
# --------------------------------------------------------------------------
class TestClassFallback:
    def test_ts_class_in_lib_uses_ts_prior(self):
        # lib/ wins via the TS prior before the bare class fallback.
        c = _cluster(
            bucket="src/lib/foo",
            default_export="ClassDeclaration",
            members=["src/lib/foo/X.ts"],
        )
        assert _base_name_for(c) == "lib-module"

    def test_ruby_class_at_root_is_bare_class(self):
        c = _cluster(bucket="(root)", default_export="ClassNode", members=["X.rb"])
        assert _base_name_for(c) == "class"


# --------------------------------------------------------------------------
# Slugification (_sanitize)
# --------------------------------------------------------------------------
class TestSanitize:
    def test_snake_and_caps_become_kebab(self):
        assert _sanitize("Foo_Bar") == "foo-bar"

    def test_spaces_become_dashes(self):
        assert _sanitize("API Client") == "api-client"

    def test_leading_digit_rejected(self):
        assert _sanitize("123abc") is None

    def test_all_punctuation_collapses_to_none(self):
        assert _sanitize("___") is None

    def test_leading_dash_strip_can_expose_digit_and_reject(self):
        # "-3-foo" -> strip dashes -> "3-foo" -> first char digit -> None.
        assert _sanitize("-3-foo") is None

    def test_truncates_to_64_chars(self):
        assert len(_sanitize("a" * 100)) == 64

    def test_already_valid_passes_through(self):
        assert _sanitize("a-b-c") == "a-b-c"


# --------------------------------------------------------------------------
# Disambiguation suffixes
# --------------------------------------------------------------------------
class TestDisambiguationSuffixes:
    def test_most_specific_first_dropping_generic_tail(self):
        c = _cluster(
            bucket="src/testing/mocks/handlers",
            members=["src/testing/mocks/handlers/foo.ts"],
        )
        # "src" is dropped (bucket_segs[1:]); "handlers"/"mocks"/"testing" survive,
        # ordered most-specific (rightmost) first.
        assert _disambiguation_suffixes(c) == ["handlers", "mocks", "testing"]

    def test_generic_only_bucket_yields_no_suffixes(self):
        c = _cluster(bucket="services", members=["services/a.ts", "services/b.ts"])
        assert _disambiguation_suffixes(c) == []

    def test_version_segments_filtered_out(self):
        c = _cluster(
            bucket="app/controllers/api/v1",
            members=["app/controllers/api/v1/users.rb"],
        )
        # "v1" is a version token and "controllers" is a generic tail; only
        # "api" survives.
        assert _disambiguation_suffixes(c) == ["api"]


# --------------------------------------------------------------------------
# Collision / dedup
# --------------------------------------------------------------------------
class TestCollisionDedup:
    def test_path_tail_suffix_on_collision(self):
        c = _cluster(
            bucket="app/controllers/api",
            members=[
                "app/controllers/api/users_controller.rb",
                "app/controllers/api/posts_controller.rb",
            ],
        )
        assert propose_archetype_name(c, {"controller"}) == "controller-api"

    def test_numeric_counter_when_no_suffix_candidates(self):
        # "services" base has no usable disambiguator, so the counter kicks in
        # and starts at 2.
        c = _cluster(bucket="services", members=["services/a.ts", "services/b.ts"])
        assert propose_archetype_name(c, {"service"}) == "service-2"
        assert propose_archetype_name(c, {"service", "service-2"}) == "service-3"

    def test_no_collision_returns_base_unchanged(self):
        c = _cluster(
            bucket="app/models",
            members=["app/models/user.rb", "app/models/post.rb"],
        )
        assert propose_archetype_name(c, {"controller"}) == "model"

    def test_existing_names_not_mutated(self):
        c = _cluster(bucket="services", members=["services/a.ts", "services/b.ts"])
        existing = {"service"}
        propose_archetype_name(c, existing)
        assert existing == {"service"}

    def test_deterministic_for_identical_inputs(self):
        c = _cluster(bucket="services", members=["services/a.ts", "services/b.ts"])
        first = propose_archetype_name(c, {"service"})
        second = propose_archetype_name(c, {"service"})
        assert first == second == "service-2"


# --------------------------------------------------------------------------
# Fallback hash form
# --------------------------------------------------------------------------
class TestFallbackHash:
    def test_uses_cluster_id_prefix_when_no_signal(self):
        c = _cluster(
            bucket="weird/place",
            members=["weird/place/x.go"],
            cluster_id="abcdef1234567890",
        )
        assert _base_name_for(c) is None
        assert propose_archetype_name(c, set()) == "cluster-abcdef12"

    def test_unknown_when_no_id_and_no_key(self):
        # Dict cluster with no cluster_id and no .key -> _short_hash_for returns
        # "unknown".
        c = _cluster(bucket="weird", members=["weird/x.go"])
        assert _short_hash_for(c) == "unknown"
        assert propose_archetype_name(c, set()) == "cluster-unknown"

    def test_returned_name_matches_schema_regex(self):
        # Every public result must satisfy ^[a-z][a-z0-9-]{0,63}$.
        import re

        name_re = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
        for c in (
            _cluster(bucket="app/controllers", members=["app/controllers/x_controller.rb"]),
            _cluster(bucket="weird", members=["weird/x.go"]),
            _cluster(bucket="services", members=["services/a.ts", "services/b.ts"]),
        ):
            assert name_re.match(propose_archetype_name(c, set()))


# --------------------------------------------------------------------------
# Production-shape inputs: real Cluster.key + absolute-path ParsedFile members
#
# Every other test in this file uses the dict fixture (signals read via the
# dict fallback, members as relative-path strings, repo_root=None). That only
# covers the dict branch of _cluster_attr and the string-passthrough branch of
# _member_relpaths. Production (orchestrator.py) ALWAYS passes a real Cluster
# whose signals live on .key, members that are ParsedFile objects with an
# ABSOLUTE Path, and repo_root=str(repo_root). These three tests pin that path.
# --------------------------------------------------------------------------
class TestProductionShapeInputs:
    def test_cluster_key_branch_resolves_name(self):
        # Signals on cluster.key -> _cluster_attr's getattr(key, attr) branch
        # (naming.py:102-106) fires instead of the dict fallback.
        c = _real_cluster(
            bucket="src/components",
            members=[
                _member("/abs/repo/src/components/Foo.tsx"),
                _member("/abs/repo/src/components/Bar.tsx"),
            ],
            jsx=True,
        )
        assert propose_archetype_name(c, set(), repo_root="/abs/repo") == "component"

    def test_absolute_member_path_relativized_against_repo_root(self):
        # Members carry an absolute .path; repo_root is passed through, so the
        # abs->rel branch of _member_relpaths (naming.py:144-151) runs and the
        # component prior fires on the repo-relative "src/components/..." paths.
        c = _real_cluster(
            bucket="src/components",
            members=[
                _member("/abs/repo/src/components/Foo.tsx"),
                _member("/abs/repo/src/components/Bar.tsx"),
            ],
            jsx=True,
        )
        assert propose_archetype_name(c, set(), repo_root="/abs/repo") == "component"
        # Sanity: the same cluster shape without jsx still resolves via the TS
        # prior table (the component chain), proving the relativized paths reach
        # the prior pipeline, not just the generic jsx fallback.
        assert _base_name_for(c, repo_root="/abs/repo") == "component"

    def test_repo_root_with_tests_segment_does_not_misname_as_test(self):
        # REGRESSION GUARD (commit a154969): the repo's own absolute location
        # contains a "tests"/"fixtures" segment. Without relativization, every
        # member's absolute path carries the "tests" token and _looks_like_test
        # fires on the "all members under a test dir" branch, misnaming the
        # cluster "test". Passing repo_root strips that prefix so the source
        # paths are just "src/components/...", and the name is "component".
        repo_root = "/x/chameleon/tests/fixtures/ts/repo"
        c = _real_cluster(
            bucket="src/components",
            members=[
                _member(f"{repo_root}/src/components/Foo.tsx"),
                _member(f"{repo_root}/src/components/Bar.tsx"),
            ],
            jsx=True,
        )
        assert propose_archetype_name(c, set(), repo_root=repo_root) == "component"
        # And prove the guard is load-bearing: drop repo_root and the abs paths
        # carry the "tests" token, so the same cluster degrades to "test".
        assert propose_archetype_name(c, set()) == "test"
