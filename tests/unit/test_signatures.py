"""Unit tests for chameleon_mcp.signatures — pure functions, no I/O."""

from __future__ import annotations

import pytest

from chameleon_mcp.signatures import (
    ClusterKey,
    bucket_named_export_count,
    compute_signature,
    content_signal_match_for,
    nestjs_role_for_path,
    path_pattern_bucket_for,
    python_role_for_path,
    ts_spec_role_for_path,
)


class TestBucketNamedExportCount:
    def test_zero(self):
        assert bucket_named_export_count(0) == "0"

    def test_negative_maps_to_zero(self):
        assert bucket_named_export_count(-1) == "0"

    def test_one(self):
        assert bucket_named_export_count(1) == "1"

    def test_two(self):
        assert bucket_named_export_count(2) == "2-4"

    def test_four(self):
        assert bucket_named_export_count(4) == "2-4"

    def test_five(self):
        assert bucket_named_export_count(5) == "5-9"

    def test_nine(self):
        assert bucket_named_export_count(9) == "5-9"

    def test_ten(self):
        assert bucket_named_export_count(10) == "10+"

    def test_large(self):
        assert bucket_named_export_count(100) == "10+"


class TestPathPatternBucketFor:
    def test_single_segment_root(self):
        bucket, sub = path_pattern_bucket_for("Gemfile")
        assert bucket == "(root)"
        assert sub == ""

    def test_two_segments(self):
        bucket, sub = path_pattern_bucket_for("app/models/user.rb")
        assert bucket == "app/models"
        assert sub == ""

    def test_deep_path_default_depth(self):
        bucket, sub = path_pattern_bucket_for("app/controllers/api/v1/users.rb")
        assert bucket == "app/controllers"
        assert sub == "api/v1"

    def test_ruby_gem_layer_buckets_by_role(self):
        # RubyGems' standard layout is lib/<gem>/<layer>/. At the default
        # bucket depth the layer segment fell into sub_bucket, so every layer of
        # a gem (services, repositories, serializers, validators, clients, ...)
        # collapsed into ONE lib/<gem> archetype -- 56 files in a single cluster
        # on a real fixture -- and no per-role convention could clear its
        # dominance floor against that inflated denominator.
        bucket, sub = path_pattern_bucket_for("lib/freightline/services/rate_service.rb")
        assert bucket == "lib/freightline/services"
        assert sub == ""
        other, _ = path_pattern_bucket_for("lib/freightline/repositories/order_repo.rb")
        assert other == "lib/freightline/repositories"
        assert other != bucket

    def test_ruby_gem_layer_merges_deeper_nesting(self):
        # A file nested below the layer still belongs to that layer's cohort.
        bucket, sub = path_pattern_bucket_for("lib/freightline/services/billing/invoice.rb")
        assert bucket == "lib/freightline/services"
        assert sub == ""

    def test_lib_layout_untouched_for_non_ruby(self):
        # The gem-layout rule is a Ruby convention. A TS file under lib/ keeps
        # the existing directory bucketing so feature layouts do not fragment.
        bucket, sub = path_pattern_bucket_for("lib/features/auth/LoginForm.tsx")
        assert bucket == "lib/features"
        assert sub == "auth"

    def test_python_src_layout_buckets_by_layer(self):
        # PyPA src-layout (src/<pkg>/<layer>/) is the Python twin of RubyGems'
        # lib/<gem>/<layer>/: src/ is the source root, <pkg> is the distribution
        # package, and its subdirectories are the layers. At the default bucket
        # depth the layer fell into sub_bucket, collapsing handlers, clients and
        # repositories into one src/<pkg> archetype so no per-role convention
        # could clear its dominance floor -- inheritance derived to {} on a repo
        # where 7/7 files in a layer share a base class.
        bucket, sub = path_pattern_bucket_for("src/coldchain/handlers/api_handler.py")
        assert bucket == "src/coldchain/handlers"
        assert sub == ""
        other, _ = path_pattern_bucket_for("src/coldchain/repositories/shipment_repository.py")
        assert other == "src/coldchain/repositories"
        assert other != bucket

    def test_src_layout_untouched_for_typescript(self):
        # Scoped to Python: src/ in a TS repo is a feature-layout root, and
        # bucketing it at depth 3 would re-fragment those cohorts.
        bucket, sub = path_pattern_bucket_for("src/features/auth/LoginForm.tsx")
        assert bucket == "src/features"
        assert sub == "auth"

    def test_monorepo_workspace(self):
        bucket, sub = path_pattern_bucket_for("packages/excalidraw/components/Foo.tsx")
        assert bucket == "packages/excalidraw/components"

    def test_monorepo_apps(self):
        bucket, sub = path_pattern_bucket_for("apps/web/routes/page.tsx")
        assert bucket == "apps/web/routes"

    def test_monorepo_libs(self):
        # libs/ is an Nx-style workspace root; it buckets like packages/ and
        # apps/, folding the workspace name into the bucket.
        bucket, sub = path_pattern_bucket_for("libs/auth/services/token.py")
        assert bucket == "libs/auth/services"

    def test_include_extension_tsx(self):
        bucket, _ = path_pattern_bucket_for(
            "src/components/Button.tsx",
            include_extension=True,
        )
        assert bucket.endswith(":tsx")

    def test_include_extension_ts(self):
        bucket, _ = path_pattern_bucket_for(
            "src/components/helper.ts",
            include_extension=True,
        )
        assert bucket.endswith(":ts")

    def test_no_extension_by_default(self):
        bucket, _ = path_pattern_bucket_for("src/components/Button.tsx")
        assert ":" not in bucket

    def test_tsx_and_ts_differ_with_extension(self):
        b1, _ = path_pattern_bucket_for("src/components/Button.tsx", include_extension=True)
        b2, _ = path_pattern_bucket_for("src/components/helper.ts", include_extension=True)
        assert b1 != b2


