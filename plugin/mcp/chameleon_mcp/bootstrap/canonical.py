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
- Among remaining: rank by recency weight, break ties on (typicality of AST
  shape, lexicographic path) for reproducibility.

Recency weight decays smoothly off each witness's LAST GIT COMMIT TIME (a single
`git log` walk per bootstrap builds the {path: commit_epoch} map): a recently
committed file gets the full multiplier, older commits decay toward 1.0 with a
configurable half-life. Commit time survives a fresh clone's uniform mtimes,
which is why it replaces the old mtime step -- on a fresh clone mtime carried no
signal, so a mid-migration repo whose NEW idiom is the cluster minority
(typicality favors the legacy majority) mispicked the legacy witness. When git
is unavailable, or a file is untracked, weight falls back to the legacy mtime
step (multiplier within the window, else 1.0) and the degraded mode is recorded.

The recency window, multiplier, and decay half-life are calibration targets.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int
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

    witness_path: Path
    sha_hint: str | None
    secret_scan_passed: bool
    injection_scan_passed: bool
    poisoning_scan_passed: bool

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


# ASCII Unit Separator. A git-tracked path cannot begin with it, so it
# unambiguously marks a commit-header line in the combined `git log --name-only`
# stream (a bare `%ct` header could otherwise be mistaken for a numeric filename
# after an empty/merge commit that lists no files).
_COMMIT_MARK = "\x1f"


