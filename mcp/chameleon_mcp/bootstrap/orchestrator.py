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
from chameleon_mcp.bootstrap.comment_scan import detect_commented_out_code_by_group
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
    compute_doc_coverage_from_content,
    extract_all_conventions,
    extract_declarations_from_content,
    serialize_conventions,
)
from chameleon_mcp.extractors._base import Extractor, ExtractorUnavailableError
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


def _parse_looks_degraded(files_parsed: int, files_skipped: int) -> bool:
    """True when a parse run is too damaged to commit a profile from.

    A dying extractor child (OOM-killed node, interrupted ruby) surfaces as a
    mass of skipped files, not an exception. Committing the residue would
    atomically replace a healthy profile with a near-empty one under a success
    status. Healthy repos parse at ~100%; the ratio gate leaves room for a
    handful of genuinely unparseable files while still catching truncated
    runs, and the floor spares tiny repos where one bad file dominates the
    ratio.
    """
    from chameleon_mcp._thresholds import threshold_float, threshold_int

    attempted = files_parsed + files_skipped
    if files_parsed == 0:
        return attempted > 0
    skip_floor = threshold_int("EXTRACTOR_DEGRADED_MIN_SKIPPED")
    skip_ratio = threshold_float("EXTRACTOR_DEGRADED_RATIO")
    return files_skipped >= skip_floor and files_skipped / attempted > skip_ratio


def resolve_extractor(repo_root: Path) -> Extractor | None:
    """Resolve the extractor for ``repo_root`` the way bootstrap does.

    ``_select_extractor`` only inspects the repo root, so a TS monorepo
    whose root ``package.json`` carries no TS deps (Turborepo / pnpm
    workspaces / Nx, with ``tsconfig.json`` living under ``apps/*``)
    yields None. Bootstrap recovers from that via
    ``_detect_workspace_ts_monorepo``; the cross-file read tools must
    apply the same fallback or they parse the edited file with no
    extractor and bail. Returns the TypeScript extractor when the
    workspace scan finds at least one TS workspace, else None.
    """
    extractor = _select_extractor(repo_root)
    if extractor is not None:
        return extractor
    workspace_roots, _fanout_capped = _detect_workspace_ts_monorepo(repo_root)
    if workspace_roots:
        return TypeScriptExtractor()
    return None


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
    idiom_warnings: list[str] = field(default_factory=list)
    """Carried-forward idioms.md looked damaged (unreadable, non-UTF8, or no
    parseable idiom blocks despite non-template content). Taught idioms are
    user-authored and unrecoverable by a refresh, so the damage is surfaced
    here instead of committing silently."""
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
    workspace_skipped_warnings: list[str] = field(default_factory=list)
    """Workspace package paths that could not be resolved and were skipped.

    A broken symlink anywhere in a workspace package's path makes
    Path.resolve() raise, which would otherwise abort the whole monorepo
    fan-out. Such packages are skipped and recorded here (relative to the
    repo root when possible) so the user knows a package was dropped and why.
    """
    workspace_glob_warnings: list[str] = field(default_factory=list)
    """Workspace globs that failed to expand or matched nothing usable.

    pnpm/turbo configs allow brace expansion ("packages/{ui,api}") and a
    typo'd or malformed glob would otherwise expand to zero packages with no
    diagnostic. Each entry names the offending glob and why it produced no
    package, so a misconfigured workspace pattern is visible instead of
    silently dropping packages.
    """
    workspace_potential_paths: list[str] = field(default_factory=list)
    """Repo-relative dirs that matched a workspace glob but had no package.json.

    These look like intended workspace packages but were excluded for lacking
    a manifest. Surfaced so the user can add the missing package.json rather
    than wonder why a directory was ignored.
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
    pnpm-workspaces, Nx). Empty for single-root TS repos. Persisted under
    profile.json's ``workspace.workspace_roots`` so the coordination metadata
    survives a reload (no schema bump; the key lives inside the existing
    ``workspace`` object).
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
            "idiom_warnings": list(self.idiom_warnings),
            "nested_profile_warnings": list(self.nested_profile_warnings),
            "workspaces": list(self.workspace_reports),
            "workspace_skipped_warnings": list(self.workspace_skipped_warnings),
            "workspace_glob_warnings": list(self.workspace_glob_warnings),
            "workspace_potential_paths": list(self.workspace_potential_paths),
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


def _witness_relpath(witness_path: Path, repo_root: Path) -> str:
    """Witness path relative to repo_root, always with forward-slash segments.

    Profile artifacts store every path with forward slashes so the downstream
    overlap/dedup comparisons (which split on "/") stay correct no matter which
    OS authored the profile. On Windows str(Path) yields backslashes, so the
    raw separator must be folded here, at the storage source. A witness outside
    repo_root falls back to its absolute path, normalized the same way.
    """
    try:
        rel = witness_path.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return str(witness_path).replace("\\", "/")


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
"""Cap on the per-bootstrap sparse_cluster_warnings list.

