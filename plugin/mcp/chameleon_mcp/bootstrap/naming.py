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
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

_GENERIC_TAIL_SEGMENTS = frozenset(
    {
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
    }
)

_TEST_DIR_TOKENS = frozenset({"spec", "specs", "test", "tests", "__tests__"})

# pytest/unittest: the dominant convention is the `test_` PREFIX (test_views.py),
# which a suffix list can't catch; also `*_test.py`, `conftest.py`, and Django
# startapp's default bare `tests.py` / `test.py` (no prefix or suffix).
_PY_TEST_BASENAME_RE = re.compile(r"^(test_.+|.+_test|conftest|tests?)\.pyi?$")

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
        p = getattr(m, "path", None)
        if p is None:
            if isinstance(m, str):
                paths.append(m.replace("\\", "/"))
            continue
        if resolved_root is not None:
            try:
                rel = Path(p).resolve().relative_to(resolved_root)
                paths.append(str(rel).replace("\\", "/"))
                continue
            except ValueError:
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

    def _is_test_basename(p: str) -> bool:
        name = p.rsplit("/", 1)[-1]
        return any(p.endswith(suf) for suf in _TEST_FILE_SUFFIXES) or bool(
            _PY_TEST_BASENAME_RE.match(name)
        )

    suffix_hits = sum(1 for p in member_list if _is_test_basename(p))
    if suffix_hits * 2 >= len(member_list):
        return True

    if all(any(seg in _TEST_DIR_TOKENS for seg in p.split("/")) for p in member_list):
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

    The depth-2 bucket for ``app/controllers/api/v1/foo.rb`` is
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
    return matches * 2 >= len(members)


def _filenames(member_paths: Iterable[str]) -> list[str]:
    """Return just the basename for each path string."""
    return [p.rsplit("/", 1)[-1] for p in member_paths]


def _is_ruby_cluster(member_paths: Iterable[str]) -> bool:
    """Return True when the cluster's canonical witness lives in a `.rb` file.

    Bug 2: Rails priors must only fire when the cluster's members
    are Ruby — otherwise a TS file under ``app/models/`` (e.g., a stray
    type definition the team dropped in the Rails tree) would be named
    ``model`` even though it has no Rails semantics. We use the first
    member's extension as the language tell; the cluster-purity check
    elsewhere keeps mixed clusters honest.
    """
    members = list(member_paths)
    if not members:
        return False
    return members[0].endswith(".rb")


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

    Bug C: The TS-prior table must only fire when the cluster's
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


_PY_EXTENSIONS_NAMING: tuple[str, ...] = (".py", ".pyi")


def _is_python_cluster(member_paths: Iterable[str]) -> bool:
    """Return True when the cluster's canonical witness is a ``.py`` file.

    Mirrors ``_is_ruby_cluster`` / ``_is_typescript_cluster``: the Python prior
    table must not fire on a TS/Ruby cluster. First member's extension is the
    language tell.
    """
    members = list(member_paths)
    if not members:
        return False
    return members[0].endswith(_PY_EXTENSIONS_NAMING)


def _python_prior_match(member_paths: Iterable[str]) -> str | None:
    """Name a Python cluster by the Django/DRF role its members share.

    Django expresses role in the filename (``models.py`` -> ``model``), so the
    role is read per-member and the strict-majority role wins -- the same
    majority discipline ``_has_dir_chain`` uses, so one stray file in a coarse
    cluster can't yank the name. Returns None when no role reaches a majority
    (the cluster degrades to the language-agnostic fallback).
    """
    from collections import Counter

    from chameleon_mcp.signatures import python_role_for_path

    members = list(member_paths)
    if not members:
        return None
    roles: Counter[str] = Counter()
    for p in members:
        role = python_role_for_path(p)
        if role:
            roles[role] += 1
    if not roles:
        return None
    role, count = roles.most_common(1)[0]
    if count * 2 >= len(members):
        return role
    return None


