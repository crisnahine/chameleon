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

from chameleon_mcp.bootstrap.canonical import derive_ast_query, select_canonicals
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
from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import TypeScriptExtractor


def _is_rails_with_frontend(repo_root: Path) -> bool:
    """Detect a Rails-with-frontend hybrid (Bug 2 fix; v0.5.3 Bug E broaden).

    Rails+Stimulus / Rails+Hotwire / Rails+ImportMaps colocate a JS/TS
    sidecar alongside Ruby production code under a handful of well-known
    conventions:

      - ``app/javascript/`` — modern Rails 6+ webpacker / esbuild /
        importmap-rails entry point (forem, mastodon).
      - ``app/assets/javascripts/`` — legacy Rails 5 sprockets layout
        (gitlabhq, older Discourse). v0.5.3 (Bug E) added.
      - ``app/frontend/`` — Rails 7 convention used by some teams
        (Vite-rails default, jumpstart-pro). v0.5.3 (Bug E) added.

    The pre-v0.5.1 ``_select_extractor`` picked TypeScript first when both
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
    # v0.5.3 (Bug E): accept legacy / modern / new Rails JS layouts.
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
    cap = 50
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
            hints.append({
                "subdir": rel,
                "abs_path": str(child),
                "language": language,
            })
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


# v0.5.3 (Bug B): first-level workspace fanout cap. Bounded so a
# misconfigured tree with hundreds of empty apps/* dirs can't walk
# forever. 50 is generous: real Turborepo / pnpm / Nx repos almost never
# exceed ~30 first-level workspaces (excalidraw: 9; mastodon: 1; the
# largest real-world Turborepo we know of, Vercel internal, ships ~40).
_WORKSPACE_FANOUT_CAP = 50

# v0.5.3 (Bug B): conventional monorepo first-level directories. We drill
# one layer deep into each of these when the root carries package.json
# but no TS deps and no tsconfig.json.
_WORKSPACE_PARENT_DIRS = ("apps", "packages", "services", "workspaces")


def _detect_workspace_ts_monorepo(
    repo_root: Path,
) -> tuple[list[str], bool]:
    """Detect a TS monorepo whose root package.json has no TS deps.

    v0.5.3 (Bug B): the common Turborepo / pnpm-workspaces / Nx pattern
    leaves the root ``package.json`` carrying only ``scripts`` (no
    ``dependencies``/``devDependencies``) and puts ``tsconfig.json`` +
    TS deps inside workspace dirs under ``apps/*``, ``packages/*``,
    ``services/*``, ``workspaces/*``. The pre-v0.5.3 ``_select_extractor``
    saw no TS signal at the root and returned None → bootstrap reported
    ``failed_unsupported_language`` (bulletproof-react dogfood).

    A workspace dir qualifies when it contains either:
      - a ``tsconfig.json``, OR
      - a ``package.json`` whose content carries a TS-flavored token
        (``typescript``, ``ts-node``, ``vite``) — same signal
        ``TypeScriptExtractor.can_handle`` uses on a single repo.

    The first-level scan is bounded at ``_WORKSPACE_FANOUT_CAP`` (50)
    entries per parent dir so a pathological tree with hundreds of
    empty entries can't walk forever. When the cap fires, the orchestrator
    sets ``fanout_capped=True`` in the bootstrap envelope.

    Args:
        repo_root: absolute path to repo root

    Returns:
        Tuple of ``(workspace_roots, fanout_capped)``.
        - ``workspace_roots`` is a sorted list of repo-relative POSIX
          paths (e.g. ``["apps/api", "apps/web"]``) — empty if no
          qualifying workspaces found.
        - ``fanout_capped`` is True if any parent dir hit the
          ``_WORKSPACE_FANOUT_CAP`` ceiling.
    """
    package_json = repo_root / "package.json"
    if not package_json.is_file():
        return ([], False)
    # If the root itself has a tsconfig.json or TS deps in package.json,
    # the regular single-root path handles this — don't drill.
    if (repo_root / "tsconfig.json").exists():
        return ([], False)
    try:
        content = package_json.read_text(errors="replace")
    except OSError:
        return ([], False)
    if any(token in content for token in ("typescript", '"ts-node"', '"vite"')):
        return ([], False)

    # Walk each conventional parent dir one level deep.
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
        if len(entries) > _WORKSPACE_FANOUT_CAP:
            fanout_capped = True
            entries = entries[:_WORKSPACE_FANOUT_CAP]
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

PROFILE_SCHEMA_VERSION = 7

# BUG-007: read engine version from installed package metadata instead of
# hardcoding it. Pre-v0.5.6 the constant stayed at "0.4.0" through several
# releases and leaked into profile.json and profile.summary.md.
try:
    from importlib.metadata import PackageNotFoundError as _PkgNotFound
    from importlib.metadata import version as _pkg_version

    try:
        ENGINE_MIN_VERSION = _pkg_version("chameleon-mcp")
    except _PkgNotFound:  # pragma: no cover - editable install w/o metadata
        ENGINE_MIN_VERSION = "0.5.6"
except Exception:  # pragma: no cover - defensive fallback
    ENGINE_MIN_VERSION = "0.5.6"
# v0.5.2 schema-v7 bump rationale:
#   - paths_pattern strings now carry the file extension suffix
#     (e.g. "src/components:tsx") to fix the .tsx vs .ts collision.
#   - paths_pattern preserves the workspace name on monorepo paths
#     (e.g. "packages/excalidraw/components" instead of
#     "packages/components/Group").
# Old profiles still load because the loader doesn't gate on schema_version
# value; only `engine_min_version` is checked. Tools that EXPECT v6
# extension-blind buckets (notably tools.get_archetype) fall back to the
# extension-blind bucket explicitly so v0.5.x archetypes.json files keep
# matching post-upgrade.


@dataclass
class BootstrapReport:
    """Summary of a bootstrap run, returned to the MCP caller."""

    status: str  # "success" | "failed_too_many_files" | "failed_no_typescript" | "failed"
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
    workspace_reports: list[dict] = field(default_factory=list)
    """v0.4 (2D.3): per-workspace bootstrap summaries for monorepos.

    Each entry mirrors the root report shape: {"workspace_path": str,
    "repo_id": str, "profile_dir": str, "repo_root": str, "status": str,
    "archetypes_detected": int, "files_processed": int, "duration_ms": int,
    "error": str | None}. Empty list for non-monorepo repos.
    """
    language_hint: dict | None = None
    """v0.5.1 (Bug 2): hybrid-language detection envelope.

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
    """v0.5.3 (Bug B): repo-relative workspace dirs found when the root
    package.json has no TS deps but TS lives one level down (Turborepo,
    pnpm-workspaces, Nx). Empty for single-root TS repos. Envelope-only
    today (not persisted to profile.json so the schema doesn't bump).
    """
    fanout_capped: bool = False
    """v0.5.3 (Bug B): True when the first-level workspace scan hit the
    50-entry cap. Surfaced so an unusually large monorepo's report is
    distinguishable from a clean run.
    """
    discovered_files_pre_exclusion: int = 0
    """v0.5.3 (Bug D): total files walked by discovery, before
    EXCLUDE_FROM_CLUSTERING_DIRS / EXTENSIONS / EXACT_RELPATHS dropped
    anything. Lets coverage tooling reason about where files went.
    """
    discovered_files_post_exclusion: int = 0
    """v0.5.3 (Bug D): files that survived the discovery-layer exclusion
    sets and were handed to the extractor. Always <= pre.
    """
    sparse_dropped_files: int = 0
    """v0.5.3 (Bug D): files dropped because their cluster fell below
    the adaptive sparse_threshold. Sparse-cluster members never reach
    canonical selection but contribute to the post-clustering count.
    Always >= 0.
    """
    discovery_hints: list[dict] = field(default_factory=list)
    """BUG-001 (v0.5.6): when bootstrap fails with
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
        # BUG-011: archetypes_detected should be the SUM across workspaces,
        # not just the root's count. Pre-v0.5.6 a workspace bootstrap that
        # produced N archetypes per sub-workspace reported the root's local
        # count (often 0) and hid the per-workspace breakdown deep in the
        # ``workspaces`` array.
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
            "workspaces": list(self.workspace_reports),
        }
        # Always include language_hint in the envelope (None when no hybrid
        # detected) so downstream consumers can rely on a stable key.
        out["language_hint"] = self.language_hint
        # v0.5.3 (Bug B): always surface workspace_roots / fanout_capped so
        # callers can rely on stable keys.
        out["workspace_roots"] = list(self.workspace_roots)
        out["fanout_capped"] = bool(self.fanout_capped)
        # v0.5.3 (Bug D): instrumentation counters. clustered_files is an
        # alias for files_processed kept around for clarity.
        out["discovered_files_pre_exclusion"] = int(self.discovered_files_pre_exclusion)
        out["discovered_files_post_exclusion"] = int(self.discovered_files_post_exclusion)
        out["discovery_hints"] = list(self.discovery_hints)
        out["clustered_files"] = int(self.files_processed)
        out["sparse_dropped_files"] = int(self.sparse_dropped_files)
        return out


def _compute_repo_id(repo_root: Path) -> str:
    """Compute repo_id per ARCHITECTURE.md rule:
    sha256(canonicalize(git_remote_url)) if remote present, else
    sha256(canonicalize_path(repo_root)).

    v0.4 (4.6): delegates to `tools._compute_repo_id` so the orchestrator
    and the MCP tool layer can never disagree on the canonical id — a
    drift the v0.1–v0.3 code path tolerated only because the two
    implementations happened to be byte-identical.
    """
    from chameleon_mcp.tools import _compute_repo_id as _tools_compute_repo_id

    return _tools_compute_repo_id(repo_root)


# v0.5.1 (Bug 3): schema for the user-rename overlay file. Bumped if and
# only if the on-disk shape changes incompatibly. v1 layout:
#   { "schema_version": 1, "renames": {auto_name: user_name, ...},
#     "updated_at": "<ISO 8601 Z>" }
RENAMES_SCHEMA_VERSION = 1


def _load_user_renames(profile_dir: Path) -> dict[str, str]:
    """Return the {auto_name: user_name} overlay from `.chameleon/renames.json`.

    Returns an empty dict when the file is absent, malformed, or carries
    a future schema_version this build cannot interpret. The bootstrap
    pipeline applies the returned mapping AFTER the heuristic naming
    pass, so unknown auto-names (e.g., the heuristic produced something
    different from what was originally renamed) are simply skipped —
    they remain in renames.json untouched for the next pass.
    """
    path = profile_dir / "renames.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv > RENAMES_SCHEMA_VERSION:
        return {}
    renames = data.get("renames", {})
    if not isinstance(renames, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in renames.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            out[k] = v
    return out


def _generation_counter(now: float | None = None) -> int:
    """Profile generation counter. Round 4 distributed-systems addition.

    All four committed JSON files embed the same generation counter; loaders
    verify consistency via the double-fstat pattern.
    """
    return int(now if now is not None else time.time())


# v0.5.2 (Bug 3): Rails top-level dirs whose second segment is load-bearing
# and must NOT be dropped from the displayed paths_pattern. The signature
# v5 bucket formula (``parts[0]/parts[-3]/parts[-2]`` for ≥4 segments)
# collapses ``app/models/rule/action_executor/auto_categorize.rb`` into
# the bucket ``app/rule/action_executor`` — silently losing the
# load-bearing ``models/`` segment.
#
# Touching the bucket key itself is out of scope here (see signatures.py;
# the clustering agent owns that). Instead we re-derive the paths_pattern
# WRITTEN INTO archetypes.json from the canonical witness path so what
# the team reviewer sees on a profile-change PR matches the actual file.
_RAILS_LOAD_BEARING_SECOND_SEGS = frozenset({
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
})


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
        # Bucket formula only collapses on ≥4-segment paths; shorter
        # witness paths agree with the bucket by construction.
        return bucket
    if witness_parts[0] != "app":
        return bucket
    if witness_parts[1] not in _RAILS_LOAD_BEARING_SECOND_SEGS:
        return bucket
    # Already-honest bucket: the bucket *contains* the load-bearing
    # segment. Keep it; rewriting would be a no-op or worse.
    if witness_parts[1] in bucket.split("/"):
        return bucket
    # Construct the corrected bucket: app/<load-bearing>/<dir-of-witness>.
    # witness_parts[-2] is the directory immediately containing the file.
    return f"{witness_parts[0]}/{witness_parts[1]}/{witness_parts[-2]}"


# Phase 2C.3: how many sample paths to include per warning. Just enough for
# the future interview UI to give the user a hint without dumping the full
# cluster membership.
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
"""BUG-008/009 (v0.5.6): cap on the per-bootstrap sparse_cluster_warnings
list. Pre-v0.5.6 bootstrap returned 2000-6000 warning entries on
mid-sized repos and exceeded the MCP protocol's response size, breaking
chameleon-init. The cap is applied after the same-paths_pattern
aggregation step below."""


def _build_sparse_warnings(sparse_clusters, repo_root: Path) -> list[dict]:
    """Build the sparse-cluster warning payload for BootstrapReport.

    Phase 2C.3: surface clusters with <threshold members. v0.5.2 (Bug 4)
    makes the threshold adaptive based on corpus size, so each warning
    records the cluster's resolved threshold instead of the legacy
    module-level constant.

    BUG-008/009 (v0.5.6): aggregate by ``paths_pattern`` first so 50
    singletons at ``src/x/y:ts`` collapse to one row with
    ``cluster_count: 50, total_members: 50``. After aggregation, cap at
    ``_SPARSE_WARNING_LIMIT`` and surface ``truncated`` + ``total_groups``
    so consumers know the cap fired.

    Each warning entry includes the path bucket, size, and a handful of
    sample paths so the future interview UI can ask "merge with X?".
    """
    # First pass: aggregate by paths_pattern.
    grouped: dict[str, dict] = {}
    insertion_order: list[str] = []
    for cluster in sparse_clusters:
        bucket = cluster.key.path_pattern_bucket or "(unknown)"
        sample_paths = [
            _rel_or_abs(m.path, repo_root)
            for m in cluster.members[:_WARNING_SAMPLE_PATHS]
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
            # Keep up to _WARNING_SAMPLE_PATHS paths total across the group.
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
            g["reason"] = (
                f"cluster has {g['total_members']} members "
                f"(threshold {threshold_str})"
            )
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

    # Second pass: enforce the cap. The full count is surfaced separately
    # so consumers know how many were elided.
    total_groups = len(warnings)
    truncated = total_groups > _SPARSE_WARNING_LIMIT
    if truncated:
        warnings = warnings[:_SPARSE_WARNING_LIMIT]
        warnings.append({
            "kind": "sparse_cluster_truncated",
            "truncated": True,
            "total_groups": total_groups,
            "shown": _SPARSE_WARNING_LIMIT,
            "note": (
                f"BUG-008/009: {total_groups - _SPARSE_WARNING_LIMIT} "
                "additional sparse-cluster groups omitted to keep the "
                "bootstrap response within MCP transport limits."
            ),
        })
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
                _stringify_distribution_key(value): count
                for value, count in raw.items()
            }
        sample_paths = [
            _rel_or_abs(m.path, repo_root)
            for m in cluster.members[:_WARNING_SAMPLE_PATHS]
        ]
        warnings.append({
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
        })
    return warnings


def bootstrap_repo(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
) -> BootstrapReport:
    """Run the full bootstrap pipeline on a repo.

    Phase 2B emits a non-interactive profile. Phase 2D wraps this with the
    interactive interview flow.

    v0.4 (2D.3): when `detect_workspace` returns one or more workspace_paths
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
    )

    if report.status != "success":
        return report

    # v0.4 2D.3: per-workspace bootstrap. We re-detect the workspace from
    # the freshly committed root profile rather than re-walking the
    # filesystem so the catalog and the per-workspace runs see consistent
    # state. The workspace paths are an architecture-defined input to the
    # bootstrap pipeline, so cycling them here is safe even when the root
    # discovery glob didn't include them.
    workspace = detect_workspace(repo_root)
    if not workspace.has_workspaces:
        return report

    workspace_reports: list[dict] = []
    for ws_path in workspace.workspace_paths:
        ws_root = ws_path.resolve()
        # Skip a workspace that happens to ALIAS the repo root (defensive —
        # `apps/.` style paths would otherwise re-bootstrap the root and
        # clobber the just-written profile).
        try:
            if ws_root == repo_root.resolve():
                continue
        except OSError:
            continue
        # Per-workspace bootstrap. Use a workspace-local glob so the
        # extractor only walks files inside the workspace; pass paths_glob
        # through so the user-supplied scope override still applies if set.
        ws_report = _bootstrap_single(
            ws_root,
            paths_glob=paths_glob,
            profile_dir_name=profile_dir_name,
        )
        from chameleon_mcp.tools import _compute_repo_id as _id

        workspace_reports.append({
            "workspace_path": str(ws_path),
            "repo_root": str(ws_root),
            "repo_id": _id(ws_root),
            "profile_dir": (
                str(ws_report.profile_path) if ws_report.profile_path else None
            ),
            "status": ws_report.status,
            "archetypes_detected": ws_report.archetypes_detected,
            "files_processed": ws_report.files_processed,
            "duration_ms": ws_report.duration_ms,
            "error": ws_report.error,
        })

    # Mutate the report to attach the per-workspace summaries AND amend
    # profile.json to advertise the workspaces. The amendment goes through
    # a second atomic_profile_commit so the loader's double-fstat check
    # never sees a half-written profile.
    if workspace_reports:
        report.workspace_reports = workspace_reports
        _amend_root_profile_with_workspaces(
            repo_root / profile_dir_name, workspace_reports
        )

    return report


def _amend_root_profile_with_workspaces(
    profile_dir: Path, workspace_reports: list[dict]
) -> None:
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

    # Read sibling artifacts so we can re-emit them inside the new txn
    # with the SAME generation counter (the loader verifies all four match).
    artifact_names = ("archetypes.json", "canonicals.json", "rules.json")
    siblings: dict[str, str] = {}
    for name in artifact_names:
        path = profile_dir / name
        try:
            siblings[name] = path.read_text(encoding="utf-8")
        except OSError:
            return  # Corrupt profile — leave alone.

    idioms_text: str
    idioms_path = profile_dir / "idioms.md"
    try:
        idioms_text = (
            idioms_path.read_text(encoding="utf-8") if idioms_path.is_file() else ""
        )
    except OSError:
        idioms_text = ""

    summary_path = profile_dir / "profile.summary.md"
    try:
        summary_text = (
            summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
        )
    except OSError:
        summary_text = ""

    # v0.5.1 (Bug 3): re-emit renames.json inside the workspace-amendment
    # txn so the user's rename overlay survives the dir replacement.
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


def _bootstrap_single(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
) -> BootstrapReport:
    """The original (v0.3) single-target bootstrap pipeline.

    Extracted so the v0.4 monorepo loop can call it once per workspace
    without duplicating the discovery → cluster → canonical → commit
    plumbing. Behavior on a non-monorepo repo is byte-identical to the
    pre-v0.4 implementation.
    """
    started_at = time.time()
    profile_dir = repo_root / profile_dir_name

    # 1a. Detect workspace structure (pnpm/yarn/lerna/turbo/nx)
    workspace = detect_workspace(repo_root)
    # Phase 2C: workspace info is recorded in profile.json for visibility,
    # but Phase 2D will use it to drive per-workspace bootstrapping. For now,
    # we always bootstrap at repo_root.

    # 1b. Read tool configs as ground truth for rules.
    # BUG-019 (v0.5.6): when bootstrap_repo is invoked on a sidecar like
    # ``<rails-root>/app/javascript`` that carries no own package.json /
    # tsconfig.json but has a parent that does, read tool configs from
    # the parent. The language_hint envelope (Bug 2) suggested running
    # ``bootstrap_repo(<repo>/app/javascript)`` for the TS half, but that
    # call returned ``rules: {}`` because the sidecar dir lacked its own
    # tooling configuration.
    inherited_signals_from: Path | None = None
    # BUG-014 (v0.5.7): the sidecar walk-up fires only when the repo has
    # NEITHER its own JS signals (package.json / tsconfig.json) NOR its
    # own Ruby signals (Gemfile / *.gemspec). Pre-fix the check looked
    # only at JS signals, which fired on pure-Ruby repos and walked up
    # past their Gemfile into HOME, then ran read_tool_configs on
    # /Users/<name> with no rubocop config to find. ef-api's rubocop
    # silently disappeared.
    own_js = (repo_root / "package.json").is_file() or (repo_root / "tsconfig.json").is_file()
    own_ruby = (repo_root / "Gemfile").is_file() or any(repo_root.glob("*.gemspec"))
    if not own_js and not own_ruby:
        ancestor = repo_root.parent
        for _ in range(4):  # walk up at most 4 dirs to find the parent root
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

    # 1c. Detect language (TS or Ruby in v1.5; ADR-0003)
    # v0.5.3 (Bug B): when the root doesn't carry TS signals directly,
    # try first-level workspace drill-down (Turborepo / pnpm-workspaces
    # / Nx pattern). If a qualifying workspace is found, treat the repo
    # as TypeScript and scan only inside the workspace dirs.
    # BUG-019: a sidecar with inherited signals from a parent root that
    # has package.json should still try TS detection — _select_extractor
    # at the sidecar would otherwise return None.
    extractor = _select_extractor(repo_root)
    if extractor is None and inherited_signals_from is not None:
        # BUG-019 (v0.5.7): sidecar bootstraps should pick the extractor by
        # what's IN the sidecar, not by what the parent's primary language
        # was. forem is a Rails-with-frontend repo; _select_extractor at
        # the forem root returns Ruby (correctly), but the user asked us
        # to bootstrap forem/app/javascript — the JS half. Inheriting
        # Ruby extractor → glob "**/*.rb" → zero files in the sidecar.
        #
        # Heuristic: count source files of each language inside the
        # sidecar (depth-limited to avoid scanning node_modules etc.) and
        # pick the dominant one. Uses the module-level imports.
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
            # Truly empty — fall back to parent for the error trail.
            extractor = _select_extractor(inherited_signals_from)
    workspace_roots: list[str] = []
    fanout_capped = False
    if extractor is None:
        workspace_roots, fanout_capped = _detect_workspace_ts_monorepo(repo_root)
        if workspace_roots:
            extractor = TypeScriptExtractor()
            # BUG-003 (v0.5.7): for ad-hoc monorepos that have per-workspace
            # tool configs (apps/<x>/.eslintrc.cjs, apps/<x>/.prettierrc, ...)
            # but no root-level ones, fall back to the first workspace's
            # config. Otherwise rules.json is empty even though every
            # workspace has perfectly readable lint rules. Single-config
            # representative isn't perfect (each workspace may differ
            # slightly) but is strictly better than reporting zero rules.
            if (
                not tool_configs.eslint
                and not tool_configs.prettier
                and not tool_configs.tsconfig
            ):
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
                        # Adopt the workspace's configs as repo-wide. Tag
                        # the source so the user knows it came from a
                        # sub-workspace.
                        tool_configs = ws_configs
                        tool_configs.sources = {
                            k: f"{ws_rel}/{v}" for k, v in ws_configs.sources.items()
                        }
                        break
    if extractor is None:
        # BUG-001: surface discoverable sub-projects so the slash-command
        # UI can prompt the user. We walk apps/* and packages/* one level
        # deep (cheap), looking for children that have their own
        # package.json or Gemfile.
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

    # v0.5.1 (Bug 2): Rails-with-frontend repos pick Ruby; emit a
    # language_hint so the caller knows the JS sidecar (modern
    # app/javascript, legacy app/assets/javascripts, or Rails 7
    # app/frontend) was deliberately excluded from the Ruby scan.
    # v0.5.3 (Bug E): the dir is resolved via _rails_frontend_dir so the
    # hint points at whichever convention the repo actually uses.
    # BUG-017 (v0.5.6): when TS wins but the repo also carries a Gemfile
    # plus a meaningful Ruby sidecar, emit the reciprocal hint so the
    # user sees "we picked TS, but there's a Ruby half here too".
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
        # BUG-017: TS won, but the Gemfile suggests this is a Rails repo
        # whose Rails-with-frontend signal didn't fire (legacy layout,
        # non-standard JS dir, no app/javascript or app/assets/javascripts
        # or app/frontend marker). The reciprocal hint warns the user that
        # the Ruby half exists and was deliberately not scanned.
        ruby_count = _count_ruby_files_under(repo_root)
        if ruby_count >= 50:  # threshold: substantive Ruby presence
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

    # 2. Discover candidate files (use language-appropriate glob if no override).
    # v0.5.3 (Bug B): when workspace_roots is non-empty, the discovery walker
    # scans only inside those dirs (apps/web, packages/foo, …) instead of
    # the whole repo. This keeps a monorepo's empty root + sibling
    # config dirs from blowing past the size guard.
    # v0.5.3 (Bug D): compute pre/post counters off the same walker so the
    # numbers always agree.
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
        # BUG-012: "no source files" was emitting status="failed" while the
        # "no language signals" branch above emitted "failed_unsupported_language".
        # Both are semantically the same case (nothing for chameleon to do).
        # Unify on failed_unsupported_language with the original detail
        # appended so callers don't need to track two distinct statuses.
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
            error="No source files found matching the discovery glob",
            workspace_roots=list(workspace_roots),
            fanout_capped=fanout_capped,
            discovered_files_pre_exclusion=pre_exclusion_count,
            discovered_files_post_exclusion=post_exclusion_count,
        )

    # 3. Parse via ts_dump.mjs subprocess
    # Pass the discovered file list so bootstrap/discovery.py exclusions are
    # honored (don't re-glob inside the extractor).
    parse_result = extractor.parse_repo(repo_root, paths=candidates)
    files_skipped_parse = len(parse_result.skipped)

    # 4. Cluster by signature
    clustering = cluster_files(parse_result.files, repo_root=repo_root)
    files_skipped_generated = len(clustering.skipped_generated)
    # v0.5.3 (Bug D): count files that ended up in a sparse cluster —
    # they were parsed and clustered but never made it to archetype/canonical
    # selection. Useful for explaining gaps between files_processed and
    # archetypes_detected.
    sparse_dropped_files = sum(c.size for c in clustering.sparse_clusters)

    # 4b. Phase 2C.3: collect sparse + bimodal warnings. These are surfaced
    # in BootstrapReport so the future interview UI (v0.4) can prompt the
    # user; today they are pure diagnostics and do not block the bootstrap.
    sparse_warnings = _build_sparse_warnings(clustering.sparse_clusters, repo_root)
    bimodal_warnings = _build_bimodal_warnings(clustering.bimodal_clusters, repo_root)

    # 5. Pick canonicals (only from dense clusters; sparse get user
    # confirmation in Phase 2C/D interview)
    selection = select_canonicals(clustering.dense_clusters, repo_root)
    canonicals_skipped_failed_scans = len(selection.clusters_with_only_failing_canonicals)

    # 6. Build profile artifacts (Phase 2B: minimal viable shape)
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

    # v0.5.1 (Bug 3): load any user-curated renames so they survive
    # /chameleon-refresh's full-bootstrap fallthrough. The file lives at
    # `.chameleon/renames.json` and is meant to be committed to git so
    # the team shares it. The orchestrator applies the rename overlay
    # AFTER `propose_archetype_name` runs, so the auto-derived name still
    # determines collision detection — and the overlay simply re-keys the
    # archetypes_data / canonicals_data dicts before they're written.
    rename_map = _load_user_renames(profile_dir)

    # Build archetypes from dense clusters. Phase 2D.2 derives meaningful
    # names (controller, service, react-component, ...) from cluster
    # signals instead of the opaque ``cluster-<16hex>`` placeholder used
    # in Phase 2B. Iteration order is largest-cluster-first (see
    # ClusteringResult.dense_clusters), so the most common archetype gets
    # the unsuffixed base name and smaller clusters take the suffix.
    assigned_names: set[str] = set()
    # v0.5.1 (Bug 3): every user-mapped target is reserved up-front so
    # auto-naming never produces a candidate that collides with one. When
    # a collision would have occurred, the auto-derivation gets a numeric
    # suffix (handled inside `propose_archetype_name` via its existing
    # assigned_names set). This is the "user's mapping wins" rule.
    for target in rename_map.values():
        assigned_names.add(target)

    for cluster in clustering.dense_clusters:
        cluster_id = next(
            (cid for cid, sel in selection.selections.items()
             if sel.witness_path in {pf.path for pf in cluster.members}),
            None,
        )
        if not cluster_id:
            # No canonical selected (no eligible candidates passed scanners)
            continue
        auto_name = propose_archetype_name(
            cluster, assigned_names, workspace_roots=workspace_roots or None
        )
        # v0.5.1 (Bug 3): overlay the user's rename if one applies.
        effective_name = rename_map.get(auto_name, auto_name)
        assigned_names.add(auto_name)
        assigned_names.add(effective_name)
        # Canonical entry — selected up-front because v0.5.2 (Bug 3) uses
        # the witness path to surface a Rails-honest display string.
        sel = selection.selections[cluster_id]
        try:
            witness_relpath = str(sel.witness_path.relative_to(repo_root))
        except ValueError:
            # Witness somehow lives outside repo_root — defensive: skip
            # the paths_pattern repair and keep whatever the bucket says.
            witness_relpath = ""
        # v0.5.2 (Bug 3): the signature-v5 bucket formula drops the
        # ``models/`` segment for paths like
        # ``app/models/rule/action_executor/auto_categorize.rb`` →
        # ``app/rule/action_executor``. That bucket is still what runtime
        # archetype lookup keys on (path_pattern_bucket_for produces the
        # same string), so we keep ``paths_pattern`` byte-equal to the
        # bucket. We add ``paths_pattern_display`` carrying the Rails-honest
        # form (``app/models/action_executor`` here) so reviewers reading
        # archetypes.json / profile.summary.md aren't misled about where
        # the cluster actually lives. The display form falls back to the
        # bucket when the witness path doesn't trigger the repair.
        bucket = cluster.key.path_pattern_bucket
        display_pattern = _displayed_paths_pattern(bucket, witness_relpath)
        archetypes_data["archetypes"][effective_name] = {
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
        canonicals_data["canonicals"][effective_name] = [{
            "witness": {
                "path": witness_relpath or str(sel.witness_path),
                "sha_hint": sel.sha_hint,
            },
            "normative_shape": {
                # Phase 2C.1: derive the normative AST shape from the
                # cluster signature. A file conforms when every non-null
                # field matches the file's parsed shape. See
                # canonical.derive_ast_query for the field contract.
                "ast_query": derive_ast_query(cluster.key),
            },
            "normative_idioms": {
                "comments": [],  # Phase 2D: collect via interview / chameleon-teach
            },
            "secret_scan_passed": sel.secret_scan_passed,
            "injection_scan_passed": sel.injection_scan_passed,
            "poisoning_scan_passed": sel.poisoning_scan_passed,
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]

    archetype_count = len(archetypes_data["archetypes"])

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
    # v0.5.1 Bug 2: only emit language_hint when a sibling language was
    # actually detected. Including `null` in the envelope would mask
    # legitimately-single-language repos with the same shape as hybrids.
    if language_hint is not None:
        profile_data["language_hint"] = language_hint

    # Build initial rules from tool configs (Phase 2C — basic; Phase 4 expands)
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
        # Phase 4.7: surface the resolved extends chain so /chameleon-status
        # can show e.g. "tsconfig.json → @tsconfig/strictest → ./base.json".
        if tool_configs.tsconfig_extends_chain:
            ts_rule["extends_chain"] = tool_configs.tsconfig_extends_chain
        if "tsconfig" in tool_configs.parse_warnings:
            ts_rule["parse_warning"] = tool_configs.parse_warnings["tsconfig"]
        rules_data["rules"]["typescript"] = ts_rule
    # Phase 2C.4: surface ESLint rules whenever we have them (JSON, YAML, or
    # best-effort JS extraction). If parsing failed, record only the warning
    # so /chameleon-status can flag the gap.
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
    # BUG-014 (v0.5.6): surface rubocop config so Ruby files get linting
    # guidance equivalent to what TS files have via ESLint.
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

    # Preserve any user-curated idioms across a refresh: read the existing
    # idioms.md (if present) and re-emit it inside the transaction. Bootstrap
    # used to overwrite this file with an empty template every time, which
    # silently destroyed the human-curated layer the architecture is supposed
    # to protect.
    existing_idioms_path = profile_dir / "idioms.md"
    if existing_idioms_path.is_file():
        try:
            idioms_content = existing_idioms_path.read_text(encoding="utf-8")
        except OSError:
            idioms_content = _EMPTY_IDIOMS_TEMPLATE
    else:
        idioms_content = _EMPTY_IDIOMS_TEMPLATE

    # 7. Write atomically (Phase 1C transaction.py)
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
        # v0.5.1 (Bug 3): re-emit `renames.json` inside the txn so the
        # user's rename overlay file survives the directory replacement.
        # Without this, atomic_profile_commit's rename clobbers the file
        # even though the in-memory dicts were already renamed in-place.
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

    duration_ms = int((time.time() - started_at) * 1000)
    return BootstrapReport(
        status="success",
        archetypes_detected=archetype_count,
        rules_extracted=len(rules_data["rules"]),
        idioms_collected=0,  # Phase 2D: interview-driven via /chameleon-teach
        canonicals_skipped_failed_scans=canonicals_skipped_failed_scans,
        files_processed=len(parse_result.files),
        files_skipped_generated=files_skipped_generated,
        files_skipped_parse=files_skipped_parse,
        duration_ms=duration_ms,
        profile_path=profile_dir,
        sparse_cluster_warnings=sparse_warnings,
        bimodal_cluster_warnings=bimodal_warnings,
        language_hint=language_hint,
        # v0.5.3 (Bug B): workspace drill-down envelope fields. Empty for
        # single-root repos so the keys stay stable.
        workspace_roots=list(workspace_roots),
        fanout_capped=fanout_capped,
        # v0.5.3 (Bug D): instrumentation counters.
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


def _extract_active_idioms(idioms_md: str) -> str:
    """Return the contents of the `## active` section of an idioms.md doc.

    Used to inline idiom bodies in profile.summary.md so that the trust gate
    actually shows users what they're approving. Without this, a poisoned
    profile committed to a branch can ship prompt-injection-shaped idioms
    that the trust review never displays.
    """
    return _extract_idioms_section(idioms_md, "## active")


def _extract_idioms_section(idioms_md: str, marker: str) -> str:
    """Return the contents of the given level-2 section of an idioms.md doc.

    v0.5.4: factored out so ``_build_summary_md`` can render both the
    active and deprecated sections. Returns an empty string when the
    marker is absent OR when the section body is just the
    ``_(none)_`` / "no idioms yet" placeholder.
    """
    if marker not in idioms_md:
        return ""
    after = idioms_md.split(marker, 1)[1]
    section = after.split("\n## ", 1)[0] if "\n## " in after else after
    section = section.strip()
    # Treat placeholders as empty so profile.summary.md doesn't render
    # a "Deprecated idioms" heading above "_(none)_" (cycle-3 dogfood
    # observation).
    placeholder_markers = ("_(none)_", "no idioms yet")
    if not section or all(
        section == m or section.startswith(m) for m in placeholder_markers if section == m
    ):
        return section if section and "no idioms yet" not in section and section != "_(none)_" else ""
    if section in {"_(none)_"} or "no idioms yet" in section:
        return ""
    return section


def _count_terminal_rules(block: dict, depth: int = 0) -> int:
    """Return a rough count of terminal rule entries in a nested config block.

    Used by ``_build_summary_md`` to surface a "N rule(s) extracted" line
    for each tool config without rendering the full JSON tree (which can
    be hundreds of lines for an eslint config). Caps recursion at depth
    6 so a pathological config can't cause unbounded recursion.
    """
    if depth > 6 or not isinstance(block, dict):
        return 0
    count = 0
    for v in block.values():
        if isinstance(v, dict):
            count += _count_terminal_rules(v, depth + 1)
        elif isinstance(v, list):
            count += len(v)
        else:
            count += 1
    return count


def _build_summary_md(
    archetypes_data: dict,
    canonicals_data: dict,
    profile_data: dict,
    idioms_md: str,
    rules_data: dict | None = None,
) -> str:
    """Generate the human-readable profile.summary.md for PR review.

    Per Round 5 DX recommendation: profile.summary.md is what reviewers
    actually read on profile-change PRs.

    v0.5.4: the rules section used to render a "_Phase 2C: tool config
    rules + AST stats._" placeholder. Now summarizes the actual contents
    of rules.json (when ``rules_data`` is provided) or explains that no
    tool configs were detected.
    """
    lines = [
        "# chameleon profile summary",
        "",
        f"Generated: {profile_data['created_at']}",
        f"Engine: chameleon v{ENGINE_MIN_VERSION}",
        f"Language: {profile_data['language']}",
        f"Source: {profile_data['source']}",
        f"Generation: {profile_data['generation']}",
        f"Schema version: {profile_data['schema_version']}",
        "",
    ]

    # v0.5.1 Bug 2: prominent secondary-language warning for Rails+JS
    # hybrids (and any future hybrid). Reviewers must see this before
    # the archetype list because a wrong primary-language pick silently
    # excludes ~half the repo.
    hint = profile_data.get("language_hint")
    if hint:
        lines.extend([
            "## Secondary language detected",
            "",
            (
                f"This bootstrap scanned **{hint.get('primary')}** only. "
                f"A sibling **{hint.get('secondary_detected')}** codebase "
                f"({hint.get('secondary_file_count')} files at "
                f"`{hint.get('secondary_path')}`) was deliberately excluded."
            ),
            "",
            hint.get("note", ""),
            "",
        ])

    lines.extend([
        f"## {profile_data['archetype_count']} archetypes detected",
        "",
    ])
    for name, arch in sorted(archetypes_data["archetypes"].items()):
        canonicals = canonicals_data["canonicals"].get(name, [])
        canonical_path = canonicals[0]["witness"]["path"] if canonicals else "(none)"
        # v0.5.2 (Bug 3): prefer the witness-honest display string when
        # signature-v5's bucket collapsed a load-bearing Rails segment.
        # Falls back to the bucket for older profiles + non-Rails repos.
        display_paths = arch.get("paths_pattern_display") or arch["paths_pattern"]
        lines.append(
            f"- **{name}** (cluster_size {arch['cluster_size']}, "
            f"paths {display_paths}) — canonical: `{canonical_path}`"
        )
    lines.extend([
        "",
        "## Rules",
        "",
    ])
    # v0.5.4 — render the actual tool-config rules detected at bootstrap
    # instead of the v0.4-era "_Phase 2C: tool config rules + AST stats._"
    # placeholder, which read like an unfinished feature after Phase 2C
    # actually shipped in v0.5.0. When rules.json is empty we explain
    # WHY (no eslint/tsconfig/prettier/rubocop/editorconfig found) so
    # reviewers don't wonder if something broke.
    rules_block = (rules_data or {}).get("rules") if rules_data else None
    detected_tools = sorted(rules_block.keys()) if isinstance(rules_block, dict) else []
    if detected_tools:
        lines.append(
            f"_Auto-derived from {len(detected_tools)} tool config file(s): "
            f"{', '.join(f'`{t}`' for t in detected_tools)}._"
        )
        lines.append("")
        for tool in detected_tools:
            tool_block = rules_block[tool]
            if not isinstance(tool_block, dict):
                continue
            rule_count = _count_terminal_rules(tool_block)
            lines.append(f"- **{tool}** — {rule_count} rule(s) extracted")
    else:
        lines.append(
            "_No tool-config rules detected._ The bootstrap looked for "
            "`eslint`, `tsconfig`, `prettier`, `rubocop`, and `.editorconfig` "
            "and found none of them. Auto-derived rules will appear here "
            "once those configs exist."
        )
    lines.extend([
        "",
        "## Idioms",
        "",
    ])

    active_idioms = _extract_active_idioms(idioms_md)
    if active_idioms:
        lines.append(
            "_The following idioms ship in this profile and will be injected "
            "into the model's context before each Edit/Write. Review carefully "
            "before granting trust._"
        )
        lines.append("")
        lines.append(active_idioms)
        lines.append("")
    else:
        lines.append(
            "_No idioms captured yet. Run /chameleon-teach to record team "
            "conventions._"
        )
        lines.append("")

    # v0.5.4 — render the deprecated section only when it carries content.
    # Pre-v0.5.4 the section always rendered with `_(none)_` for clean
    # profiles, which read like an unfinished feature.
    deprecated_idioms = _extract_idioms_section(idioms_md, "## deprecated")
    if deprecated_idioms:
        lines.append("## Deprecated idioms")
        lines.append("")
        lines.append(
            "_The following idioms were retired by `/chameleon-teach`. They "
            "are kept here for audit history and are NOT injected into "
            "context._"
        )
        lines.append("")
        lines.append(deprecated_idioms)
        lines.append("")

    return "\n".join(lines)