An uncapped bootstrap returned 2000-6000 warning entries on mid-sized repos and
exceeded the MCP protocol's response size, breaking chameleon-init. The cap is
applied after the same-paths_pattern aggregation step below."""


def _build_sparse_warnings(sparse_clusters, repo_root: Path) -> list[dict]:
    """Build the sparse-cluster warning payload for BootstrapReport.

    Phase 2C.3: surface clusters with <threshold members. Bug 4
    makes the threshold adaptive based on corpus size, so each warning
    records the cluster's resolved threshold instead of the legacy
    module-level constant.

    Aggregate by ``paths_pattern`` first so 50
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
                    f"{total_groups - _SPARSE_WARNING_LIMIT} "
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
    analysis_root: Path | None = None,
    derivation_source: dict | None = None,
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
        analysis_root: when set, the tree that is DISCOVERED/PARSED (a
            materialized production-ref worktree) while every write —
            profile dir, repo identity, drift baseline — stays bound to
            ``repo_root``. Persisted paths are repo-relative, so artifacts
            derived from the analysis tree apply 1:1 to the real checkout.
        derivation_source: provenance dict stamped into profile.json
            (branch/ref/sha) when derivation is pinned to a ref.

    Returns:
        BootstrapReport summarizing the run. `workspace_reports` lists the
        per-workspace outcomes when applicable.
    """
    # The scan root must be symlink-resolved: the extractors emit resolved
    # file paths, and every downstream relative_to() against this root has
    # to agree on the prefix or persisted paths degrade to absolute.
    # repo_root arrives resolved per the contract above; an analysis_root
    # is re-resolved here so no caller can hand the pipeline a symlinked
    # scan root.
    if analysis_root is not None:
        try:
            analysis_root = analysis_root.resolve()
        except (OSError, RuntimeError):
            pass
        scan_root = analysis_root
    else:
        scan_root = repo_root
    report = _bootstrap_single(
        scan_root,
        paths_glob=paths_glob,
        profile_dir_name=profile_dir_name,
        now=now,
        write_root=repo_root if analysis_root is not None else None,
        derivation_source=derivation_source,
    )

    workspace = detect_workspace(scan_root)
    # Surface glob-expansion diagnostics even when no packages resolved: a
    # brace-typo or a manifest-less directory is exactly the case where
    # has_workspaces is False, and dropping it silently is the bug.
    if workspace.glob_warnings:
        report.workspace_glob_warnings = list(workspace.glob_warnings)
    if workspace.potential_workspace_paths:
        report.workspace_potential_paths = list(workspace.potential_workspace_paths)
    if not workspace.has_workspaces:
        return report

    # NB: do NOT early-return on a failed root here. A pnpm/yarn/lerna root
    # whose packages live under a non-standard dir (modules/*, clients/*, ...)
    # fails its own language detection, but detect_workspace still located the
    # bootstrappable workspaces — fan out to them anyway (coordinator-only root).

    from chameleon_mcp.tools import _compute_repo_id as _id

    root_repo_id = _id(repo_root)

    workspace_reports: list[dict] = []
    workspace_skipped: list[str] = []
    # The fanout cap must bound THIS loop, not just the scripts-only fallback
    # scan: a manifest-driven workspace list (pnpm-workspace.yaml, package.json
    # "workspaces") arrives here uncapped, and under a pinned analysis tree
    # every fanout also pays a per-workspace indexing pass. Truncation is
    # recorded (flag + skipped labels), never silent.
    from chameleon_mcp import _thresholds as _th

    _fanout_cap = _th.threshold_int("WORKSPACE_FANOUT_CAP")
    capped_ws_paths = list(workspace.workspace_paths)
    if len(capped_ws_paths) > _fanout_cap:
        report.fanout_capped = True
        for dropped in capped_ws_paths[_fanout_cap:]:
            try:
                workspace_skipped.append(str(dropped.relative_to(scan_root)) + " (over fanout cap)")
            except ValueError:
                workspace_skipped.append(str(dropped) + " (over fanout cap)")
        capped_ws_paths = capped_ws_paths[:_fanout_cap]
    for ws_path in capped_ws_paths:
        # resolve() can raise on a workspace path that contains a broken/looping
        # symlink (OSError, or RuntimeError for a detected symlink loop). Skip the
        # offending package and record it rather than aborting the whole fan-out.
        try:
            ws_root = ws_path.resolve()
            if ws_root == scan_root.resolve():
                continue
        except (OSError, RuntimeError):
            try:
                skipped_label = str(ws_path.relative_to(scan_root))
            except ValueError:
                skipped_label = str(ws_path)
            workspace_skipped.append(skipped_label)
            continue
        try:
            parent_ws_path = ws_root.relative_to(scan_root.resolve()).as_posix()
        except ValueError:
            parent_ws_path = str(ws_path)
        # Under a pinned analysis tree, the workspace was detected inside the
        # materialized worktree; its profile must still land in the REAL
        # checkout's matching workspace dir. A workspace that exists at the
        # ref but not in the checkout has nowhere to write — skip it.
        ws_write_root: Path | None = None
        if analysis_root is not None:
            try:
                ws_rel = ws_root.relative_to(scan_root.resolve())
                candidate_write = (repo_root / ws_rel).resolve()
            except (ValueError, OSError, RuntimeError):
                workspace_skipped.append(parent_ws_path)
                continue
            if not candidate_write.is_dir():
                workspace_skipped.append(parent_ws_path)
                continue
            ws_write_root = candidate_write
        ws_report = _bootstrap_single(
            ws_root,
            paths_glob=paths_glob,
            profile_dir_name=profile_dir_name,
            now=now,
            parent_repo_id=root_repo_id,
            parent_workspace_path=parent_ws_path,
            write_root=ws_write_root,
            derivation_source=derivation_source,
        )

        ws_identity_root = ws_write_root if ws_write_root is not None else ws_root
        ws_entry = {
            "workspace_path": (str(ws_path) if ws_write_root is None else str(ws_identity_root)),
            "repo_root": str(ws_identity_root),
            "repo_id": _id(ws_identity_root),
            "profile_dir": (str(ws_report.profile_path) if ws_report.profile_path else None),
            "status": ws_report.status,
            "archetypes_detected": ws_report.archetypes_detected,
            "files_processed": ws_report.files_processed,
            "duration_ms": ws_report.duration_ms,
            "error": ws_report.error,
        }
        if ws_write_root is not None:
            # The tree the second indexing pass must hash (same tree the
            # bootstrap analyzed), distinct from the identity/write root.
            ws_entry["analysis_root"] = str(ws_root)
        workspace_reports.append(ws_entry)

    if workspace_skipped:
        report.workspace_skipped_warnings = workspace_skipped

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
            # claim init failed when profiles were actually created. Clear the
            # root's "no language signals" error so a success envelope doesn't
            # also carry a failure-sounding string.
            report.status = "success_workspaces_only"
            report.error = None

    return report


def _amend_root_profile_with_workspaces(profile_dir: Path, workspace_reports: list[dict]) -> None:
    """Re-write profile.json with a `workspaces` array describing each
    successfully bootstrapped sub-workspace.

    Wraps the rewrite in the same atomic_profile_commit transaction the
    initial bootstrap used so concurrent loaders never see a half-written
    profile. Every other protocol artifact the root bootstrap wrote (the
    JSON artifacts, idioms.md, summary, renames, calls index) is re-read
    from the existing committed profile and re-emitted verbatim inside the
    new txn so the generation counter stays consistent across files (the
    loader's double-fstat check requires it).
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

    # idioms.md is user-authored and re-emitted verbatim, so carry the raw
    # bytes: decoding here could only lose data (a non-UTF8 file must survive
    # the rewrite byte-identical, never be clobbered to empty).
    idioms_raw: bytes
    idioms_path = profile_dir / "idioms.md"
    try:
        idioms_raw = idioms_path.read_bytes() if idioms_path.is_file() else b""
    except OSError:
        idioms_raw = b""

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

    # calls_index.json is a protocol file (a failed full rebuild drops it
    # rather than serving stale judge facts), so the commit will not carry it
    # forward on its own. This rewrite only adds the workspaces array to a
    # profile the root bootstrap just derived, so re-emit the index verbatim
    # or every monorepo root would lose it moments after it was written.
    calls_index_path = profile_dir / "calls_index.json"
    calls_index_text: str | None = None
    if calls_index_path.is_file():
        try:
            calls_index_text = calls_index_path.read_text(encoding="utf-8")
        except OSError:
            calls_index_text = None

    with atomic_profile_commit(profile_dir) as txn_dir:
        (txn_dir / "profile.json").write_text(
            json.dumps(profile_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        for name, body in siblings.items():
            (txn_dir / name).write_text(body, encoding="utf-8")
        (txn_dir / "idioms.md").write_bytes(idioms_raw)
        (txn_dir / "profile.summary.md").write_text(summary_text, encoding="utf-8")
        if renames_text is not None:
            (txn_dir / "renames.json").write_text(renames_text, encoding="utf-8")
        if calls_index_text is not None:
            (txn_dir / "calls_index.json").write_text(calls_index_text, encoding="utf-8")


# Plain-word labels for the AST node kinds that lead the Tier 1 pointer —
# "imports, declarations" reads; "ImportDeclaration, FirstStatement" is
# parser jargon in the single most frequent injection a user sees.
_KIND_LABELS: dict[str, str] = {
    "ImportDeclaration": "imports",
    "ExportDeclaration": "exports",
    "ExportNamedDeclaration": "exports",
    "ExportAssignment": "default export",
    "FunctionDeclaration": "functions",
    "ClassDeclaration": "classes",
    "InterfaceDeclaration": "interfaces",
    "TypeAliasDeclaration": "type aliases",
    "EnumDeclaration": "enums",
    "VariableStatement": "declarations",
    "FirstStatement": "declarations",
    "CodeDeclaration": "declarations",
    "ExpressionStatement": "statements",
    "ClassNode": "classes",
    "ModuleNode": "modules",
    "DefNode": "methods",
    "CallNode": "method calls",
    "ConstantWriteNode": "constant assignments",
    "LocalVariableWriteNode": "assignments",
}


def _humanize_kind(kind: str) -> str:
    if kind.startswith("DslCall:"):
        return kind.split(":", 1)[1] + " calls"
    return _KIND_LABELS.get(kind, kind)


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
        labels = list(dict.fromkeys(_humanize_kind(k) for k in kinds[:5]))
        parts.append("typical shape: " + ", ".join(labels[:3]))

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
    parent_repo_id: str | None = None,
    parent_workspace_path: str | None = None,
    write_root: Path | None = None,
    derivation_source: dict | None = None,
) -> BootstrapReport:
    """The original single-target bootstrap pipeline.

    Extracted so the monorepo loop can call it once per workspace
    without duplicating the discovery → cluster → canonical → commit
    plumbing. Behavior on a non-monorepo repo is byte-identical to the
    An earlier implementation.

    ``parent_repo_id`` / ``parent_workspace_path`` are set only when this
    runs as a per-workspace fan-out under a monorepo root. They are
    persisted in ``profile.json.workspace.parent`` so a workspace profile
    back-references the catalog the root holds in ``profile.json.workspaces``;
    downstream tools can then walk either direction of the tree.

    ``write_root`` splits derivation from persistence: when set,
    ``repo_root`` is only the tree that gets discovered/parsed (a
    materialized production-ref worktree) while the profile dir, repo
    identity, prior-profile carry-forward (idioms.md, renames, taught
    competing imports), and the drift baseline all bind to ``write_root``
    — the real checkout. Stored paths are relative to the analysis tree,
    which maps 1:1 onto the checkout.
    """
    started_at = time.time()
    target_root = write_root if write_root is not None else repo_root
    profile_dir = target_root / profile_dir_name

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
                # The hint is persisted in profile.json, so its path must point
                # at the REAL checkout: under a pinned derivation js_dir lives
                # in the disposable analysis worktree and would dangle after
                # cleanup. Map it through the relative path; same below.
                try:
                    js_dir_persisted = target_root / js_dir.relative_to(repo_root)
                except ValueError:
                    js_dir_persisted = js_dir
                language_hint = {
                    "primary": "ruby",
                    "secondary_detected": "typescript",
                    "secondary_file_count": secondary_count,
                    "secondary_path": str(js_dir_persisted),
                    "note": (
                        "Ruby-with-frontend repo detected; JS sidecar in "
                        f"{js_dir_display}/ not scanned by this bootstrap. "
                        f"Run bootstrap_repo({js_dir_persisted}) for the JS half."
                    ),
                }
    elif extractor.language == "typescript" and (repo_root / "Gemfile").is_file():
        ruby_count = _count_ruby_files_under(repo_root)
        if ruby_count >= 50:
            language_hint = {
                "primary": "typescript",
                "secondary_detected": "ruby",
                "secondary_file_count": ruby_count,
                "secondary_path": str(target_root),
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
    except ExtractorUnavailableError as exc:
        # The language toolchain couldn't be provisioned (node/ts_dump.mjs for
        # TS, ruby/prism_dump.rb for Ruby). Degrade to a clean report instead
        # of aborting the whole bootstrap run.
        return BootstrapReport(
            status=(
                "failed_node_unavailable"
                if isinstance(exc, NodeUnavailableError)
                else "failed_ruby_unavailable"
            ),
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

    files_parsed = len(parse_result.files)
    attempted = files_parsed + files_skipped_parse
    if _parse_looks_degraded(files_parsed, files_skipped_parse):
        sample = "; ".join(f"{path.name}: {reason}" for path, reason in parse_result.skipped[:3])
        return BootstrapReport(
            status="failed_extractor_degraded",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=files_parsed,
            files_skipped_generated=0,
            files_skipped_parse=files_skipped_parse,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error=(
                f"{files_skipped_parse} of {attempted} files failed to parse "
                f"({sample}). The extractor subprocess likely died mid-run; "
                "the existing profile was left untouched. Re-run once the "
                "toolchain is healthy."
            ),
            workspace_roots=list(workspace_roots),
            fanout_capped=fanout_capped,
            discovered_files_pre_exclusion=pre_exclusion_count,
            discovered_files_post_exclusion=post_exclusion_count,
        )

    clustering = cluster_files(parse_result.files, repo_root=repo_root)
    files_skipped_generated = len(clustering.skipped_generated)
    sparse_dropped_files = sum(c.size for c in clustering.sparse_clusters)

    sparse_warnings = _build_sparse_warnings(clustering.sparse_clusters, repo_root)
    bimodal_warnings = _build_bimodal_warnings(clustering.bimodal_clusters, repo_root)

    selection = select_canonicals(clustering.dense_clusters, repo_root, now=now)
    canonicals_skipped_failed_scans = len(selection.clusters_with_only_failing_canonicals)

    generation = _generation_counter(now=started_at)
    repo_id = _compute_repo_id(target_root)

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
                witness_relpath = _witness_relpath(sel.witness_path, repo_root)
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
                        "path": witness_relpath or str(sel.witness_path).replace("\\", "/"),
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

    # Re-check each archetype's witness for commented-out code: the strippers
    # blanked comments before every other scan, so this is the one place the
    # comment text is parsed. One batched extractor call covers every witness.
    # Best-effort — any failure leaves the witnesses without the advisory.
    try:
        witness_content_by_arch: dict[str, list[str]] = {}
        for arch_name, entries in canonicals_data["canonicals"].items():
            if not entries:
                continue
            witness_rel = (entries[0].get("witness") or {}).get("path")
            if not witness_rel:
                continue
            wpath = repo_root / witness_rel
            try:
                wcontent = wpath.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            witness_content_by_arch[arch_name] = [wcontent]
        if witness_content_by_arch:
            cot_counts = detect_commented_out_code_by_group(
                witness_content_by_arch,
                language=extractor.language,
                extractor=extractor,
            )
            for arch_name, count in cot_counts.items():
                entries = canonicals_data["canonicals"].get(arch_name)
                if entries and count > 0:
                    idioms = entries[0].setdefault("normative_idioms", {})
                    idioms["commented_out_code_blocks"] = count
    except Exception:
        pass

    # One per-member re-read serves two derivations off the same file bytes:
    # the TS interface/type/enum declaration names (naming conventions) and the
    # per-file (documented, public) declaration counts (doc_coverage, both
    # languages). The dump output carries neither, so the bytes are re-read here
    # rather than threaded back through the subprocess.
    declarations_by_archetype: dict[str, dict[str, list[str]]] = {}
    doc_coverage_by_archetype: dict[str, list[tuple[int, int]]] = {}
    for arch_name, pf_list in files_by_archetype.items():
        merged: dict[str, list[str]] = {}
        coverage_pairs: list[tuple[int, int]] = []
        for pf in pf_list:
            try:
                # 1 MB (matches the extractor MAX_FILE_SIZE) so declarations
                # past 100KB in a member file are not dropped from naming.
                content = pf.path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            # Language-dispatching: TS yields interface/type/enum names (prefix
            # conventions), Ruby yields method/class/constant names (casing
            # conventions). Unknown languages yield nothing.
            decls = extract_declarations_from_content(content, language=extractor.language)
            for decl_type, names in decls.items():
                merged.setdefault(decl_type, []).extend(names)
            documented, public = compute_doc_coverage_from_content(
                content, language=extractor.language
            )
            if public > 0:
                coverage_pairs.append((documented, public))
        if merged:
            declarations_by_archetype[arch_name] = merged
        if coverage_pairs:
            doc_coverage_by_archetype[arch_name] = coverage_pairs

    conventions_data = extract_all_conventions(
        files_by_archetype=files_by_archetype,
        declarations_by_archetype=declarations_by_archetype,
        generation=generation,
        language=extractor.language,
        doc_coverage_by_archetype=doc_coverage_by_archetype,
        repo_root=repo_root,
    )

    # Mirror the per-archetype callable-signature consensus into each canonical's
    # normative_shape so a consumer reading the witness contract finds the exact
    # signatures alongside the AST query, without a second pass over conventions.
    try:
        sig_section = conventions_data.get("conventions", {}).get("callable_signatures", {})
        if isinstance(sig_section, dict):
            for arch_name, entries in canonicals_data["canonicals"].items():
                sig = sig_section.get(arch_name)
                if entries and isinstance(sig, dict) and sig.get("signatures"):
                    shape = entries[0].setdefault("normative_shape", {})
                    shape["callable_signatures"] = sig["signatures"]
    except Exception:
        pass

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
            # Repo-relative TS-monorepo sub-roots found when the root carries no
            # TS deps but workspaces live one level down. Persisted so this
            # coordination metadata survives a profile reload instead of living
            # only in the in-memory bootstrap envelope.
            "workspace_roots": list(workspace_roots),
        },
        "tool_configs": {
            "sources": tool_configs.sources,
            "warnings": {
                "prettier_js_plugins": tool_configs.has_prettier_js_plugins,
                "eslint_js_plugins": tool_configs.has_eslint_js_plugins,
            },
        },
    }
    if parent_repo_id is not None or parent_workspace_path is not None:
        # Back-reference to the monorepo root this workspace was fanned out
        # from. The root profile catalogs its children in
        # profile.json.workspaces; this lets a child point back at the root.
        profile_data["workspace"]["parent"] = {
            "repo_id": parent_repo_id,
            "workspace_path": parent_workspace_path,
        }
    if language_hint is not None:
        profile_data["language_hint"] = language_hint

    profile_data["clustering_algorithm_version"] = 2

    if paths_glob is not None:
        profile_data["discovery"] = {"paths_glob": paths_glob}

    if derivation_source is not None:
        # Provenance of a ref-pinned derivation: which branch/ref/commit the
        # analyzed tree came from. Refresh compares this SHA against the
        # current ref tip to decide noop vs re-derive; absent for plain
        # working-tree derivations. Optional key — older engines ignore it.
        profile_data["derivation_source"] = dict(derivation_source)

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
    if tool_configs.editorconfig:
        rules_data["rules"]["editorconfig"] = {
            "source": tool_configs.sources.get("editorconfig", ".editorconfig"),
            "rules": tool_configs.editorconfig,
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

    # idioms.md is user-authored: taught idioms cannot be regenerated, so a
    # damaged file must be carried forward byte-identical and flagged loudly,
    # never silently replaced with the empty template (that would destroy the
    # only on-disk copy) and never silently committed as if healthy (the user
    # would not learn their idioms stopped injecting until much later).
    existing_idioms_path = profile_dir / "idioms.md"
    idiom_warnings: list[str] = []
    idioms_collected = 0
    idioms_raw_bytes: bytes | None = None
    idioms_content = _EMPTY_IDIOMS_TEMPLATE
    if existing_idioms_path.is_file():
        try:
            raw_idioms = existing_idioms_path.read_bytes()
        except OSError:
            raw_idioms = None
            idiom_warnings.append(
                "idioms.md exists but could not be read; a fresh template was "
                "written. If idioms were taught here, restore the file from "
                "git history."
            )
        if raw_idioms is not None:
            try:
                idioms_content = raw_idioms.decode("utf-8")
            except UnicodeDecodeError:
                idioms_raw_bytes = raw_idioms
                idiom_warnings.append(
                    "idioms.md is not valid UTF-8; the file was carried forward "
                    "unchanged, but taught idioms cannot be read until it is "
                    "repaired (restore from git history)."
                )
    elif (profile_dir / "profile.json").is_file():
        # Re-deriving over an existing profile, but idioms.md is gone: it was
        # deleted (or lost to a torn write) since the last derivation. A fresh
        # template gets written below, dropping any idioms that lived only here.
        # Warn so the user can restore from git before the empty template is
        # committed -- without this, deletion silently empties the one
        # user-authored artifact while a corrupt file (above) correctly warns.
        idiom_warnings.append(
            "idioms.md was missing; a fresh template was written. If idioms "
            "were previously taught here, restore the file from git history "
            "before they are lost."
        )
    if idioms_raw_bytes is None:
        from chameleon_mcp.idiom_coverage import parse_idiom_blocks

        idiom_blocks = parse_idiom_blocks(idioms_content)
        idioms_collected = sum(1 for b in idiom_blocks if b.get("section") == "active")
        if (
            existing_idioms_path.is_file()
            and not idiom_blocks
            and idioms_content.strip()
            and idioms_content.strip() != _EMPTY_IDIOMS_TEMPLATE.strip()
        ):
            idiom_warnings.append(
                "idioms.md exists but contains no parseable idiom blocks; if "
                "idioms were previously taught here the file may be damaged - "
                "check git history."
            )

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
        # Symbol indexes back the phantom-symbol check and the cross-file edit
        # advisory. Only TypeScript/JS files carry the export/import extras the
        # builders read, so the indexes are TS-only; both are hashed into the
        # trust SHA, so they are written inside this same atomic transaction.
        # Best-effort: a build failure must not abort the whole profile commit.
        if extractor.language == "typescript":
            try:
                from chameleon_mcp.symbol_index import (
                    build_exports_index,
                    build_reverse_index,
                )

                (txn_dir / "exports_index.json").write_text(
                    json.dumps(
                        build_exports_index(parse_result.files, repo_root),
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                (txn_dir / "reverse_index.json").write_text(
                    json.dumps(
                        build_reverse_index(parse_result.files, repo_root),
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass
        # The function catalog backs the cross-file duplication prefilter. Both
        # TypeScript/JS and Ruby files carry the callable_signatures extras the
        # builder reads, so the catalog is built for every supported language. It
        # is hashed into the trust SHA, so it is written inside this same atomic
        # transaction. Best-effort: a build failure must not abort the commit.
        try:
            from chameleon_mcp.function_catalog import build_function_catalog

            (txn_dir / "function_catalog.json").write_text(
                json.dumps(
                    build_function_catalog(parse_result.files, repo_root),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        # The calls index backs the judge's cross-file caller facts. Both
        # languages carry call_sites extras; hashed into the trust SHA, so it
        # is written inside this same atomic transaction. Best-effort: a build
        # failure must not abort the commit.
        try:
            from chameleon_mcp.calls_index import build_calls_index

            (txn_dir / "calls_index.json").write_text(
                json.dumps(
                    build_calls_index(parse_result.files, repo_root, language=extractor.language),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        if idioms_raw_bytes is not None:
            (txn_dir / "idioms.md").write_bytes(idioms_raw_bytes)
        else:
            (txn_dir / "idioms.md").write_text(idioms_content, encoding="utf-8")
        (txn_dir / "profile.summary.md").write_text(
            _build_summary_md(
                archetypes_data,
                canonicals_data,
                profile_data,
                idioms_content if idioms_raw_bytes is None else _EMPTY_IDIOMS_TEMPLATE,
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
        idioms_collected=idioms_collected,
        canonicals_skipped_failed_scans=canonicals_skipped_failed_scans,
        files_processed=len(parse_result.files),
        files_skipped_generated=files_skipped_generated,
        files_skipped_parse=files_skipped_parse,
        duration_ms=duration_ms,
        profile_path=profile_dir,
        sparse_cluster_warnings=sparse_warnings,
        bimodal_cluster_warnings=bimodal_warnings,
        idiom_warnings=idiom_warnings,
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
