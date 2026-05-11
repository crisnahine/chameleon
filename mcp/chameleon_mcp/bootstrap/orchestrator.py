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

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.bootstrap.canonical import derive_ast_query, select_canonicals
from chameleon_mcp.bootstrap.clustering import (
    BIMODAL_DOMINANT_SHARE_THRESHOLD,
    SPARSE_CLUSTER_THRESHOLD,
    cluster_files,
)
from chameleon_mcp.bootstrap.discovery import (
    REPO_SIZE_GUARD,
    TooManyFilesError,
    discover_files,
)
from chameleon_mcp.bootstrap.naming import propose_archetype_name
from chameleon_mcp.bootstrap.tool_config import read_tool_configs
from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
from chameleon_mcp.bootstrap.workspace import detect_workspace
from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import TypeScriptExtractor


def _select_extractor(repo_root: Path) -> Extractor | None:
    """Pick the extractor whose can_handle() returns True for this repo.

    Precedence: TypeScript > Ruby. A repo that has both Gemfile and
    tsconfig.json (e.g., a Rails app with a Stimulus/Vite frontend in the
    same repo) bootstraps with the TS extractor. For monorepos with truly
    separate language subtrees, run /chameleon-init per subtree.
    """
    for ext_cls in (TypeScriptExtractor, RubyExtractor):
        ext = ext_cls()
        if ext.can_handle(repo_root):
            return ext
    return None


def _glob_for_extractor(extractor: Extractor) -> str:
    if extractor.language == "ruby":
        return "**/*.rb"
    return "**/*.{ts,tsx,js,jsx,mjs,cjs}"

PROFILE_SCHEMA_VERSION = 5
ENGINE_MIN_VERSION = "0.2.0"


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

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "archetypes_detected": self.archetypes_detected,
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
        }