class TestTsSpecRoleBucket:
    """Co-located *.spec.ts / *.test.ts(x) files bucket cross-directory into one
    "spec" role bucket so the repo's test layer forms an archetype instead of
    per-feature 1-member sparse clusters."""

    def test_colocated_specs_share_one_bucket(self):
        b1, s1 = path_pattern_bucket_for("src/orders/orders.service.spec.ts")
        b2, s2 = path_pattern_bucket_for("src/inventory/inventory.service.spec.ts")
        assert b1 == b2 == "spec"
        # Empty sub_bucket so the cross-dir merge survives _split_by_sub_bucket.
        assert s1 == s2 == ""

    def test_test_suffix_and_tsx_bucket(self):
        b1, _ = path_pattern_bucket_for("src/components/Button.test.tsx")
        b2, _ = path_pattern_bucket_for("__tests__/expense-form.test.tsx")
        assert b1 == b2 == "spec"

    def test_controller_spec_does_not_take_nest_role(self):
        # suppliers.controller.spec.ts ends in .spec.ts, NOT .controller.ts: it
        # must land in the spec bucket, never the nest controller role.
        bucket, _ = path_pattern_bucket_for("src/suppliers/suppliers.controller.spec.ts")
        assert bucket == "spec"
        assert nestjs_role_for_path("src/suppliers/suppliers.controller.spec.ts") is None

    def test_e2e_spec_stays_directory_bucketed(self):
        # *.e2e-spec.ts does not end in a bare .spec.ts; the e2e suite keeps its
        # own directory cluster.
        bucket, _ = path_pattern_bucket_for("test/orders.e2e-spec.ts")
        assert bucket != "spec"

    def test_existing_role_buckets_keep_precedence(self):
        # The spec check runs after the framework role matchers: a controller and
        # an app-router page still take their role buckets.
        bucket, _ = path_pattern_bucket_for("src/orders/orders.controller.ts")
        assert bucket == "controller"
        bucket, _ = path_pattern_bucket_for("app/dashboard/page.tsx")
        assert bucket == "app-page"

    def test_python_test_files_untouched(self):
        # Python tests keep their existing directory bucketing (the Python role
        # machinery owns .py routing).
        bucket, _ = path_pattern_bucket_for("tests/core/test_registry.py")
        assert bucket != "spec"

    def test_monorepo_workspace_prefix_preserved(self):
        b1, s1 = path_pattern_bucket_for("apps/web/components/Button.spec.ts")
        b2, _ = path_pattern_bucket_for("apps/admin/components/Nav.spec.ts")
        assert b1 == "apps/web/spec"
        assert b2 == "apps/admin/spec"
        assert s1 == ""

    def test_extension_suffix_applies(self):
        bucket, _ = path_pattern_bucket_for(
            "src/orders/orders.service.spec.ts", include_extension=True
        )
        assert bucket == "spec:ts"

    def test_role_helper_gates_on_suffix(self):
        assert ts_spec_role_for_path("src/orders/orders.service.spec.ts") == "spec"
        assert ts_spec_role_for_path("src/orders/orders.service.ts") is None
        assert ts_spec_role_for_path("spec/models/user_spec.rb") is None
        assert ts_spec_role_for_path("test/orders.e2e-spec.ts") is None