def _rel_posix(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX key for the commit-time map, mirroring git's output."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _build_commit_time_map(repo_root: Path) -> tuple[dict[str, int] | None, str]:
    """Map repo-relative POSIX path -> last (most-recent) commit epoch for every
    tracked file under ``repo_root``, from a SINGLE ``git log`` walk.

    Returns ``(map, "git")`` on success (an empty map is a success: a repo with
    no history). On any failure returns ``(None, reason)`` and the caller falls
    back to mtime for the whole pass. Bounded by one subprocess with a timeout
    (the same discipline as judge._run_git); this is the only subprocess
    canonical selection spawns, and it runs at bootstrap/refresh time, never on a
    hook hot path.

    ``--relative`` scopes the walk to ``repo_root``'s subtree and emits paths
    relative to it, so a workspace root inside a larger monorepo gets aligned
    keys and a bounded walk. ``--no-renames`` keeps a rename as delete+add (both
    paths carry the commit time; a stale deleted key is harmless, it is never
    looked up). Log order is newest-first, so the first time a path appears is
    its most recent commit -- a plain first-wins fill is correct.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "-c",
                "core.quotePath=false",
                "log",
                "--no-renames",
                "--relative",
                f"--format={_COMMIT_MARK}%ct",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=threshold_int("CANONICAL_GIT_LOG_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        # ValueError guards an unencodable arg before spawn, mirroring _run_git.
        return None, "git-error"
    if proc.returncode != 0:
        # Not a work tree, a bare repo, or git absent: fall back to mtime.
        return None, "git-unavailable"

    result: dict[str, int] = {}
    current: int | None = None
    for raw in (proc.stdout or "").split("\n"):
        if not raw:
            continue
        if raw.startswith(_COMMIT_MARK):
            try:
                current = int(raw[len(_COMMIT_MARK) :])
            except ValueError:
                current = None
            continue
        if current is None:
            continue
        if raw not in result:  # newest-first: first occurrence is the latest commit
            result[raw] = current
    return result, "git"


def _file_recency_weight(
    path: Path,
    *,
    now: float | None = None,
    commit_epoch: int | float | None = None,
    half_life_days: float | None = None,
) -> float:
    """Selection weight for a witness, in [1.0, RECENCY_WEIGHT_MULTIPLIER].

    When ``commit_epoch`` is supplied (the file's last commit time, from the
    bootstrap-pass git walk), the weight decays smoothly with commit age:
    RECENCY_WEIGHT_MULTIPLIER at age 0, its boost above 1.0 halving every
    ``half_life_days``. Commit time survives a fresh clone's uniform mtimes, so a
    recently committed minority idiom outranks an older majority one.

    When ``commit_epoch`` is None (git unavailable, or an untracked file), it
    falls back to the legacy mtime step: RECENCY_WEIGHT_MULTIPLIER within
    RECENCY_WINDOW_DAYS, else 1.0. Defensive: a stat failure returns 1.0.

    A commit time AHEAD of the bootstrap clock (cross-machine / CI clock skew is
    routine) clamps to age 0 -- it is treated as most-recent, so a legitimately
    newest file is never penalized and selection does not flip across refreshes
    as the clock passes the commit time. A bogus far-future stamp reaches only
    the SAME max boost as a genuine recent file, never more, so it cannot outrank
    one. The mtime fallback keeps the stricter future -> 1.0 guard: a future mtime
    is same-machine clock weirdness, not cross-machine commit skew.
    """
    reference = time.time() if now is None else now
    if commit_epoch is not None:
        age_seconds = max(0.0, reference - float(commit_epoch))
        hl_days = (
            threshold_float("CANONICAL_RECENCY_HALF_LIFE_DAYS")
            if half_life_days is None
            else half_life_days
        )
        if hl_days <= 0:
            # A non-positive half-life is nonsensical; treat as no decay (full
            # boost for any past commit) rather than a divide-by-zero.
            return RECENCY_WEIGHT_MULTIPLIER
        age_days = age_seconds / 86400.0
        # A TRUE half-life: the boost above 1.0 halves every ``hl_days`` (2**-1 at
        # one half-life), matching the CANONICAL_RECENCY_HALF_LIFE_DAYS name -- not
        # an e-folding exp(-age/hl), which would decay by 1/e (~0.37), not 1/2.
        boost = (RECENCY_WEIGHT_MULTIPLIER - 1.0) * 2.0 ** (-age_days / hl_days)
        return 1.0 + boost

    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 1.0
    age_seconds = reference - mtime
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
    # Snapshot the clock ONCE for the whole pass. With now=None the per-file
    # _file_recency_weight calls would each evaluate time.time() independently, so
    # two files sharing a commit epoch get recency weights that differ by
    # sub-millisecond jitter -- and because recency sorts ABOVE typicality, that
    # jitter silently decided the witness instead of the typicality tiebreak the
    # fresh-clone / single-commit case depends on. One snapshot makes tied epochs
    # weigh exactly equally so typicality breaks the tie as documented.
    effective_now = time.time() if now is None else now

    selections: dict[str, CanonicalSelection] = {}
    no_eligible: list[Cluster] = []
    only_failing: list[Cluster] = []

    # One git-log walk for the whole pass builds {rel_path: last_commit_epoch}.
    # When git is unavailable the pass falls back to mtime for every file
    # silently -- a capability fallback, not a hook-delivery degradation, so it is
    # not telemetered (doctor/get_status surface profile health on demand); a
    # per-file untracked miss falls back the same way. CHAMELEON_CANONICAL_GIT_RECENCY=0
    # is the kill switch: skip the git walk and use the legacy mtime step.
    if os.environ.get("CHAMELEON_CANONICAL_GIT_RECENCY") == "0":
        commit_map = None
    else:
        commit_map, _recency_source = _build_commit_time_map(repo_root)

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
            (
                pf,
                _file_recency_weight(
                    pf.path,
                    now=effective_now,
                    commit_epoch=(
                        commit_map.get(_rel_posix(pf.path, repo_root))
                        if commit_map is not None
                        else None
                    ),
                ),
                typicality.get(i, 0),
                demote.get(i, False),
            )
            for i, pf in enumerate(eligible)
            if not trivial.get(i, False)
        ]
        # demote (abstract base / imports-only aggregator / obsolete-version
        # migration) is a HARD deprioritization ABOVE every other signal: a
        # structurally hollow witness must lose to any concrete sibling even when
        # it was committed more recently. Commit-time recency is now continuous
        # (not the old mtime step that tied on a fresh clone), so if demote sat
        # below it a just-touched application_record.rb would outrank an older
        # concrete model. Below demote: recency (newer commit wins -- this is what
        # lets a recent minority idiom beat the legacy majority), then typicality
        # (breaks the fresh-clone same-commit tie toward the majority shape), then
        # the path string for full determinism.
        scored.sort(
            key=lambda item: (
                item[3],
                -item[1],
                -item[2],
                str(item[0].path),
            )
        )

        chosen: CanonicalSelection | None = None
        for candidate, _weight, _typ, _dem in scored:
            try:
                content = candidate.path.read_text(errors="replace")
            except OSError:
                continue

            secret_hits = scan_for_secrets(content)
            injection_hits = scan_for_injection_signals(content)
            poisoning_hits = scan_for_dangerous_patterns(content)

            passed = (not secret_hits) and (not injection_hits) and (not poisoning_hits)

            sel = CanonicalSelection(
                witness_path=candidate.path,
                sha_hint=candidate.sha_hint,
                secret_scan_passed=not secret_hits,
                injection_scan_passed=not injection_hits,
                poisoning_scan_passed=not poisoning_hits,
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
