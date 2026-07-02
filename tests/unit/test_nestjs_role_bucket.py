"""Tests for NestJS / Angular filename-role path bucketing.

NestJS and Angular co-locate by feature (``users/users.controller.ts``), so the
role lives in the filename SUFFIX, not a directory. Clustering must bucket these
files by role across feature directories -- all ``*.controller.ts`` become one
"controller" archetype -- which means ``path_pattern_bucket_for`` returns the role
(not the feature dir) for a recognized suffix, with an EMPTY sub_bucket so the
cross-directory merge survives ``_split_by_sub_bucket``. Mirrors the Django/Next.js
role bucketers. Without this a nested NestJS layout fragments into one mixed
cluster per feature dir (controller + service + module together), none of which
reaches a per-role sample size, so class contract and reusable exports never
derive and the per-edit block degrades to a mixed ``cluster-*`` witness.
"""

from __future__ import annotations

from chameleon_mcp.signatures import nestjs_role_for_path, path_pattern_bucket_for

# --------------------------------------------------------------------------- #
# nestjs_role_for_path — the role detector
# --------------------------------------------------------------------------- #


def test_role_from_filename_suffix():
    assert nestjs_role_for_path("src/orders/orders.controller.ts") == "controller"
    assert nestjs_role_for_path("src/orders/orders.service.ts") == "service"
    assert nestjs_role_for_path("src/orders/orders.module.ts") == "module"
    assert nestjs_role_for_path("src/graph/user.resolver.ts") == "resolver"
    assert nestjs_role_for_path("src/ws/chat.gateway.ts") == "gateway"
    assert nestjs_role_for_path("src/common/auth.guard.ts") == "guard"


def test_case_insensitive_suffix():
    assert nestjs_role_for_path("src/Orders/Orders.Controller.ts") == "controller"


def test_no_role_for_plain_component_or_module_index():
    assert nestjs_role_for_path("src/app.ts") is None
    assert nestjs_role_for_path("src/components/Button.tsx") is None
    assert nestjs_role_for_path("src/orders/orders.dto.ts") is None  # not in the role set


def test_no_role_for_non_ts():
    assert nestjs_role_for_path("src/orders/orders.controller.js") is None
    assert nestjs_role_for_path("app/foo.service.rb") is None


# --------------------------------------------------------------------------- #
# path_pattern_bucket_for — role bucket + empty sub_bucket (split survival)
# --------------------------------------------------------------------------- #


def test_role_bucket_is_role_with_empty_subbucket():
    bucket, sub = path_pattern_bucket_for("src/orders/orders.controller.ts")
    assert bucket == "controller"
    assert sub == ""  # empty so _split_by_sub_bucket cannot re-fragment by feature dir


def test_role_bucket_with_extension():
    bucket, sub = path_pattern_bucket_for("src/orders/orders.controller.ts", include_extension=True)
    assert bucket == "controller:ts"
    assert sub == ""


def test_cross_feature_controllers_share_a_bucket():
    a, _ = path_pattern_bucket_for("src/orders/orders.controller.ts", include_extension=True)
    b, _ = path_pattern_bucket_for("src/products/products.controller.ts", include_extension=True)
    assert a == b == "controller:ts"


def test_monorepo_workspace_prefix_keeps_apps_distinct():
    # Two NestJS apps in a monorepo must not merge their controllers.
    a, _ = path_pattern_bucket_for("apps/api/orders/orders.controller.ts", include_extension=True)
    b, _ = path_pattern_bucket_for("apps/admin/users/users.controller.ts", include_extension=True)
    assert a == "apps/api/controller:ts"
    assert b == "apps/admin/controller:ts"
    assert a != b


def test_non_suffixed_ts_falls_through_to_directory_bucket():
    bucket, _ = path_pattern_bucket_for("src/orders/helpers.ts", include_extension=True)
    assert bucket == "src/orders:ts"  # directory bucketing, unchanged


def test_non_ts_unaffected():
    bucket, _ = path_pattern_bucket_for("app/models/user.rb", include_extension=True)
    assert bucket == "app/models:rb"
