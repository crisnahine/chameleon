"""Cluster ParsedFile records by ClusterKey signature.

Per docs/architecture.md "Cluster signature function" → "Incremental algorithm":
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

from chameleon_mcp._thresholds import threshold_float
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.signatures import (
    ClusterKey,
    bucket_named_export_count,
    compute_signature,
    path_pattern_bucket_for,
)

SPARSE_CLUSTER_THRESHOLD = 5

BIMODAL_DOMINANT_SHARE_THRESHOLD = 0.6

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
    sparse_threshold: int = SPARSE_CLUSTER_THRESHOLD
    cluster_tier: str = "tight"
    sub_bucket_counts: dict[str, int] = field(default_factory=dict)
    # Discriminator for clusters split out of a shared key by
    # _split_by_sub_bucket (e.g. "concerns" vs "" for the base). Folded into the
    # cluster-id hash so split children get distinct ids instead of colliding.
    split_tag: str = ""

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
            from chameleon_mcp.signatures import content_signal_match_for

            return content_signal_match_for(member.content_first_200_bytes)
        return None

    def dimension_distribution(self, dimension: str) -> dict[object, int]:
        """Return a Counter-style dict of {value: count} for `dimension`.

        Empty when the cluster has no members.
        """
        return dict(Counter(self._dimension_value_for(m, dimension) for m in self.members))

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

    An earlier hard-coded threshold of 5 killed recall on feature-per-folder
    layouts (excalidraw: 94.8% sparse warnings; mastodon: 0 archetypes from
    856 files). Repos under ~1k files routinely have meaningful clusters
    with 3–4 members; lowering the floor lets the long tail surface.

    Heuristic (corpus-size tiered):
      - total_files <  1000 → threshold = 3
      - total_files <  5000 → threshold = 4
      - total_files >= 5000 → threshold = 5 (behavior preserved
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
                   repos get the adaptive behavior (Bug 4).

    Returns:
        ClusteringResult with clusters sorted by size (largest first) for
        deterministic archetype proposal ordering. Each cluster's
        ``sparse_threshold`` reflects the resolved threshold so
        ``cluster.is_sparse`` agrees with ``ClusteringResult.sparse_clusters``.
    """
    from chameleon_mcp.bootstrap.discovery import is_likely_generated

    by_key: dict[ClusterKey, list[ParsedFile]] = defaultdict(list)
    sub_bucket_counter: dict[ClusterKey, Counter[str]] = defaultdict(Counter)
    skipped_generated: list[ParsedFile] = []

    for pf in parsed_files:
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

        _, sub_bucket = path_pattern_bucket_for(file_path_for_signature, include_extension=True)
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
        sub_bucket_counter[key][sub_bucket] += 1

    total_clustered = sum(len(members) for members in by_key.values())
    if min_cluster_size is None:
        resolved_threshold = _adaptive_sparse_threshold(total_clustered)
    else:
        resolved_threshold = max(1, int(min_cluster_size))

    clusters = [
        Cluster(
            key=k,
            members=members,
            sparse_threshold=resolved_threshold,
            sub_bucket_counts=dict(sub_bucket_counter[k]),
        )
        for k, members in by_key.items()
    ]

    clusters = _loose_merge_sparse_clusters(clusters, resolved_threshold)

    clusters = _shape_fuzzy_merge(clusters)

    clusters = _split_by_sub_bucket(clusters, resolved_threshold, repo_root)

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


def _loose_merge_sparse_clusters(clusters: list[Cluster], sparse_threshold: int) -> list[Cluster]:
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
    for group in sparse_by_bucket.values():
        if len(group) < 2:
            merged.extend(group)
            continue
        parent: list[int] = list(range(len(group)))

        def _find(i: int, _p: list[int] = parent) -> int:
            while _p[i] != i:
                _p[i] = _p[_p[i]]
                i = _p[i]
            return i

        def _union(i: int, j: int, _p: list[int] = parent) -> None:
            ri, rj = _find(i, _p), _find(j, _p)
            if ri != rj:
                _p[ri] = rj

        shapes = [_node_kinds_set(c.members[0]) for c in group]
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                if _jaccard(shapes[i], shapes[j]) >= 0.5:
                    _union(i, j)

        by_root: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            by_root[_find(i)].append(i)
        for indices in by_root.values():
            if len(indices) == 1:
                merged.append(group[indices[0]])
                continue
            combined_members: list[ParsedFile] = []
            for idx in indices:
                combined_members.extend(group[idx].members)
            new_cluster = Cluster(
                key=group[indices[0]].key,
                members=combined_members,
                sparse_threshold=sparse_threshold,
                cluster_tier="loose",
                sub_bucket_counts=_merge_sub_bucket_counts([group[idx] for idx in indices]),
            )
            merged.append(new_cluster)

    return tight + merged


