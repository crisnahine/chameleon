"""Regression tests for v0.5.4 naming pipeline.

Two changes ship under v0.5.4 Bug F:

  1. ``_strip_workspace_prefix`` — strips ``apps/<ws>/`` and
     ``packages/<ws>/`` (and the other conventional monorepo parents)
     from member paths before TS-prior matching. Two strategies:
     explicit ``workspace_roots`` from the bootstrap envelope (Bug B
     path) AND a path-shape fallback for repos that slip past the
     orchestrator's workspace detector (plane case — pnpm catalog refs
     in root package.json mask the workspace from Bug B).

  2. New TS prior table entries covering patterns surfaced by the
     cycle-3 dogfood: ``features/``, ``testing/mocks/``,
     ``mocks/handlers/``, ``icons/``, ``locales/`` / ``i18n/``,
     ``constants/``, ``schema/`` / ``schemas/``, ``providers/``,
     ``contexts/``, ``layouts/``, ``config/`` / ``configs/``.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_4_naming_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))


PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


from chameleon_mcp.bootstrap.naming import (  # noqa: E402
    _strip_workspace_prefix,
    _ts_prior_match,
    propose_archetype_name,
)


# Helper: build a minimal cluster shape that propose_archetype_name accepts.
class _Member:
    def __init__(self, path: str) -> None:
        self.path = path


class _Key:
    def __init__(
        self,
        *,
        path_pattern_bucket: str = "",
        default_export_kind: str | None = None,
        top_level_node_kinds: tuple[str, ...] = (),
        jsx_present: bool = False,
    ) -> None:
        self.path_pattern_bucket = path_pattern_bucket
        self.default_export_kind = default_export_kind
        self.top_level_node_kinds = top_level_node_kinds
        self.jsx_present = jsx_present


class _Cluster:
    def __init__(self, key: _Key, member_paths: list[str]) -> None:
        self.key = key
        self.members = [_Member(p) for p in member_paths]


# ---------------------------------------------------------------------------
section("_strip_workspace_prefix: explicit roots")
# Verify-before: TS-prior matching against `apps/<ws>/src/components/` paths
# would not match the bare `components/` rule because the chain check
# doesn't anchor at the workspace boundary.
# Verify-after: with explicit roots, the prefix is stripped and the cluster
# names land on the TS prior rule for `components/`.

stripped = _strip_workspace_prefix(
    ["apps/web/src/components/Foo.tsx", "apps/web/src/components/Bar.tsx"],
    ["apps/web", "apps/api"],
)
t(
    "explicit root strips apps/web/ prefix",
    stripped == ["src/components/Foo.tsx", "src/components/Bar.tsx"],
    f"got {stripped}",
)

# Longest-match wins so apps/admin-app/ isn't accidentally stripped to admin-app/...
stripped = _strip_workspace_prefix(
    ["apps/admin-app/src/foo.ts"],
    ["apps", "apps/admin-app"],
)
t(
    "longest workspace root wins over shorter prefix",
    stripped == ["src/foo.ts"],
    f"got {stripped}",
)

# Paths that don't match any explicit root fall back to the path-shape
# strategy: an `apps/<ws>/...` path still gets stripped via the
# fallback. This is intentional — the fallback exists for repos that
# slip past Bug B's workspace detector. Flat paths (without the
# `apps|packages|services|workspaces/<ws>/` shape) stay untouched.
stripped = _strip_workspace_prefix(
    ["src/foo.ts", "apps/web/src/bar.ts"],
    ["apps/api"],  # explicit set has only apps/api
)
t(
    "non-matching explicit roots fall back to path-shape strip",
    stripped == ["src/foo.ts", "src/bar.ts"],
    f"got {stripped}",
)
# A truly flat path (no workspace shape) is untouched whether or not
# explicit roots are passed.
stripped = _strip_workspace_prefix(
    ["src/foo.ts", "lib/bar.ts"],
    ["apps/api"],
)
t(
    "flat paths untouched even when explicit roots are passed",
    stripped == ["src/foo.ts", "lib/bar.ts"],
    f"got {stripped}",
)

# Empty / None roots: no-op
t(
    "no-op on empty workspace_roots",
    _strip_workspace_prefix(["src/foo.ts"], []) == ["src/foo.ts"],
)
t(
    "no-op on None workspace_roots",
    _strip_workspace_prefix(["src/foo.ts"], None) == ["src/foo.ts"],
)


# ---------------------------------------------------------------------------
section("_strip_workspace_prefix: path-shape fallback (plane / pnpm catalog)")
# Verify-before: plane's root package.json has `typescript: catalog:` and
# `vite: catalog:` (pnpm catalog refs), which makes the v0.5.3 Bug B
# workspace detector treat the repo as a flat TS repo. Workspaces still
# exist on disk, but `workspace_roots` arrives empty.
# Verify-after: the path-shape fallback strips the 2-segment prefix when
# parts[0] is one of apps/packages/services/workspaces and parts[1] is
# a non-empty workspace name.

stripped = _strip_workspace_prefix(
    [
        "packages/propel/src/components/Foo.tsx",
        "packages/propel/src/components/Bar.tsx",
    ],
    None,  # No explicit roots — fallback path
)
t(
    "path-shape fallback strips packages/<ws>/",
    stripped == [
        "src/components/Foo.tsx",
        "src/components/Bar.tsx",
    ],
    f"got {stripped}",
)

stripped = _strip_workspace_prefix(
    ["services/api-server/src/handlers/users.ts"], None,
)
t(
    "path-shape fallback strips services/<ws>/",
    stripped == ["src/handlers/users.ts"],
    f"got {stripped}",
)

stripped = _strip_workspace_prefix(
    ["workspaces/lib/src/main.ts"], None,
)
t(
    "path-shape fallback strips workspaces/<ws>/",
    stripped == ["src/main.ts"],
    f"got {stripped}",
)

# Flat repos (no workspace shape) pass through untouched even with the
# fallback active.
stripped = _strip_workspace_prefix(
    ["src/components/Foo.tsx", "lib/utils.ts"], None,
)
t(
    "flat-repo paths unaffected by path-shape fallback",
    stripped == ["src/components/Foo.tsx", "lib/utils.ts"],
)

# Edge: single-segment path or path with only 2 segments — no prefix to
# strip (parts[1] would be the filename, not a workspace dir).
stripped = _strip_workspace_prefix(["apps/web"], None)
t(
    "fallback doesn't strip incomplete shape (path too short)",
    stripped == ["apps/web"],
)


# ---------------------------------------------------------------------------
section("_strip_workspace_prefix: explicit takes precedence over fallback")
# When both strategies could fire, the explicit roots win (more specific
# user intent). This matters when a repo has nested-monorepo layouts.

stripped = _strip_workspace_prefix(
    ["apps/web/src/foo.ts"],
    ["apps/web"],
)
t(
    "explicit root used when both strategies could fire",
    stripped == ["src/foo.ts"],
)


# ---------------------------------------------------------------------------
section("TS prior table — v0.5.4 new entries")

# features/ → feature-module
t(
    "features/<feature>/ → feature-module",
    _ts_prior_match([
        "src/features/comments/api/get-comments.ts",
        "src/features/comments/api/post-comment.ts",
        "src/features/comments/api/delete-comment.ts",
    ]) == "feature-module",
)

# testing/mocks/ → test-mock
t(
    "testing/mocks/ → test-mock",
    _ts_prior_match([
        "src/testing/mocks/handlers/users.ts",
        "src/testing/mocks/handlers/comments.ts",
    ]) == "test-mock",
)

# mocks/handlers/ (standalone) → test-mock-handler
t(
    "mocks/handlers/ → test-mock-handler",
    _ts_prior_match([
        "src/mocks/handlers/users.ts",
        "src/mocks/handlers/comments.ts",
    ]) == "test-mock-handler",
)

# icons/ → icon-set
t(
    "icons/ → icon-set",
    _ts_prior_match([
        "src/icons/brand/index.ts",
        "src/icons/brand/logo.ts",
        "src/icons/brand/mark.ts",
    ]) == "icon-set",
)

# locales/ → locale-table
t(
    "locales/ → locale-table",
    _ts_prior_match([
        "src/locales/cs/editor.ts",
        "src/locales/en/editor.ts",
    ]) == "locale-table",
)

# i18n/ → locale-table (alias)
t(
    "i18n/ → locale-table (alias)",
    _ts_prior_match([
        "src/i18n/en.ts",
        "src/i18n/cs.ts",
    ]) == "locale-table",
)

# constants/ → constants-module
t(
    "constants/ → constants-module",
    _ts_prior_match([
        "src/constants/api.ts",
        "src/constants/colors.ts",
    ]) == "constants-module",
)

# schema/ → schema-module
t(
    "schema/ → schema-module",
    _ts_prior_match([
        "src/schema/user.ts",
        "src/schema/order.ts",
    ]) == "schema-module",
)

# schemas/ (plural) → schema-module
t(
    "schemas/ (plural) → schema-module",
    _ts_prior_match([
        "src/schemas/user.ts",
        "src/schemas/order.ts",
    ]) == "schema-module",
)

# providers/ → provider
t(
    "providers/ → provider",
    _ts_prior_match([
        "src/providers/auth.tsx",
        "src/providers/theme.tsx",
    ]) == "provider",
)

# contexts/ → context
t(
    "contexts/ → context",
    _ts_prior_match([
        "src/contexts/AuthContext.tsx",
        "src/contexts/ThemeContext.tsx",
    ]) == "context",
)

# layouts/ → layout
t(
    "layouts/ → layout",
    _ts_prior_match([
        "src/layouts/AppLayout.tsx",
        "src/layouts/PublicLayout.tsx",
    ]) == "layout",
)

# config/ → config-module
t(
    "config/ → config-module",
    _ts_prior_match([
        "src/config/env.ts",
        "src/config/api.ts",
    ]) == "config-module",
)

# configs/ (plural) → config-module
t(
    "configs/ (plural) → config-module",
    _ts_prior_match([
        "src/configs/env.ts",
        "src/configs/api.ts",
    ]) == "config-module",
)


# ---------------------------------------------------------------------------
section("Integration: propose_archetype_name with workspace_roots")

# Verify-before: a Turborepo cluster with paths
# `apps/web/src/components/Foo.tsx` would NOT match the `components/`
# prior rule because the workspace prefix isn't stripped.
# Verify-after: passing workspace_roots strips the prefix and the
# cluster is named `component`.

cluster = _Cluster(
    _Key(path_pattern_bucket="apps/web/src/components"),
    [
        "apps/web/src/components/Foo.tsx",
        "apps/web/src/components/Bar.tsx",
        "apps/web/src/components/Baz.tsx",
    ],
)
name = propose_archetype_name(
    cluster, set(), workspace_roots=["apps/web", "apps/api"]
)
t(
    "Turborepo cluster with explicit workspace_roots → component",
    name == "component",
    f"got {name!r}",
)

# Same cluster WITHOUT explicit roots — should still get `component`
# via the path-shape fallback.
name = propose_archetype_name(cluster, set())
t(
    "Turborepo cluster without explicit workspace_roots → component (fallback)",
    name == "component",
    f"got {name!r}",
)


# ---------------------------------------------------------------------------
section("Regression: flat-repo naming unchanged")

# Verify-after: a flat-repo cluster (no workspace prefix) should produce
# the same name regardless of whether workspace_roots is passed. This
# guards against the v0.5.4 strip helper accidentally affecting flat
# repos.

flat = _Cluster(
    _Key(path_pattern_bucket="src/components"),
    [
        "src/components/Foo.tsx",
        "src/components/Bar.tsx",
        "src/components/Baz.tsx",
    ],
)
name_no_roots = propose_archetype_name(flat, set())
name_with_roots = propose_archetype_name(
    flat, set(), workspace_roots=["apps/web"]
)
t(
    "flat repo: same name with vs without workspace_roots",
    name_no_roots == name_with_roots == "component",
    f"no_roots={name_no_roots!r}, with_roots={name_with_roots!r}",
)


# ---------------------------------------------------------------------------
section("Most-specific-wins still applies after strip")

# A path like `apps/web/src/app/api/users/route.ts` should still hit
# `app-route-handler` (the most-specific rule), not just `app-page-component`.
cluster = _Cluster(
    _Key(path_pattern_bucket="apps/web/src/app/api"),
    [
        "apps/web/src/app/api/users/route.ts",
        "apps/web/src/app/api/orders/route.ts",
    ],
)
name = propose_archetype_name(cluster, set())
t(
    "Turborepo Next.js: app/api/route.ts → app-route-handler (most-specific)",
    name == "app-route-handler",
    f"got {name!r}",
)


print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")

sys.exit(0 if FAIL == 0 else 1)
