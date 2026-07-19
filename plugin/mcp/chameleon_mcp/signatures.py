"""Cluster signature function — `f: file → ClusterKey`.

Per docs/architecture.md "Cluster signature function":

  sig(file) = (
    path_pattern_bucket,         # /api/**/*.ts → "api-route"
    content_signal_match,        # 'use client', 'use server', shebang, ...
    top_level_node_kinds,        # tuple of ts.SyntaxKind names
    default_export_kind,         # 'FunctionDeclaration' | 'ClassDeclaration' | ...
    named_export_count_bucket,   # 0, 1, 2-4, 5-9, 10+
    import_module_set_hash,      # sha256 of sorted (module, ?default, ?named-set)
    jsx_present                  # bool
  )

Properties (per architecture):
- Computable in a single forEachChild pass on the AST (work happens in ts_dump.mjs)
- Cluster keys are exact-match equivalence classes; archetypes are clusters
- Stability: same input → byte-identical signature (idempotence)
- Per-file cache key: ``(repo_id, path, mtime_ns, cardinality)`` (see
  ``tools._refresh_repo_locked`` max-mtime short-circuit).

The live cache-invalidation lever for the whole profile is
``chameleon_mcp.profile.schema.CURRENT_SCHEMA_VERSION``. A profile whose
``schema_version`` exceeds ``load_profile_dir``'s ``MAX_SUPPORTED_SCHEMA_VERSION``
is refused on load, which the orchestrator handles by re-bootstrapping.
A previous draft kept
a SIGNATURE_FUNCTION_VERSION constant here as a finer-grained lever for
signature-only invalidation, but it was never wired into any cache key
and was therefore documentation theatre; the constant has been removed
so future contributors aren't misled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.conventions import _is_test_path


@dataclass(frozen=True)
class ClusterKey:
    """The 7-tuple cluster signature.

    Frozen + hashable so it can be used as a dict key for clustering.
    """

    path_pattern_bucket: str
    content_signal_match: str
    top_level_node_kinds: tuple[str, ...]
    default_export_kind: str | None
    named_export_count_bucket: str
    import_module_set_hash: str
    jsx_present: bool

    def to_dict(self) -> dict:
        """Stable JSON-serializable form for caching in drift.db."""
        return {
            "path_pattern_bucket": self.path_pattern_bucket,
            "content_signal_match": self.content_signal_match,
            "top_level_node_kinds": list(self.top_level_node_kinds),
            "default_export_kind": self.default_export_kind,
            "named_export_count_bucket": self.named_export_count_bucket,
            "import_module_set_hash": self.import_module_set_hash,
            "jsx_present": self.jsx_present,
        }


def bucket_named_export_count(count: int) -> str:
    """Bucket the named-export count into stable categories.

    Stability rationale: file with 4 exports vs 5 exports might both be the same
    archetype; bucketing prevents spurious cluster fragmentation.
    """
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    if count <= 9:
        return "5-9"
    return "10+"


# Top-level dirs that hold per-workspace package roots in a monorepo, so the
# workspace name is preserved in the bucket (packages/auth/* must not collapse
# into packages/billing/*). "libs" is the Nx workspace root (JS and Python Nx
# monorepos alike). "src" is deliberately NOT here: it is the dominant
# single-package source root (Python src-layout, TS src/), so treating it as a
# workspace root would re-bucket ordinary deeply-nested source trees.
_MONOREPO_WORKSPACE_ROOTS: frozenset[str] = frozenset(
    {
        "packages",
        "apps",
        "workspaces",
        "libs",
    }
)


# Django (and DRF) express a file's role in its FILENAME, not a directory chain
# like Rails (app/models/). So a Python file named ``models.py`` is a "model"
# regardless of which app it lives in. Mapping a known role filename -> a clean
# singular archetype name lets clustering group all ``models.py`` across apps
# into one cross-app "model" archetype, which is how Django developers reason.
_PY_ROLE_NAMES: dict[str, str] = {
    "models": "model",
    "views": "view",
    "serializers": "serializer",
    "admin": "admin",
    "urls": "urls",
    "forms": "form",
    "apps": "app-config",
    "signals": "signal",
    "tasks": "task",
    "managers": "manager",
    "permissions": "permission",
    "filters": "filter",
    "middleware": "middleware",
    "viewsets": "viewset",
    "schemas": "schema",
    "querysets": "queryset",
    "consumers": "consumer",
    "routing": "routing",
    "validators": "validator",
    "decorators": "decorator",
    "migrations": "migration",
    # Flask / FastAPI web layer (freeform frameworks; the routes/routers/
    # endpoints dir is the role signal since route files have generic names).
    "routes": "route",
    "routers": "route",
    "router": "route",
    "endpoints": "route",
    "blueprints": "blueprint",
    "deps": "dependency",
    "dependencies": "dependency",
    "schema": "schema",
    "crud": "crud",
}

# Directory names that imply a role for the PACKAGE form: a big app splits
# ``models.py`` into ``models/__init__.py`` + ``models/base.py``, so any file
# under a ``models/`` (or ``views/`` ...) package is still a model. A subset of
# the role names above -- only the dirs that conventionally hold many files.
_PY_ROLE_DIRS: frozenset[str] = frozenset(
    {
        "models",
        "views",
        "serializers",
        "admin",
        "migrations",
        "forms",
        "tasks",
        "managers",
        "viewsets",
        "permissions",
        "filters",
        "consumers",
        "routes",
        "routers",
        "endpoints",
        "blueprints",
        # FastAPI/Pydantic package forms: app/schemas/*.py holds BaseModel
        # schemas and app/dependencies/ (or deps/) holds Depends() providers.
        # Both were already role names for the FILENAME form above; without the
        # package form a 2-file dependencies/ dir fell through to path-prefix
        # fallback and matched the schemas archetype's Pydantic guidance.
        "schemas",
        "dependencies",
        "deps",
        "crud",
    }
)


def python_role_for_path(file_path: str) -> str | None:
    """Return the Django/DRF role archetype for a ``.py`` path, or None.

    Self-gated on the ``.py`` / ``.pyi`` extension so it never reshapes a
    TypeScript or Ruby file. Recognizes the role from the basename
    (``models.py`` -> ``model``) first, then the PACKAGE form where a parent
    directory is a role dir (``shop/models/base.py`` -> ``model``). Test files
    are intentionally NOT a role here -- they fall through to the existing
    test-archetype machinery. Ordinary files (``utils.py``) return None and are
    directory-bucketed like any other source file.
    """
    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if not parts:
        return None
    last = parts[-1]
    if last.endswith(".py"):
        stem = last[:-3]
    elif last.endswith(".pyi"):
        stem = last[:-4]
    else:
        return None

    # A test file routes to the test archetype, never the production role of
    # its basename or an enclosing role dir (tests/models.py is a test, not a
    # model; tests/api/routes/test_users.py is a test, not a route).
    if _is_test_path(file_path, language="python"):
        return None

    role = _PY_ROLE_NAMES.get(stem)
    if role is not None:
        return role
    dirs = parts[:-1]
    for d in reversed(dirs):
        if d in _PY_ROLE_DIRS:
            return _PY_ROLE_NAMES[d]
    # Alembic revision files live in a `versions/` dir under `alembic/` (or a
    # `migrations/` root). Their auto-generated `revision`/`upgrade`/`downgrade`
    # globals are migration internals, not app symbols -- role them as migrations
    # so they cluster apart instead of polluting the generic app archetype (whose
    # "reuse these" list would otherwise hand a model edit `op`/`revision`). Gated
    # on the alembic/migrations ancestor so a plain `versions/` dir is untouched.
    if "versions" in dirs and ("alembic" in dirs or "migrations" in dirs):
        return "migration"
    return None


# Next.js app-router expresses a file's role in a SPECIAL FILENAME (page.tsx,
# layout.tsx, ...) that sits in a per-route directory, NOT in a role directory
# the way Django uses models/. So app-router role files SCATTER one per route
# dir (app/page.tsx, app/dashboard/page.tsx, app/about/page.tsx); plain
# directory bucketing makes each its own below-threshold bucket and the "page"
# archetype never forms in small/medium repos (large repos cluster only because
# their app tree has enough sibling files). Bucketing by the filename role lets
# every page.tsx across route dirs cluster into one app-page archetype, the same
# way python_role_for_path groups models.py across Django apps. route.ts is
# deliberately EXCLUDED: app/api/**/route.ts already co-locates under one
# app/api directory and clusters as app-route-handler, so role-bucketing it
# would only disturb a cluster that already forms.
_NEXT_APP_ROLE_NAMES: dict[str, str] = {
    "page": "app-page",
    "layout": "app-layout",
    "loading": "app-special",
    "error": "app-special",
    "global-error": "app-special",
    "not-found": "app-special",
    "template": "app-special",
    "default": "app-special",
}

# Source-root directory names that are NOT Next.js route segments. Rails puts its
# TS/JS under app/javascript (Webpacker/jsbundling) or the legacy
# app/assets/javascripts, so a file stem-named page/layout/error living under one
# of those is a Rails file, not an app-router role. Kept DELIBERATELY NARROW to
# the two names that are unambiguously Rails JS roots and never plausible Next.js
# route names -- `assets`/`images`/`fonts`/`stylesheets` are all valid Next.js
# route segments (e.g. an /images gallery route), so excluding them would drop
# genuine app-router pages. The legacy app/assets/javascripts path is still caught
# because `javascripts` appears as a segment.
_NEXT_NON_ROUTE_DIRS: frozenset[str] = frozenset({"javascript", "javascripts"})


def nextjs_role_for_path(file_path: str) -> str | None:
    """Return the Next.js app-router role archetype for a TS/JS path, or None.

    Self-gated: fires only for a recognized app-router special filename
    (``page``/``layout``/``loading``/``error``/``not-found``/``template``/
    ``default``/``global-error``) that sits UNDER an ``app/`` segment -- the
    app-router signal. ``route.ts`` is intentionally absent from the role map (it
    already co-locates under ``app/api``). A ``page.tsx`` outside ``app/``, a
    Pages-router ``index.tsx``, a NestJS ``*.controller.ts``, or a plain
    component all return None and are directory-bucketed unchanged, so this never
    reshapes a non-app-router file.
    """
    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        return None
    last = parts[-1]
    for ext in (".tsx", ".ts", ".jsx", ".js"):
        if last.endswith(ext):
            stem = last[: -len(ext)]
            break
    else:
        return None
    role = _NEXT_APP_ROLE_NAMES.get(stem)
    if role is None:
        return None
    if "app" not in parts[:-1]:
        return None
    # The `app` ancestor must be a Next.js ROUTING root, not a Rails source root
    # named `app`. `javascript`/`javascripts` CAN be a real Next.js route name
    # (a /javascript tutorial route), so a bare directory-name match over-excludes.
    # The discriminator is depth: Rails nests its source DEEP under app/javascript
    # (app/javascript/entrypoints/error.ts, app/javascript/packs/...), while a
    # Next.js route file sits DIRECTLY in its route segment (app/docs/javascript/
    # page.tsx). So exclude only when the Rails source name is an ANCESTOR of the
    # file's immediate route segment -- ``parts[app_idx+1 : -2]`` drops the segment
    # itself from the check, preserving a route literally named /javascript while
    # still excluding the deep Rails app/javascript tree.
    app_idx = parts.index("app")
    if any(seg in _NEXT_NON_ROUTE_DIRS for seg in parts[app_idx + 1 : -2]):
        return None
    return role


# NestJS / Angular co-locate by feature (users/users.controller.ts), so the role
# lives in the filename SUFFIX, not a directory. These are the framework-distinctive
# suffixes; the mapped name mirrors bootstrap.naming's role priors so the role
# cluster gets a proper archetype name. .controller/.resolver/.gateway are
# NestJS-specific; .service/.module/.guard are shared with Angular (role grouping is
# correct for both).
_NESTJS_ROLE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".controller.ts", "controller"),
    (".resolver.ts", "resolver"),
    (".gateway.ts", "gateway"),
    (".service.ts", "service"),
    (".module.ts", "module"),
    (".guard.ts", "guard"),
    # The roles a real Nest codebase carries most, which the original six left
    # to directory bucketing. Measured on a 6-feature Nest API: `.dto.ts` is the
    # single LARGEST role at 17 files, ahead of every mapped suffix, plus
    # `.repository.ts` (6), `.entity.ts` (6), `.interceptor.ts` (2), `.filter.ts`
    # and `.decorator.ts` -- 33 files that never reached a per-role sample size
    # and instead formed per-feature mixed clusters, leaving 54% of the repo's
    # archetypes named `cluster-<hash>`.
    #
    # `.config.ts` is deliberately NOT here: it is not Nest-distinctive (Next.js,
    # Vite and Jest all use it) and the config-module prior already covers the
    # Nest case.
    (".dto.ts", "dto"),
    (".entity.ts", "entity"),
    (".repository.ts", "repository"),
    (".interceptor.ts", "interceptor"),
    (".filter.ts", "filter"),
    (".decorator.ts", "decorator"),
    (".pipe.ts", "pipe"),
    (".middleware.ts", "middleware"),
    (".strategy.ts", "strategy"),
)


def nestjs_role_for_path(file_path: str) -> str | None:
    """Return the NestJS/Angular filename-role archetype for a TS path, or None.

    NestJS and Angular co-locate by feature (``users/users.controller.ts``), so the
    role lives in the filename suffix. A file whose name ends in a framework
    suffix (``.controller.ts`` / ``.service.ts`` / ``.module.ts`` / ...) buckets by
    ROLE across feature directories -- the same cross-dir merge Django and Next.js
    already get. Without it each feature directory forms its own mixed cluster
    (controller + service + module) that never reaches a per-role sample size, so
    the role's class contract and reusable exports never derive and the per-edit
    block degrades to a mixed ``cluster-*`` witness. A plain component or a
    non-suffixed ``.ts`` returns None and is directory-bucketed unchanged, so this
    never reshapes a file that isn't a recognized framework role.
    """
    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        return None
    last = parts[-1].lower()
    for suffix, role in _NESTJS_ROLE_SUFFIXES:
        if last.endswith(suffix):
            return role
    return None


def path_pattern_bucket_for(
    file_path: str,
    archetype_paths: dict[str, list[str]] | None = None,
    *,
    include_extension: bool = False,
) -> tuple[str, str]:
    """Bucket a file path so files with the same role cluster together.

    Returns a 2-tuple ``(bucket, sub_bucket)`` where:
    - ``bucket`` is the shallow cluster key used as ``ClusterKey.path_pattern_bucket``.
    - ``sub_bucket`` is the deeper remaining path (empty string when the path
      is not deep enough to have one). Stored as cluster metadata to preserve
      visibility into subdirectory structure without fragmenting clusters.

    `archetype_paths` is accepted as a forward-compat parameter for future
    glob-against-known-archetypes matching, but the current implementation
    always uses path-segment bucketing because that's the only signal
    available during the initial bootstrap pass (no archetypes exist yet).

    Schema v4 used `parts[-3:-1]` — the 2 directory segments immediately
    enclosing the file. That was fine for granularity but collapsed
    `app/controllers/api/v1/foo.rb` and `spec/controllers/api/v1/foo_spec.rb`
    into the same `"api/v1"` bucket, and tools that picked a primary
    archetype by cluster_size routinely surfaced the spec cluster for an
    `app/` file.

    Schema v5 keeps the same enclosing-directory information but prepends
    the top-level segment so `app/...` and `spec/...` always disambiguate.
    For shallow paths (≤3 segments) the result is just `parts[0]/parts[-2]`,
    matching v4's behavior on those paths.

    Bug 1, opt-in via ``include_extension``: when True, append
    ``:<ext>`` (e.g. ``:tsx``) to the bucket so ``.tsx`` and ``.ts`` files
    in the same directory don't share a cluster. The clustering pipeline
    flips this on; ``get_archetype`` keeps the default (False) so
    profiles' ``paths_pattern`` strings still match without migration.

    Bug 2: when ``parts[0]`` is a monorepo workspace root
    (``packages``, ``apps``, ``workspaces``) and the path has at least 4
    segments, ``parts[1]`` (the workspace name) is preserved so files from
    distinct workspaces don't collide on identical sub-directory shapes.
    An earlier formula ``parts[0]/parts[-3]/parts[-2]`` dropped the
    workspace name for any ≥5-part monorepo path.

    Option 4: for non-monorepo paths with 4+ segments, the bucket
    depth dropped from 3 to 2 (``CLUSTER_PATH_BUCKET_DEPTH`` env var,
    default 2). ``app/services/zoom/recordings.rb`` now maps to bucket
    ``app/services`` (not ``app/services/zoom``), collapsing the long tail
    of subdirectory fragmentation. The deeper ``parts[-3]/parts[-2]``
    information is returned as ``sub_bucket`` so cluster metadata still
    records which subdirectories contributed.
    """
    del archetype_paths

    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        bucket = "(root)"
        if include_extension and parts:
            ext = _extension_of(parts[-1])
            if ext:
                bucket = f"{bucket}:{ext}"
        return (bucket, "")

    # Django role bucketing: a known role filename (models.py, views.py, ...)
    # buckets by ROLE across apps, not by app directory. The sub_bucket is empty
    # so the cross-app merge survives _split_by_sub_bucket -- a per-app sub_bucket
    # would shatter the "model" cluster straight back into per-app clusters.
    py_role = python_role_for_path(file_path)
    if py_role is not None:
        bucket = py_role
        if include_extension:
            ext = _extension_of(parts[-1])
            if ext:
                bucket = f"{bucket}:{ext}"
        return (bucket, "")

    # Next.js app-router role bucketing: page.tsx/layout.tsx/... scatter one per
    # route dir, so bucket by the filename role to cluster them. UNLIKE the
    # Django bucketer above, the monorepo workspace prefix is preserved
    # (``apps/web`` vs ``apps/admin`` are distinct Next.js apps whose pages must
    # not merge). sub_bucket is empty so the within-app cross-route merge
    # survives _split_by_sub_bucket. Naming falls out of the existing _TS_PRIORS
    # ``app-page-component``/``app-layout`` entries (they read member_paths, not
    # the bucket), so no naming change is needed here.
    next_role = nextjs_role_for_path(file_path)
    if next_role is not None:
        if len(parts) >= 4 and parts[0] in _MONOREPO_WORKSPACE_ROOTS:
            bucket = f"{parts[0]}/{parts[1]}/{next_role}"
        else:
            bucket = next_role
        if include_extension:
            ext = _extension_of(parts[-1])
            if ext:
                bucket = f"{bucket}:{ext}"
        return (bucket, "")

    # NestJS / Angular filename-role bucketing (below Next.js so app/pages routing
    # wins first). Same shape as the Next.js branch: role bucket, empty sub_bucket
    # so the cross-feature-dir merge survives _split_by_sub_bucket, monorepo
    # workspace prefix preserved so two apps' controllers don't collide.
    nest_role = nestjs_role_for_path(file_path)
    if nest_role is not None:
        if len(parts) >= 4 and parts[0] in _MONOREPO_WORKSPACE_ROOTS:
            bucket = f"{parts[0]}/{parts[1]}/{nest_role}"
        else:
            bucket = nest_role
        if include_extension:
            ext = _extension_of(parts[-1])
            if ext:
                bucket = f"{bucket}:{ext}"
        return (bucket, "")

    sub_bucket = ""
    if len(parts) >= 4 and parts[0] in _MONOREPO_WORKSPACE_ROOTS:
        bucket = f"{parts[0]}/{parts[1]}/{parts[2]}"
    elif len(parts) >= 4:
        depth = threshold_int("CLUSTER_PATH_BUCKET_DEPTH")
        if depth >= 3:
            bucket = f"{parts[0]}/{parts[-3]}/{parts[-2]}"
        else:
            bucket = f"{parts[0]}/{parts[1]}"
            if len(parts) >= 4:
                inner = parts[2:-1]
                sub_bucket = "/".join(inner) if inner else ""
    else:
        bucket = f"{parts[0]}/{parts[-2]}"

    if include_extension:
        ext = _extension_of(parts[-1])
        if ext:
            bucket = f"{bucket}:{ext}"
    return (bucket, sub_bucket)


def _extension_of(filename: str) -> str:
    """Return the file extension (without leading dot), or '' if none.

    Examples:
      "Foo.tsx"           -> "tsx"
      "helper.ts"         -> "ts"
      "page.test.tsx"     -> "tsx"   (final dot only)
      "Dockerfile"        -> ""
      ".gitignore"        -> ""      (leading dot is not an extension)
    """
    dot = filename.rfind(".")
    if dot <= 0:
        return ""
    return filename[dot + 1 :]


def content_signal_match_for(
    content_first_200_bytes: str,
    archetype_signals: dict[str, dict] | None = None,
) -> str:
    """Detect file-level lexical directives in the first 200 bytes.

    Per the architecture's content_signal boundary rule:
      content_signal only encodes file-level lexical directives that appear
      in the first 200 bytes of the file. Anything that requires AST traversal,
      type information, or class-body inspection is idioms.md territory.

    Phase 2A returns a coarse signal: known directive present, or "none".
    Phase 2B integrates archetype-specific signal matching against the
    active profile's archetypes.json content_signal definitions.
    """
    head = content_first_200_bytes
    if '"use client"' in head or "'use client'" in head:
        return "use_client"
    if '"use server"' in head or "'use server'" in head:
        return "use_server"
    if head.startswith("#!"):
        return "shebang"
    if head.lstrip().startswith("// @ts-"):
        return "ts_pragma"
    del archetype_signals
    return "none"


def compute_signature(
    file_path: str,
    content_first_200_bytes: str,
    top_level_node_kinds: Sequence[str],
    default_export_kind: str | None,
    named_export_count: int,
    import_specifiers: Sequence[tuple[str, str]],
    has_jsx: bool,
    *,
    archetype_paths: dict[str, list[str]] | None = None,
    archetype_signals: dict[str, dict] | None = None,
    include_extension_in_bucket: bool = False,
) -> ClusterKey:
    """Compute the 7-tuple cluster signature for a parsed file.

    Inputs come from the ts_dump.mjs subprocess (extractors/typescript.py
    deserializes one ParsedFile per stdin line and calls this function).

    Outputs are exact-equality bucketed; clusters group files with identical
    signatures.

    ``include_extension_in_bucket`` forwards to
    :func:`path_pattern_bucket_for`; the clustering pipeline turns this on
    so ``.tsx`` and ``.ts`` files in the same dir cluster separately
    (Bug 1). Callers that need backward-compatible bucket strings
    (e.g. ``get_archetype`` reading legacy ``paths_pattern`` entries)
    leave it False.
    """
    bucket, _sub = path_pattern_bucket_for(
        file_path,
        archetype_paths,
        include_extension=include_extension_in_bucket,
    )
    del import_specifiers  # intentionally not part of the cluster key (see below)
    return ClusterKey(
        path_pattern_bucket=bucket,
        content_signal_match=content_signal_match_for(content_first_200_bytes, archetype_signals),
        # Order- and multiplicity-insensitive so clustering AGREES with the
        # runtime lint conformance check (lint_engine compares node kinds as a
        # coarse set). The old order-sensitive tuple over-fragmented files that
        # are the same archetype but declare members in a different order.
        top_level_node_kinds=tuple(sorted(set(top_level_node_kinds))),
        default_export_kind=default_export_kind,
        named_export_count_bucket=bucket_named_export_count(named_export_count),
        # Dropped from the cluster key. The exact import-module set made every
        # service its own cluster (each imports its own deps) even though the
        # lint engine ignores imports entirely — the single largest source of
        # over-fragmentation. Clustering is now by shape + path. Import
        # conventions are still derived separately (conventions.extract_import_conventions).
        import_module_set_hash="",
        jsx_present=has_jsx,
    )