def _merge_sub_bucket_counts(clusters: list[Cluster]) -> dict[str, int]:
    """Combine sub_bucket_counts from multiple clusters into one dict."""
    merged: Counter[str] = Counter()
    for c in clusters:
        merged.update(c.sub_bucket_counts)
    return dict(merged)


_SPLIT_BY_SUB_BUCKET_SUFFIXES: frozenset[str] = frozenset(
    {
        "concerns",
        "base",
        "__tests__",
        "spec",
        "tests",
        "test",
    }
)


def _member_sub_bucket(member: ParsedFile, repo_root: Path | None = None) -> str:
    """Recompute a member's sub_bucket so the split pass can re-partition.

    sub_bucket_counts on the cluster is aggregate; we need per-member
    classification to actually split. Recomputes via the same
    ``path_pattern_bucket_for`` helper used during initial clustering
    so the buckets stay in lockstep.
    """
    from chameleon_mcp.signatures import path_pattern_bucket_for

    if member.path is None:
        return ""
    if repo_root is not None:
        try:
            rel = str(member.path.relative_to(repo_root))
        except ValueError:
            rel = str(member.path)
    else:
        rel = str(member.path)
    _, sub = path_pattern_bucket_for(rel)
    return sub or ""


def _split_by_sub_bucket(
    clusters: list[Cluster],
    sparse_threshold: int,
    repo_root: Path | None = None,
) -> list[Cluster]:
    """Split clusters whose sub_bucket distribution carries a semantic suffix.

    Rec 2: for each cluster, examine sub_bucket_counts; if a
    _SPLIT_BY_SUB_BUCKET_SUFFIXES entry holds ``>= sparse_threshold``
    members AND the dominant non-suffix sub_bucket holds
    ``>= BIMODAL_DOMINANT_SHARE_THRESHOLD`` of the remaining members,
    split into two child clusters:
      - one for members whose sub_bucket starts with the suffix
      - one for everything else

    Both children inherit the parent's ``key``, ``sparse_threshold``,
    and ``cluster_tier``, but get freshly-computed ``sub_bucket_counts``.
    Naming sees them as distinct clusters and picks distinct names
    (e.g. ``model`` vs ``model-concern`` via the _RAILS_PRIORS table).

    Idempotent: a cluster already at the suffix or already non-suffix
    only is unaffected.
    """
    result: list[Cluster] = []
    for cluster in clusters:
        if cluster.size < 2:
            result.append(cluster)
            continue

        member_buckets = [(m, _member_sub_bucket(m, repo_root)) for m in cluster.members]

        def _split_key(sb: str) -> str | None:
            if not sb:
                return None
            head = sb.split("/", 1)[0]
            if head in _SPLIT_BY_SUB_BUCKET_SUFFIXES:
                return head
            return None

        with_suffix: dict[str, list[tuple[ParsedFile, str]]] = {}
        without_suffix: list[tuple[ParsedFile, str]] = []
        for m, sb in member_buckets:
            tag = _split_key(sb)
            if tag is None:
                without_suffix.append((m, sb))
            else:
                with_suffix.setdefault(tag, []).append((m, sb))

        if not with_suffix or not without_suffix:
            result.append(cluster)
            continue

        keep_split = False
        for _tag, members in with_suffix.items():
            if len(members) < sparse_threshold:
                continue
            keep_split = True
            break

        if not keep_split:
            result.append(cluster)
            continue

        no_suffix_subs = Counter(sb for _m, sb in without_suffix)
        if not no_suffix_subs:
            result.append(cluster)
            continue
        dominant_share = max(no_suffix_subs.values()) / len(without_suffix)
        if dominant_share < BIMODAL_DOMINANT_SHARE_THRESHOLD:
            result.append(cluster)
            continue

        for _tag, suffix_members in with_suffix.items():
            if len(suffix_members) < sparse_threshold:
                without_suffix.extend(suffix_members)
                continue
            sub_counts = Counter(sb for _m, sb in suffix_members)
            result.append(
                Cluster(
                    key=cluster.key,
                    members=[m for m, _sb in suffix_members],
                    sparse_threshold=cluster.sparse_threshold,
                    cluster_tier=cluster.cluster_tier,
                    sub_bucket_counts=dict(sub_counts),
                    split_tag=_tag,
                )
            )
        sub_counts = Counter(sb for _m, sb in without_suffix)
        result.append(
            Cluster(
                key=cluster.key,
                members=[m for m, _sb in without_suffix],
                sparse_threshold=cluster.sparse_threshold,
                cluster_tier=cluster.cluster_tier,
                sub_bucket_counts=dict(sub_counts),
                split_tag="",
            )
        )

    return result


