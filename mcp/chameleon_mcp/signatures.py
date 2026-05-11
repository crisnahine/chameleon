"""Cluster signature function — `f: file → ClusterKey`.

Per ARCHITECTURE.md "Cluster signature function":

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
- Cache cell: keyed by (path, content_sha256, sig_function_version, ts_version)

Cache invalidation triggers:
- TS version bump → invalidate all cached sigs
- tsconfig.json change affecting parse mode → invalidate all
- This module's SIGNATURE_FUNCTION_VERSION bump → invalidate all
- Per-file content change (sha mismatch) → invalidate that file
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

# Bumped when the signature function's behavior changes.
# Forces drift.db cache invalidation per ARCHITECTURE.md "Incremental algorithm".
SIGNATURE_FUNCTION_VERSION = 1


@dataclass(frozen=True, slots=True)
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


def hash_import_set(import_specifiers: Sequence[tuple[str, str]]) -> str:
    """Compute a stable sha256 hex digest of a file's import set.

    The set is sorted by (module_name, kind) to ensure deterministic output
    regardless of source order in the file.

    Pre-condition: each entry is (module_name, kind) where kind ∈
    {"default", "named", "namespace"}.
    """
    sorted_imports = sorted(import_specifiers)
    canonical = "\n".join(f"{module}\t{kind}" for module, kind in sorted_imports)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# v0.5.2 (Bug 2): top-level segments that announce "this is a monorepo
# workspace dir, the package name follows" — when one of these is at
# parts[0] AND parts[1] looks like a workspace name, keep parts[1] in
# the bucket so files from sibling workspaces (e.g.
# packages/excalidraw/components/X.tsx vs packages/element/components/X.tsx)
# don't collide. Schema-v6 bucketing was monorepo-blind: any path of the
# form `packages/<ws>/components/Group/X.tsx` collapsed to
# "packages/components/Group" regardless of the workspace name.
_MONOREPO_WORKSPACE_ROOTS: frozenset[str] = frozenset({
    "packages",
    "apps",
    "workspaces",
})


def path_pattern_bucket_for(
    file_path: str,
    archetype_paths: dict[str, list[str]] | None = None,
    *,
    include_extension: bool = False,
) -> str:
    """Bucket a file path so files with the same role cluster together.

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

    v0.5.2 (Bug 1, opt-in via ``include_extension``): when True, append
    ``:<ext>`` (e.g. ``:tsx``) to the bucket so ``.tsx`` and ``.ts`` files
    in the same directory don't share a cluster. The clustering pipeline
    flips this on; ``get_archetype`` keeps the default (False) so v0.5.x
    profiles' ``paths_pattern`` strings still match without migration.

    v0.5.2 (Bug 2): when ``parts[0]`` is a monorepo workspace root
    (``packages``, ``apps``, ``workspaces``) and the path has at least 4
    segments, ``parts[1]`` (the workspace name) is preserved so files from
    distinct workspaces don't collide on identical sub-directory shapes.
    The pre-v0.5.2 formula ``parts[0]/parts[-3]/parts[-2]`` dropped the
    workspace name for any ≥5-part monorepo path.
    """
    del archetype_paths  # reserved for forward-compat; not used today

    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        return "(root)"
    # Examples:
    #   app/controllers/api/v1/users.rb       → "app/api/v1"
    #   spec/controllers/api/v1/users_spec.rb → "spec/api/v1"
    #   app/models/listing.rb                 → "app/models"
    #   src/components/base/Button.tsx        → "src/components/base"
    #   src/components/Button.tsx             → "src/components"
    #   Gemfile (1 part)                      → "(root)"
    #   packages/excalidraw/components/Foo.tsx → "packages/excalidraw/components"
    #   apps/web/routes/(marketing)/page.tsx  → "apps/web/routes"
    if (
        len(parts) >= 4
        and parts[0] in _MONOREPO_WORKSPACE_ROOTS
    ):
        # Monorepo: anchor on the workspace name (parts[1]) plus the
        # workspace-internal top-level segment (parts[2]). This keeps
        # sibling workspaces (packages/excalidraw/... vs
        # packages/element/...) discriminated AND keeps every file inside
        # a workspace's top-level directory in the same cluster — which
        # is the same coarseness as `src/components` for a non-monorepo
        # repo, just one segment deeper.
        bucket = f"{parts[0]}/{parts[1]}/{parts[2]}"
    elif len(parts) >= 4:
        bucket = f"{parts[0]}/{parts[-3]}/{parts[-2]}"
    else:
        bucket = f"{parts[0]}/{parts[-2]}"

    if include_extension:
        # Suffix the file's extension so ``.tsx`` and ``.ts`` (or ``.js``
        # and ``.jsx``) in the same dir cluster separately. JSX-vs-non-JSX
        # re-discrimination happens downstream, but by then the cluster's
        # already been forced — keying the bucket on extension prevents
        # that.
        ext = _extension_of(parts[-1])
        if ext:
            bucket = f"{bucket}:{ext}"
    return bucket


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
        # No dot, or leading dot (".gitignore" → no extension to bucket on).
        return ""
    return filename[dot + 1:]


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
    # Common TS/JS file-level directives
    if '"use client"' in head or "'use client'" in head:
        return "use_client"
    if '"use server"' in head or "'use server'" in head:
        return "use_server"
    if head.startswith("#!"):
        return "shebang"
    if head.lstrip().startswith("// @ts-"):
        return "ts_pragma"
    # The `archetype_signals` parameter is a forward-compat hook: future
    # archetypes may declare their own first-200-byte directives beyond the
    # universal set above (e.g., a team's `# typed: strict` Sorbet pragma).
    # Wire-through would compare `head` against the archetype's stored
    # signal table here. Today's set covers every signal the bootstrap
    # pipeline actually clusters on; per-archetype signals add no value
    # until calibration evidence shows a real per-team divergence.
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
    (v0.5.2 Bug 1). Callers that need backward-compatible bucket strings
    (e.g. ``get_archetype`` reading v0.5.x ``paths_pattern`` entries)
    leave it False.
    """
    return ClusterKey(
        path_pattern_bucket=path_pattern_bucket_for(
            file_path,
            archetype_paths,
            include_extension=include_extension_in_bucket,
        ),
        content_signal_match=content_signal_match_for(
            content_first_200_bytes, archetype_signals
        ),
        top_level_node_kinds=tuple(top_level_node_kinds),
        default_export_kind=default_export_kind,
        named_export_count_bucket=bucket_named_export_count(named_export_count),
        import_module_set_hash=hash_import_set(import_specifiers),
        jsx_present=has_jsx,
    )