def _compute_repo_id(repo_root: Path) -> str:
    """Compute repo_id per ARCHITECTURE.md rule:
    sha256(canonicalize(git_remote_url)) if remote present, else
    sha256(canonicalize_path(repo_root)).

    Phase 2B simplified: always uses canonical absolute path. Phase 2C
    integrates git remote URL detection.
    """
    canonical = str(repo_root.resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _generation_counter(now: float | None = None) -> int:
    """Profile generation counter. Round 4 distributed-systems addition.

    All four committed JSON files embed the same generation counter; loaders
    verify consistency via the double-fstat pattern.
    """
    return int(now if now is not None else time.time())


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


def _build_sparse_warnings(sparse_clusters, repo_root: Path) -> list[dict]:
    """Build the sparse-cluster warning payload for BootstrapReport.

    Phase 2C.3: surface clusters with <SPARSE_CLUSTER_THRESHOLD members.
    Each warning entry includes the path bucket, size, and a handful of
    sample paths so the future interview UI can ask "merge with X?".
    """
    warnings: list[dict] = []
    for cluster in sparse_clusters:
        sample_paths = [
            _rel_or_abs(m.path, repo_root)
            for m in cluster.members[:_WARNING_SAMPLE_PATHS]
        ]
        warnings.append({
            "kind": "sparse_cluster",
            "reason": (
                f"cluster has {cluster.size} members "
                f"(threshold {SPARSE_CLUSTER_THRESHOLD})"
            ),
            "paths_pattern": cluster.key.path_pattern_bucket,
            "size": cluster.size,
            "sample_paths": sample_paths,
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

    Args:
        repo_root: absolute path to repo root (resolved before passing in)
        paths_glob: optional user-supplied scope override
        profile_dir_name: name of the committed profile dir (default ".chameleon")

    Returns:
        BootstrapReport summarizing the run.
    """
    started_at = time.time()
    profile_dir = repo_root / profile_dir_name

    # 1a. Detect workspace structure (pnpm/yarn/lerna/turbo/nx)
    workspace = detect_workspace(repo_root)
    # Phase 2C: workspace info is recorded in profile.json for visibility,
    # but Phase 2D will use it to drive per-workspace bootstrapping. For now,
    # we always bootstrap at repo_root.

    # 1b. Read tool configs as ground truth for rules
    tool_configs = read_tool_configs(repo_root)

    # 1c. Detect language (TS or Ruby in v1.5; ADR-0003)
    extractor = _select_extractor(repo_root)
    if extractor is None:
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
        )

    # 2. Discover candidate files (use language-appropriate glob if no override)
    discovery_glob = paths_glob or _glob_for_extractor(extractor)
    try:
        candidates = discover_files(repo_root, glob=discovery_glob, paths_glob=paths_glob)
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
        )

    if not candidates:
        return BootstrapReport(
            status="failed",
            archetypes_detected=0,
            rules_extracted=0,
            idioms_collected=0,
            canonicals_skipped_failed_scans=0,
            files_processed=0,
            files_skipped_generated=0,
            files_skipped_parse=0,
            duration_ms=int((time.time() - started_at) * 1000),
            profile_path=None,
            error="No TypeScript files found matching the discovery glob",
        )

    # 3. Parse via ts_dump.mjs subprocess
    # Pass the discovered file list so bootstrap/discovery.py exclusions are
    # honored (don't re-glob inside the extractor).
    parse_result = extractor.parse_repo(repo_root, paths=candidates)
    files_skipped_parse = len(parse_result.skipped)

    # 4. Cluster by signature
    clustering = cluster_files(parse_result.files, repo_root=repo_root)
    files_skipped_generated = len(clustering.skipped_generated)

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

    # Build archetypes from dense clusters. Phase 2D.2 derives meaningful
    # names (controller, service, react-component, ...) from cluster
    # signals instead of the opaque ``cluster-<16hex>`` placeholder used
    # in Phase 2B. Iteration order is largest-cluster-first (see
    # ClusteringResult.dense_clusters), so the most common archetype gets
    # the unsuffixed base name and smaller clusters take the suffix.
    assigned_names: set[str] = set()
    for cluster in clustering.dense_clusters:
        cluster_id = next(
            (cid for cid, sel in selection.selections.items()
             if sel.witness_path in {pf.path for pf in cluster.members}),
            None,
        )
        if not cluster_id:
            # No canonical selected (no eligible candidates passed scanners)
            continue
        archetype_name = propose_archetype_name(cluster, assigned_names)
        assigned_names.add(archetype_name)
        archetypes_data["archetypes"][archetype_name] = {
            "cluster_id": cluster_id,
            "cluster_size": cluster.size,
            "paths_pattern": cluster.key.path_pattern_bucket,
            "content_signal": cluster.key.content_signal_match,
            "top_level_node_kinds": list(cluster.key.top_level_node_kinds),
            "jsx_present": cluster.key.jsx_present,
            "default_export_kind": cluster.key.default_export_kind,
            "named_export_count_bucket": cluster.key.named_export_count_bucket,
        }

        # Canonical entry
        sel = selection.selections[cluster_id]
        canonicals_data["canonicals"][archetype_name] = [{
            "witness": {
                "path": str(sel.witness_path.relative_to(repo_root)),
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
                archetypes_data, canonicals_data, profile_data, idioms_content
            ),
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
    if "## active" not in idioms_md:
        return ""
    after_active = idioms_md.split("## active", 1)[1]
    # Stop at the next level-2 heading (typically `## deprecated`).
    if "\n## " in after_active:
        section = after_active.split("\n## ", 1)[0]
    else:
        section = after_active
    return section.strip()


def _build_summary_md(
    archetypes_data: dict,
    canonicals_data: dict,
    profile_data: dict,
    idioms_md: str,
) -> str:
    """Generate the human-readable profile.summary.md for PR review.

    Per Round 5 DX recommendation: profile.summary.md is what reviewers
    actually read on profile-change PRs.
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
        f"## {profile_data['archetype_count']} archetypes detected",
        "",
    ]
    for name, arch in sorted(archetypes_data["archetypes"].items()):
        canonicals = canonicals_data["canonicals"].get(name, [])
        canonical_path = canonicals[0]["witness"]["path"] if canonicals else "(none)"
        lines.append(
            f"- **{name}** (cluster_size {arch['cluster_size']}, "
            f"paths {arch['paths_pattern']}) — canonical: `{canonical_path}`"
        )
    lines.extend([
        "",
        "## Rules",
        "",
        "_Phase 2C: tool config rules + AST stats._",
        "",
        "## Idioms",
        "",
    ])

    active_idioms = _extract_active_idioms(idioms_md)
    if active_idioms and "no idioms yet" not in active_idioms:
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

    return "\n".join(lines)