def _union_shape(cluster: Cluster) -> frozenset[str]:
    """Union of all members' top_level_node_kinds for a cluster.

    Using the union rather than a single member's set means statistical
    outliers (files with one extra node kind) don't prevent the merge.
    Empty-member clusters (shouldn't happen in practice) return empty set.
    """
    result: frozenset[str] = frozenset()
    for member in cluster.members:
        result = result | frozenset(member.top_level_node_kinds or ())
    return result


def _shape_fuzzy_merge(clusters: list[Cluster]) -> list[Cluster]:
    """Option 1 post-pass: merge clusters with near-identical AST shapes.

    Group clusters by ``(path_pattern_bucket, default_export_kind,
    jsx_present)``. Within each group, build a union-find: two clusters
    merge when the Jaccard similarity of their UNION top_level_node_kinds
    sets is >= CLUSTER_SHAPE_JACCARD_THRESHOLD (default 0.7,
    env-overridable via ``CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD``).

    The merged cluster:
      - Takes the key of the SMALLEST-keyed original cluster (deterministic
        across Python runs because ClusterKey is a frozen dataclass with
        string fields, and Python tuples/strings have a stable total order
        within a single run).
      - Carries ``cluster_tier="shape-merged"`` so consumers can
        distinguish it from tight (exact) or loose (sparse) clusters.
      - Retains the sparse_threshold from the first cluster in the group
        (all clusters in the same session share the same threshold).

    Clusters that don't participate in any merge pass through with their
    original tier and key unchanged.
    """
    jaccard_threshold = threshold_float("CLUSTER_SHAPE_JACCARD_THRESHOLD")

    GroupKey = tuple
    by_group: dict[GroupKey, list[Cluster]] = defaultdict(list)
    for c in clusters:
        gk: GroupKey = (
            c.key.path_pattern_bucket or "",
            c.key.default_export_kind,
            bool(c.key.jsx_present),
        )
        by_group[gk].append(c)

    result: list[Cluster] = []
    for group in by_group.values():
        if len(group) < 2:
            result.extend(group)
            continue

        shapes = [_union_shape(c) for c in group]
        n = len(group)

        parent: list[int] = list(range(n))

        def _find(i: int, _p: list[int] = parent) -> int:
            while _p[i] != i:
                _p[i] = _p[_p[i]]
                i = _p[i]
            return i

        def _union(i: int, j: int, _p: list[int] = parent) -> None:
            ri, rj = _find(i, _p), _find(j, _p)
            if ri != rj:
                _p[ri] = rj

        for i in range(n):
            for j in range(i + 1, n):
                if _jaccard(shapes[i], shapes[j]) >= jaccard_threshold:
                    _union(i, j)

        by_root: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            by_root[_find(i)].append(i)

        for indices in by_root.values():
            if len(indices) == 1:
                result.append(group[indices[0]])
                continue
            combined_members: list[ParsedFile] = []
            for idx in indices:
                combined_members.extend(group[idx].members)
            representative_cluster = min(
                (group[idx] for idx in indices),
                key=lambda c: (
                    c.key.path_pattern_bucket or "",
                    c.key.content_signal_match or "",
                    c.key.top_level_node_kinds,
                    c.key.default_export_kind or "",
                    c.key.named_export_count_bucket or "",
                    c.key.import_module_set_hash or "",
                    c.key.jsx_present,
                ),
            )
            new_cluster = Cluster(
                key=representative_cluster.key,
                members=combined_members,
                sparse_threshold=group[indices[0]].sparse_threshold,
                cluster_tier="shape-merged",
                sub_bucket_counts=_merge_sub_bucket_counts([group[idx] for idx in indices]),
            )
            result.append(new_cluster)

    return result