class TestContentSignalMatchFor:
    def test_use_client_double_quotes(self):
        assert content_signal_match_for('"use client";') == "use_client"

    def test_use_client_single_quotes(self):
        assert content_signal_match_for("'use client';") == "use_client"

    def test_use_server(self):
        assert content_signal_match_for('"use server";') == "use_server"

    def test_shebang(self):
        assert content_signal_match_for("#!/usr/bin/env node\n") == "shebang"

    def test_ts_pragma(self):
        assert content_signal_match_for("// @ts-nocheck\n") == "ts_pragma"

    def test_ts_pragma_with_leading_whitespace(self):
        assert content_signal_match_for("  // @ts-nocheck\n") == "ts_pragma"

    def test_none_signal(self):
        assert content_signal_match_for("import React from 'react';") == "none"

    def test_empty_string(self):
        assert content_signal_match_for("") == "none"


class TestComputeSignature:
    def test_returns_cluster_key(self):
        key = compute_signature(
            file_path="src/components/Button.tsx",
            content_first_200_bytes='"use client";\nimport React from "react";\n',
            top_level_node_kinds=["ImportDeclaration", "FunctionDeclaration"],
            default_export_kind="FunctionDeclaration",
            named_export_count=0,
            import_specifiers=[("react", "default")],
            has_jsx=True,
        )
        assert isinstance(key, ClusterKey)

    def test_cluster_key_is_order_and_import_insensitive(self):
        """v8 metric: node-kind ORDER and the import set no longer fragment the
        cluster (clustering now agrees with the lint set-match)."""
        a = compute_signature(
            file_path="app/services/s.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["ClassDeclaration", "ImportDeclaration"],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[("axios", "default")],
            has_jsx=False,
        )
        b = compute_signature(
            file_path="app/services/s.ts",
            content_first_200_bytes="",
            top_level_node_kinds=[
                "ImportDeclaration",
                "ClassDeclaration",
            ],  # different order
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[("lodash", "named")],  # different imports
            has_jsx=False,
        )
        assert a == b

    def test_frozen_hashable(self):
        key = compute_signature(
            file_path="src/utils/math.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["FunctionDeclaration"],
            default_export_kind=None,
            named_export_count=3,
            import_specifiers=[],
            has_jsx=False,
        )
        d = {key: True}
        assert d[key] is True

    def test_same_inputs_same_key(self):
        kwargs = dict(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["ImportDeclaration"],
            default_export_kind="FunctionDeclaration",
            named_export_count=1,
            import_specifiers=[("lodash", "named")],
            has_jsx=False,
        )
        k1 = compute_signature(**kwargs)
        k2 = compute_signature(**kwargs)
        assert k1 == k2

    def test_bucket_wired_correctly(self):
        key = compute_signature(
            file_path="src/components/Button.tsx",
            content_first_200_bytes="",
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.path_pattern_bucket == "src/components"

    def test_content_signal_wired(self):
        key = compute_signature(
            file_path="src/app.ts",
            content_first_200_bytes='"use client";',
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.content_signal_match == "use_client"

    def test_named_export_bucket_wired(self):
        key = compute_signature(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=7,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.named_export_count_bucket == "5-9"

    def test_to_dict_roundtrip(self):
        key = compute_signature(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["ImportDeclaration"],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=True,
        )
        d = key.to_dict()
        assert d["jsx_present"] is True
        assert d["top_level_node_kinds"] == ["ImportDeclaration"]
        assert d["default_export_kind"] is None


# --------------------------------------------------------------------------- #
# GAP-009b: the NestJS role-suffix map covered only 6 suffixes, so the roles a
# real Nest codebase uses most were left to directory bucketing and scattered
# into per-feature mixed clusters. Measured on a 6-feature Nest API: .dto.ts is
# the single largest role at 17 files, plus .repository.ts (6), .entity.ts (6),
# .interceptor.ts (2), .filter.ts and .decorator.ts -- 33 files that never
# reached a per-role sample size, leaving 54% of archetypes as cluster-<hash>.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/invoices/dto/create-invoice.dto.ts", "dto"),
        ("src/invoices/entities/invoice.entity.ts", "entity"),
        ("src/invoices/invoices.repository.ts", "repository"),
        ("src/common/interceptors/logging.interceptor.ts", "interceptor"),
        ("src/common/filters/all-exceptions.filter.ts", "filter"),
        ("src/common/decorators/public.decorator.ts", "decorator"),
        ("src/common/pipes/parse-id.pipe.ts", "pipe"),
        ("src/auth/jwt.strategy.ts", "strategy"),
        ("src/common/middleware/request-id.middleware.ts", "middleware"),
    ],
)
def test_nestjs_role_suffixes_cover_the_common_roles(path, expected):
    assert nestjs_role_for_path(path) == expected


def test_nestjs_roles_group_across_feature_directories():
    # The point of the map: the same role in different features buckets together.
    a = nestjs_role_for_path("src/invoices/dto/create-invoice.dto.ts")
    b = nestjs_role_for_path("src/shipments/dto/create-shipment.dto.ts")
    assert a == b == "dto"


def test_already_mapped_roles_are_unchanged():
    assert nestjs_role_for_path("src/invoices/invoices.controller.ts") == "controller"
    assert nestjs_role_for_path("src/invoices/invoices.service.ts") == "service"
    assert nestjs_role_for_path("src/invoices/invoices.module.ts") == "module"


def test_non_role_typescript_is_still_directory_bucketed():
    # A plain .ts must return None so it is bucketed by directory unchanged.
    assert nestjs_role_for_path("src/utils/money.ts") is None
    assert nestjs_role_for_path("src/main.ts") is None
    # A spec is a test, not a role -- test detection owns it.
    assert nestjs_role_for_path("src/invoices/invoices.service.spec.ts") != "service"


# --------------------------------------------------------------------------- #
# The Python filename-role map had the same incompleteness the NestJS suffix map
# had (GAP-009b): it covered Django's built-in roles but not the service-layer
# names real Django/DRF codebases add. Measured across five Python columns:
# services.py appears 13 times and selectors.py 13 times -- MORE often than
# serializers.py, routes.py, permissions.py, forms.py and filters.py (7 each),
# all of which were mapped. Unmapped, they fall into a per-app cluster whose
# archetype name is the app rather than the role.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,expected",
    [
        ("meridian/billing/services.py", "service"),
        ("meridian/billing/selectors.py", "selector"),
        ("meridian/billing/exceptions.py", "exception"),
        ("meridian/billing/repositories.py", "repository"),
        ("meridian/billing/mixins.py", "mixin"),
        ("meridian/billing/factories.py", "factory"),
        ("meridian/billing/policies.py", "policy"),
        ("meridian/billing/clients.py", "client"),
    ],
)
def test_python_service_layer_roles_are_mapped(path, expected):
    assert python_role_for_path(path) == expected


