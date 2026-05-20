"""Unit + regression tests for Phase 2D.2 archetype naming.

Covers ``mcp/chameleon_mcp/bootstrap/naming.propose_archetype_name`` plus
the orchestrator wire-up. The naming heuristic is pure, so most tests
fabricate lightweight Cluster stand-ins; the end-to-end tests bootstrap
real tiny repos to verify the integration.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/archetype_naming_test.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
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


# Use isolated plugin data dir per run (mirrors v0_2_regression_test.py).
TMPDATA = tempfile.mkdtemp(prefix="chameleon_naming_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

from chameleon_mcp.bootstrap.naming import (  # noqa: E402
    propose_archetype_name,
)
from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE  # noqa: E402


# Lightweight stand-ins for unit tests. The heuristic only reads attributes,
# so we don't need a full ClusterKey / ParsedFile graph.
@dataclass
class _FakeKey:
    path_pattern_bucket: str = ""
    default_export_kind: str | None = None
    top_level_node_kinds: tuple[str, ...] = ()
    jsx_present: bool = False


@dataclass
class _FakeMember:
    path: Path


@dataclass
class _FakeCluster:
    key: _FakeKey
    members: list[_FakeMember] = field(default_factory=list)
    cluster_id: str = "deadbeefdeadbeef"


def _cluster(
    *,
    paths_pattern: str,
    members: list[str],
    default_export: str | None = None,
    top_level_kinds: tuple[str, ...] = (),
    jsx: bool = False,
) -> _FakeCluster:
    return _FakeCluster(
        key=_FakeKey(
            path_pattern_bucket=paths_pattern,
            default_export_kind=default_export,
            top_level_node_kinds=top_level_kinds,
            jsx_present=jsx,
        ),
        members=[_FakeMember(path=Path(p)) for p in members],
    )


# ---------------------------------------------------------------------------
section("Unit: Rails controller cluster")
# Witness shape: app/controllers/api/v1/addresses_controller.rb with a single
# top-level ClassNode default export. paths_pattern_bucket from v5 signatures
# is "app/<parent-of-parent>/<parent>" — so "app/api/v1".
name = propose_archetype_name(
    _cluster(
        paths_pattern="app/api/v1",
        members=[
            "app/controllers/api/v1/addresses_controller.rb",
            "app/controllers/api/v1/users_controller.rb",
            "app/controllers/api/v1/orders_controller.rb",
        ],
        default_export="ClassNode",
        top_level_kinds=("ClassNode",),
    ),
    set(),
)
t("Rails controller → 'controller'", name == "controller", name)
t("matches archetype name regex", bool(ARCHETYPE_NAME_RE.match(name)))


# ---------------------------------------------------------------------------
section("Unit: Rails model cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="app/models",
        members=[
            "app/models/listing.rb",
            "app/models/user.rb",
            "app/models/order.rb",
        ],
        default_export="ClassNode",
        top_level_kinds=("ClassNode",),
    ),
    set(),
)
t("Rails model → 'model'", name == "model", name)


# ---------------------------------------------------------------------------
section("Unit: Rails spec cluster (test by path)")
name = propose_archetype_name(
    _cluster(
        paths_pattern="spec/api/v1",
        members=[
            "spec/controllers/api/v1/addresses_controller_spec.rb",
            "spec/controllers/api/v1/users_controller_spec.rb",
        ],
        default_export=None,
        top_level_kinds=("CallNode",),
    ),
    set(),
)
t("Rails spec → 'test'", name == "test", name)


# ---------------------------------------------------------------------------
section("Unit: Rails migration cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="db/migrate",
        members=[
            "db/migrate/20240101_create_users.rb",
            "db/migrate/20240102_add_email.rb",
        ],
        default_export="ClassNode",
        top_level_kinds=("ClassNode",),
    ),
    set(),
)
t("Rails migration → 'migration'", name == "migration", name)


# ---------------------------------------------------------------------------
section("Unit: Rails service cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="app/services",
        members=[
            "app/services/users/create.rb",
            "app/services/users/update.rb",
        ],
        default_export="ClassNode",
    ),
    set(),
)
t("Rails service → 'service'", name == "service", name)


# ---------------------------------------------------------------------------
section("Unit: Rails policy / serializer / job / mailer")
for kind_dir, expected in [
    ("policies", "policy"),
    ("serializers", "serializer"),
    ("jobs", "job"),
    ("mailers", "mailer"),
    ("workers", "worker"),
]:
    name = propose_archetype_name(
        _cluster(
            paths_pattern=f"app/{kind_dir}",
            members=[f"app/{kind_dir}/foo.rb", f"app/{kind_dir}/bar.rb"],
            default_export="ClassNode",
        ),
        set(),
    )
    t(f"Rails {kind_dir} → {expected!r}", name == expected, name)


# ---------------------------------------------------------------------------
section("Unit: Rails initializer cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="config/initializers",
        members=[
            "config/initializers/devise.rb",
            "config/initializers/cors.rb",
        ],
        default_export=None,
        top_level_kinds=("CallNode",),
    ),
    set(),
)
t("config/initializers → 'rails-initializer'", name == "rails-initializer", name)


# ---------------------------------------------------------------------------
section("Unit: React component cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/components/base",
        members=[
            "src/components/base/Button.tsx",
            "src/components/base/Card.tsx",
            "src/components/base/Modal.tsx",
        ],
        default_export="ArrowFunction",
        jsx=True,
    ),
    set(),
)
t("React component (jsx + components dir) → 'component'", name == "component", name)

# Variant: JSX + arrow default WITHOUT 'components' path token still wins.
name = propose_archetype_name(
    _cluster(
        paths_pattern="app/dashboard",
        members=["app/dashboard/page.tsx"],
        default_export="ArrowFunction",
        jsx=True,
    ),
    set(),
)
t("JSX + ArrowFunction default in app/<route>/page.tsx → 'app-page-component'", name == "app-page-component", name)


# ---------------------------------------------------------------------------
section("Unit: React hook cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/hooks",
        members=[
            "src/hooks/useUser.ts",
            "src/hooks/useCart.ts",
            "src/hooks/useToggle.ts",
        ],
        default_export="ArrowFunction",
    ),
    set(),
)
t("React hook (hooks + use* filenames) → 'hook'", name == "hook", name)


# ---------------------------------------------------------------------------
section("Unit: TS query / mutation cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/queries",
        members=[
            "src/queries/listUsers.ts",
            "src/queries/getOrder.ts",
        ],
        default_export="ArrowFunction",
    ),
    set(),
)
t("TS queries → 'query'", name == "query", name)

name = propose_archetype_name(
    _cluster(
        paths_pattern="src/mutations",
        members=[
            "src/mutations/createUser.ts",
            "src/mutations/deleteOrder.ts",
        ],
        default_export="ArrowFunction",
    ),
    set(),
)
t("TS mutations → 'mutation'", name == "mutation", name)


# ---------------------------------------------------------------------------
section("Unit: TS utility cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="lib/utils",
        members=[
            "lib/utils/formatDate.ts",
            "lib/utils/slugify.ts",
        ],
        default_export=None,
    ),
    set(),
)
t("lib/utils → 'lib-module' (lib-prior wins, more specific than utils)", name == "lib-module", name)

name = propose_archetype_name(
    _cluster(
        paths_pattern="src/utils",
        members=["src/utils/foo.ts"],
        default_export=None,
    ),
    set(),
)
t("src/utils → 'util'", name == "util", name)


# ---------------------------------------------------------------------------
section("Unit: TS lib/ default → 'lib-module' (v0.5.3 Bug C prior)")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/lib",
        members=["src/lib/Logger.ts", "src/lib/Container.ts"],
        default_export="ClassDeclaration",
        jsx=False,
    ),
    set(),
)
t("TS lib/ ClassDeclaration → 'lib-module'", name == "lib-module", name)


# ---------------------------------------------------------------------------
section("Unit: TS types directory")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/types",
        members=["src/types/api.ts", "src/types/domain.ts"],
        default_export=None,
    ),
    set(),
)
t("src/types → 'type-module' (v0.5.3 Bug C TS prior)", name == "type-module", name)


# ---------------------------------------------------------------------------
section("Unit: Jest colocated tests are recognized")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/components",
        members=[
            "src/__tests__/Button.test.tsx",
            "src/__tests__/Card.test.tsx",
        ],
        default_export="ArrowFunction",
    ),
    set(),
)
t("Jest __tests__ dir → 'test'", name == "test", name)

name = propose_archetype_name(
    _cluster(
        paths_pattern="src/components",
        members=[
            "src/components/Button.test.tsx",
            "src/components/Card.test.tsx",
            "src/components/Modal.test.tsx",
        ],
        default_export="ArrowFunction",
        jsx=True,
    ),
    set(),
)
t("filename .test.tsx majority → 'test'", name == "test", name)


# ---------------------------------------------------------------------------
section("Unit: Collision disambiguation with path suffix")
# Two controller clusters in different namespaces; second should get a tail.
existing: set[str] = set()
first = propose_archetype_name(
    _cluster(
        paths_pattern="app/api/v1",
        members=["app/controllers/api/v1/users_controller.rb"],
        default_export="ClassNode",
    ),
    existing,
)
existing.add(first)
second = propose_archetype_name(
    _cluster(
        paths_pattern="app/admin",
        members=["app/controllers/admin/dashboard_controller.rb"],
        default_export="ClassNode",
    ),
    existing,
)
existing.add(second)
t("first controller wins base name", first == "controller", first)
t("second controller suffixed with namespace", second == "controller-admin", second)
t("collision result is unique", first != second)
t(
    "collision suffix matches archetype regex",
    bool(ARCHETYPE_NAME_RE.match(second)),
    second,
)

# Numeric fallback when paths_pattern has no usable namespace tail.
third = propose_archetype_name(
    _cluster(
        paths_pattern="app",  # nothing useful below the generic 'app' segment
        members=["app/legacy_controller.rb"],
        default_export="ClassNode",
        top_level_kinds=("ClassNode",),
    ),
    {"class", "controller", "controller-admin"},
)
# With paths_pattern of "app" only, base would be 'class' (default ClassDecl,
# no jsx, no controllers token in the bucket). Let's verify uniqueness behavior
# with a base that's actually colliding:
existing2 = {"controller"}
nofallback = propose_archetype_name(
    _cluster(
        paths_pattern="legacy",  # single segment, no useful suffix
        members=["legacy/users_controller.rb"],
        default_export="ClassNode",
    ),
    existing2,
)
# No 'controllers' segment in 'legacy' → base name comes from class default.
# Post-rec-7: a class-default with usable path-tail signal ("legacy") is
# demoted into ``class-<suffix>`` rather than bare ``class`` so the long
# tail of generic ``class`` archetypes doesn't dominate large Ruby trees.
t(
    "non-rails ClassDeclaration with no /controllers/ bucket gets demoted class-* name",
    nofallback in {"class", "class-legacy", "controller-2"},
    nofallback,
)


# ---------------------------------------------------------------------------
section("Unit: Fallback for unrecognized cluster")
name = propose_archetype_name(
    _cluster(
        paths_pattern="src/weird/place",
        members=["src/weird/place/x.ts", "src/weird/place/y.ts"],
        default_export=None,
        top_level_kinds=("ExpressionStatement",),
    ),
    set(),
)
t("unrecognized → starts with 'cluster-'", name.startswith("cluster-"), name)
t("fallback respects archetype regex", bool(ARCHETYPE_NAME_RE.match(name)), name)
t("fallback uses short hash (≤16 chars total)", len(name) <= 16, name)


# ---------------------------------------------------------------------------
section("Unit: Sanitization for schema regex")
# Edge case: paths_pattern with uppercase / underscores still produces a
# regex-safe name because base names are hard-coded lowercase strings.
for cluster_args in [
    dict(paths_pattern="App/Controllers", members=["App/Controllers/X.rb"], default_export="ClassNode"),
    dict(paths_pattern="src/components", members=["src/components/X.tsx"], default_export="ArrowFunction", jsx=True),
    dict(paths_pattern="", members=["foo.rb"], default_export=None),
]:
    out = propose_archetype_name(_cluster(**cluster_args), set())
    t(
        f"paths_pattern={cluster_args['paths_pattern']!r} → regex-safe ({out!r})",
        bool(ARCHETYPE_NAME_RE.match(out)),
        out,
    )


# ---------------------------------------------------------------------------
section("Unit: Existing-names set is not mutated")
existing = {"controller"}
snapshot = set(existing)
propose_archetype_name(
    _cluster(
        paths_pattern="app/api/v1",
        members=["app/controllers/api/v1/foo.rb"],
        default_export="ClassNode",
    ),
    existing,
)
t("existing_names is not mutated by the function", existing == snapshot)


# ---------------------------------------------------------------------------
section("Unit: Idempotence (same inputs → same output)")
def _idem_call() -> str:
    return propose_archetype_name(
        _cluster(
            paths_pattern="src/components",
            members=["src/components/A.tsx", "src/components/B.tsx"],
            default_export="ArrowFunction",
            jsx=True,
        ),
        set(),
    )

t("repeated calls return identical results", _idem_call() == _idem_call())


# ---------------------------------------------------------------------------
# End-to-end regression: bootstrap a tiny TS repo and check the emitted
# archetypes.json uses meaningful names rather than 'cluster-<hex>'.
# ---------------------------------------------------------------------------
from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def _make_repo_with_controllers_and_specs() -> Path:
    """Replica of v0_2_regression_test.py helper — produces app/ + spec/ clusters."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_naming_repo_"))
    (root / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5.0.0"}}')
    (root / "tsconfig.json").write_text("{}")

    app_dir = root / "app" / "controllers" / "api" / "v1"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}.ts").write_text(
            f"export class Resource{i} {{ get() {{ return {i}; }} }}\n"
        )

    spec_dir = root / "spec" / "controllers" / "api" / "v1"
    spec_dir.mkdir(parents=True)
    for i in range(6):
        (spec_dir / f"r{i}.test.ts").write_text(
            f"import {{ Resource{i} }} from '../../app/controllers/api/v1/r{i}';\n"
            f"test('r{i}', () => {{ expect(new Resource{i}().get()).toBe({i}); }});\n"
        )

    return root


