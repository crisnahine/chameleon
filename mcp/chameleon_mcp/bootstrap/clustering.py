"""Cluster ParsedFile records by ClusterKey signature.

Per ARCHITECTURE.md "Cluster signature function" → "Incremental algorithm":
- Same ClusterKey → same cluster (exact-match equivalence)
- Clusters become candidate archetypes (named in Phase 2C interview)
- Sparse cluster threshold: <5 files → candidate for "miscellaneous" or merge
- Bimodal threshold: clusters split 60/40 or worse on a key dimension are flagged

Phase 2C.3 adds bimodal detection: a cluster's members may all share the
seven-tuple ClusterKey while still disagreeing on a separately-observable
dimension (e.g. default_export_kind drawn from the ParsedFile, which is
fixed for any single ClusterKey, vs. `named_export_count` raw counts which
are bucketed but whose raw distribution still varies). The signature
function is intentionally lossy; bimodal detection reveals when that loss
hides a real split the bootstrap interview should surface.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.signatures import ClusterKey, bucket_named_export_count, compute_signature

# Files in clusters smaller than this aren't proposed as their own archetype
# without explicit user confirmation.
SPARSE_CLUSTER_THRESHOLD = 5

# A cluster is "bimodal" if, on at least one observable dimension, the
# most-common value holds STRICTLY LESS than this fraction of its members.
# i.e. a 60/40 split crosses the threshold (40% on the minority = 60% on the
# majority, and 60% < 60% is false, so 60/40 exact is the boundary — see
# `is_bimodal` docstring for the precise definition).
BIMODAL_DOMINANT_SHARE_THRESHOLD = 0.6

# Dimensions inspected for bimodality. These are signals the ClusterKey
# already encodes (or bucketizes) but where the raw per-file value can still
# vary inside a single cluster — e.g. two files in the same path bucket
# may have different default_export_kind values yet land in the same cluster
# because of identical top_level_node_kinds + identical import hash, with
# default_export_kind being the discriminator buried in the tuple. Only
# dimensions where intra-cluster variance is actually possible go here.
BIMODAL_INSPECTED_DIMENSIONS: tuple[str, ...] = (
    "default_export_kind",
    "named_export_count_bucket",
    "jsx_present",
    "content_signal_match",
)


@dataclass
class Cluster:
    """A group of files sharing a cluster signature."""

    key: ClusterKey
    members: list[ParsedFile] = field(default_factory=list)
    # v0.5.2 (Bug 4): per-cluster sparse threshold so the adaptive
    # heuristic in `cluster_files` can be reflected in `.is_sparse`
    # without changing the module-level default. Callers that build
    # Cluster instances directly (older tests) inherit the v0.5.1
    # threshold-5 behavior unchanged.
    sparse_threshold: int = SPARSE_CLUSTER_THRESHOLD
    # BUG-002 (v0.5.6): loose-merge clustering tier. Clusters created by
    # the tight (exact-signature) pass have tier "tight"; clusters formed
    # by the second-pass loose merge — same paths_pattern + AST shape
    # Jaccard >= 0.5 — get tier "loose" so consumers can distinguish.
    cluster_tier: str = "tight"

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_sparse(self) -> bool:
        return self.size < self.sparse_threshold

    def _dimension_value_for(self, member: ParsedFile, dimension: str) -> object:
        """Return the raw per-member value of an observable dimension.

        For dimensions encoded in the ClusterKey but bucketed (e.g.
        named_export_count_bucket), this returns the bucketed value of the
        member's raw count — NOT the cluster-key value — so two members
        with different bucket values fall into separate counter bins.

        Members of a single cluster share the ClusterKey by construction,
        so for dimensions that come directly off the key the counter
        degenerates to size-of-one. The interesting dimensions here are
        ones that *could* split inside a cluster if upstream signature
        derivation ever weakens — defensive coverage for forward changes.
        """
        if dimension == "default_export_kind":
            return member.default_export_kind
        if dimension == "named_export_count_bucket":
            return bucket_named_export_count(member.named_export_count)
        if dimension == "jsx_present":
            return member.has_jsx
        if dimension == "content_signal_match":
            # Inspect the first 200 bytes via the same matcher used for
            # signature derivation so this stays in lockstep with the key.
            from chameleon_mcp.signatures import content_signal_match_for
            return content_signal_match_for(member.content_first_200_bytes)
        return None

    def dimension_distribution(self, dimension: str) -> dict[object, int]:
        """Return a Counter-style dict of {value: count} for `dimension`.

        Empty when the cluster has no members.
        """
        return dict(Counter(
            self._dimension_value_for(m, dimension) for m in self.members
        ))

    @property
    def bimodal_dimensions(self) -> list[str]:
        """Names of inspected dimensions on which this cluster splits bimodally.

        A dimension is bimodal when the most-common value holds STRICTLY
        LESS than BIMODAL_DOMINANT_SHARE_THRESHOLD of the members. A
        cluster with only one member, or no minority value at all, is
        never bimodal.

        60/40 exact: dominant share = 0.6, which is NOT < 0.6, so a clean
        60/40 split is the boundary case and reports as non-bimodal. 59/41,
        50/50, etc. all flag. This matches the spec's "60/40 or worse" —
        worse = the minority share grows.
        """
        if self.size < 2:
            return []
        flagged: list[str] = []
        for dim in BIMODAL_INSPECTED_DIMENSIONS:
            dist = self.dimension_distribution(dim)
            if len(dist) < 2:
                continue
            dominant_count = max(dist.values())
            if (dominant_count / self.size) < BIMODAL_DOMINANT_SHARE_THRESHOLD:
                flagged.append(dim)
        return flagged

    @property
    def is_bimodal(self) -> bool:
        return bool(self.bimodal_dimensions)


@dataclass
class ClusteringResult:
    """Output of clustering a parsed corpus."""

    clusters: list[Cluster]
    """All clusters, sorted by size descending (largest first)."""

    skipped_generated: list[ParsedFile] = field(default_factory=list)
    """Files identified as auto-generated by content heuristic; excluded from clustering."""

    @property
    def cluster_count(self) -> int:
        return len(self.clusters)

    @property
    def total_files_clustered(self) -> int:
        return sum(c.size for c in self.clusters)

    @property
    def sparse_clusters(self) -> list[Cluster]:
        return [c for c in self.clusters if c.is_sparse]

    @property
    def dense_clusters(self) -> list[Cluster]:
        return [c for c in self.clusters if not c.is_sparse]

    @property
    def bimodal_clusters(self) -> list[Cluster]:
        """Clusters that split bimodally on at least one observable dimension.

        Bimodal flagging considers ALL clusters (sparse + dense). A sparse
        cluster that also splits bimodally is interesting twice: the
        bootstrap report surfaces both warnings so the future interview UI
        (Phase 2D) can either rename it, merge it, or split it manually.
        """
        return [c for c in self.clusters if c.is_bimodal]


def _adaptive_sparse_threshold(total_files: int) -> int:
    """Pick a sparse-cluster threshold based on corpus size.

    The v0.5.1 hard-coded threshold of 5 killed recall on feature-per-folder
    layouts (excalidraw: 94.8% sparse warnings; mastodon: 0 archetypes from
    856 files). Repos under ~1k files routinely have meaningful clusters
    with 3–4 members; lowering the floor lets the long tail surface.

    Heuristic (corpus-size tiered):
      - total_files <  1000 → threshold = 3
      - total_files <  5000 → threshold = 4
      - total_files >= 5000 → threshold = 5 (v0.5.1 behavior preserved
                              for large monorepos where 5+ members is
                              easy to clear and noisier clusters dominate)

    These cutoffs are intentionally coarse so the rule is easy to reason
    about during bootstrap review. Callers needing determinism in tests
    pass an explicit ``min_cluster_size`` to ``cluster_files``.
    """
    if total_files < 1000:
        return 3
    if total_files < 5000:
        return 4
    return 5


def cluster_files(
    parsed_files: Iterable[ParsedFile],
    repo_root: Path | None = None,
    *,
    min_cluster_size: int | None = None,
) -> ClusteringResult:
    """Group parsed files by ClusterKey signature.

    Args:
        parsed_files: iterable of successfully-parsed files (from
                      TypeScriptExtractor.parse_repo().files)
        repo_root: repo root used to relativize each file's path before
                   bucketing. Required for the path-pattern bucket to match
                   what the runtime archetype lookup computes (also from a
                   repo-relative path). Optional for backward compatibility;
                   callers that don't pass it get absolute-path bucketing.
        min_cluster_size: explicit sparse threshold. When ``None`` (the
                   default) the adaptive heuristic in
                   :func:`_adaptive_sparse_threshold` picks a value based
                   on the corpus size — 3 for repos < 1k files, 4 for
                   1k–5k, 5 for larger. Tests pass an explicit value for
                   determinism; the orchestrator passes ``None`` so real
                   repos get the adaptive behavior (v0.5.2 Bug 4).

    Returns:
        ClusteringResult with clusters sorted by size (largest first) for
        deterministic archetype proposal ordering. Each cluster's
        ``sparse_threshold`` reflects the resolved threshold so
        ``cluster.is_sparse`` agrees with ``ClusteringResult.sparse_clusters``.
    """
    from chameleon_mcp.bootstrap.discovery import is_likely_generated

    by_key: dict[ClusterKey, list[ParsedFile]] = defaultdict(list)
    skipped_generated: list[ParsedFile] = []

    for pf in parsed_files:
        # Generated-code heuristic on file content head
        if is_likely_generated(pf.content_first_200_bytes):
            skipped_generated.append(pf)
            continue

        if repo_root is not None:
            try:
                file_path_for_signature = str(pf.path.relative_to(repo_root))
            except ValueError:
                file_path_for_signature = str(pf.path)
        else:
            file_path_for_signature = str(pf.path)

        # v0.5.2 (Bug 1): clustering keys bucket on extension so .tsx
        # and .ts files in the same dir cluster separately. The runtime
        # archetype lookup keeps the extension-blind bucket so old
        # v0.5.x profiles remain matchable.
        key = compute_signature(
            file_path=file_path_for_signature,
            content_first_200_bytes=pf.content_first_200_bytes,
            top_level_node_kinds=pf.top_level_node_kinds,
            default_export_kind=pf.default_export_kind,
            named_export_count=pf.named_export_count,
            import_specifiers=pf.import_specifiers,
            has_jsx=pf.has_jsx,
            include_extension_in_bucket=True,
        )
        by_key[key].append(pf)

    # v0.5.2 (Bug 4): adaptive sparse threshold. Resolved AFTER bucketing
    # so the threshold reflects the actual clustered file count, not the
    # candidate-before-generated-filter count.
    total_clustered = sum(len(members) for members in by_key.values())
    if min_cluster_size is None:
        resolved_threshold = _adaptive_sparse_threshold(total_clustered)
    else:
        resolved_threshold = max(1, int(min_cluster_size))

    clusters = [
        Cluster(key=k, members=members, sparse_threshold=resolved_threshold)
        for k, members in by_key.items()
    ]

    # BUG-002 (v0.5.6): loose-merge second pass. Pre-v0.5.6 the strict
    # signature clustering left 90%+ of files in singleton clusters
    # because the seven-tuple key keys on AST top-level-node-kinds —
    # minor shape differences (one file has an extra TypeAlias) split
    # the cluster. The vast majority of files in a real codebase then
    # return archetype=null from get_pattern_context.
    #
    # The loose-merge tier groups sparse clusters sharing the same
    # paths_pattern + extension + JSX flag and folds them into a single
    # cluster when pairwise top-level-node-kinds Jaccard >= 0.5. The
    # merged cluster is marked tier="loose" so consumers can apply
    # lower confidence to it.
    clusters = _loose_merge_sparse_clusters(clusters, resolved_threshold)
    clusters.sort(key=lambda c: c.size, reverse=True)

    return ClusteringResult(
        clusters=clusters,
        skipped_generated=skipped_generated,
    )


def _node_kinds_set(member: ParsedFile) -> frozenset[str]:
    return frozenset(member.top_level_node_kinds or ())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _loose_merge_sparse_clusters(
    clusters: list[Cluster], sparse_threshold: int
) -> list[Cluster]:
    """Second-pass merge of sparse clusters by paths_pattern + Jaccard.

    Group by ``(path_pattern_bucket, jsx_present)``; within each group,
    walk the sparse clusters and merge any pair whose top-level node
    kinds satisfy Jaccard >= 0.5. The result is one or more loose
    clusters per bucket, each carrying ``cluster_tier="loose"``. Tight
    (non-sparse) clusters are passed through unchanged.
    """
    tight: list[Cluster] = []
    sparse_by_bucket: dict[tuple[str, bool], list[Cluster]] = defaultdict(list)
    for c in clusters:
        if c.size >= sparse_threshold:
            tight.append(c)
            continue
        bucket_key = (c.key.path_pattern_bucket or "", bool(c.key.jsx_present))
        sparse_by_bucket[bucket_key].append(c)

    merged: list[Cluster] = []
    for (bucket, jsx), group in sparse_by_bucket.items():
        if len(group) < 2:
            # Single sparse cluster — nothing to merge with; keep as-is.
            merged.extend(group)
            continue
        # Greedy union-find on Jaccard >= 0.5.
        parent: list[int] = list(range(len(group)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        # Compute representative node-kinds-set per cluster (use the
        # first member's set as the cluster's shape signature).
        shapes = [_node_kinds_set(c.members[0]) for c in group]
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                if _jaccard(shapes[i], shapes[j]) >= 0.5:
                    union(i, j)

        # Build merged clusters from union-find buckets.
        by_root: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            by_root[find(i)].append(i)
        for root, indices in by_root.items():
            if len(indices) == 1:
                merged.append(group[indices[0]])
                continue
            # Combine members from all indices, retain first cluster's key.
            combined_members: list[ParsedFile] = []
            for idx in indices:
                combined_members.extend(group[idx].members)
            new_cluster = Cluster(
                key=group[indices[0]].key,
                members=combined_members,
                sparse_threshold=sparse_threshold,
                cluster_tier="loose",
            )
            merged.append(new_cluster)

    return tight + merged
