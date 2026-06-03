"""Bootstrap orchestrator — main entry point for `/chameleon-init`.

Wires Phase 2A pieces (extractor + signatures + drift.db) and Phase 2B
pieces (discovery + clustering + canonical selection) together with the
atomic-transaction commit pattern from Phase 1C.

Phase 2B scope: full bootstrap pipeline producing committable
.chameleon/profile.json artifacts. Phase 2C adds:
- Workspace detection (pnpm/yarn/lerna/turbo/nx)
- Tool config reading (.prettierrc, tsconfig, .eslintrc, .editorconfig)
- Recency-weighted clustering
- Bimodal/sparse cluster surfacing in interview

Phase 2D adds:
- Interactive interview (≤3 user-facing prompts)
- /chameleon-trust integration
- /chameleon-teach integration

Phase 2B emits a profile non-interactively: archetype names default to
auto-generated identifiers (cluster_<hash>) which Phase 2D rename via
interview.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.bootstrap.canonical import (
    _hash_cluster_key,
    derive_ast_query,
    select_canonicals,
)
from chameleon_mcp.bootstrap.clustering import (
    BIMODAL_DOMINANT_SHARE_THRESHOLD,
    cluster_files,
)
from chameleon_mcp.bootstrap.discovery import (
    REPO_SIZE_GUARD,
    TooManyFilesError,
    discover_files,
    discovery_stats,
)
from chameleon_mcp.bootstrap.naming import propose_archetype_name
from chameleon_mcp.bootstrap.tool_config import read_tool_configs
from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
from chameleon_mcp.bootstrap.workspace import detect_workspace
from chameleon_mcp.conventions import (
    extract_all_conventions,
    extract_declarations_from_content,
    serialize_conventions,
)
from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import NodeUnavailableError, TypeScriptExtractor


def _is_rails_with_frontend(repo_root: Path) -> bool:
    """Detect a Rails-with-frontend hybrid (Bug 2 fix; Bug E broaden).

    Rails+Stimulus / Rails+Hotwire / Rails+ImportMaps colocate a JS/TS
    sidecar alongside Ruby production code under a handful of well-known
    conventions:

      - ``app/javascript/`` — modern Rails 6+ webpacker / esbuild /
        importmap-rails entry point (forem, mastodon).
      - ``app/assets/javascripts/`` — legacy Rails 5 sprockets layout
        (gitlabhq, older Discourse). Bug E added.
      - ``app/frontend/`` — Rails 7 convention used by some teams
        (Vite-rails default, jumpstart-pro). Bug E added.

    An earlier ``_select_extractor`` picked TypeScript first when both
    ``package.json`` and ``Gemfile`` existed, which silently excluded
    thousands of Ruby files (forem: 3,515; mastodon: 3,179). This
    predicate now fires on any of the three layouts so legacy Rails 5
    repos like gitlabhq are correctly classified.

    Signal: all three of
      - ``Gemfile`` present at the root
      - ``config/application.rb`` present (the canonical Rails marker —
        rules out vendored gemspecs / dual-language SDKs)
      - at least one of the three JS sidecar dirs above
    """
    if not (repo_root / "Gemfile").is_file():
        return False
    if not (repo_root / "config" / "application.rb").is_file():
        return False
    js_dir_candidates = (
        repo_root / "app" / "javascript",
        repo_root / "app" / "assets" / "javascripts",
        repo_root / "app" / "frontend",
    )
    return any(d.is_dir() for d in js_dir_candidates)


def _rails_frontend_dir(repo_root: Path) -> Path | None:
    """Return the first matching Rails JS sidecar dir, or ``None``.

    Mirror of the search order in ``_is_rails_with_frontend``. Used by
    the language_hint envelope so the message points at the actual dir
    on disk (``app/assets/javascripts`` on gitlabhq, ``app/javascript``
    on forem, …).
    """
    for sub in (
        ("app", "javascript"),
        ("app", "assets", "javascripts"),
        ("app", "frontend"),
    ):
        candidate = repo_root.joinpath(*sub)
        if candidate.is_dir():
            return candidate
    return None


def _count_ts_files_under(directory: Path) -> int:
    """Best-effort count of .ts(x) / .js(x) files under ``directory``.

    Used to populate the secondary-language file count in the
    ``language_hint`` field. Bounded by a hard 50_000 stop so a
    pathological symlink loop can't wedge bootstrap.
    """
    if not directory.is_dir():
        return 0
    count = 0
    cap = 50_000
    for ext in ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs"):
        try:
            for _ in directory.rglob(ext):
                count += 1
                if count >= cap:
                    return count
        except OSError:
            continue
    return count


def _ad_hoc_discovery_hints(repo_root: Path) -> list[dict]:
    """BUG-001: scan apps/* and packages/* for discoverable sub-projects.

    Walks at most two depths down standard monorepo parent dirs and
    returns one hint per child that carries its own ``package.json`` or
    ``Gemfile``. Caps at 50 results so a pathological tree doesn't
    balloon the response. Used only on the ``failed_unsupported_language``
    return path; the orchestrator's regular workspace fanout handles
    declared workspaces (yarn / pnpm / Turborepo).
    """
    hints: list[dict] = []
    cap = 500  # high enough to surface every real sub-project; the four parent dirs bound it
    for parent in ("apps", "packages", "services", "workspaces"):
        parent_dir = repo_root / parent
        if not parent_dir.is_dir():
            continue
        try:
            children = sorted(parent_dir.iterdir())
        except (OSError, PermissionError):
            continue
        for child in children:
            if not child.is_dir():
                continue
            language: str | None = None
            if (child / "package.json").is_file() or (child / "tsconfig.json").is_file():
                language = "typescript"
            elif (child / "Gemfile").is_file():
                language = "ruby"
            if language is None:
                continue
            try:
                rel = str(child.relative_to(repo_root))
            except ValueError:
                rel = str(child)
            hints.append(
                {
                    "subdir": rel,
                    "abs_path": str(child),
                    "language": language,
                }
            )
            if len(hints) >= cap:
                return hints
    return hints


def _count_ruby_files_under(directory: Path) -> int:
    """Best-effort count of .rb files under ``directory`` (mirror of
    _count_ts_files_under). BUG-017."""
    if not directory.is_dir():
        return 0
    count = 0
    cap = 50_000
    try:
        for _ in directory.rglob("*.rb"):
            count += 1
            if count >= cap:
                return count
    except OSError:
        pass
    return count


def _select_extractor(repo_root: Path) -> Extractor | None:
    """Pick the extractor whose can_handle() returns True for this repo.

    Precedence:
      1. Rails-with-frontend (Gemfile + config/application.rb + app/javascript/)
         → Ruby. The TS sidecar in ``app/javascript/`` is a separate
         workspace concern; the user can run ``bootstrap_repo`` on it
         independently. Surfaced via ``BootstrapReport.language_hint``.
      2. TypeScript > Ruby for all other repos. A repo that has both
         Gemfile and tsconfig.json without the Rails-with-frontend
         signal bootstraps with the TS extractor.
    """
    if _is_rails_with_frontend(repo_root):
        return RubyExtractor()
    for ext_cls in (TypeScriptExtractor, RubyExtractor):
        ext = ext_cls()
        if ext.can_handle(repo_root):
            return ext
    return None


# Raised from 50: workspaces 501+ were never analyzed (a sampling cap, not a
# safety guard). REPO_SIZE_GUARD is the real post-exclusion DoS backstop.
# Back-compat alias only; the live cap is read from _thresholds at call time
# (see _detect_workspace_ts_monorepo) so CHAMELEON_WORKSPACE_FANOUT_CAP works.
_WORKSPACE_FANOUT_CAP = 500

_WORKSPACE_PARENT_DIRS = ("apps", "packages", "services", "workspaces")


def _detect_workspace_ts_monorepo(
    repo_root: Path,
) -> tuple[list[str], bool]:
    """Detect a TS monorepo whose root package.json has no TS deps.

    Bug B: the common Turborepo / pnpm-workspaces / Nx pattern
    leaves the root ``package.json`` carrying only ``scripts`` (no
    ``dependencies``/``devDependencies``) and puts ``tsconfig.json`` +
    TS deps inside workspace dirs under ``apps/*``, ``packages/*``,
    ``services/*``, ``workspaces/*``. An earlier ``_select_extractor``
    saw no TS signal at the root and returned None → bootstrap reported
    ``failed_unsupported_language`` (bulletproof-react dogfood).

    A workspace dir qualifies when it contains either:
      - a ``tsconfig.json``, OR
      - a ``package.json`` whose content carries a TS-flavored token
        (``typescript``, ``ts-node``, ``vite``) — same signal
        ``TypeScriptExtractor.can_handle`` uses on a single repo.

    The first-level scan is bounded at the ``WORKSPACE_FANOUT_CAP``
    threshold (default 500, overridable via
    ``CHAMELEON_WORKSPACE_FANOUT_CAP``) entries per parent dir so a
    pathological tree with hundreds of empty entries can't walk forever.
    When the cap fires, the orchestrator sets ``fanout_capped=True`` in
    the bootstrap envelope.

    Args:
        repo_root: absolute path to repo root

    Returns:
        Tuple of ``(workspace_roots, fanout_capped)``.
        - ``workspace_roots`` is a sorted list of repo-relative POSIX
          paths (e.g. ``["apps/api", "apps/web"]``) — empty if no
          qualifying workspaces found.
        - ``fanout_capped`` is True if any parent dir hit the
          ``WORKSPACE_FANOUT_CAP`` ceiling.
    """
    package_json = repo_root / "package.json"
    if not package_json.is_file():
        return ([], False)
    if (repo_root / "tsconfig.json").exists():
        return ([], False)
    try:
        content = package_json.read_text(errors="replace")
    except OSError:
        return ([], False)
    if any(token in content for token in ("typescript", '"ts-node"', '"vite"')):
        return ([], False)

    from chameleon_mcp import _thresholds

    cap = _thresholds.threshold_int("WORKSPACE_FANOUT_CAP")
    workspace_roots: list[str] = []
    fanout_capped = False
    for parent_name in _WORKSPACE_PARENT_DIRS:
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        try:
            entries = sorted(p for p in parent.iterdir() if p.is_dir())
        except OSError:
            continue
        if len(entries) > cap:
            fanout_capped = True
            entries = entries[:cap]
        for entry in entries:
            if _is_ts_workspace(entry):
                workspace_roots.append(f"{parent_name}/{entry.name}")
    workspace_roots.sort()
    return (workspace_roots, fanout_capped)


def _is_ts_workspace(workspace_dir: Path) -> bool:
    """Return True if ``workspace_dir`` looks like a TS workspace.

    Mirrors ``TypeScriptExtractor.can_handle``'s rules at one level of
    depth: tsconfig.json wins, otherwise check package.json for TS
    tokens. Best-effort and tolerant of unreadable files.
    """
    if (workspace_dir / "tsconfig.json").is_file():
        return True
    pkg = workspace_dir / "package.json"
    if not pkg.is_file():
        return False
    try:
        content = pkg.read_text(errors="replace")
    except OSError:
        return False
    return any(token in content for token in ("typescript", '"ts-node"', '"vite"'))


def _glob_for_extractor(extractor: Extractor) -> str:
    if extractor.language == "ruby":
        return "**/*.rb"
    return "**/*.{ts,tsx,js,jsx,mjs,cjs}"


