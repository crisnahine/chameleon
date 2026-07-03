"""Canonical selection — pick a witness file for each cluster.

Per docs/architecture.md "Bootstrap interview flow" + "Profile schema" → canonicals.json:

A canonical has three faces (trichotomized):
1. Witness — the actual file (the one selected by this module)
2. Normative shape — AST query derived from the cluster signature
3. Normative idioms — prose annotations (user-provided via /chameleon-teach)

Phase 2C selects the witness with deterministic recency weighting:
- Exclude files in the canonical-pool denylist dirs / leaf globs (tests, legacy)
- Exclude files containing detected secrets
- Exclude files containing instruction-shaped natural language
- Among remaining: rank by recency weight (mtime-within-window doubles vote),
  break ties on (typicality of AST shape, lexicographic path) for reproducibility.

The recency window + multiplier are calibration targets.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp.bootstrap.canonical_scanner import scan_for_injection_signals
from chameleon_mcp.bootstrap.clustering import Cluster
from chameleon_mcp.bootstrap.discovery import is_eligible_as_canonical
from chameleon_mcp.profile.poisoning_scanner import scan_for_dangerous_patterns

# A Rails abstract base (`application_job.rb`, `application_controller.rb`,
# `application_record.rb`, `application_mailer.rb`, ...) is the parent every
# concrete sibling extends; it defines no `perform`/action and makes a hollow
# "mirror this" witness. Demoted below its concrete siblings.
_RAILS_ABSTRACT_BASE_RE = re.compile(r"(?:^|/)application_[a-z0-9_]+\.rb$", re.IGNORECASE)
# A NestJS module file. An imports-only `@Module({ imports: [...] })` root
# aggregator (AppModule) registers no controllers/providers and is a poor
# feature-module witness; demoted below a real feature module.
_NEST_MODULE_FILE_RE = re.compile(r"\.module\.(?:ts|tsx|js|jsx|mts|cts)$", re.IGNORECASE)
_MODULE_DECORATOR_RE = re.compile(r"@Module\s*\(\s*\{")
# `class Foo < ActiveRecord::Migration[7.0]` -> (7, 0); used to prefer a
# current-schema-version migration witness over an obsolete one in the cluster.
_MIGRATION_VERSION_RE = re.compile(r"ActiveRecord::Migration\[(\d+)\.(\d+)\]")


def _is_rails_abstract_base(path: Path) -> bool:
    return bool(_RAILS_ABSTRACT_BASE_RE.search(path.as_posix()))


def _is_imports_only_nest_module(path: Path, content: str) -> bool:
    """A NestJS `.module.ts` whose `@Module({...})` registers imports but no
    controllers/providers -- the root aggregator, not a representative feature
    module. Crude by design: a substring scan of the decorator head is enough to
    separate `imports:[...]`-only from a body that also lists controllers/providers."""
    if not _NEST_MODULE_FILE_RE.search(path.name):
        return False
    m = _MODULE_DECORATOR_RE.search(content)
    if not m:
        return False
    body = content[m.end() : m.end() + 4000]
    has_registration = "controllers" in body or "providers" in body
    return "imports" in body and not has_registration


def _migration_version(content: str) -> tuple[int, int] | None:
    m = _MIGRATION_VERSION_RE.search(content)
    return (int(m.group(1)), int(m.group(2))) if m else None


from chameleon_mcp.profile.secret_scanner import scan_for_secrets
from chameleon_mcp.signatures import ClusterKey

RECENCY_WEIGHT_MULTIPLIER = 2.0
RECENCY_WINDOW_DAYS = 90
_RECENCY_WINDOW_SECONDS = RECENCY_WINDOW_DAYS * 86400


@dataclass
class CanonicalSelection:
    """The chosen canonical witness for a cluster, plus scanner verdicts."""

    cluster_key_hash: str
    witness_path: Path
    sha_hint: str | None
    secret_scan_passed: bool
    injection_scan_passed: bool
    poisoning_scan_passed: bool
    recency_weight: float = 1.0
    """Selection weight applied to the chosen witness. 2.0 means it fell
    inside the recency window; 1.0 means it did not (or mtime was
    unreadable, in which case we conservatively use 1.0)."""

    @property
    def all_scans_passed(self) -> bool:
        return self.secret_scan_passed and self.injection_scan_passed and self.poisoning_scan_passed


@dataclass
class CanonicalSelectionResult:
    """Aggregate result of canonical selection across all clusters."""

    selections: dict[str, CanonicalSelection]
    clusters_without_eligible_canonical: list[Cluster]
    clusters_with_only_failing_canonicals: list[Cluster]


def _hash_cluster_key(cluster: Cluster) -> str:
    """Stable hash for cluster cross-references in canonicals.json.

    Uses the cluster signature's deterministic dict representation so two
    independent runs over the same repo produce identical hashes.
    """
    import hashlib
    import json

    key_dict = cluster.key.to_dict()
    split_tag = getattr(cluster, "split_tag", "") or ""
    if split_tag:
        # Split children share their parent's key; discriminate them so they
        # don't collide on the same cluster_id (and silently overwrite one
        # archetype). Non-split clusters (split_tag == "") keep the legacy
        # key-only hash, so existing profiles' ids stay stable.
        payload = {"k": key_dict, "s": split_tag}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    else:
        canonical = json.dumps(key_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _file_recency_weight(path: Path, *, now: float | None = None) -> float:
    """Return RECENCY_WEIGHT_MULTIPLIER if path was modified within the
    recency window, else 1.0.

    Defensive: stat failures fall back to 1.0 (no boost) instead of
    aborting; an unreadable mtime should not exclude a file that already
    cleared every safety scanner.
    """
    reference = time.time() if now is None else now
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 1.0
    age_seconds = reference - mtime
    # A future mtime (clock skew, an archive extracted with bogus timestamps, a
    # crafted file) gets no boost: only files genuinely modified within the
    # window are treated as recent, so a far-future timestamp cannot outrank a
    # just-edited file in canonical selection.
    if 0 <= age_seconds <= _RECENCY_WINDOW_SECONDS:
        return RECENCY_WEIGHT_MULTIPLIER
    return 1.0


def derive_ast_query(cluster_key: ClusterKey) -> dict:
    """Build the normative-shape AST query for a cluster.

    The query is a JSON-serializable dict whose fields correspond 1:1 to
    the dimensions of the cluster signature that an AST-only lint can
    re-derive for any single file. A file conforms to the query iff every
    non-null field equals the file's derived value.

    Fields:
        top_level_node_kinds: list[str] of top-level AST node kinds
        default_export_kind: str | None — the kind of the default export
        named_export_count_bucket: str — one of "0", "1", "2-4", "5-9", "10+"
        jsx_present: bool — JSX/TSX elements detected anywhere in the file
        content_signal: str | None — file-level lexical directive (e.g.,
            "use_client", "use_server", "shebang", "ts_pragma") or None
            when the cluster's content_signal_match is "none" — encoded
            as None so the lint engine can treat "no directive" as
            "any directive is acceptable" if it wishes, vs. the literal
            string "none" which would forbid a directive.

    The structure intentionally omits path_pattern_bucket and the import
    set hash: those are used to *form* clusters, but they aren't useful
    for a single-file conformance check at edit time (the lint engine
    already knows which archetype a file belongs to before it looks at
    the AST query).
    """
    content_signal = cluster_key.content_signal_match
    return {
        "top_level_node_kinds": list(cluster_key.top_level_node_kinds),
        "default_export_kind": cluster_key.default_export_kind,
        "named_export_count_bucket": cluster_key.named_export_count_bucket,
        "jsx_present": bool(cluster_key.jsx_present),
        "content_signal": content_signal if content_signal != "none" else None,
    }


def select_canonicals(
    clusters: list[Cluster],
    repo_root: Path,
    *,
    now: float | None = None,
) -> CanonicalSelectionResult:
    """Choose a canonical witness for each cluster.

    Selection order within a cluster:
      1. Drop files in test/legacy/archive/etc. (canonical-pool exclusions)
      2. Sort remaining by (-recency_weight, -typicality, path-string).
         Recency-weighted files come first; ties resolve to the more typical
         AST shape (most common signature in the cluster), then lexicographic
         path for full determinism.
      3. Walk the sorted list; first file that passes secret + injection +
         poisoning scans wins. Failing files are tracked so the bootstrap
         report can surface clusters that have NO clean canonical.

    Args:
        clusters: clustering output (typically the dense clusters only;
                  sparse clusters get user confirmation in Phase 2D interview)
        repo_root: absolute path to repo root (for relative-path computation)
        now: optional unix timestamp override (test seam — pinning `now`
             makes recency reproducible across hosts).

    Returns:
        CanonicalSelectionResult with per-cluster selections + diagnostic lists
        for clusters that couldn't get a clean canonical.
    """
    selections: dict[str, CanonicalSelection] = {}
    no_eligible: list[Cluster] = []
    only_failing: list[Cluster] = []

    for cluster in clusters:
        cluster_id = _hash_cluster_key(cluster)

        eligible = []
        for pf in cluster.members:
            try:
                rel = str(pf.path.relative_to(repo_root))
            except ValueError:
                # Member resolved outside repo_root (stray/symlinked path).
                # Mirror clustering.py's guard instead of crashing bootstrap.
                rel = str(pf.path)
            if is_eligible_as_canonical(rel):
                eligible.append(pf)
        if not eligible:
            no_eligible.append(cluster)
            continue

        from collections import Counter

        from chameleon_mcp.lint_engine import (
            _normalize_kind,
            detect_language,
            extract_dimensions,
        )

        signatures: dict[int, tuple[str, ...]] = {}
        trivial: dict[int, bool] = {}
        # Non-representativeness signals: an abstract base / imports-only aggregator
        # makes a hollow "mirror this" witness even when it shares its siblings'
        # typicality, and mtime recency is uniform on a fresh clone so it can't
        # separate them -- without this the str(path) tiebreak picks them (Rails
        # application_*.rb, NestJS AppModule). Migration versions are collected so a
        # cluster prefers a current-schema-version witness over an obsolete one.
        abstract_base: dict[int, bool] = {}
        mig_version: dict[int, tuple[int, int] | None] = {}
        for i, pf in enumerate(eligible):
            try:
                content = pf.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
                abstract_base[i] = _is_rails_abstract_base(pf.path) or _is_imports_only_nest_module(
                    pf.path, content
                )
                mig_version[i] = _migration_version(content)
                # An empty / whitespace-only file makes a useless canonical example
                # (no code to mirror), so rank it last. A non-trivial sibling then
                # wins, while the trivial file stays eligible as a last resort for a
                # cluster whose members are ALL trivial.
                trivial[i] = not content.strip()
                lang = detect_language(str(pf.path))
                snap = extract_dimensions(content, language=lang, file_path=str(pf.path))
                # A file with non-whitespace content but NO top-level code/export
                # nodes (a comment-only header, a license block) teaches nothing as
                # a canonical exemplar, like a blank file. Rank it trivial so a
                # structured sibling wins. Barrels / imports-only files keep a
                # non-empty signature (export/import nodes) and stay eligible.
                if not snap.top_level_node_kinds:
                    trivial[i] = True
                jsx_tag = ("jsx",) if snap.jsx_present else ()
                sig = (
                    tuple(sorted(set(_normalize_kind(k) for k in snap.top_level_node_kinds)))
                    + jsx_tag
                )
            except Exception:
                sig = ()
                trivial[i] = True
                abstract_base.setdefault(i, False)
                mig_version.setdefault(i, None)
            signatures[i] = sig

        sig_counts = Counter(signatures.values())
        typicality = {i: sig_counts[sig] for i, sig in signatures.items()}

        # A migration whose ActiveRecord::Migration[x.y] version is behind the
        # newest in the cluster is an obsolete-shape witness ("mirror this" would
        # copy the stale version); demote it below current-version siblings.
        _versions = [v for v in mig_version.values() if v is not None]
        _max_ver = max(_versions) if _versions else None
        demote = {
            i: (
                abstract_base.get(i, False)
                or (
                    _max_ver is not None
                    and mig_version.get(i) is not None
                    and mig_version[i] < _max_ver
                )
            )
            for i in range(len(eligible))
        }

        # Exclude empty / whitespace-only files from the canonical pool entirely: a
        # blank witness teaches nothing, and an all-empty cluster (e.g. a package of
        # bare __init__.py files) would otherwise pick a blank last-resort witness
        # that then merges into a real archetype's sub-buckets. A cluster left with
        # no non-trivial member is reported as lacking a clean canonical, same as an
        # all-generated cluster. Files with real content (incl. thin barrel
        # re-exports) are unaffected.
        scored = [
            (pf, _file_recency_weight(pf.path, now=now), typicality.get(i, 0), demote.get(i, False))
            for i, pf in enumerate(eligible)
            if not trivial.get(i, False)
        ]
        # demote (abstract base / imports-only aggregator / obsolete-version
        # migration) is the tiebreak BELOW recency+typicality but ABOVE the path
        # string, so a representative concrete sibling wins when the primary
        # signals tie -- which they do on a fresh clone with uniform mtimes.
        scored.sort(
            key=lambda item: (
                -item[1],
                -item[2],
                item[3],
                str(item[0].path),
            )
        )

        chosen: CanonicalSelection | None = None
        for candidate, weight, _typ, _dem in scored:
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
                recency_weight=weight,
            )

            if passed:
                chosen = sel
                break
            chosen = sel

        if chosen is None:
            no_eligible.append(cluster)
            continue

        if not chosen.all_scans_passed:
            only_failing.append(cluster)
            continue

        selections[cluster_id] = chosen

    return CanonicalSelectionResult(
        selections=selections,
        clusters_without_eligible_canonical=no_eligible,
        clusters_with_only_failing_canonicals=only_failing,
    )