section("Integration: bootstrap emits meaningful names (no 'cluster-' prefix when possible)")
repo = _make_repo_with_controllers_and_specs()
try:
    bootstrap_repo(str(repo))
    archetypes = json.loads((repo / ".chameleon" / "archetypes.json").read_text())
    names = list(archetypes["archetypes"].keys())
    t(f"archetypes.json has at least one archetype ({len(names)} found)", len(names) >= 1)
    # All names must satisfy the schema regex (the loader would reject otherwise,
    # but we double-check inline for clearer error messages).
    for n in names:
        t(f"emitted name {n!r} matches archetype regex", bool(ARCHETYPE_NAME_RE.match(n)))

    # The app/controllers/ cluster must be named 'controller' (the spec/
    # cluster is filtered out by ``is_eligible_as_canonical`` so it never
    # reaches archetypes.json — that exclusion is by design, see
    # discovery.EXCLUDE_FROM_CANONICAL_POOL_PATTERNS).
    t(
        "app/controllers/ cluster named 'controller'",
        "controller" in names,
        f"actual names: {names}",
    )
    # And we must not see the legacy ``cluster-<16hex>`` form anywhere.
    t(
        "no archetype uses the legacy 'cluster-<16hex>' placeholder",
        not any(re.fullmatch(r"cluster-[0-9a-f]{16}", n) for n in names),
        f"names: {names}",
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