# Must track profile.schema.CURRENT_SCHEMA_VERSION — this is the version stamped
# into the profiles the bootstrap WRITES. v8: cluster signature unified with the
# conformance metric (sorted-set node kinds, import hash dropped from the key).
# Profiles written by old code (<=7) still load under this engine, but a profile
# written here (8) is refused by old engines (MAX=7), signaling the rebuild.
PROFILE_SCHEMA_VERSION = 8

# Use the package's bump-synced __version__ (same source as the read-side gate
# in profile/loader.py:ENGINE_VERSION). importlib.metadata.version("chameleon-mcp")
# returns a stale 0.5.7 fallback when the package isn't pip-installed (run via
# PYTHONPATH / the plugin's module path), which would make the engine stamp
# meaningless and the refresh engine-version guard inert.
from chameleon_mcp import __version__ as ENGINE_MIN_VERSION


@dataclass
class BootstrapReport:
    """Summary of a bootstrap run, returned to the MCP caller."""

    status: str
    archetypes_detected: int
    rules_extracted: int
    idioms_collected: int
    canonicals_skipped_failed_scans: int
    files_processed: int
    files_skipped_generated: int
    files_skipped_parse: int
    duration_ms: int
    profile_path: Path | None
    error: str | None = None
    sparse_cluster_warnings: list[dict] = field(default_factory=list)
    """Phase 2C.3: clusters with fewer than SPARSE_CLUSTER_THRESHOLD members.

    Each entry: {"paths_pattern": str, "size": int, "sample_paths": list[str]}.
    Sparse clusters are excluded from canonical selection but surfaced here so
    the future interview UI can prompt the user to merge or confirm them.
    """
    bimodal_cluster_warnings: list[dict] = field(default_factory=list)
    """Phase 2C.3: clusters that split bimodally on at least one signal.

    Each entry: {"paths_pattern": str, "size": int, "dimensions": [str, ...],
    "distributions": {dim: {value_str: count}}}. The future interview UI uses
    these to offer a manual split.
    """
    nested_profile_warnings: list[str] = field(default_factory=list)
    """BUG-NEW-005: pre-existing `.chameleon/` directories found
    in workspace subdirectories of the repo being bootstrapped.

    When a prior test/dogfood run bootstrapped a sub-workspace directly,
    its `.chameleon/` persists. Subsequent bootstraps at the parent root
    don't prune these — so `detect_repo` for a file in that workspace
    will resolve to the stale sub-profile instead of the freshly-written
    root profile. Surfacing them as warnings lets the user prune manually.

    Each entry is a relative path from repo_root, e.g.
    "apps/react-vite/.chameleon".
    """
    workspace_reports: list[dict] = field(default_factory=list)
    """Per-workspace bootstrap summaries for monorepos.

    Each entry mirrors the root report shape: {"workspace_path": str,
    "repo_id": str, "profile_dir": str, "repo_root": str, "status": str,
    "archetypes_detected": int, "files_processed": int, "duration_ms": int,
    "error": str | None}. Empty list for non-monorepo repos.
    """
    language_hint: dict | None = None
    """Bug 2: hybrid-language detection envelope.

    Populated when bootstrap picks one language but a meaningful sidecar
    in another language exists in the same repo (Rails+JS, TS-with-Ruby-
    scripts, etc.). Shape:
        {
          "primary": "ruby",
          "secondary_detected": "typescript",
          "secondary_file_count": int,
          "secondary_path": "<repo>/app/javascript",
          "note": "...recommendation...",
        }
    Surfaced in the bootstrap_repo envelope, persisted in profile.json,
    and rendered in profile.summary.md so the user can decide whether
    to run a second bootstrap on the sidecar.
    """
    workspace_roots: list[str] = field(default_factory=list)
    """Bug B: repo-relative workspace dirs found when the root
    package.json has no TS deps but TS lives one level down (Turborepo,
    pnpm-workspaces, Nx). Empty for single-root TS repos. Envelope-only
    today (not persisted to profile.json so the schema doesn't bump).
    """
    fanout_capped: bool = False
    """Bug B: True when the first-level workspace scan hit the
    50-entry cap. Surfaced so an unusually large monorepo's report is
    distinguishable from a clean run.
    """
    discovered_files_pre_exclusion: int = 0
    """Bug D: total files walked by discovery, before
    EXCLUDE_FROM_CLUSTERING_DIRS / EXTENSIONS / EXACT_RELPATHS dropped
    anything. Lets coverage tooling reason about where files went.
    """
    discovered_files_post_exclusion: int = 0
    """Bug D: files that survived the discovery-layer exclusion
    sets and were handed to the extractor. Always <= pre.
    """
    sparse_dropped_files: int = 0
    """Bug D: files dropped because their cluster fell below
    the adaptive sparse_threshold. Sparse-cluster members never reach
    canonical selection but contribute to the post-clustering count.
    Always >= 0.
    """
    discovery_hints: list[dict] = field(default_factory=list)
    """BUG-001: when bootstrap fails with
    ``failed_unsupported_language`` on a directory that *looks* like an
    ad-hoc monorepo (apps/* or packages/* subdirs each carrying their
    own package.json), this list names the discoverable sub-projects so
    the slash-command UI can prompt the user to bootstrap each one.

    Shape:
        [
          {"subdir": "apps/web", "abs_path": "/.../apps/web", "language": "typescript"},
          {"subdir": "apps/api", "abs_path": "/.../apps/api", "language": "ruby"},
        ]
    """

    def to_dict(self) -> dict:
        per_workspace: dict[str, int] = {}
        ws_total = 0
        for w in self.workspace_reports:
            if w.get("status") == "success":
                count = int(w.get("archetypes_detected") or 0)
                per_workspace[str(w.get("workspace_path") or w.get("repo_root") or "")] = count
                ws_total += count
        archetypes_total = self.archetypes_detected + ws_total
        out: dict = {
            "status": self.status,
            "archetypes_detected": archetypes_total,
            "archetypes_detected_root": self.archetypes_detected,
            "archetypes_per_workspace": per_workspace,
            "rules_extracted": self.rules_extracted,
            "idioms_collected": self.idioms_collected,
            "canonicals_skipped_failed_scans": self.canonicals_skipped_failed_scans,
            "files_processed": self.files_processed,
            "files_skipped_generated": self.files_skipped_generated,
            "files_skipped_parse": self.files_skipped_parse,
            "duration_ms": self.duration_ms,
            "profile_path": str(self.profile_path) if self.profile_path else None,
            "error": self.error,
            "sparse_cluster_warnings": list(self.sparse_cluster_warnings),
            "bimodal_cluster_warnings": list(self.bimodal_cluster_warnings),
            "nested_profile_warnings": list(self.nested_profile_warnings),
            "workspaces": list(self.workspace_reports),
        }
        out["language_hint"] = self.language_hint
        out["workspace_roots"] = list(self.workspace_roots)
        out["fanout_capped"] = bool(self.fanout_capped)
        out["discovered_files_pre_exclusion"] = int(self.discovered_files_pre_exclusion)
        out["discovered_files_post_exclusion"] = int(self.discovered_files_post_exclusion)
        out["discovery_hints"] = list(self.discovery_hints)
        out["clustered_files"] = int(self.files_processed)
        out["sparse_dropped_files"] = int(self.sparse_dropped_files)
        return out


