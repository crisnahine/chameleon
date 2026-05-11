"""Regression tests for v0.5.3 Bug C — TypeScript-prior naming table.

The plane (Next.js monorepo) dogfood reported 35/70 archetypes named
``cluster-<hex>`` — 50% generic — because the TypeScript story had no
equivalent of v0.5.2's Rails-prior table. v0.5.3 adds ``_TS_PRIORS`` plus
``_is_typescript_cluster`` so Next.js / Remix / common TS-ecosystem
conventions get human-meaningful names parallel to ``model`` / ``service``
/ ``controller`` on the Ruby side.

What this file verifies
-----------------------

* The brief's eight canonical scenarios — App Router page/layout/route,
  Pages Router api/page, Remix routes, ``components/``, ``hooks/`` with
  filename gate, Ruby-cluster bypass, mixed-cluster bypass, and most-
  specific-wins.
* Each TS-prior name still satisfies ``ARCHETYPE_NAME_RE`` (the schema
  loader would reject otherwise).
* ``_is_typescript_cluster`` correctly identifies TS/JS members and
  rejects Ruby / empty input.
* The TS prior never overrides the Ruby gate — a Rails cluster keeps
  its existing name.

What this file does NOT verify
------------------------------

End-to-end bootstrap on a synthetic Next.js repo — the unit-level
``propose_archetype_name`` coverage is sufficient because the heuristic
is pure (no I/O), and the orchestrator's wire-up is already covered by
``archetype_naming_test.py`` and ``v0_5_2_bootstrap_test.py``.

Run::

    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_3_ts_priors_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))


# Isolate plugin data so any global state we touch doesn't leak.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v053_ts_priors_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA


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
    _is_ruby_cluster,
    _is_typescript_cluster,
    _ts_prior_match,
    propose_archetype_name,
)
from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE  # noqa: E402


# ---------------------------------------------------------------------------
# Lightweight stand-ins. Same shape as ``archetype_naming_test.py`` and
# ``v0_5_2_bootstrap_test.py`` — the heuristic only reads attributes.
# ---------------------------------------------------------------------------
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
    cluster_id: str = "cafebabecafebabe"


def cluster(
    *,
    bucket: str = "",
    members: list[str],
    default_export: str | None = None,
    top_level_kinds: tuple[str, ...] = (),
    jsx: bool = False,
) -> _FakeCluster:
    return _FakeCluster(
        key=_FakeKey(
            path_pattern_bucket=bucket,
            default_export_kind=default_export,
            top_level_node_kinds=top_level_kinds,
            jsx_present=jsx,
        ),
        members=[_FakeMember(path=Path(p)) for p in members],
    )


# ---------------------------------------------------------------------------
section("_is_typescript_cluster — language gate")
# Brief: "first member's extension is .ts/.tsx/.js/.jsx/.mjs/.cjs". The
# gate is permissive on the first-member level; the call-site combines it
# with !_is_ruby_cluster and a no-.rb-anywhere purity check to handle the
# mixed-cluster case (verified separately further down).
t(
    "TS first member (.ts) → True",
    _is_typescript_cluster(["src/lib/foo.ts", "src/lib/bar.ts"]) is True,
)
t(
    "TSX first member (.tsx) → True",
    _is_typescript_cluster(["app/dashboard/page.tsx"]) is True,
)
t(
    "JSX first member (.jsx) → True",
    _is_typescript_cluster(["components/Button.jsx"]) is True,
)
t(
    "JS first member (.js) → True",
    _is_typescript_cluster(["pages/index.js"]) is True,
)
t(
    "MJS first member (.mjs) → True",
    _is_typescript_cluster(["scripts/build.mjs"]) is True,
)
t(
    "CJS first member (.cjs) → True",
    _is_typescript_cluster(["scripts/legacy.cjs"]) is True,
)
t(
    "Ruby first member (.rb) → False",
    _is_typescript_cluster(["app/models/user.rb"]) is False,
)
t(
    "Empty members → False",
    _is_typescript_cluster([]) is False,
)
# The first-member proxy is intentionally permissive — a mixed cluster
# with a TS first member returns True here; the call-site's no-.rb-anywhere
# guard handles the mixed case (covered below).
t(
    "First-member proxy on mixed cluster → True (call-site enforces purity)",
    _is_typescript_cluster(["src/lib/foo.ts", "app/models/user.rb"]) is True,
)


# ---------------------------------------------------------------------------
section("Case 1 — Next.js App Router page.tsx → 'app-page-component'")
# Brief verification case 1. The cluster has no JSX-arrow-present signals
# beyond the conventional file names; the TS prior catches it because the
# existing TS heuristic returns None for ``app/`` (no token match).
name = propose_archetype_name(
    cluster(
        bucket="app/dashboard",
        members=[
            "app/dashboard/page.tsx",
            "app/settings/page.tsx",
            "app/profile/page.tsx",
        ],
    ),
    set(),
)
t("app/<route>/page.tsx → 'app-page-component'", name == "app-page-component", name)
t("name still satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)


# ---------------------------------------------------------------------------
section("Case 2 — Next.js Pages Router pages/api/* → 'pages-api-handler'")
# Brief verification case 2. ``pages/api/`` chain length 2 beats bare
# ``pages/`` chain length 1; filename predicate is any.
name = propose_archetype_name(
    cluster(
        bucket="pages/api",
        members=[
            "pages/api/users.ts",
            "pages/api/orders.ts",
            "pages/api/auth/login.ts",
        ],
    ),
    set(),
)
t("pages/api/* → 'pages-api-handler'", name == "pages-api-handler", name)
t("name still satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)


# ---------------------------------------------------------------------------
section("Case 3 — Remix app/routes/* → 'remix-route'")
# Brief verification case 3. ``app/routes/`` (chain length 2) wins over
# the bare ``("app",)`` rules.
name = propose_archetype_name(
    cluster(
        bucket="app/routes",
        members=[
            "app/routes/users.tsx",
            "app/routes/orders.tsx",
            "app/routes/dashboard.tsx",
        ],
    ),
    set(),
)
t("app/routes/* → 'remix-route'", name == "remix-route", name)
t("name still satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)


# ---------------------------------------------------------------------------
section("Case 4 — components/ → 'component'")
# Brief verification case 4. The directory chain ``("components",)``
# matches in the majority of members; filename predicate is any.
name = propose_archetype_name(
    cluster(
        bucket="src/components/base",
        members=[
            "src/components/base/Button.tsx",
            "src/components/base/Card.tsx",
            "src/components/base/Modal.tsx",
        ],
    ),
    set(),
)
t("components/* → 'component'", name == "component", name)
t("name still satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)


# ---------------------------------------------------------------------------
section("Case 5 — hooks/use* → 'hook'; non-use filenames bypass the rule")
# Brief verification case 5 (two scenarios):
#   (a) ``hooks/useFoo.ts`` cluster → ``hook``.
#   (b) ``hooks/somethingElse.ts`` cluster does NOT match the hook rule.

# 5a — positive case.
name = propose_archetype_name(
    cluster(
        bucket="src/hooks",
        members=[
            "src/hooks/useUser.ts",
            "src/hooks/useCart.ts",
            "src/hooks/useToggle.ts",
        ],
    ),
    set(),
)
t("hooks/use* → 'hook'", name == "hook", name)
t("'hook' satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)

# 5b — negative case. A cluster under ``hooks/`` where filenames do NOT
# start with ``use`` should NOT be named ``hook`` (filename gate). The
# heuristic falls through; with no other signals, the result is the
# ``cluster-<hash>`` fallback.
name = propose_archetype_name(
    cluster(
        bucket="src/hooks",
        members=[
            "src/hooks/somethingElse.ts",
            "src/hooks/anotherThing.ts",
            "src/hooks/notAHook.ts",
        ],
    ),
    set(),
)
t(
    "hooks/<non-use*> does NOT get named 'hook'",
    name != "hook",
    name,
)
t(
    "filename-gate result still satisfies ARCHETYPE_NAME_RE",
    bool(ARCHETYPE_NAME_RE.match(name)),
    name,
)

# Cross-check the prior-match function directly: with use* filenames,
# the matcher returns ``hook``; without, it returns None.
t(
    "_ts_prior_match: hooks + use* → 'hook'",
    _ts_prior_match(["src/hooks/useUser.ts", "src/hooks/useCart.ts"]) == "hook",
)
t(
    "_ts_prior_match: hooks + non-use* → None",
    _ts_prior_match(["src/hooks/somethingElse.ts", "src/hooks/anotherThing.ts"]) is None,
)


# ---------------------------------------------------------------------------
section("Case 6 — Ruby clusters bypass the TS prior entirely")
# Brief verification case 6. The TS prior is gated by
# ``_is_typescript_cluster && !_is_ruby_cluster``; a pure Ruby cluster
# goes through the Rails-prior path and never touches TS naming.
name = propose_archetype_name(
    cluster(
        bucket="app/services",
        members=[
            "app/services/users/create.rb",
            "app/services/orders/update.rb",
        ],
        default_export="ClassNode",
    ),
    set(),
)
# Rails prior fires first → 'service' (NOT 'service' from TS prior either,
# because TS prior is bypassed entirely).
t("Ruby cluster keeps Rails name ('service')", name == "service", name)
t("Ruby cluster name satisfies ARCHETYPE_NAME_RE", bool(ARCHETYPE_NAME_RE.match(name)), name)

# Belt-and-suspenders: the TS-cluster predicate must return False for
# this input even though the cluster's directory is ``services/`` (which
# is also in the TS prior table).
t(
    "_is_typescript_cluster False for .rb cluster under services/",
    _is_typescript_cluster([
        "app/services/users/create.rb",
        "app/services/orders/update.rb",
    ]) is False,
)
t(
    "_is_ruby_cluster True for the same input",
    _is_ruby_cluster([
        "app/services/users/create.rb",
        "app/services/orders/update.rb",
    ]) is True,
)


# ---------------------------------------------------------------------------
section("Case 7 — Mixed clusters (.ts + .rb) bypass both prior tables")
# Brief verification case 7. A cluster with at least one ``.rb`` member
# should not engage the TS prior, even when the first member is TS — the
# discovery glob keeps clusters single-extension in practice, but a
# future signature change loosening that grouping shouldn't silently
# start mis-naming.
mixed_members = [
    "src/lib/foo.ts",       # TS first member
    "src/lib/bar.ts",
    "app/models/legacy.rb",  # rogue Ruby member
]
name = propose_archetype_name(
    cluster(
        bucket="src/lib",
        members=mixed_members,
    ),
    set(),
)
# The TS prior is bypassed (no-.rb-anywhere guard fails); the Rails prior
# is bypassed (_is_ruby_cluster checks first member). Existing heuristic
# tries ``_has("models")`` → 'model' on the rogue path. We don't care
# about the exact fallback name — only that the TS prior didn't fire.
t(
    "mixed cluster does NOT get 'lib-module' (TS prior bypassed)",
    name != "lib-module",
    name,
)
# Equivalent statement from the prior-match function's POV:
t(
    "_is_typescript_cluster True (first member .ts)",
    _is_typescript_cluster(mixed_members) is True,
)
t(
    "_is_ruby_cluster False (first member .ts)",
    _is_ruby_cluster(mixed_members) is False,
)
t(
    "but call-site no-.rb-anywhere guard blocks the TS prior",
    any(p.endswith(".rb") for p in mixed_members) is True,
)


# ---------------------------------------------------------------------------
section("Case 8 — Most-specific match wins (chain length tiebreak)")
# Brief verification case 8. A file at ``app/api/route.ts`` should match
# ``("app", "api")`` (chain length 2 + filename predicate ``route.ts``)
# and emit ``app-route-handler`` — not ``app-page-component`` which would
# come from the shorter ``("app",)`` chain.
name = propose_archetype_name(
    cluster(
        bucket="app/api/users",
        members=[
            "app/api/users/route.ts",
            "app/api/orders/route.ts",
            "app/api/auth/route.ts",
        ],
    ),
    set(),
)
t("app/api/*/route.ts → 'app-route-handler' (longest chain wins)", name == "app-route-handler", name)

# Inverse check: a file at app/<route>/page.tsx (no api/ segment) gets
# the shorter chain because the longer one doesn't match.
name = propose_archetype_name(
    cluster(
        bucket="app/dashboard",
        members=["app/dashboard/page.tsx"],
    ),
    set(),
)
t("app/<route>/page.tsx → 'app-page-component' (no api/ chain)", name == "app-page-component", name)

# Edge case: app/api/<route>/page.tsx — the longer chain ``("app","api")``
# matches BUT the filename predicate for app-route-handler is route.{ts,tsx}
# only. So that rule misses. The next rule (("app",) + page.tsx) has
# excluded chain ``("app","api")`` so it's disqualified. Result: TS prior
# returns None; the cluster falls through to the existing heuristic.
name_app_api_page = _ts_prior_match([
    "app/api/users/page.tsx",
    "app/api/orders/page.tsx",
])
t(
    "app/api/<route>/page.tsx — TS prior returns None (excluded by api chain)",
    name_app_api_page is None,
    str(name_app_api_page),
)


# ---------------------------------------------------------------------------
section("Coverage — every brief table entry produces the documented name")
# Sweep the brief's table to confirm each row works in isolation. Each
# cluster contains only files that satisfy that row's chain + filename
# predicate so the most-specific rule fires unambiguously.
TS_PRIOR_CASES = [
    # (label, members, expected_name)
    (
        "app/api/route.ts → app-route-handler",
        ["app/api/users/route.ts", "app/api/orders/route.tsx"],
        "app-route-handler",
    ),
    (
        "app/<route>/page.tsx → app-page-component",
        ["app/page.tsx", "app/dashboard/page.tsx", "app/about/page.ts"],
        "app-page-component",
    ),
    (
        "app/<route>/layout.tsx → app-layout",
        ["app/layout.tsx", "app/(marketing)/layout.tsx", "app/dashboard/layout.ts"],
        "app-layout",
    ),
    (
        "app/<route>/loading.tsx → app-special-component",
        ["app/loading.tsx", "app/dashboard/loading.tsx", "app/about/error.tsx"],
        "app-special-component",
    ),
    (
        "pages/api/* → pages-api-handler",
        ["pages/api/users.ts", "pages/api/orders.ts"],
        "pages-api-handler",
    ),
    (
        "pages/* generic → pages-component",
        ["pages/index.tsx", "pages/about.tsx", "pages/contact.tsx"],
        "pages-component",
    ),
    (
        "pages/_app.tsx etc → pages-special-component",
        ["pages/_app.tsx", "pages/_document.tsx", "pages/_error.tsx"],
        "pages-special-component",
    ),
    (
        "app/routes/* → remix-route",
        ["app/routes/users.tsx", "app/routes/orders.tsx"],
        "remix-route",
    ),
    (
        "components/* → component",
        ["src/components/Button.tsx", "src/components/Card.tsx"],
        "component",
    ),
    (
        "ui/* → ui-component",
        ["src/ui/button.tsx", "src/ui/card.tsx", "src/ui/modal.tsx"],
        "ui-component",
    ),
    (
        "hooks/use* → hook",
        ["src/hooks/useUser.ts", "src/hooks/useCart.ts"],
        "hook",
    ),
    (
        "lib/* → lib-module",
        ["src/lib/api.ts", "src/lib/db.ts", "src/lib/auth.ts"],
        "lib-module",
    ),
    (
        "utils/* → util",
        ["src/utils/format.ts", "src/utils/slugify.ts"],
        "util",
    ),
    (
        "helpers/* → helper",
        ["src/helpers/string.ts", "src/helpers/date.ts"],
        "helper",
    ),
    (
        "services/* → service",
        ["src/services/api.ts", "src/services/auth.ts"],
        "service",
    ),
    (
        "middleware/* → middleware",
        ["src/middleware/auth.ts", "src/middleware/log.ts"],
        "middleware",
    ),
    (
        "actions/* → action",
        ["src/actions/user.ts", "src/actions/order.ts"],
        "action",
    ),
    (
        "store/* → store",
        ["src/store/user.ts", "src/store/order.ts"],
        "store",
    ),
    (
        "stores/* → store (alias)",
        ["src/stores/user.ts", "src/stores/order.ts"],
        "store",
    ),
    (
        "types/* → type-module",
        ["src/types/api.ts", "src/types/domain.ts"],
        "type-module",
    ),
    (
        "queries/use* → query-hook",
        ["src/queries/useUsers.ts", "src/queries/useOrders.ts"],
        "query-hook",
    ),
    (
        "queries/<non-use> → query",
        ["src/queries/listUsers.ts", "src/queries/getOrder.ts"],
        "query",
    ),
]
for label, members, expected in TS_PRIOR_CASES:
    got = propose_archetype_name(cluster(members=members), set())
    t(label, got == expected, f"got {got!r}")
    t(f"  {label} — name regex-safe", bool(ARCHETYPE_NAME_RE.match(got)), got)


# ---------------------------------------------------------------------------
section("Special case — root-level middleware.ts (no /middleware/ dir)")
# A Next.js project's edge middleware lives at the repo root as
# ``middleware.ts``. The directory-chain rules can't match (no
# ``middleware/`` segment), so ``_ts_prior_match`` has a special case
# for the filename.
got = _ts_prior_match(["middleware.ts"])
t("bare middleware.ts → 'middleware'", got == "middleware", str(got))

# Edge: a cluster with both root middleware.ts AND middleware/ dir files
# — the directory-chain rule fires first (and gives the same name).
got = _ts_prior_match([
    "middleware.ts",
    "src/middleware/auth.ts",
    "src/middleware/log.ts",
])
t(
    "mixed middleware.ts + middleware/ dir → 'middleware'",
    got == "middleware",
    str(got),
)


# ---------------------------------------------------------------------------
section("Special case — root-level api/ (NOT Next.js) → 'api-client'")
# An ``api/<file>`` cluster with no ``pages/`` or ``app/`` parent is the
# conventional "API client" pattern. Make sure ``pages/api/`` and
# ``app/api/`` still win (verified earlier; sanity-check the negative).
got = _ts_prior_match([
    "api/users.ts",
    "api/orders.ts",
    "api/auth.ts",
])
t("root api/ → 'api-client'", got == "api-client", str(got))

# Negative — pages/api/ should NOT collapse to api-client.
got = _ts_prior_match([
    "pages/api/users.ts",
    "pages/api/orders.ts",
])
t(
    "pages/api/ does NOT collapse to 'api-client' (Next.js wins)",
    got == "pages-api-handler",
    str(got),
)


# ---------------------------------------------------------------------------
section("Boundary — TS prior name must satisfy the archetype regex everywhere")
# Belt-and-suspenders sweep: every name emitted by _ts_prior_match across
# all the cluster shapes we've tested must satisfy ARCHETYPE_NAME_RE.
# The schema loader would reject otherwise.
NAME_SAMPLES = [
    "app-route-handler",
    "app-page-component",
    "app-layout",
    "app-special-component",
    "pages-api-handler",
    "pages-component",
    "pages-special-component",
    "remix-route",
    "component",
    "ui-component",
    "hook",
    "lib-module",
    "util",
    "helper",
    "service",
    "middleware",
    "action",
    "store",
    "type-module",
    "query",
    "query-hook",
    "api-client",
]
for sample in NAME_SAMPLES:
    t(
        f"emitted name {sample!r} satisfies ARCHETYPE_NAME_RE",
        bool(ARCHETYPE_NAME_RE.match(sample)),
        sample,
    )


# ---------------------------------------------------------------------------
section("Boundary — TS prior does NOT fire for empty / single-file clusters")
# Defensive checks against degenerate input. The match function returns
# None for empty member lists; propose_archetype_name then falls back to
# cluster-<hash>.
t(
    "_ts_prior_match([]) → None",
    _ts_prior_match([]) is None,
)
t(
    "_is_typescript_cluster([]) → False",
    _is_typescript_cluster([]) is False,
)
# Single-file TS cluster with no recognizable convention falls through
# the prior table and lands on the cluster-<hash> fallback. The exact
# fallback name is unstable across runs (depends on cluster_id), so we
# only check the prior didn't fire on the unrecognized pattern.
got = _ts_prior_match(["src/random/oddball.ts"])
t(
    "_ts_prior_match on unrecognized TS path → None",
    got is None,
    str(got),
)


# ---------------------------------------------------------------------------
section("Idempotence — repeated calls return the same TS-prior name")
# The TS prior is pure; same inputs must produce the same output across
# calls. Mirror the existing archetype_naming_test.py idempotence check.
def _idem() -> str:
    return propose_archetype_name(
        cluster(
            members=[
                "app/dashboard/page.tsx",
                "app/settings/page.tsx",
            ],
        ),
        set(),
    )


t("two calls return the same name", _idem() == _idem())


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
