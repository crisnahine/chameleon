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
