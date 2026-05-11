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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

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

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_sparse(self) -> bool:
        return self.size < SPARSE_CLUSTER_THRESHOLD

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


def cluster_files(
    parsed_files: Iterable[ParsedFile],
    repo_root: Path | None = None,
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

    Returns:
        ClusteringResult with clusters sorted by size (largest first) for
        deterministic archetype proposal ordering.
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

        key = compute_signature(
            file_path=file_path_for_signature,
            content_first_200_bytes=pf.content_first_200_bytes,
            top_level_node_kinds=pf.top_level_node_kinds,
            default_export_kind=pf.default_export_kind,
            named_export_count=pf.named_export_count,
            import_specifiers=pf.import_specifiers,
            has_jsx=pf.has_jsx,
        )
        by_key[key].append(pf)

    clusters = [Cluster(key=k, members=members) for k, members in by_key.items()]
    clusters.sort(key=lambda c: c.size, reverse=True)

    return ClusteringResult(
        clusters=clusters,
        skipped_generated=skipped_generated,
    )