def _compute_repo_id(repo_root: Path) -> str:
    """Compute repo_id per docs/architecture.md rule:
    sha256(canonicalize(git_remote_url)) if remote present, else
    sha256(canonicalize_path(repo_root)).

    Delegates to `tools._compute_repo_id` so the orchestrator
    and the MCP tool layer can never disagree on the canonical id — a
    drift the original code path tolerated only because the two
    implementations happened to be byte-identical.
    """
    from chameleon_mcp.tools import _compute_repo_id as _tools_compute_repo_id

    return _tools_compute_repo_id(repo_root)


RENAMES_SCHEMA_VERSION = 1


def _load_user_renames(profile_dir: Path) -> dict[str, str]:
    """Return the {auto_name: user_name} overlay from `.chameleon/renames.json`.

    Returns an empty dict when the file is absent, malformed, oversized,
    carries a future schema_version this build cannot interpret, exceeds
    the entry cap, or contains values that do not satisfy ARCHETYPE_NAME_RE.

    Security: the read is funneled through safe_read_profile_artifact, which
    refuses symlinks (O_NOFOLLOW) and enforces the 5 MB artifact cap so a
    teammate cannot weaponize a committed renames.json. Values are then
    validated against ARCHETYPE_NAME_RE so prompt-injection content disguised
    as a rename target cannot reach LLM context. The cardinality cap closes
    the per-entry memory amplifier.

    The bootstrap pipeline applies the returned mapping AFTER the heuristic
    naming pass, so unknown auto-names (e.g., the heuristic produced
    something different from what was originally renamed) are simply
    skipped — they remain in renames.json untouched for the next pass.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE
    from chameleon_mcp.safe_open import (
        UnsafeFileError,
        safe_read_profile_artifact,
    )

    path = profile_dir / "renames.json"
    try:
        text = safe_read_profile_artifact(path)
    except FileNotFoundError:
        return {}
    except (OSError, UnsafeFileError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv > RENAMES_SCHEMA_VERSION:
        return {}
    renames = data.get("renames", {})
    if not isinstance(renames, dict):
        return {}
    cap = threshold_int("RENAMES_OVERLAY_CAP")
    if len(renames) > cap:
        return {}
    out: dict[str, str] = {}
    for k, v in renames.items():
        if not (isinstance(k, str) and isinstance(v, str) and k and v):
            continue
        if not ARCHETYPE_NAME_RE.match(v):
            continue
        out[k] = v
    return out


def _generation_counter(now: float | None = None) -> int:
    """Profile generation counter. Round 4 distributed-systems addition.

    All four committed JSON files embed the same generation counter; loaders
    verify consistency via the double-fstat pattern.
    """
    return int(now if now is not None else time.time())


_RAILS_LOAD_BEARING_SECOND_SEGS = frozenset(
    {
        "models",
        "controllers",
        "services",
        "jobs",
        "mailers",
        "helpers",
        "policies",
        "serializers",
        "presenters",
        "workers",
        "views",
        "channels",
        "javascript",
    }
)


def _displayed_paths_pattern(
    bucket: str,
    witness_relpath: str,
) -> str:
    """Return the paths_pattern string to surface in archetypes.json.

    Falls back to the cluster's signature bucket unchanged in the common
    case. When the witness's first segment is ``app`` AND its second
    segment is a load-bearing Rails dir (``models``, ``controllers``, …)
    that the v5 bucket formula dropped, we re-derive a Rails-honest
    bucket of shape ``app/<second>/<directory-of-the-witness-tail>`` so
    the displayed path always agrees with the witness.

    Examples (bucket → displayed):
      ``app/rule/action_executor`` + witness
        ``app/models/rule/action_executor/auto_categorize.rb``
        → ``app/models/action_executor``

      ``app/admin/dashboards`` + witness
        ``app/controllers/admin/dashboards/foo.rb``
        → ``app/controllers/dashboards``

      ``app/models`` + witness ``app/models/user.rb``
        → ``app/models`` (unchanged; bucket already correct)

      ``src/components/base`` + witness ``src/components/base/Button.tsx``
        → ``src/components/base`` (non-Rails; unchanged)
    """
    if not witness_relpath:
        return bucket
    witness_parts = [p for p in witness_relpath.split("/") if p]
    if len(witness_parts) < 4:
        return bucket
    if witness_parts[0] != "app":
        return bucket
    if witness_parts[1] not in _RAILS_LOAD_BEARING_SECOND_SEGS:
        return bucket
    if witness_parts[1] in bucket.split("/"):
        return bucket
    return f"{witness_parts[0]}/{witness_parts[1]}/{witness_parts[-2]}"


_WARNING_SAMPLE_PATHS = 3


def _rel_or_abs(path: Path, repo_root: Path) -> str:
    """Best-effort relative path; falls back to absolute if outside repo_root."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _stringify_distribution_key(value: object) -> str:
    """Render an arbitrary value as a stable JSON-dict-key string.

    Booleans → "true" / "false"; None → "null"; everything else → str(value).
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


_SPARSE_WARNING_LIMIT = 50
"""BUG-008/009: cap on the per-bootstrap sparse_cluster_warnings
list. Earlier bootstrap returned 2000-6000 warning entries on
mid-sized repos and exceeded the MCP protocol's response size, breaking
chameleon-init. The cap is applied after the same-paths_pattern
aggregation step below."""


def _build_sparse_warnings(sparse_clusters, repo_root: Path) -> list[dict]:
    """Build the sparse-cluster warning payload for BootstrapReport.

    Phase 2C.3: surface clusters with <threshold members. Bug 4
    makes the threshold adaptive based on corpus size, so each warning
    records the cluster's resolved threshold instead of the legacy
    module-level constant.

    BUG-008/009: aggregate by ``paths_pattern`` first so 50
    singletons at ``src/x/y:ts`` collapse to one row with
    ``cluster_count: 50, total_members: 50``. After aggregation, cap at
    ``_SPARSE_WARNING_LIMIT`` and surface ``truncated`` + ``total_groups``
    so consumers know the cap fired.

    Each warning entry includes the path bucket, size, and a handful of
    sample paths so the future interview UI can ask "merge with X?".
    """
    grouped: dict[str, dict] = {}
    insertion_order: list[str] = []
    for cluster in sparse_clusters:
        bucket = cluster.key.path_pattern_bucket or "(unknown)"
        sample_paths = [
            _rel_or_abs(m.path, repo_root) for m in cluster.members[:_WARNING_SAMPLE_PATHS]
        ]
        if bucket not in grouped:
            grouped[bucket] = {
                "kind": "sparse_cluster",
                "paths_pattern": bucket,
                "cluster_count": 1,
                "total_members": int(cluster.size),
                "min_size": int(cluster.size),
                "max_size": int(cluster.size),
                "sample_paths": list(sample_paths),
                "thresholds": [int(cluster.sparse_threshold)],
            }
            insertion_order.append(bucket)
        else:
            g = grouped[bucket]
            g["cluster_count"] += 1
            g["total_members"] += int(cluster.size)
            g["min_size"] = min(g["min_size"], int(cluster.size))
            g["max_size"] = max(g["max_size"], int(cluster.size))
            g["thresholds"].append(int(cluster.sparse_threshold))
            remaining = _WARNING_SAMPLE_PATHS - len(g["sample_paths"])
            if remaining > 0 and sample_paths:
                g["sample_paths"].extend(sample_paths[:remaining])

    warnings: list[dict] = []
    for bucket in insertion_order:
        g = grouped[bucket]
        thresholds = g.pop("thresholds")
        threshold_str = (
            str(thresholds[0])
            if len(set(thresholds)) == 1
            else f"{min(thresholds)}-{max(thresholds)}"
        )
        if g["cluster_count"] == 1:
            g["reason"] = f"cluster has {g['total_members']} members (threshold {threshold_str})"
            g["size"] = g.pop("total_members")
            g.pop("min_size", None)
            g.pop("max_size", None)
            g.pop("cluster_count", None)
        else:
            g["reason"] = (
                f"{g['cluster_count']} sparse clusters at this paths_pattern "
                f"({g['total_members']} files; sizes {g['min_size']}-{g['max_size']}; "
                f"threshold {threshold_str})"
            )
        warnings.append(g)

    total_groups = len(warnings)
    truncated = total_groups > _SPARSE_WARNING_LIMIT
    if truncated:
        warnings = warnings[:_SPARSE_WARNING_LIMIT]
        warnings.append(
            {
                "kind": "sparse_cluster_truncated",
                "truncated": True,
                "total_groups": total_groups,
                "shown": _SPARSE_WARNING_LIMIT,
                "note": (
                    f"BUG-008/009: {total_groups - _SPARSE_WARNING_LIMIT} "
                    "additional sparse-cluster groups omitted to keep the "
                    "bootstrap response within MCP transport limits."
                ),
            }
        )
    return warnings


def _build_bimodal_warnings(bimodal_clusters, repo_root: Path) -> list[dict]:
    """Build the bimodal-cluster warning payload for BootstrapReport.

    For each flagged cluster, record the dimensions that split bimodally
    and the per-dimension value distribution. JSON-friendly: keys are
    stringified so booleans and Nones don't clash with JSON dict-key
    constraints when callers serialize the report.
    """
    warnings: list[dict] = []
    for cluster in bimodal_clusters:
        flagged_dims = cluster.bimodal_dimensions
        distributions: dict[str, dict[str, int]] = {}
        for dim in flagged_dims:
            raw = cluster.dimension_distribution(dim)
            distributions[dim] = {
                _stringify_distribution_key(value): count for value, count in raw.items()
            }
        sample_paths = [
            _rel_or_abs(m.path, repo_root) for m in cluster.members[:_WARNING_SAMPLE_PATHS]
        ]
        warnings.append(
            {
                "kind": "bimodal_cluster",
                "reason": (
                    f"cluster splits 60/40 or worse on "
                    f"{', '.join(flagged_dims)} "
                    f"(threshold {int(BIMODAL_DOMINANT_SHARE_THRESHOLD * 100)}% dominant share)"
                ),
                "paths_pattern": cluster.key.path_pattern_bucket,
                "size": cluster.size,
                "dimensions": flagged_dims,
                "distributions": distributions,
                "sample_paths": sample_paths,
            }
        )
    return warnings


def _collapse_same_pattern_archetypes(
    archetypes: dict,
    canonicals: dict,
) -> tuple[dict, dict]:
    """Merge archetypes that share a paths_pattern into the
    highest-cluster_size sibling. Discarded archetypes' canonical
    entries are appended to the kept archetype's canonicals list
    (preserving the kept archetype's primary canonical at index 0).
    Cluster sizes accumulate. Result: paths_pattern is unique across
    archetypes, eliminating the resolver's cluster_size-tiebreak
    unreachability for same-directory-witness siblings.
    """
    by_pattern: dict[str, list[str]] = {}
    for name, meta in archetypes.items():
        pat = meta.get("paths_pattern", "")
        if not pat:
            continue
        by_pattern.setdefault(pat, []).append(name)

    new_archetypes = dict(archetypes)
    new_canonicals = dict(canonicals)

    for _pat, names in by_pattern.items():
        if len(names) <= 1:
            continue
        names_sorted = sorted(
            names,
            key=lambda n: (
                -archetypes[n].get("cluster_size", 0),
                n,
            ),
        )
        keeper = names_sorted[0]
        losers = names_sorted[1:]
        kept_meta = dict(new_archetypes[keeper])
        kept_meta["cluster_size"] = sum(archetypes[n].get("cluster_size", 0) for n in names_sorted)
        new_archetypes[keeper] = kept_meta
        merged_canonicals = list(new_canonicals.get(keeper, []))
        for loser in losers:
            for entry in new_canonicals.get(loser, []):
                merged_canonicals.append(entry)
            new_canonicals.pop(loser, None)
            new_archetypes.pop(loser, None)
        new_canonicals[keeper] = merged_canonicals

    return new_archetypes, new_canonicals


def _resolve_cluster_id(cluster, selection):
    """Map a dense cluster to its ``(cluster_id, selection_or_None)``.

    - ``(cluster_id, CanonicalSelection)`` when the cluster has a chosen witness.
    - ``(cluster_id, None)`` when the cluster has no eligible/clean canonical
      (e.g. an all-spec/test cluster, where every member is canonical-pool
      excluded). The archetype is still emitted — just without a witness — so
      its files get rules + nearby-sibling guidance instead of resolving to
      ``archetype=None``. Mirrors the ``EXCLUDE_FROM_CANONICAL_POOL`` contract
      in ``discovery.py`` ("clustered but never picked as canonical").
    - ``(None, None)`` when the cluster is unknown to the selection (only-failing
      scans, which intentionally stay dropped so an unsafe witness is never
      surfaced).
    """
    members = {pf.path for pf in cluster.members}
    for cid, sel in selection.selections.items():
        if sel.witness_path in members:
            return cid, sel
    cid = _hash_cluster_key(cluster)
    no_canonical_ids = {_hash_cluster_key(c) for c in selection.clusters_without_eligible_canonical}
    if cid in no_canonical_ids:
        return cid, None
    return None, None


def bootstrap_repo(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
    now: float | None = None,
) -> BootstrapReport:
    """Run the full bootstrap pipeline on a repo.

    Phase 2B emits a non-interactive profile. Phase 2D wraps this with the
    interactive interview flow.

    When `detect_workspace` returns one or more workspace_paths
    (pnpm/yarn/lerna/turbo/nx), this also runs the full pipeline per
    workspace and writes a `.chameleon/` to each workspace root in addition
    to the repo-root profile. The repo-root profile catalogs the workspaces
    in `profile.json.workspaces` so `/chameleon-list-profiles` and the
    trust resolution layer can find them. Non-monorepo repos
    (`workspace_paths == []`) bootstrap unchanged.

    Args:
        repo_root: absolute path to repo root (resolved before passing in)
        paths_glob: optional user-supplied scope override
        profile_dir_name: name of the committed profile dir (default ".chameleon")

    Returns:
        BootstrapReport summarizing the run. `workspace_reports` lists the
        per-workspace outcomes when applicable.
    """
    report = _bootstrap_single(
        repo_root,
        paths_glob=paths_glob,
        profile_dir_name=profile_dir_name,
        now=now,
    )

    workspace = detect_workspace(repo_root)
    if not workspace.has_workspaces:
        return report

    # NB: do NOT early-return on a failed root here. A pnpm/yarn/lerna root
    # whose packages live under a non-standard dir (modules/*, clients/*, ...)
    # fails its own language detection, but detect_workspace still located the
    # bootstrappable workspaces — fan out to them anyway (coordinator-only root).

    workspace_reports: list[dict] = []
    for ws_path in workspace.workspace_paths:
        ws_root = ws_path.resolve()
        try:
            if ws_root == repo_root.resolve():
                continue
        except OSError:
            continue
        ws_report = _bootstrap_single(
            ws_root,
            paths_glob=paths_glob,
            profile_dir_name=profile_dir_name,
            now=now,
        )
        from chameleon_mcp.tools import _compute_repo_id as _id

        workspace_reports.append(
            {
                "workspace_path": str(ws_path),
                "repo_root": str(ws_root),
                "repo_id": _id(ws_root),
                "profile_dir": (str(ws_report.profile_path) if ws_report.profile_path else None),
                "status": ws_report.status,
                "archetypes_detected": ws_report.archetypes_detected,
                "files_processed": ws_report.files_processed,
                "duration_ms": ws_report.duration_ms,
                "error": ws_report.error,
            }
        )

    if workspace_reports:
        report.workspace_reports = workspace_reports
        # Only amend the root profile if the root actually produced one; a
        # coordinator-only root (failed language detection) has no root profile
        # to amend, but its workspaces were still bootstrapped above.
        if report.status == "success":
            _amend_root_profile_with_workspaces(repo_root / profile_dir_name, workspace_reports)
        elif any(w.get("status") == "success" for w in workspace_reports):
            # Coordinator-only root (no own language) but >=1 workspace
            # bootstrapped — report partial success so the envelope doesn't
            # claim init failed when profiles were actually created.
            report.status = "success_workspaces_only"

    return report


def _amend_root_profile_with_workspaces(profile_dir: Path, workspace_reports: list[dict]) -> None:
    """Re-write profile.json with a `workspaces` array describing each
    successfully bootstrapped sub-workspace.

    Wraps the rewrite in the same atomic_profile_commit transaction the
    initial bootstrap used so concurrent loaders never see a half-written
    profile. The other four JSON artifacts + idioms.md + summary are
    re-read from the existing committed profile and re-emitted verbatim
    inside the new txn so the generation counter stays consistent across
    files (the loader's double-fstat check requires it).
    """
    profile_path = profile_dir / "profile.json"
    if not profile_path.is_file():
        return

    try:
        profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    profile_data["workspaces"] = [
        {
            "workspace_path": w["workspace_path"],
            "repo_id": w["repo_id"],
            "profile_dir": w["profile_dir"],
            "status": w["status"],
        }
        for w in workspace_reports
    ]

    artifact_names = ("archetypes.json", "canonicals.json", "rules.json")
    siblings: dict[str, str] = {}
    for name in artifact_names:
        path = profile_dir / name
        try:
            siblings[name] = path.read_text(encoding="utf-8")
        except OSError:
            return
    conventions_path = profile_dir / "conventions.json"
    if conventions_path.is_file():
        try:
            siblings["conventions.json"] = conventions_path.read_text(encoding="utf-8")
        except OSError:
            pass
    principles_path = profile_dir / "principles.md"
    if principles_path.is_file():
        try:
            siblings["principles.md"] = principles_path.read_text(encoding="utf-8")
        except OSError:
            pass

    idioms_text: str
    idioms_path = profile_dir / "idioms.md"
    try:
        idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.is_file() else ""
    except OSError:
        idioms_text = ""

    summary_path = profile_dir / "profile.summary.md"
    try:
        summary_text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    except OSError:
        summary_text = ""

    renames_path = profile_dir / "renames.json"
    renames_text: str | None = None
    if renames_path.is_file():
        try:
            renames_text = renames_path.read_text(encoding="utf-8")
        except OSError:
            renames_text = None

    with atomic_profile_commit(profile_dir) as txn_dir:
        (txn_dir / "profile.json").write_text(
            json.dumps(profile_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        for name, body in siblings.items():
            (txn_dir / name).write_text(body, encoding="utf-8")
        (txn_dir / "idioms.md").write_text(idioms_text, encoding="utf-8")
        (txn_dir / "profile.summary.md").write_text(summary_text, encoding="utf-8")
        if renames_text is not None:
            (txn_dir / "renames.json").write_text(renames_text, encoding="utf-8")


def _generate_archetype_summary(
    entry: dict,
    canonical_witness_path: Path | None,
    language: str,
) -> str:
    """Heuristic summary for Tier 1 PreToolUse pointers."""
    parts = []
    pattern = entry.get("paths_pattern_display", entry.get("paths_pattern", ""))
    if pattern:
        parts.append(pattern)

    kinds = entry.get("top_level_node_kinds", [])
    if kinds:
        parts.append(", ".join(kinds[:3]))

    signal = entry.get("content_signal", "none")
    if signal and signal != "none":
        parts.append(signal)

    if canonical_witness_path and canonical_witness_path.is_file():
        try:
            head = canonical_witness_path.read_bytes()[:2000].decode("utf-8", errors="replace")
            import re

            # [\w:]+ for the class name + base so a namespaced declaration
            # (class Api::V1::Foo < Api::V1::Base) still yields "inherits ...".
            m = re.search(r"class\s+[\w:]+\s*<\s*([\w:]+)", head)
            if not m:
                m = re.search(r"class\s+[\w:.]+\s+extends\s+([\w:.]+)", head)
            if m:
                parts.append(f"inherits {m.group(1)}")
            if "'use client'" in head or '"use client"' in head:
                parts.append("client component")
        except OSError:
            pass

    return ". ".join(parts) + "." if parts else ""


def _bootstrap_single(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
    now: float | None = None,
) -> BootstrapReport:
    """The original single-target bootstrap pipeline.

    Extracted so the monorepo loop can call it once per workspace
    without duplicating the discovery → cluster → canonical → commit
    plumbing. Behavior on a non-monorepo repo is byte-identical to the
    An earlier implementation.
    """
    started_at = time.time()
    profile_dir = repo_root / profile_dir_name

    workspace = detect_workspace(repo_root)

    inherited_signals_from: Path | None = None
    own_js = (repo_root / "package.json").is_file() or (repo_root / "tsconfig.json").is_file()
    own_ruby = (repo_root / "Gemfile").is_file() or any(repo_root.glob("*.gemspec"))
    if not own_js and not own_ruby:
        ancestor = repo_root.parent
        for _ in range(4):
            if (ancestor / "package.json").is_file() or (ancestor / "Gemfile").is_file():
                inherited_signals_from = ancestor
                break
            if ancestor.parent == ancestor:
                break
            ancestor = ancestor.parent
    if inherited_signals_from is not None:
        tool_configs = read_tool_configs(inherited_signals_from)
    else:
        tool_configs = read_tool_configs(repo_root)

    extractor = _select_extractor(repo_root)
    if extractor is None and inherited_signals_from is not None:
        ts_count = ruby_count = 0
        for ext_token in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            ts_count += sum(1 for _ in repo_root.rglob(f"*{ext_token}"))
            if ts_count > 5:
                break
        for _ in repo_root.rglob("*.rb"):
            ruby_count += 1
            if ruby_count > 5:
                break

        if ts_count > ruby_count and ts_count > 0:
            extractor = TypeScriptExtractor()
        elif ruby_count > 0:
            extractor = RubyExtractor()
        else:
            extractor = _select_extractor(inherited_signals_from)
    workspace_roots: list[str] = []
    fanout_capped = False
    if extractor is None:
        workspace_roots, fanout_capped = _detect_workspace_ts_monorepo(repo_root)
        if workspace_roots:
            extractor = TypeScriptExtractor()
            if not tool_configs.eslint and not tool_configs.prettier and not tool_configs.tsconfig:
                for ws_rel in workspace_roots:
                    ws_path = repo_root / ws_rel
                    if not ws_path.is_dir():
                        continue
                    ws_configs = read_tool_configs(ws_path)
                    if (
                        ws_configs.eslint
                        or ws_configs.prettier
                        or ws_configs.tsconfig
                        or ws_configs.rubocop
                    ):
                        tool_configs = ws_configs
                        tool_configs.sources = {
                            k: f"{ws_rel}/{v}" for k, v in ws_configs.sources.items()
                        }
                        break
    if extractor is None:
        hints = _ad_hoc_discovery_hints(repo_root)
        return BootstrapReport(
            status="failed_unsupported_language",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=0,
            files_skipped_generated=0,
            files_skipped_parse=0,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error=(
                "No TypeScript signals (tsconfig.json / package.json TS deps) "
                "and no Ruby signals (Gemfile / *.gemspec) detected"
            ),
            fanout_capped=fanout_capped,
            discovery_hints=hints,
        )

    language_hint: dict | None = None
    if extractor.language == "ruby" and _is_rails_with_frontend(repo_root):
        js_dir = _rails_frontend_dir(repo_root)
        if js_dir is not None:
            secondary_count = _count_ts_files_under(js_dir)
            if secondary_count > 0:
                try:
                    js_dir_display = str(js_dir.relative_to(repo_root))
                except ValueError:
                    js_dir_display = str(js_dir)
                language_hint = {
                    "primary": "ruby",
                    "secondary_detected": "typescript",
                    "secondary_file_count": secondary_count,
                    "secondary_path": str(js_dir),
                    "note": (
                        "Ruby-with-frontend repo detected; JS sidecar in "
                        f"{js_dir_display}/ not scanned by this bootstrap. "
                        f"Run bootstrap_repo({js_dir}) for the JS half."
                    ),
                }
    elif extractor.language == "typescript" and (repo_root / "Gemfile").is_file():
        ruby_count = _count_ruby_files_under(repo_root)
        if ruby_count >= 50:
            language_hint = {
                "primary": "typescript",
                "secondary_detected": "ruby",
                "secondary_file_count": ruby_count,
                "secondary_path": str(repo_root),
                "note": (
                    "TypeScript signals took precedence at the repo root, "
                    "but a Gemfile and a substantive Ruby tree were also "
                    "detected. Run bootstrap_repo on a Ruby-only subtree "
                    "(or re-organize the repo with a recognized Rails "
                    "frontend layout) to get Ruby archetype coverage."
                ),
            }

    discovery_glob = paths_glob or _glob_for_extractor(extractor)
    ws_arg = workspace_roots or None
    stats = discovery_stats(
        repo_root,
        glob=discovery_glob,
        paths_glob=paths_glob,
        workspace_roots=ws_arg,
    )
    pre_exclusion_count = stats.get("pre_exclusion", 0)
    post_exclusion_count = stats.get("post_exclusion", 0)
    try:
        candidates = discover_files(
            repo_root,
            glob=discovery_glob,
            paths_glob=paths_glob,
            workspace_roots=ws_arg,
        )
    except TooManyFilesError as e:
        return BootstrapReport(
            status="failed_too_many_files",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=0,
            files_skipped_generated=0,
            files_skipped_parse=0,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error=f"Repo has {e.count} files (ceiling {REPO_SIZE_GUARD}); use explicit paths_glob",
            workspace_roots=list(workspace_roots),
            fanout_capped=fanout_capped,
            discovered_files_pre_exclusion=pre_exclusion_count,
            discovered_files_post_exclusion=post_exclusion_count,
        )

    if not candidates:
        if paths_glob:
            err_msg = (
                f"No source files found matching paths_glob {paths_glob!r}. "
                "Verify the pattern (brace expansion is supported in both "
                "directory and basename) and that the chosen extensions "
                "actually exist under the repo."
            )
        else:
            err_msg = "No source files found matching the discovery glob"
        return BootstrapReport(
            status="failed_unsupported_language",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=0,
            files_skipped_generated=0,
            files_skipped_parse=0,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error=err_msg,
            workspace_roots=list(workspace_roots),
            fanout_capped=fanout_capped,
            discovered_files_pre_exclusion=pre_exclusion_count,
            discovered_files_post_exclusion=post_exclusion_count,
        )

    try:
        parse_result = extractor.parse_repo(repo_root, paths=candidates)
    except NodeUnavailableError as exc:
        # Node/npm couldn't be provisioned for the TS extractor. Degrade to a
        # clean report instead of aborting the whole bootstrap run.
        return BootstrapReport(
            status="failed_node_unavailable",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=0,
            files_skipped_generated=0,
            files_skipped_parse=0,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error=str(exc),
            workspace_roots=list(workspace_roots),
            fanout_capped=fanout_capped,
            discovered_files_pre_exclusion=pre_exclusion_count,
            discovered_files_post_exclusion=post_exclusion_count,
        )
    files_skipped_parse = len(parse_result.skipped)

    clustering = cluster_files(parse_result.files, repo_root=repo_root)
    files_skipped_generated = len(clustering.skipped_generated)
    sparse_dropped_files = sum(c.size for c in clustering.sparse_clusters)

    sparse_warnings = _build_sparse_warnings(clustering.sparse_clusters, repo_root)
    bimodal_warnings = _build_bimodal_warnings(clustering.bimodal_clusters, repo_root)

    selection = select_canonicals(clustering.dense_clusters, repo_root, now=now)
    canonicals_skipped_failed_scans = len(selection.clusters_with_only_failing_canonicals)

    generation = _generation_counter(now=started_at)
    repo_id = _compute_repo_id(repo_root)

    archetypes_data: dict = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "generation": generation,
        "archetypes": {},
    }

    canonicals_data: dict = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "generation": generation,
        "canonicals": {},
    }

    rules_data: dict = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "generation": generation,
        "rules": {},
    }

    rename_map = _load_user_renames(profile_dir)

    assigned_names: set[str] = set()
    for target in rename_map.values():
        assigned_names.add(target)

    for cluster in clustering.dense_clusters:
        cluster_id, sel = _resolve_cluster_id(cluster, selection)
        if cluster_id is None:
            continue
        auto_name = propose_archetype_name(
            cluster,
            assigned_names,
            workspace_roots=workspace_roots or None,
            repo_root=str(repo_root),
        )
        effective_name = rename_map.get(auto_name, auto_name)
        assigned_names.add(auto_name)
        assigned_names.add(effective_name)
        # Clusters whose members are all canonical-pool-excluded (e.g. an
        # all-spec/test cluster) have sel is None: emit the archetype with no
        # witness so those files still get rules + nearby guidance.
        if sel is not None:
            try:
                witness_relpath = str(sel.witness_path.relative_to(repo_root))
            except ValueError:
                witness_relpath = ""
        else:
            witness_relpath = ""
        bucket = cluster.key.path_pattern_bucket
        display_pattern = _displayed_paths_pattern(bucket, witness_relpath)
        archetype_entry: dict = {
            "cluster_id": cluster_id,
            "cluster_size": cluster.size,
            "paths_pattern": bucket,
            "paths_pattern_display": display_pattern,
            "content_signal": cluster.key.content_signal_match,
            "top_level_node_kinds": list(cluster.key.top_level_node_kinds),
            "jsx_present": cluster.key.jsx_present,
            "default_export_kind": cluster.key.default_export_kind,
            "named_export_count_bucket": cluster.key.named_export_count_bucket,
        }
        if cluster.sub_bucket_counts:
            archetype_entry["sub_buckets"] = dict(cluster.sub_bucket_counts)
        witness_path = None
        try:
            if hasattr(sel, "witness_path") and sel.witness_path:
                witness_path = repo_root / sel.witness_path
        except Exception:
            pass
        archetype_entry["summary"] = _generate_archetype_summary(
            archetype_entry,
            witness_path,
            extractor.language,
        )
        archetypes_data["archetypes"][effective_name] = archetype_entry
        if sel is not None:
            canonicals_data["canonicals"][effective_name] = [
                {
                    "witness": {
                        "path": witness_relpath or str(sel.witness_path),
                        "sha_hint": sel.sha_hint,
                    },
                    "normative_shape": {
                        "ast_query": derive_ast_query(cluster.key),
                    },
                    "normative_idioms": {
                        "comments": [],
                    },
                    "secret_scan_passed": sel.secret_scan_passed,
                    "injection_scan_passed": sel.injection_scan_passed,
                    "poisoning_scan_passed": sel.poisoning_scan_passed,
                    "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ]

    archetypes_data["archetypes"], canonicals_data["canonicals"] = (
        _collapse_same_pattern_archetypes(
            archetypes_data["archetypes"],
            canonicals_data["canonicals"],
        )
    )

    archetype_count = len(archetypes_data["archetypes"])

    _cid_to_archname: dict[str, str] = {}
    for arch_name, body in archetypes_data["archetypes"].items():
        cid = body.get("cluster_id")
        if isinstance(cid, str):
            _cid_to_archname[cid] = arch_name

    files_by_archetype: dict[str, list] = {}
    for cluster in clustering.dense_clusters:
        cluster_id, _sel = _resolve_cluster_id(cluster, selection)
        arch_name = _cid_to_archname.get(cluster_id) if cluster_id else None
        if arch_name:
            files_by_archetype.setdefault(arch_name, []).extend(cluster.members)

    declarations_by_archetype: dict[str, dict[str, list[str]]] = {}
    if extractor.language == "typescript":
        for arch_name, pf_list in files_by_archetype.items():
            merged: dict[str, list[str]] = {}
            for pf in pf_list:
                try:
                    # 1 MB (matches the extractor MAX_FILE_SIZE) so declarations
                    # past 100KB in a member file are not dropped from naming.
                    content = pf.path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    decls = extract_declarations_from_content(content, language="typescript")
                    for decl_type, names in decls.items():
                        merged.setdefault(decl_type, []).extend(names)
                except (OSError, UnicodeDecodeError):
                    continue
            if merged:
                declarations_by_archetype[arch_name] = merged

    conventions_data = extract_all_conventions(
        files_by_archetype=files_by_archetype,
        declarations_by_archetype=declarations_by_archetype,
        generation=generation,
        language=extractor.language,
    )

    # Carry user-taught banned imports (conventions.imports.<arch>.competing) across
    # the re-derive. extract_all_conventions only produces the derived `preferred`
    # lists, so without this a refresh drops every /chameleon-teach banned import and
    # silently disables banned-import enforcement.
    try:
        prior_conv_path = profile_dir / "conventions.json"
        if prior_conv_path.is_file():
            from chameleon_mcp.conventions import merge_taught_competing

            prior_conv = json.loads(prior_conv_path.read_text(encoding="utf-8"))
            merge_taught_competing(prior_conv, conventions_data)
    except Exception:
        pass

    profile_data: dict = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "generation": generation,
        "repo_id": repo_id,
        "language": extractor.language,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "bootstrap",
        "archetype_count": archetype_count,
        "workspace": {
            "is_workspace": workspace.is_workspace,
            "manager": workspace.manager,
            "workspace_count": len(workspace.workspace_paths),
        },
        "tool_configs": {
            "sources": tool_configs.sources,
            "warnings": {
                "prettier_js_plugins": tool_configs.has_prettier_js_plugins,
                "eslint_js_plugins": tool_configs.has_eslint_js_plugins,
            },
        },
    }
    if language_hint is not None:
        profile_data["language_hint"] = language_hint

    profile_data["clustering_algorithm_version"] = 2

    if paths_glob is not None:
        profile_data["discovery"] = {"paths_glob": paths_glob}

    if tool_configs.prettier:
        rules_data["rules"]["formatting"] = {
            "source": tool_configs.sources.get("prettier", ".prettierrc"),
            "rules": tool_configs.prettier,
        }
    if tool_configs.tsconfig and isinstance(tool_configs.tsconfig.get("compilerOptions"), dict):
        co = tool_configs.tsconfig["compilerOptions"]
        ts_rule: dict = {
            "source": tool_configs.sources.get("tsconfig", "tsconfig.json"),
            "strict": bool(co.get("strict")),
            "noImplicitAny": bool(co.get("noImplicitAny", True)),
            "strictNullChecks": bool(co.get("strictNullChecks", True)),
            "target": co.get("target"),
            "paths": co.get("paths"),
        }
        if tool_configs.tsconfig_extends_chain:
            ts_rule["extends_chain"] = tool_configs.tsconfig_extends_chain
        if "tsconfig" in tool_configs.parse_warnings:
            ts_rule["parse_warning"] = tool_configs.parse_warnings["tsconfig"]
        rules_data["rules"]["typescript"] = ts_rule
    if tool_configs.eslint:
        eslint_rule: dict = {
            "source": tool_configs.sources.get("eslint", ".eslintrc"),
            "rules": tool_configs.eslint,
        }
        if "eslint" in tool_configs.parse_warnings:
            eslint_rule["parse_warning"] = tool_configs.parse_warnings["eslint"]
        rules_data["rules"]["eslint"] = eslint_rule
    elif "eslint" in tool_configs.parse_warnings:
        rules_data["rules"]["eslint"] = {
            "source": tool_configs.sources.get("eslint", ""),
            "parse_warning": tool_configs.parse_warnings["eslint"],
        }
    if tool_configs.rubocop:
        rubocop_rule: dict = {
            "source": tool_configs.sources.get("rubocop", ".rubocop.yml"),
            "rules": tool_configs.rubocop,
        }
        if "rubocop" in tool_configs.parse_warnings:
            rubocop_rule["parse_warning"] = tool_configs.parse_warnings["rubocop"]
        rules_data["rules"]["rubocop"] = rubocop_rule
    elif "rubocop" in tool_configs.parse_warnings:
        rules_data["rules"]["rubocop"] = {
            "source": tool_configs.sources.get("rubocop", ""),
            "parse_warning": tool_configs.parse_warnings["rubocop"],
        }

    existing_idioms_path = profile_dir / "idioms.md"
    if existing_idioms_path.is_file():
        try:
            idioms_content = existing_idioms_path.read_text(encoding="utf-8")
        except OSError:
            idioms_content = _EMPTY_IDIOMS_TEMPLATE
    else:
        idioms_content = _EMPTY_IDIOMS_TEMPLATE

    with atomic_profile_commit(profile_dir) as txn_dir:
        (txn_dir / "profile.json").write_text(
            json.dumps(profile_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        (txn_dir / "archetypes.json").write_text(
            json.dumps(archetypes_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        (txn_dir / "canonicals.json").write_text(
            json.dumps(canonicals_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        (txn_dir / "conventions.json").write_text(
            serialize_conventions(conventions_data), encoding="utf-8"
        )
        try:
            from chameleon_mcp.principles import generate_principles

            (txn_dir / "principles.md").write_text(
                generate_principles(
                    language=extractor.language,
                    conventions=conventions_data,
                    archetypes=archetypes_data,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        (txn_dir / "rules.json").write_text(
            json.dumps(rules_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        (txn_dir / "idioms.md").write_text(idioms_content, encoding="utf-8")
        (txn_dir / "profile.summary.md").write_text(
            _build_summary_md(
                archetypes_data,
                canonicals_data,
                profile_data,
                idioms_content,
                rules_data,
            ),
            encoding="utf-8",
        )
        if rename_map:
            renames_payload = {
                "schema_version": 1,
                "renames": dict(rename_map),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (txn_dir / "renames.json").write_text(
                json.dumps(renames_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    try:
        from chameleon_mcp.drift.observations import record_bootstrap_baseline

        cluster_id_to_name: dict[str, str] = {}
        for arch_name, body in archetypes_data["archetypes"].items():
            cid = body.get("cluster_id")
            if isinstance(cid, str):
                cluster_id_to_name[cid] = arch_name

        baseline_rows: list[tuple[str, str | None, str | None]] = []
        for cluster in clustering.dense_clusters:
            cluster_id = next(
                (
                    cid
                    for cid, sel in selection.selections.items()
                    if sel.witness_path in {pf.path for pf in cluster.members}
                ),
                None,
            )
            arch_name = cluster_id_to_name.get(cluster_id) if cluster_id else None
            confidence = "high" if cluster.size >= 5 else "medium"
            for member in cluster.members:
                try:
                    rel = str(member.path.relative_to(repo_root))
                except ValueError:
                    rel = str(member.path)
                baseline_rows.append((rel, arch_name, confidence))
        for cluster in clustering.sparse_clusters:
            for member in cluster.members:
                try:
                    rel = str(member.path.relative_to(repo_root))
                except ValueError:
                    rel = str(member.path)
                baseline_rows.append((rel, None, "low"))
        record_bootstrap_baseline(repo_id, baseline_rows)
    except Exception:
        pass

    duration_ms = int((time.time() - started_at) * 1000)

    nested_warnings: list[str] = []
    for pat in (
        "apps/*/.chameleon",
        "packages/*/.chameleon",
        "services/*/.chameleon",
        "workspaces/*/.chameleon",
        "examples/*/.chameleon",
    ):
        for match in repo_root.glob(pat):
            try:
                rel = str(match.relative_to(repo_root))
            except ValueError:
                rel = str(match)
            nested_warnings.append(rel)

    return BootstrapReport(
        status="success",
        archetypes_detected=archetype_count,
        rules_extracted=len(rules_data["rules"]),
        idioms_collected=0,
        canonicals_skipped_failed_scans=canonicals_skipped_failed_scans,
        files_processed=len(parse_result.files),
        files_skipped_generated=files_skipped_generated,
        files_skipped_parse=files_skipped_parse,
        duration_ms=duration_ms,
        profile_path=profile_dir,
        sparse_cluster_warnings=sparse_warnings,
        bimodal_cluster_warnings=bimodal_warnings,
        nested_profile_warnings=nested_warnings,
        language_hint=language_hint,
        workspace_roots=list(workspace_roots),
        fanout_capped=fanout_capped,
        discovered_files_pre_exclusion=pre_exclusion_count,
        discovered_files_post_exclusion=post_exclusion_count,
        sparse_dropped_files=sparse_dropped_files,
    )


_EMPTY_IDIOMS_TEMPLATE = (
    "# idioms\n\n"
    "## active\n\n"
    "_(no idioms yet — run /chameleon-teach to capture team conventions)_\n\n"
    "## deprecated\n\n"
    "_(none)_\n"
)


def _build_summary_md(
    archetypes_data: dict,
    canonicals_data: dict,
    profile_data: dict,
    idioms_md: str,
    rules_data: dict | None = None,
) -> str:
    """Generate the human-readable profile.summary.md for PR review.

    Delegates to the shared renderer in ``chameleon_mcp.profile.summary``.
    The ``engine_version`` override ensures bootstrap uses the live
    ENGINE_MIN_VERSION constant rather than reading it back from profile.json
    (which hasn't been written yet at summary-generation time).
    """
    from chameleon_mcp.profile.summary import render_summary_md

    return render_summary_md(
        archetypes=archetypes_data,
        canonicals=canonicals_data,
        profile_meta=profile_data,
        idioms_text=idioms_md,
        rules_data=rules_data,
        engine_version=ENGINE_MIN_VERSION,
    )