def test_python_service_roles_group_across_apps():
    # The point of the map: the same role in different apps buckets together.
    a = python_role_for_path("meridian/billing/services.py")
    b = python_role_for_path("meridian/carriers/services.py")
    assert a == b == "service"


def test_existing_python_roles_unchanged():
    assert python_role_for_path("meridian/billing/models.py") == "model"
    assert python_role_for_path("meridian/billing/views.py") == "view"
    assert python_role_for_path("meridian/billing/serializers.py") == "serializer"


def test_python_test_files_still_win_over_a_role_name():
    # tests/services.py is a test, not a service.
    assert python_role_for_path("tests/services.py") is None
    assert python_role_for_path("meridian/billing/tests.py") is None


def test_ambiguous_grabbag_filenames_are_not_roles():
    # base/utils/helpers are grab-bags, not a layer: grouping them cross-app
    # would merge unrelated code under one archetype.
    for stem in ("base", "utils", "helpers", "constants"):
        assert python_role_for_path(f"meridian/billing/{stem}.py") is None


class TestFrozenStringLiteralSignal:
    def test_directive_on_first_line(self):
        head = "# frozen_string_literal: true\n\nclass User\nend\n"
        assert content_signal_match_for(head) == "frozen_string_literal"

    def test_directive_below_shebang(self):
        head = "#!/usr/bin/env ruby\n# frozen_string_literal: true\nputs 1\n"
        assert content_signal_match_for(head) == "frozen_string_literal"

    def test_directive_below_encoding_comment(self):
        head = "# encoding: utf-8\n# frozen_string_literal: true\nmodule M\nend\n"
        assert content_signal_match_for(head) == "frozen_string_literal"

    def test_no_space_after_hash(self):
        assert content_signal_match_for("#frozen_string_literal: true\nx = 1\n") == (
            "frozen_string_literal"
        )

    def test_shebang_without_directive_stays_shebang(self):
        assert content_signal_match_for("#!/usr/bin/env ruby\nputs 1\n") == "shebang"

    def test_false_value_is_not_a_signal(self):
        head = "# frozen_string_literal: false\nclass User\nend\n"
        assert content_signal_match_for(head) == "none"

    def test_directive_after_code_is_not_a_signal(self):
        head = "class User\nend\n# frozen_string_literal: true\n"
        assert content_signal_match_for(head) == "none"

    def test_plain_leading_comment_is_none(self):
        assert content_signal_match_for("# just a comment\nclass User\nend\n") == "none"

    def test_signature_records_signal_for_rb_corpus(self):
        # Derivation side: siblings carrying the magic comment share one
        # cluster key whose content_signal_match is the new signal.
        keys = [
            compute_signature(
                file_path=f"app/models/{name}.rb",
                content_first_200_bytes="# frozen_string_literal: true\n\nclass X\nend\n",
                top_level_node_kinds=["ClassNode"],
                default_export_kind="ClassNode",
                named_export_count=1,
                import_specifiers=[],
                has_jsx=False,
            )
            for name in ("user", "order", "invoice")
        ]
        assert all(k.content_signal_match == "frozen_string_literal" for k in keys)
        assert len(set(keys)) == 1

    def test_derive_ast_query_persists_signal(self):
        from chameleon_mcp.bootstrap.canonical import derive_ast_query

        key = compute_signature(
            file_path="app/models/user.rb",
            content_first_200_bytes="# frozen_string_literal: true\n\nclass User\nend\n",
            top_level_node_kinds=["ClassNode"],
            default_export_kind="ClassNode",
            named_export_count=1,
            import_specifiers=[],
            has_jsx=False,
        )
        assert derive_ast_query(key)["content_signal"] == "frozen_string_literal"

    def test_derive_ast_query_none_when_directive_absent(self):
        from chameleon_mcp.bootstrap.canonical import derive_ast_query

        key = compute_signature(
            file_path="app/models/user.rb",
            content_first_200_bytes="class User\nend\n",
            top_level_node_kinds=["ClassNode"],
            default_export_kind="ClassNode",
            named_export_count=1,
            import_specifiers=[],
            has_jsx=False,
        )
        assert derive_ast_query(key)["content_signal"] is None