_RAILS_PRIORS: tuple[tuple[tuple[str, ...], str | None, str], ...] = (
    (("app", "controllers", "concerns"), None, "controller-concern"),
    (("app", "models", "concerns"), None, "model-concern"),
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
            if tuple(segs[i : i + len(chain)]) == chain:
                matches += 1
                break
    return matches * 2 >= len(member_paths)


_WORKSPACE_PARENT_DIRS_NAMING = ("apps", "packages", "services", "workspaces")


def _strip_workspace_prefix(
    member_paths: list[str], workspace_roots: list[str] | None
) -> list[str]:
    """Return member paths with any matching workspace prefix removed.

    Bug F: Bug B introduced workspace-level bootstrap and
    Turborepo / pnpm / Nx layouts deliver paths as
    ``apps/<ws>/src/components/Foo.tsx`` or ``packages/<ws>/src/...``
    instead of root-relative ``src/...``. The TS-prior table's
    directory-chain rules (``components/``, ``app/``, ``pages/``) were
    authored for root-relative paths and silently missed on the
    workspace-relative ones, leaving plane at 12/70 generic and
    bulletproof-react at 6/12 generic in the cycle-3 dogfood.

    Two stripping strategies, both safe:

    1. **Explicit roots**: when ``workspace_roots`` is non-empty (the
       Bug B path), strip the longest matching root prefix.
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
    roots_sorted = sorted(workspace_roots, key=len, reverse=True) if workspace_roots else []
    out: list[str] = []
    for p in member_paths:
        stripped: str | None = None
        for root in roots_sorted:
            prefix = root.rstrip("/") + "/"
            if p.startswith(prefix):
                stripped = p[len(prefix) :]
                break
        if stripped is None:
            segs = p.split("/", 2)
            if len(segs) >= 3 and segs[0] in _WORKSPACE_PARENT_DIRS_NAMING and segs[1]:
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
        suffix_hits = sum(1 for p in member_paths if p.endswith(filename_suffix))
        if suffix_hits * 2 >= len(member_paths):
            return name
    return None


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


def _fn_ends_with(suffix: str) -> Callable[[str], bool]:
    """Build a predicate that matches basenames ending with ``suffix``."""

    def _pred(name: str) -> bool:
        return name.endswith(suffix)

    return _pred


_NEXT_APP_PAGE_FILES = frozenset({"page.tsx", "page.ts"})
_NEXT_APP_LAYOUT_FILES = frozenset({"layout.tsx", "layout.ts"})
_NEXT_APP_SPECIAL_FILES = frozenset({"loading.tsx", "error.tsx", "not-found.tsx"})
_NEXT_APP_ROUTE_FILES = frozenset({"route.ts", "route.tsx"})
_NEXT_PAGES_SPECIAL_FILES = frozenset({"_app.tsx", "_document.tsx", "_error.tsx"})


_TS_PRIORS: tuple[
    tuple[
        tuple[str, ...],
        Callable[[str], bool],
        tuple[tuple[str, ...], ...],
        str,
    ],
    ...,
] = (
    (("app", "api"), _fn_in(_NEXT_APP_ROUTE_FILES), (), "app-route-handler"),
    (("app", "routes"), _fn_any, (), "remix-route"),
    (("app",), _fn_in(_NEXT_APP_PAGE_FILES), (("app", "api"),), "app-page-component"),
    (("app",), _fn_in(_NEXT_APP_LAYOUT_FILES), (("app", "api"),), "app-layout"),
    (("app",), _fn_in(_NEXT_APP_SPECIAL_FILES), (("app", "api"),), "app-special-component"),
    (("pages", "api"), _fn_any, (), "pages-api-handler"),
    (("pages",), _fn_in(_NEXT_PAGES_SPECIAL_FILES), (("pages", "api"),), "pages-special-component"),
    (("pages",), _fn_not_in(_NEXT_PAGES_SPECIAL_FILES), (("pages", "api"),), "pages-component"),
    # NestJS / Angular filename-role suffixes. These frameworks co-locate by
    # feature (users/users.controller.ts), so the role lives in the filename, not
    # a directory chain -- an empty dir chain matches any location, and the
    # majority filename predicate gates. Names are framework-neutral (the suffix
    # IS the role); .controller/.resolver/.gateway are NestJS-distinct while
    # .service/.module/.guard are shared with Angular. Placed above the generic
    # directory entries so a *.service.ts wins its role over a coarse services/
    # bucket; placed below the Next.js/Remix entries so app/pages routing wins.
    ((), _fn_ends_with(".controller.ts"), (), "controller"),
    ((), _fn_ends_with(".resolver.ts"), (), "resolver"),
    ((), _fn_ends_with(".gateway.ts"), (), "gateway"),
    ((), _fn_ends_with(".service.ts"), (), "service"),
    ((), _fn_ends_with(".module.ts"), (), "module"),
    ((), _fn_ends_with(".guard.ts"), (), "guard"),
    (("components",), _fn_any, (), "component"),
    (("ui",), _fn_any, (), "ui-component"),
    (("hooks",), _fn_starts_with("use"), (), "hook"),
    (("lib",), _fn_any, (), "lib-module"),
    (("utils",), _fn_any, (), "util"),
    (("helpers",), _fn_any, (), "helper"),
    (("services",), _fn_any, (), "service"),
    (("middleware",), _fn_any, (), "middleware"),
    (("actions",), _fn_any, (), "action"),
    (("store",), _fn_any, (), "store"),
    (("stores",), _fn_any, (), "store"),
    (("types",), _fn_any, (), "type-module"),
    (("queries",), _fn_starts_with("use"), (), "query-hook"),
    (("queries",), _fn_any, (), "query"),
    (("features",), _fn_any, (), "feature-module"),
    (("testing", "mocks"), _fn_any, (), "test-mock"),
    (("mocks", "handlers"), _fn_any, (), "test-mock-handler"),
    (("icons",), _fn_any, (), "icon-set"),
    (("locales",), _fn_any, (), "locale-table"),
    (("i18n",), _fn_any, (), "locale-table"),
    (("constants",), _fn_any, (), "constants-module"),
    (("schema",), _fn_any, (), "schema-module"),
    (("schemas",), _fn_any, (), "schema-module"),
    (("providers",), _fn_any, (), "provider"),
    (("contexts",), _fn_any, (), "context"),
    (("layouts",), _fn_any, (), "layout"),
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
        hits = sum(1 for n in file_names if filename_pred(n))
        if hits * 2 >= member_count:
            return name

    middleware_hits = sum(1 for p in member_paths if p.rsplit("/", 1)[-1] == "middleware.ts")
    if middleware_hits * 2 >= member_count and not _has_dir_chain(member_paths, ("middleware",)):
        return "middleware"

    api_first_segment = sum(1 for p in member_paths if p.split("/", 1)[0] == "api")
    if api_first_segment * 2 >= member_count:
        nextjs_overlap = _has_dir_chain(member_paths, ("pages", "api")) or _has_dir_chain(
            member_paths, ("app", "api")
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

    Bug F: when ``workspace_roots`` is non-empty the TS-prior
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
    ts_member_paths = _strip_workspace_prefix(member_paths, workspace_roots)

    is_class_default = default_export in {
        "ClassNode",
        "ClassDeclaration",
        "ModuleNode",
        "ClassDef",  # libcst (Python): a single top-level class
    }
    is_arrow_default = default_export in {"ArrowFunction", "FunctionExpression"}

    def _has(token: str) -> bool:
        """Either the bucket OR the majority of member paths contains ``token``.

        Signature v5 collapses ``app/controllers/api/v1/foo.rb`` into the
        bucket ``app/api/v1``, dropping the load-bearing ``controllers``
        segment. We fall back to inspecting the raw member paths so the
        heuristic still recognizes Rails clusters with deep namespaces.
        """
        return _pattern_contains(paths_pattern, token) or _members_contain(member_paths, token)

    if _looks_like_test(paths_pattern, member_paths):
        return "test"

    if _is_ruby_cluster(member_paths):
        prior_name = _rails_prior_match(member_paths)
        if prior_name is not None:
            return prior_name

    if (
        _is_typescript_cluster(member_paths)
        and not _is_ruby_cluster(member_paths)
        and not any(p.endswith(".rb") for p in member_paths)
    ):
        prior_name = _ts_prior_match(ts_member_paths)
        if prior_name is not None:
            return prior_name

    if (
        _is_python_cluster(member_paths)
        and not _is_ruby_cluster(member_paths)
        and not any(p.endswith(".rb") for p in member_paths)
    ):
        prior_name = _python_prior_match(member_paths)
        if prior_name is not None:
            return prior_name

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

    if _has("initializers") and _has("config"):
        return "rails-initializer"

    if _has("migrate") or _has("migrations"):
        return "migration"

    if _has("services"):
        return "service"

    if _has("components") and jsx_present:
        return "component"

    if _has("hooks") and any(n.startswith("use") for n in file_names):
        return "hook"

    if _has("queries"):
        return "query"
    if _has("mutations"):
        return "mutation"

    if _has("utils") or _has("util"):
        return "util"

    if _has("types"):
        return "type-module"

    if jsx_present and is_arrow_default:
        return "component"

    if is_class_default and not jsx_present:
        if paths_pattern and paths_pattern != "(root)":
            suffix_candidates = _disambiguation_suffixes(cluster, repo_root=repo_root)
            for suffix in suffix_candidates:
                if suffix == "root":
                    continue
                candidate = _sanitize(f"class-{suffix}")
                if candidate is not None:
                    return candidate
        return "class"

    # Before giving up on a name, take one from the directory the cohort lives
    # in. The token ladder above is an allow-list of 19 well-known names, so a
    # repo whose layers are called anything else -- repositories, validators,
    # selectors, dto, entities, guards, adapters -- fell through to
    # `cluster-<hash>`. Measured: 54% of a NestJS repo's archetypes unnamed, and
    # a 7-file cohort living entirely in src/repositories/ named
    # cluster-63d4a2fb. The archetype name is the primary thing the model is
    # told a file IS, and a hash conveys nothing it can reason from.
    #
    # Placed here, AFTER every specific rule, so it only ever replaces the hash
    # and can never preempt a known token.
    dir_name = _dominant_layer_name(member_paths)
    if dir_name is not None:
        return dir_name

    return None


# Directory segments that group code without describing its ROLE. A cohort
# sitting in one of these shares a location, not a purpose, so naming it after
# the segment would be worse than the hash it replaces.
_STRUCTURAL_DIRS: frozenset[str] = frozenset(
    {
        "src",
        "lib",
        "app",
        "apps",
        "pkg",
        "pkgs",
        "packages",
        "internal",
        "source",
        "sources",
        "core",
        "common",
        "shared",
        "modules",
        "main",
        "code",
        "project",
        "dist",
        "build",
        "out",
        "vendor",
        "node_modules",
        "test",
        "tests",
        "spec",
        "specs",
        "__tests__",
    }
)

# How much of a cohort must agree on one directory before it names the whole
# archetype. Below this the cohort spans layers and no single name is honest.
_DOMINANT_DIR_RATIO = 0.8

# A directory only evidences a LAYER once several files share it. One or two
# files in a folder is a location, not a role, so those keep the hash rather
# than borrowing a name they have not earned.
_DOMINANT_DIR_MIN_MEMBERS = 3


def _singularize(token: str) -> str:
    """`repositories` -> `repository`, `validators` -> `validator`.

    Archetype names read as a singular role ("controller", "model"), matching
    every entry in the token ladder above.
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _dominant_layer_name(member_paths: Iterable[str]) -> str | None:
    """Name derived from the directory a cohort overwhelmingly shares, or None.

    Reads each member's IMMEDIATE parent directory, which is where a layer name
    lives (``src/repositories/carrier-repository.ts`` -> ``repositories``).
    Returns None when the cohort spans directories, when the shared segment is
    structural rather than a role, or when the result would not sanitize.
    """
    parents: list[str] = []
    for path in member_paths or ():
        segments = [s for s in str(path).replace("\\", "/").split("/")[:-1] if s]
        if segments:
            parents.append(segments[-1].lower())
    if len(parents) < _DOMINANT_DIR_MIN_MEMBERS:
        return None

    top, count = Counter(parents).most_common(1)[0]
    if count / len(parents) < _DOMINANT_DIR_RATIO:
        return None
    if top in _STRUCTURAL_DIRS:
        return None
    return _sanitize(_singularize(top))


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
    if ":" in paths_pattern:
        paths_pattern = paths_pattern.split(":", 1)[0]
    candidate_streams: list[list[str]] = []
    bucket_segs = _segments(paths_pattern)
    if len(bucket_segs) > 1:
        candidate_streams.append(bucket_segs[1:])
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
            ``bootstrap_repo``'s monorepo detection. When
            provided, the TS-prior pipeline strips the matching prefix
            from member paths before running the directory-chain rules
            so workspace-internal layouts (``apps/web/src/components/``,
            ``packages/propel/src/hooks/``) match the prior table.
            Bug F.
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
        base = f"cluster-{_short_hash_for(cluster)}"
        sanitized = _sanitize(base)
        if sanitized is not None:
            base = sanitized
        else:
            base = "cluster-unknown"

    if base not in existing_names:
        return base

    suffix_candidates = _disambiguation_suffixes(cluster, repo_root=repo_root)
    base_segs = set(base.split("-"))
    for suffix in suffix_candidates:
        if suffix in base_segs:
            continue
        candidate = _sanitize(f"{base}-{suffix}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    for i in range(len(suffix_candidates)):
        for j in range(i + 1, len(suffix_candidates)):
            if suffix_candidates[i] in base_segs or suffix_candidates[j] in base_segs:
                continue
            paired = f"{suffix_candidates[i]}-{suffix_candidates[j]}"
            candidate = _sanitize(f"{base}-{paired}")
            if candidate is not None and candidate not in existing_names:
                return candidate

    for i in range(2, 1000):
        candidate = _sanitize(f"{base}-{i}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    return _sanitize(f"{base}-{_short_hash_for(cluster)}") or f"cluster-{_short_hash_for(cluster)}"
