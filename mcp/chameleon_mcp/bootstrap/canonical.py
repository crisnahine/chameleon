"""Canonical selection — pick a witness file for each cluster.

Per ARCHITECTURE.md "Bootstrap interview flow" + "Profile schema" → canonicals.json:

A canonical has three faces (trichotomized):
1. Witness — the actual file (the one selected by this module)
2. Normative shape — AST query (Phase 2C derived from cluster signature)
3. Normative idioms — prose annotations (user-provided via /chameleon-teach)

Phase 2B picks the witness deterministically:
- Exclude files matching EXCLUDE_FROM_CANONICAL_POOL_PATTERNS (tests, legacy)
- Exclude files containing detected secrets (Phase 4 wires real scanner)
- Exclude files containing instruction-shaped natural language (canonical_scanner)
- Among remaining: pick by deterministic rule (shortest path first; tiebreak alpha)

Phase 2C will add recency-weighted selection (last 90 days = 2× vote) once
git history integration lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp.bootstrap.canonical_scanner import scan_for_injection_signals
from chameleon_mcp.bootstrap.clustering import Cluster
from chameleon_mcp.bootstrap.discovery import is_eligible_as_canonical
from chameleon_mcp.profile.poisoning_scanner import scan_for_dangerous_patterns
from chameleon_mcp.profile.secret_scanner import scan_for_secrets


@dataclass
class CanonicalSelection:
    """The chosen canonical witness for a cluster, plus scanner verdicts."""

    cluster_key_hash: str  # serialized cluster key, used for stable cross-cluster references
    witness_path: Path
    sha_hint: str | None
    secret_scan_passed: bool
    injection_scan_passed: bool
    poisoning_scan_passed: bool

    @property
    def all_scans_passed(self) -> bool:
        return (
            self.secret_scan_passed
            and self.injection_scan_passed
            and self.poisoning_scan_passed
        )


@dataclass
class CanonicalSelectionResult:
    """Aggregate result of canonical selection across all clusters."""

    selections: dict[str, CanonicalSelection]  # cluster_key_hash → selection
    clusters_without_eligible_canonical: list[Cluster]
    clusters_with_only_failing_canonicals: list[Cluster]


def _hash_cluster_key(cluster: Cluster) -> str:
    """Stable hash for cluster cross-references in canonicals.json.

    Uses the cluster signature's deterministic dict representation so two
    independent runs over the same repo produce identical hashes.
    """
    import hashlib
    import json

    canonical = json.dumps(cluster.key.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def select_canonicals(
    clusters: list[Cluster],
    repo_root: Path,
) -> CanonicalSelectionResult:
    """Choose a canonical witness for each cluster.

    Args:
        clusters: clustering output (typically the dense clusters only;
                  sparse clusters get user confirmation in Phase 2C interview)
        repo_root: absolute path to repo root (for relative-path computation)

    Returns:
        CanonicalSelectionResult with per-cluster selections + diagnostic lists
        for clusters that couldn't get a clean canonical.
    """
    selections: dict[str, CanonicalSelection] = {}
    no_eligible: list[Cluster] = []
    only_failing: list[Cluster] = []

    for cluster in clusters:
        cluster_id = _hash_cluster_key(cluster)

        # Filter to canonical-eligible files (exclude tests/legacy/etc.)
        eligible = [
            pf for pf in cluster.members
            if is_eligible_as_canonical(str(pf.path.relative_to(repo_root)))
        ]
        if not eligible:
            no_eligible.append(cluster)
            continue

        # Deterministic ordering: shortest path first, then lexicographic.
        # Recency-weighted selection (Phase 2C) will refine this once git
        # log integration lands.
        eligible.sort(key=lambda pf: (len(pf.path.parts), str(pf.path)))

        # Try each candidate in order; pick the first that passes all scanners.
        # Track failing candidates for diagnostic reporting.
        chosen: CanonicalSelection | None = None
        for candidate in eligible:
            try:
                content = candidate.path.read_text(errors="replace")
            except OSError:
                continue

            secret_hits = scan_for_secrets(content)
            injection_hits = scan_for_injection_signals(content)
            poisoning_hits = scan_for_dangerous_patterns(content)

            passed = (not secret_hits) and (not injection_hits) and (not poisoning_hits)

            sel = CanonicalSelection(
                cluster_key_hash=cluster_id,
                witness_path=candidate.path,
                sha_hint=candidate.sha_hint,
                secret_scan_passed=not secret_hits,
                injection_scan_passed=not injection_hits,
                poisoning_scan_passed=not poisoning_hits,
            )

            if passed:
                chosen = sel
                break
            # Else continue trying next candidate; Phase 2B keeps the last failing
            # one for diagnostic reporting if no candidate passes.
            chosen = sel

        if chosen is None:
            no_eligible.append(cluster)
            continue

        if not chosen.all_scans_passed:
            # Fail-closed: a failed-scan canonical must NOT reach
            # get_canonical_excerpt / get_pattern_context, because the model
            # will trust whatever ends up in <chameleon-context>. Surface the
            # cluster in `clusters_with_only_failing_canonicals` for
            # /chameleon-status diagnostics, but DO NOT add to active
            # selections. Downstream: orchestrator skips clusters without an
            # entry in selections, so this archetype simply has no canonical.
            only_failing.append(cluster)
            continue

        selections[cluster_id] = chosen

    return CanonicalSelectionResult(
        selections=selections,
        clusters_without_eligible_canonical=no_eligible,
        clusters_with_only_failing_canonicals=only_failing,
    )
