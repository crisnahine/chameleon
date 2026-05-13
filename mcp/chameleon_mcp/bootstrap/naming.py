"""Archetype name proposal — derive human-meaningful names from cluster signals.

Phase 2D.2: replaces the ``cluster-<16hex>`` placeholder names emitted in
Phase 2B with names like ``controller``, ``service``, ``react-component``,
``migration`` derived from the cluster's signature plus its witness paths.

Design

The heuristic is intentionally rule-based and non-AI. It reads only the
cluster's already-computed signature (paths_pattern_bucket, top_level
node kinds, default_export_kind, jsx_present) and the relative paths of
its members. It returns an ``[a-z][a-z0-9-]{0,63}`` string that the
schema-level regex accepts.

Two clusters can legitimately produce the same base name (e.g., two
``controller`` clusters in different namespaces or test trees). The
public entry point ``propose_archetype_name`` is given the set of
already-assigned names and appends a path-tail suffix on collision so
the final names remain stable and disambiguated.

Falls back to a short ``cluster-<hash8>`` form when no signal matches
so the result is still readable but obviously generic.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# Public regex mirror — see ``profile.schema.ARCHETYPE_NAME_RE``. Kept in
# sync by hand because importing the schema module from a bootstrap pure-
# function helper would create a small cycle.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Path tail tokens we strip when suffixing a collision because they carry
# no namespace information (a controller in ``app/controllers/foo.rb`` and
# one in ``app/controllers/bar.rb`` aren't usefully named ``controller-foo``
# and ``controller-bar`` — they're both just ``controller``). Anything not
# in this set is treated as namespace-bearing and used as the suffix.
_GENERIC_TAIL_SEGMENTS = frozenset({
    "controllers",
    "models",
    "services",
    "policies",
    "serializers",
    "jobs",
    "mailers",
    "workers",
    "components",
    "hooks",
    "utils",
    "util",
    "lib",
    "src",
    "app",
    "spec",
    "test",
    "tests",
    "queries",
    "mutations",
    "migrate",
    "migrations",
    "config",
    "initializers",
    "types",
})

# Path segments that strongly indicate test files regardless of language.
_TEST_DIR_TOKENS = frozenset({"spec", "specs", "test", "tests", "__tests__"})

# Filename suffixes that strongly indicate test files.
_TEST_FILE_SUFFIXES = (
    "_spec.rb",
    "_test.rb",
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".test.mjs",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.mjs",
)


def _segments(paths_pattern: str) -> list[str]:
    """Split a paths_pattern bucket (e.g., 'app/controllers/api') into segments.

    Filters out empty fragments produced by leading slashes or repeated
    separators so callers can rely on every element being a non-empty
    segment.
    """
    if not paths_pattern:
        return []
    return [s for s in paths_pattern.split("/") if s]


def _cluster_attr(cluster: Any, attr: str, default: Any = None) -> Any:
    """Reach into ``cluster.key.<attr>`` first, falling back to ``cluster.<attr>``.

    The orchestrator passes ``Cluster`` instances whose signals live on
    ``cluster.key``; tests can also pass a lightweight dict-like stand-in
    for unit testing. This helper keeps both paths working without leaking
    test concerns into the production call site.
    """
    key = getattr(cluster, "key", None)
    if key is not None:
        val = getattr(key, attr, None)
        if val is not None:
            return val
    if isinstance(cluster, dict):
        return cluster.get(attr, default)
    return getattr(cluster, attr, default)


def _member_relpaths(repo_root: str | None, members: Any) -> list[str]:
    """Return member paths as POSIX-style strings, relative to repo_root.

    Members are ParsedFile instances during real bootstraps and arbitrary
    dicts/strings during unit tests. We tolerate both shapes; callers use
    the result only for filename and segment inspection, never to read
    files.

    When repo_root is provided, each absolute member path is made relative
    to it so that test-token detection in _looks_like_test is not biased by
    a repo whose absolute location happens to contain "tests", "spec", or
    similar segments (e.g., chameleon/tests/fixtures/eval_repos/ts_minimal/).
    """
    if members is None:
        return []
    if not members:
        return []

    resolved_root: Path | None = None
    if repo_root is not None:
        try:
            resolved_root = Path(repo_root).resolve()
        except Exception:
            resolved_root = None

    paths: list[str] = []
    for m in members:
        # Real members are ParsedFile; .path is a pathlib.Path.
        p = getattr(m, "path", None)
        if p is None:
            if isinstance(m, str):
                paths.append(m.replace("\\", "/"))
            continue
        # Use POSIX form so the heuristic behaves the same on win32 if it's
        # ever wired up. Convert to a path relative to repo_root so the
        # test-token detection is not biased by the absolute path containing
        # tokens like "tests" / "spec" from an unrelated parent directory.
        if resolved_root is not None:
            try:
                rel = Path(p).resolve().relative_to(resolved_root)
                paths.append(str(rel).replace("\\", "/"))
                continue
            except ValueError:
                # Member path isn't under repo_root for some reason; fall back
                # to the absolute string (preserves prior behavior for those
                # cases).
                pass
        paths.append(str(p).replace("\\", "/"))
    return paths


def _extract_members(cluster: Any) -> Any:
    """Extract the members list from a cluster object or dict."""
    members = getattr(cluster, "members", None)
    if members is None and isinstance(cluster, dict):
        members = cluster.get("members", [])
    return members or []


def _looks_like_test(paths_pattern: str, member_paths: Iterable[str]) -> bool:
    """Return True if the cluster's paths or filenames smell like tests.

    Three signals — any one is enough:
      (a) the paths_pattern contains a known test directory (``spec``,
          ``__tests__``, ``test``, ``tests``);
      (b) at least half the members' filenames end in a known test suffix;
      (c) every member sits under a path component named ``spec`` or
          ``__tests__`` (catches Jest colocated tests).
    """
    pattern_segs = _segments(paths_pattern)
    if any(seg in _TEST_DIR_TOKENS for seg in pattern_segs):
        return True

    member_list = list(member_paths)
    if not member_list:
        return False

    suffix_hits = sum(
        1 for p in member_list if any(p.endswith(suf) for suf in _TEST_FILE_SUFFIXES)
    )
    if suffix_hits * 2 >= len(member_list):  # ≥50% of members look like tests
        return True

    if all(
        any(seg in _TEST_DIR_TOKENS for seg in p.split("/"))
        for p in member_list
    ):
        return True

    return False


def _pattern_contains(paths_pattern: str, token: str) -> bool:
    """Does the paths_pattern bucket contain ``token`` as a path segment?

    Substring matching gives false positives (``components`` matches
    ``componentsx``); segment matching is the intent. ``token`` is treated
    as a single segment.
    """
    return token in _segments(paths_pattern)


def _members_contain(member_paths: Iterable[str], token: str) -> bool:
    """Does at least one member's path contain ``token`` as a directory segment?

    The v0.5.9 depth-2 bucket for ``app/controllers/api/v1/foo.rb`` is
    ``app/controllers``, which already carries the ``controllers`` segment.
    However for unusual layouts where ``controllers`` isn't at parts[1],
    or when running with an old profile generated at depth=3, the bucket
    may still omit it. This fallback checks raw member paths so naming
    stays correct in both cases.

    A cluster is considered to "be a controllers cluster" only if a strict
    majority of its members live under a ``controllers/`` directory, which
    keeps the rule robust against a single rogue file dropped into the
    cluster by a coarse signature.
    """
    members = list(member_paths)
    if not members:
        return False
    matches = sum(1 for p in members if token in p.split("/"))
    # Require majority membership. The threshold matches the cluster-purity
    # ratios used elsewhere (e.g., ``_looks_like_test`` uses 50%).
    return matches * 2 >= len(members)


def _filenames(member_paths: Iterable[str]) -> list[str]:
    """Return just the basename for each path string."""
    return [p.rsplit("/", 1)[-1] for p in member_paths]


def _is_ruby_cluster(member_paths: Iterable[str]) -> bool:
    """Return True when the cluster's canonical witness lives in a `.rb` file.

    v0.5.2 (Bug 2): Rails priors must only fire when the cluster's members
    are Ruby — otherwise a TS file under ``app/models/`` (e.g., a stray
    type definition the team dropped in the Rails tree) would be named
    ``model`` even though it has no Rails semantics. We use the first
    member's extension as the language tell; the cluster-purity check
    elsewhere keeps mixed clusters honest.
    """
    members = list(member_paths)
    if not members:
        return False
    # Use the first member as a proxy for the cluster's language. The
    # bootstrap already groups by extension via the discovery glob, so a
    # cluster is either Ruby-only or TS-only in practice.
    return members[0].endswith(".rb")


# Extensions a TypeScript/JavaScript cluster's first member is expected to end
# with. Mirrors the discovery glob's extension set so the gate matches what
# the bootstrap actually clusters as TS/JS.
_TS_JS_EXTENSIONS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)


def _is_typescript_cluster(member_paths: Iterable[str]) -> bool:
    """Return True when the cluster's canonical witness is a TS/JS file.

    v0.5.3 (Bug C): The TS-prior table must only fire when the cluster's
    members are TypeScript or JavaScript — otherwise a ``.rb`` file that
    happens to sit under ``app/`` would be misnamed by the Next.js-style
    rules. We use the first member's extension as the language tell, the
    same way ``_is_ruby_cluster`` does. The TS and Ruby gates are mutually
    exclusive in practice because the discovery glob groups by extension,
    but the caller still combines both gates (``ts && !rb``) plus a
    no-.rb-anywhere purity check so a mixed cluster (which the gate
    technically can't disambiguate from the first member alone) falls
    through to the existing heuristic rather than silently picking a TS
    name.
    """
    members = list(member_paths)
    if not members:
        return False
    return members[0].endswith(_TS_JS_EXTENSIONS)


# v0.5.2 (Bug 2): Rails-prior table. Each entry: (directory chain that must
# appear contiguously somewhere in the bucket OR the member paths, optional
# filename suffix the member must end in, archetype name). The first match
# wins; specificity-ordered so ``app/controllers/concerns/`` beats the bare
# ``controllers`` token.
#
# The ``filename_suffix`` field disambiguates buckets the path test alone
# would over-match (e.g., a Sidekiq worker file named ``foo_worker.rb``
# under ``app/jobs/`` should still be named ``worker``, but in practice
# only the ``jobs/`` directory is conventional; we anchor on the dir).
# When the suffix is None, only the directory chain has to match.
_RAILS_PRIORS: tuple[tuple[tuple[str, ...], str | None, str], ...] = (
    # Most specific first: concerns live UNDER controllers/, models/, or
    # the top-level concerns/ dir.
    (("app", "controllers", "concerns"), None, "controller-concern"),
    (("app", "models", "concerns"), None, "model-concern"),
    # Then the conventional one-deep Rails dirs.
    (("app", "controllers"), None, "controller"),
    (("app", "models"), None, "model"),
    (("app", "services"), None, "service"),
    (("app", "jobs"), "_job.rb", "job"),
    (("app", "mailers"), "_mailer.rb", "mailer"),
    (("app", "helpers"), "_helper.rb", "helper"),
    (("app", "policies"), None, "policy"),
    (("app", "serializers"), None, "serializer"),
    (("app", "presenters"), None, "presenter"),
    (("app", "workers"), None, "worker"),
    (("app", "views"), None, "view"),
    (("db", "migrate"), None, "migration"),
    (("config", "initializers"), None, "rails-initializer"),
)


def _has_dir_chain(member_paths: list[str], chain: tuple[str, ...]) -> bool:
    """Return True when the directory ``chain`` appears in the majority of members.

    We treat membership the same way ``_members_contain`` does — a strict
    majority of the cluster must sit under the directory chain — so a
    single rogue file dropped into the cluster by a coarse signature can't
    yank the archetype name in a misleading direction.
    """
    if not member_paths:
        return False
    matches = 0
    for p in member_paths:
        segs = p.split("/")
        for i in range(len(segs) - len(chain) + 1):
            if tuple(segs[i:i + len(chain)]) == chain:
                matches += 1
                break
    return matches * 2 >= len(member_paths)


# v0.5.4 (Bug F): conventional monorepo parent dirs. When a path starts
# with ``<parent>/<workspace>/...`` where parent is in this set, the first
# two segments are stripped before TS-prior matching so workspace-internal
# layouts (``packages/propel/src/components/``) hit the prior rules that
# were authored for flat repos (``src/components/``).
#
# Mirrors ``_WORKSPACE_PARENT_DIRS`` in the orchestrator but maintained
# separately here so naming stays a pure module with no orchestrator
# import. Update both lists together if a new convention is added.
_WORKSPACE_PARENT_DIRS_NAMING = ("apps", "packages", "services", "workspaces")


def _strip_workspace_prefix(
    member_paths: list[str], workspace_roots: list[str] | None
) -> list[str]:
    """Return member paths with any matching workspace prefix removed.

    v0.5.4 (Bug F): v0.5.3 Bug B introduced workspace-level bootstrap and
    Turborepo / pnpm / Nx layouts deliver paths as
    ``apps/<ws>/src/components/Foo.tsx`` or ``packages/<ws>/src/...``
    instead of root-relative ``src/...``. The TS-prior table's
    directory-chain rules (``components/``, ``app/``, ``pages/``) were
    authored for root-relative paths and silently missed on the
    workspace-relative ones, leaving plane at 12/70 generic and
    bulletproof-react at 6/12 generic in the cycle-3 dogfood.

    Two stripping strategies, both safe:

    1. **Explicit roots**: when ``workspace_roots`` is non-empty (the
       v0.5.3 Bug B path), strip the longest matching root prefix.
    2. **Path-shape fallback**: when ``workspace_roots`` is empty BUT a
       path starts with ``apps/<dir>/``, ``packages/<dir>/``,
       ``services/<dir>/``, or ``workspaces/<dir>/``, strip those two
       segments. Catches the plane case (pnpm catalog: refs in root
       package.json mask the workspace from Bug B's detector) and any
       future workspace-layout repo that slips past the orchestrator.

    Paths that don't match either strategy pass through unchanged so
    flat repos are unaffected. Returns a new list; never mutates input.
    """
    if not member_paths:
        return list(member_paths)
    roots_sorted = (
        sorted(workspace_roots, key=len, reverse=True) if workspace_roots else []
    )
    out: list[str] = []
    for p in member_paths:
        stripped: str | None = None
        # Strategy 1: explicit roots (longest match wins).
        for root in roots_sorted:
            prefix = root.rstrip("/") + "/"
            if p.startswith(prefix):
                stripped = p[len(prefix):]
                break
        # Strategy 2: path-shape fallback for repos that didn't surface
        # workspace_roots via the orchestrator (plane / pnpm-catalog case).
        if stripped is None:
            segs = p.split("/", 2)
            if (
                len(segs) >= 3
                and segs[0] in _WORKSPACE_PARENT_DIRS_NAMING
                and segs[1]  # non-empty workspace dir name
            ):
                stripped = segs[2]
        out.append(stripped if stripped is not None else p)
    return out


def _rails_prior_match(member_paths: list[str]) -> str | None:
    """Walk the Rails-prior table and return the first matching archetype name.

    Each entry's directory chain must appear in the majority of member
    paths; when a filename suffix is specified, at least half the members
    must additionally end with that suffix (catches stray files in the dir).
    """
    for chain, filename_suffix, name in _RAILS_PRIORS:
        if not _has_dir_chain(member_paths, chain):
            continue
        if filename_suffix is None:
            return name
        # Suffix-aware path: a majority of members must match the suffix.
        suffix_hits = sum(1 for p in member_paths if p.endswith(filename_suffix))
        if suffix_hits * 2 >= len(member_paths):
            return name
    return None


# v0.5.3 (Bug C): TypeScript-prior table. Parallel to ``_RAILS_PRIORS`` but
# anchored on Next.js / Remix / common TS-ecosystem conventions.
#
# Each entry: ``(directory_chain, filename_predicate, excluded_chains, name)``.
#   * ``directory_chain`` — a tuple of directory segments that must appear
#     contiguously in the majority of member paths (same semantics as
#     ``_has_dir_chain``).
#   * ``filename_predicate`` — a callable taking the basename string and
#     returning bool. ``_fn_any`` matches everything; the named helpers
#     below cover the brief's filename gates (``page.tsx``, ``use*``, etc.).
#   * ``excluded_chains`` — tuples of directory chains that, when matched
#     by the same cluster, disqualify this rule. The only use today is
#     "``app/`` but NOT ``app/api/``" for App Router page/layout rules.
#   * ``name`` — the archetype name to emit.
#
# The table is ordered most-specific-first so longer chains beat shorter
# ones. ``_ts_prior_match`` walks top-to-bottom and returns the first hit,
# so reordering changes behavior — keep the comment-banded grouping below
# when adding entries.


def _fn_any(_name: str) -> bool:
    """Filename predicate: matches every basename."""
    return True


def _fn_in(allowed: frozenset[str]) -> Callable[[str], bool]:
    """Build a predicate that matches only the given exact basenames."""
    def _pred(name: str) -> bool:
        return name in allowed
    return _pred


def _fn_not_in(disallowed: frozenset[str]) -> Callable[[str], bool]:
    """Build a predicate that rejects the given basenames (everything else passes)."""
    def _pred(name: str) -> bool:
        return name not in disallowed
    return _pred


def _fn_starts_with(prefix: str) -> Callable[[str], bool]:
    """Build a predicate that matches basenames starting with ``prefix``."""
    def _pred(name: str) -> bool:
        return name.startswith(prefix)
    return _pred


# Filename sets used by multiple rules.
_NEXT_APP_PAGE_FILES = frozenset({"page.tsx", "page.ts"})
_NEXT_APP_LAYOUT_FILES = frozenset({"layout.tsx", "layout.ts"})
_NEXT_APP_SPECIAL_FILES = frozenset({"loading.tsx", "error.tsx", "not-found.tsx"})
_NEXT_APP_ROUTE_FILES = frozenset({"route.ts", "route.tsx"})
_NEXT_PAGES_SPECIAL_FILES = frozenset({"_app.tsx", "_document.tsx", "_error.tsx"})


_TS_PRIORS: tuple[
    tuple[
        tuple[str, ...],              # directory chain
        Callable[[str], bool],        # filename predicate (basename → bool)
        tuple[tuple[str, ...], ...],  # excluded chains (any match disqualifies)
        str,                          # archetype name
    ],
    ...,
] = (
    # --- Next.js App Router (most-specific chains first) ---------------------
    # ``app/api/route.{ts,tsx}`` → route handler. Sits at chain length 2 so it
    # beats the ``("app",)`` page/layout rules below for files under app/api/.
    (("app", "api"), _fn_in(_NEXT_APP_ROUTE_FILES), (), "app-route-handler"),

    # ``app/routes/`` (Remix) — chain length 2, distinguishable from Next.js
    # App Router by the explicit ``routes/`` segment. Check before bare
    # ``("app",)`` rules so a Remix repo doesn't get Next.js-shaped names.
    (("app", "routes"), _fn_any, (), "remix-route"),

    # ``app/`` page / layout / special component files. NOT under ``app/api/``
    # (excluded chain catches the mixed case where a cluster spans both, even
    # though in practice the file conventions don't collide).
    (("app",), _fn_in(_NEXT_APP_PAGE_FILES), (("app", "api"),), "app-page-component"),
    (("app",), _fn_in(_NEXT_APP_LAYOUT_FILES), (("app", "api"),), "app-layout"),
    (("app",), _fn_in(_NEXT_APP_SPECIAL_FILES), (("app", "api"),), "app-special-component"),

    # --- Next.js Pages Router ------------------------------------------------
    # ``pages/api/`` — any filename. Chain length 2 wins over bare ``pages/``.
    (("pages", "api"), _fn_any, (), "pages-api-handler"),

    # ``pages/`` specials (``_app``, ``_document``, ``_error``) before the
    # generic ``pages-component`` rule so those filenames don't accidentally
    # get the page-component name.
    (("pages",), _fn_in(_NEXT_PAGES_SPECIAL_FILES), (("pages", "api"),), "pages-special-component"),
    (("pages",), _fn_not_in(_NEXT_PAGES_SPECIAL_FILES), (("pages", "api"),), "pages-component"),

    # --- Generic TS conventions (single-segment chains) ----------------------
    # Order within this group doesn't matter for the brief's verification
    # cases because the chains don't overlap, but we keep declaration order
    # stable so adding a new rule doesn't reshuffle existing behavior.
    (("components",), _fn_any, (), "component"),
    (("ui",), _fn_any, (), "ui-component"),
    # ``hooks/use*`` first; without the use-prefix the cluster is NOT a hook.
    (("hooks",), _fn_starts_with("use"), (), "hook"),
    (("lib",), _fn_any, (), "lib-module"),
    (("utils",), _fn_any, (), "util"),
    (("helpers",), _fn_any, (), "helper"),
    (("services",), _fn_any, (), "service"),
    # ``middleware/`` directory; the root-level ``middleware.ts`` file is
    # handled by a special-case check in ``_ts_prior_match`` because it has
    # no directory chain to match against.
    (("middleware",), _fn_any, (), "middleware"),
    (("actions",), _fn_any, (), "action"),
    (("store",), _fn_any, (), "store"),
    (("stores",), _fn_any, (), "store"),
    (("types",), _fn_any, (), "type-module"),
    # ``queries/use*`` (react-query hooks) before bare ``queries/`` to avoid
    # the more-generic ``query`` name swallowing what is really a hook.
    (("queries",), _fn_starts_with("use"), (), "query-hook"),
    (("queries",), _fn_any, (), "query"),

    # --- v0.5.4 — additional patterns surfaced by cycle-3 dogfood -----------
    # bulletproof-react + plane both ship feature-based architectures:
    # ``features/<feature>/api/`` carries the feature's API client functions.
    # The chain ``("features", "api")`` doesn't appear (features sit between),
    # but the leaf ``api`` segment after a ``features`` parent is the marker.
    (("features",), _fn_any, (), "feature-module"),
    # MSW-style test mocks. Cluster name surfaces the harness layer so
    # reviewers don't confuse production handler code with test fixtures.
    (("testing", "mocks"), _fn_any, (), "test-mock"),
    (("mocks", "handlers"), _fn_any, (), "test-mock-handler"),
    # ``icons/`` directory — branded icon sets, typically a sibling of
    # ``components/``. Pre-v0.5.4 these clustered under cluster-<hash>.
    (("icons",), _fn_any, (), "icon-set"),
    # i18n directories. ``locales/`` is the conventional name; ``i18n/``
    # also common. The cluster typically holds per-locale JSON or TS
    # tables; we name the cluster after the convention so users can
    # spot non-locale files that leaked into the dir during review.
    (("locales",), _fn_any, (), "locale-table"),
    (("i18n",), _fn_any, (), "locale-table"),
    # Constants modules — typically a single export object per file with
    # repo-wide string/number tables. Common in modern React/Next.js apps.
    (("constants",), _fn_any, (), "constants-module"),
    # Schema modules — zod / yup / valibot schema definitions. Common in
    # form-heavy apps; the cluster usually has many ``z.object(...)``
    # definitions. ``schema/`` (singular) and ``schemas/`` both appear.
    (("schema",), _fn_any, (), "schema-module"),
    (("schemas",), _fn_any, (), "schema-module"),
    # Provider components / context providers. ``providers/`` parent dir
    # is the common convention.
    (("providers",), _fn_any, (), "provider"),
    (("contexts",), _fn_any, (), "context"),
    # Layouts subdir — Next.js root-layout files plus standalone layout
    # components in non-Next.js apps.
    (("layouts",), _fn_any, (), "layout"),
    # Configs subdir — domain-specific configuration modules separate
    # from build-tool configs at the repo root.
    (("config",), _fn_any, (), "config-module"),
    (("configs",), _fn_any, (), "config-module"),
)


def _ts_prior_match(member_paths: list[str]) -> str | None:
    """Walk the TS-prior table and return the first matching archetype name.

    The walk is deliberately top-to-bottom — the table is sorted with
    longer directory chains and more specific filename predicates first
    so the "most specific wins" rule from the brief falls out for free.

    Two non-table cases are also covered here:

      1. ``middleware.ts`` at the repo root (no ``middleware/`` directory) —
         when the majority of members are exactly named ``middleware.ts``
         and none sit under a ``middleware/`` directory, emit ``middleware``.
      2. Root-level ``api/`` (NOT Next.js) — when the majority of members
         have ``api`` as their first directory segment AND no member sits
         under ``pages/api/`` or ``app/api/``, emit ``api-client``.

    Test detection (``__tests__/``, ``*.test.ts``, ``*.spec.ts``) is NOT
    handled here because ``_looks_like_test`` already runs first in
    ``_base_name_for`` and returns ``"test"`` for those clusters — adding
    a duplicate rule would be dead code.
    """
    if not member_paths:
        return None

    member_count = len(member_paths)
    file_names = _filenames(member_paths)

    # Pre-compute exclusion-chain hits once per cluster so the inner loop
    # doesn't repeat the same scan for every disqualified rule.
    def _is_excluded(excluded_chains: tuple[tuple[str, ...], ...]) -> bool:
        for excl in excluded_chains:
            if _has_dir_chain(member_paths, excl):
                return True
        return False

    for chain, filename_pred, excluded_chains, name in _TS_PRIORS:
        if not _has_dir_chain(member_paths, chain):
            continue
        if excluded_chains and _is_excluded(excluded_chains):
            continue
        # Majority of basenames must satisfy the filename predicate.
        hits = sum(1 for n in file_names if filename_pred(n))
        if hits * 2 >= member_count:
            return name

    # --- Special case: root-level ``middleware.ts`` file -------------------
    # When the cluster is dominated by files literally named ``middleware.ts``
    # (e.g., a Next.js project's edge middleware) AND no member sits under
    # a ``middleware/`` directory, the directory-chain rule above doesn't
    # apply — but the convention is unambiguous.
    middleware_hits = sum(
        1 for p in member_paths if p.rsplit("/", 1)[-1] == "middleware.ts"
    )
    if (
        middleware_hits * 2 >= member_count
        and not _has_dir_chain(member_paths, ("middleware",))
    ):
        return "middleware"

    # --- Special case: root-level ``api/`` (NOT Next.js) -------------------
    # The ``("app", "api")`` and ``("pages", "api")`` rules above already
    # handle Next.js. A cluster whose members start with ``api/<file>``
    # (i.e., ``api`` is the first segment, no ``app/`` or ``pages/`` parent)
    # is the conventional "API client" pattern — REST wrappers, fetch
    # helpers, generated SDK code.
    api_first_segment = sum(
        1
        for p in member_paths
        if p.split("/", 1)[0] == "api"
    )
    if api_first_segment * 2 >= member_count:
        # Belt-and-suspenders: make sure no member is actually under
        # ``pages/api/`` or ``app/api/`` (which would already have matched
        # earlier rules; this guards against the table being reordered).
        nextjs_overlap = (
            _has_dir_chain(member_paths, ("pages", "api"))
            or _has_dir_chain(member_paths, ("app", "api"))
        )
        if not nextjs_overlap:
            return "api-client"

    return None


def _base_name_for(
    cluster: Any,
    workspace_roots: list[str] | None = None,
    repo_root: str | None = None,
) -> str | None:
    """Return a derived base name from cluster signals, or None for fallback.

    The order of rules is significant: more specific patterns are tried
    before more general ones (e.g., ``rails-initializer`` before the bare
    ``initializer`` token).

    v0.5.4 (Bug F): when ``workspace_roots`` is non-empty the TS-prior
    pipeline gets workspace-stripped paths so the directory-chain rules
    (``components/``, ``app/``, ``pages/``) match correctly on
    Turborepo / pnpm / Nx layouts. The Rails-prior pipeline doesn't get
    the strip because Rails monorepos don't use this layout.
    """
    paths_pattern = _cluster_attr(cluster, "path_pattern_bucket") or ""
    default_export = _cluster_attr(cluster, "default_export_kind")
    _cluster_attr(cluster, "top_level_node_kinds") or ()
    jsx_present = bool(_cluster_attr(cluster, "jsx_present", False))
    member_paths = _member_relpaths(repo_root, _extract_members(cluster))
    file_names = _filenames(member_paths)
    # Pre-compute the workspace-stripped variant once per cluster — the
    # TS-prior table uses it; the Rails-prior table uses the raw paths.
    ts_member_paths = _strip_workspace_prefix(member_paths, workspace_roots)

    is_class_default = default_export in {
        "ClassNode",
        "ClassDeclaration",
        "ModuleNode",  # treat Ruby modules with a single top-level body as classes
    }
    is_arrow_default = default_export in {"ArrowFunction", "FunctionExpression"}

    def _has(token: str) -> bool:
        """Either the bucket OR the majority of member paths contains ``token``.

        Signature v5 collapses ``app/controllers/api/v1/foo.rb`` into the
        bucket ``app/api/v1``, dropping the load-bearing ``controllers``
        segment. We fall back to inspecting the raw member paths so the
        heuristic still recognizes Rails clusters with deep namespaces.
        """
        return _pattern_contains(paths_pattern, token) or _members_contain(
            member_paths, token
        )

    # Test detection runs first — a "spec/controllers/..." cluster should
    # be named ``test``, not ``controller``. Without this ordering the model
    # would see two ``controller`` clusters and the disambiguator would
    # suffix one ``controller-spec``, which is misleading.
    if _looks_like_test(paths_pattern, member_paths):
        return "test"

    # v0.5.2 (Bug 2): Rails-prior table fires only when the cluster's
    # canonical witness is a `.rb` file. It catches dirs the v0.5.1
    # heuristic missed (helpers, presenters, views, controller-concerns)
    # AND tightens the existing dirs by requiring directory-chain match
    # rather than the lenient single-token search. The path-bucket from
    # signature v5 collapses inner segments, so we always test against
    # the raw member paths.
    if _is_ruby_cluster(member_paths):
        prior_name = _rails_prior_match(member_paths)
        if prior_name is not None:
            return prior_name

    # v0.5.3 (Bug C): TS-prior table fires only when the cluster's canonical
    # witness is a TS/JS file AND the Ruby gate is False. The third clause
    # (``no .rb member anywhere``) is belt-and-suspenders: a mixed cluster
    # with a TS first member should still fall through to neither prior
    # table, per the bug-C verification contract. In practice the discovery
    # glob groups clusters by extension so all three clauses agree, but a
    # future signature change that loosens grouping won't silently start
    # naming Ruby-shaped TS clusters with Next.js names.
    #
    # Covers Next.js (App Router page/layout/route, Pages Router api),
    # Remix (``app/routes/``), and the common TS-ecosystem conventions
    # (``components/``, ``hooks/``, ``lib/``, ``utils/``, ``types/``,
    # ``actions/``, ``store/``, ``middleware/``, root-level ``api/``) that
    # the v0.5.1 heuristic missed or named with a less consistent
    # vocabulary.
    if (
        _is_typescript_cluster(member_paths)
        and not _is_ruby_cluster(member_paths)
        and not any(p.endswith(".rb") for p in member_paths)
    ):
        prior_name = _ts_prior_match(ts_member_paths)
        if prior_name is not None:
            return prior_name

    # --- Rails-flavored buckets (paths_pattern starts with ``app/...``) ---
    if _has("controllers"):
        return "controller"
    if _has("models"):
        return "model"
    if _has("policies"):
        return "policy"
    if _has("serializers"):
        return "serializer"
    if _has("jobs"):
        return "job"
    if _has("mailers"):
        return "mailer"
    if _has("workers"):
        return "worker"

    # config/initializers/*.rb — these tend to be top-level Rails.application.configure blocks.
    if _has("initializers") and _has("config"):
        return "rails-initializer"

    # Migrations: ``db/migrate/...`` is the canonical Rails path; ``migrations``
    # also catches other ORMs (knex, sequelize). Check before ``services`` so
    # a ``db/migrate`` cluster isn't shadowed.
    if _has("migrate") or _has("migrations"):
        return "migration"

    if _has("services"):
        return "service"

    # --- Front-end / TS-flavored buckets (legacy fallback) ---
    # v0.5.3 vocabulary alignment: these fallbacks now match the names
    # used by ``_TS_PRIORS`` so a cluster whose path bucket missed every
    # prior-table entry but matched a structural signal still gets the
    # standardized name rather than a v0.5.1-era one. The TS prior table
    # handles the path-conventional cases first; this block is reachable
    # only for non-conventional layouts (e.g., JSX components that live
    # outside any ``components/`` dir).

    # React components: explicit ``components`` dir AND JSX present.
    if _has("components") and jsx_present:
        return "component"

    # React hooks: ``hooks`` directory AND filename starts with ``use``.
    if _has("hooks") and any(n.startswith("use") for n in file_names):
        return "hook"

    if _has("queries"):
        return "query"
    if _has("mutations"):
        return "mutation"

    # ``lib/utils`` and ``utils`` directories — utility / pure functions.
    if _has("utils") or _has("util"):
        return "util"

    # ``types`` directory or paths_pattern ending in ``/types``.
    if _has("types"):
        return "type-module"

    # Structural fallback: a JSX-present cluster whose default export is
    # arrow-y is almost always a component, even outside ``components/``.
    if jsx_present and is_arrow_default:
        return "component"

    # TS class default with no JSX — generic ``class`` is intentionally
    # distinct from ``lib-module`` (which encodes the ``lib/`` location).
    # A class outside any conventional directory keeps the structural
    # name rather than a misleading directory-flavored one.
    if is_class_default and not jsx_present:
        return "class"

    return None


def _short_hash_for(cluster: Any) -> str:
    """Derive a short fallback identifier from the cluster signature.

    Prefers the existing cluster_id attribute when callers attach one (the
    orchestrator does); otherwise hashes the cluster key dict so unit
    tests don't have to fabricate ids.
    """
    cluster_id = getattr(cluster, "cluster_id", None)
    if cluster_id is None and isinstance(cluster, dict):
        cluster_id = cluster.get("cluster_id")
    if cluster_id:
        return str(cluster_id)[:8]

    key = getattr(cluster, "key", None)
    if key is not None and hasattr(key, "to_dict"):
        import hashlib
        import json

        canonical = json.dumps(key.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return "unknown"


def _disambiguation_suffixes(cluster: Any, repo_root: str | None = None) -> list[str]:
    """All name-safe disambiguator candidates from the cluster's path metadata.

    BUG-013: previously returned a single value; when that single value
    collided with an earlier cluster's name, the caller fell back to a
    numeric counter (``react-component-10``, ``service-20``) which gives
    the user no information about what makes the cluster different.

    Now returns an ordered list of candidates so the caller can try
    several path-shaped disambiguators before falling back to a counter:
    e.g., ``[handlers, mocks, testing]`` derived from
    ``src/testing/mocks/handlers/foo.ts``. Order: most-specific (rightmost
    meaningful path segment) to least.
    """
    paths_pattern = _cluster_attr(cluster, "path_pattern_bucket") or ""
    candidate_streams: list[list[str]] = []
    bucket_segs = _segments(paths_pattern)
    if len(bucket_segs) > 1:
        candidate_streams.append(bucket_segs[1:])  # drop the noisy top-level
    elif bucket_segs:
        candidate_streams.append(bucket_segs)

    member_paths = _member_relpaths(repo_root, _extract_members(cluster))
    if member_paths:
        first_dirs = member_paths[0].rsplit("/", 1)[0].split("/")
        if len(first_dirs) > 1:
            candidate_streams.append(first_dirs[1:])

    seen: set[str] = set()
    result: list[str] = []
    for segs in candidate_streams:
        for seg in reversed(segs):
            normalized = re.sub(r"[^a-z0-9-]+", "-", seg.lower()).strip("-")
            if not normalized:
                continue
            if re.fullmatch(r"v\d+(?:\.\d+)*", normalized):
                continue
            if normalized in _GENERIC_TAIL_SEGMENTS:
                continue
            if not normalized[0].isalpha():
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return result


def _disambiguation_suffix(cluster: Any) -> str | None:
    """Backwards-compat thin wrapper around _disambiguation_suffixes.

    Returns the first (most-specific) candidate, or None.
    """
    suffixes = _disambiguation_suffixes(cluster)
    return suffixes[0] if suffixes else None


def _sanitize(name: str) -> str | None:
    """Coerce ``name`` to the archetype-name shape, or return None.

    Public callers should treat ``None`` as "the chosen base name was
    invalid, fall back to the short-hash form". This belt-and-suspenders
    check protects the schema regex against a future heuristic addition
    that accidentally produces ``Camel`` or ``snake_case``.
    """
    candidate = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not candidate:
        return None
    if not candidate[0].isalpha():
        return None
    candidate = candidate[:64]
    return candidate if _NAME_RE.match(candidate) else None


def propose_archetype_name(
    cluster: Any,
    existing_names: set[str],
    *,
    workspace_roots: list[str] | None = None,
    repo_root: str | None = None,
) -> str:
    """Propose a meaningful archetype name for ``cluster``.

    Args:
        cluster: a ``Cluster`` instance (or dict-like with ``key`` and
            ``members``) exposing ``key.path_pattern_bucket``,
            ``key.default_export_kind``, ``key.top_level_node_kinds``,
            ``key.jsx_present``, plus iterable ``members`` whose entries
            expose a ``.path`` attribute (or are strings).
        existing_names: the set of names already assigned by previous
            calls in this bootstrap run. Used for collision disambiguation
            and never mutated.
        workspace_roots: optional list of repo-relative workspace dirs
            (e.g., ``["apps/web", "packages/propel"]``) emitted by
            ``bootstrap_repo``'s monorepo detection in v0.5.3. When
            provided, the TS-prior pipeline strips the matching prefix
            from member paths before running the directory-chain rules
            so workspace-internal layouts (``apps/web/src/components/``,
            ``packages/propel/src/hooks/``) match the prior table.
            v0.5.4 (Bug F).
        repo_root: absolute path to the repo root. When provided, member
            paths are made relative to it before test-token detection so
            a repo whose absolute location contains "tests" or "spec" in
            its path does not cause non-test clusters to be misnamed.

    Returns:
        A string matching ``^[a-z][a-z0-9-]{0,63}$``. Guaranteed unique
        with respect to ``existing_names``.

    The function is pure (no I/O) and idempotent: given the same cluster
    and ``existing_names`` (and ``workspace_roots``) it always returns
    the same name.
    """
    base = _base_name_for(cluster, workspace_roots=workspace_roots, repo_root=repo_root)
    if base is not None:
        sanitized = _sanitize(base)
        if sanitized is not None:
            base = sanitized
        else:
            base = None

    if base is None:
        # Fallback: ``cluster-<hash8>`` keeps the schema invariant satisfied
        # AND signals to the user that nothing meaningful was inferred.
        base = f"cluster-{_short_hash_for(cluster)}"
        sanitized = _sanitize(base)
        if sanitized is not None:
            base = sanitized
        else:
            base = "cluster-unknown"

    if base not in existing_names:
        return base

    # Collision path. BUG-013: try EVERY meaningful path-tail suffix in
    # decreasing specificity before falling back to a numeric counter, so
    # we get ``react-component-button`` / ``react-component-icons`` /
    # ``react-component-modal`` instead of ``react-component-10``.
    suffix_candidates = _disambiguation_suffixes(cluster, repo_root=repo_root)
    for suffix in suffix_candidates:
        candidate = _sanitize(f"{base}-{suffix}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    # Also try pairs of segments (most-specific + parent) to cover the case
    # where every single-segment suffix collides too.
    for i in range(len(suffix_candidates)):
        for j in range(i + 1, len(suffix_candidates)):
            paired = f"{suffix_candidates[i]}-{suffix_candidates[j]}"
            candidate = _sanitize(f"{base}-{paired}")
            if candidate is not None and candidate not in existing_names:
                return candidate

    # As a last resort, append a numeric counter. We try ``-2`` first because
    # ``foo`` already exists, so the second instance is logically the second.
    for i in range(2, 1000):
        candidate = _sanitize(f"{base}-{i}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    # The schema allows 64 chars, so 998 numeric suffixes is unreachable
    # under any realistic codebase. Defensive fallback: include the short
    # hash to guarantee uniqueness without overflowing the regex.
    return _sanitize(f"{base}-{_short_hash_for(cluster)}") or f"cluster-{_short_hash_for(cluster)}"
