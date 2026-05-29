"""FastMCP server entry point for chameleon-mcp.

Stdio transport. Long-lived process invoked by hooks via UNIX domain socket
(Phase 4 daemon model) or directly per-call (Phase 1C-2 fallback).

See docs/architecture.md sections:
- "MCP server (`chameleon-mcp`)" — full tool surface
- "Performance characteristics" — daemon model
- "Cluster signature function" — what tools rely on
"""

from mcp.server.fastmcp import FastMCP

from chameleon_mcp import tools

mcp = FastMCP("chameleon-mcp")


@mcp.tool()
def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to.

    Args:
        file_path: absolute path to a file inside a repo (or repo root)

    Returns:
        {
          "api_version": "1",
          "data": {
            "repo_id": str,            # sha256 hex of git_remote_url or canonicalized abs path
            "repo_root": str,          # absolute path to repo root
            "profile_status": str,     # "no_repo" | "no_profile" | "profile_present" | "profile_corrupted" | "profile_unsupported_schema_version"
            "trust_state": str,        # "trusted" | "untrusted" | "stale" | "n/a"
          }
        }
    """
    return tools.detect_repo(file_path)


@mcp.tool()
def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Args:
        repo: repo_id from detect_repo
        file_path: absolute path to file

    Returns:
        archetype + alternatives + content_signal_match info
    """
    return tools.get_archetype(repo, file_path)


@mcp.tool()
def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical excerpt + rules + meta in one round trip.

    Replaces the v3-era 4-call dance (detect_repo → get_archetype →
    get_canonical_excerpt → get_rules) per Round 5 API Designer
    recommendation.

    Args:
        file_path: absolute path to file

    Returns:
        {
          "api_version": "1",
          "data": {
            "repo": {"id": str, "profile_status": str, "trust_state": str},
            "archetype": {"archetype": str, "alternatives": [str], "confidence_band": str,
                          "content_signal_match": bool, "match_quality": str, "sub_buckets_count": int},
            "canonical_excerpt": {"content": str, "witness_path": str, "truncated": bool, "sha_hint": str},
            "rules": [(source_key, config_dict), ...],
            "idioms": str | None,
            "meta": {"mtime_token": str, "computed_at": str}
          }
        }
    """
    return tools.get_pattern_context(file_path)


@mcp.tool()
def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the annotated canonical excerpt for an archetype (~500-800 tokens)."""
    return tools.get_canonical_excerpt(repo, archetype)


@mcp.tool()
def get_rules(repo: str, source: str | None = None) -> dict:
    """Return repo-global rules (eslint / rubocop / formatting / typescript), keyed by tool/source.

    `source` filters to a single source key (`"eslint"`, `"rubocop"`,
    `"formatting"`, `"typescript"`). Omit to return all. Passing an
    archetype name (e.g. `"component"`) returns a failed envelope —
    rules are SOURCE-scoped, not archetype-scoped. The legacy
    `archetype=` kwarg was removed from the MCP schema in v0.5.17
    (still accepted by the in-process function with a deprecation
    field, but no longer advertised to callers).
    """
    return tools.get_rules(repo, source)


@mcp.tool()
def lint_file(repo: str, archetype: str, content: str, file_path: str | None = None) -> dict:
    """Validate file content against archetype's rules. Returns violations + canonical confidence.

    When file_path is provided, its extension is used for language detection
    instead of falling back to the witness extension.
    """
    return tools.lint_file(repo, archetype, content, file_path=file_path)


@mcp.tool()
def get_drift_status(repo: str) -> dict:
    """Report freshness, days_since_refresh, observed_drift_score for a repo."""
    return tools.get_drift_status(repo)


@mcp.tool()
def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile. OS-level locked via flock."""
    return tools.refresh_repo(repo, force)


@mcp.tool()
def bootstrap_repo(
    path: str,
    mode: str = "full",
    paths_glob: str | None = None,
    force: bool = False,
) -> dict:
    """First-time analysis: AST scan + interview + atomic profile commit.

    Pass force=true to overwrite a committed profile (BUG-026).
    """
    return tools.bootstrap_repo(path, mode, paths_glob, force)


@mcp.tool()
def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos this user has touched. Cursor-paginated from day 1."""
    return tools.list_profiles(cursor, limit)


@mcp.tool()
def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge: re-cluster from union of ours and theirs.

    Used by `.gitattributes` merge driver registration.
    See docs/architecture.md "SQLite schemas" → "merge_profiles algorithm" subsection.
    """
    return tools.merge_profiles(repo, base, ours, theirs)


@mcp.tool()
def teach_profile(repo: str, feedback: str) -> dict:
    """Apply user-driven correction to profile (idiom, banned import, mandatory wrapper).

    Renamed from `refine_profile` in v4 to align with `/chameleon-teach` slash command.
    """
    return tools.teach_profile(repo, feedback)


@mcp.tool()
def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user.

    Requires confirmation_token (typed repo name or `yes-trust-<repo_id_short>`)
    to defeat normalization-deviance.
    """
    return tools.trust_profile(repo, confirmation_token)


@mcp.tool()
def disable_session(
    repo: str,
    session_id: str,
    force: bool = False,
) -> dict:
    """Suppress chameleon advisory injections for this Claude Code session.

    Per /chameleon-disable. preflight-and-advise checks the
    `.session_disabled.<session_id>` marker before injecting; when present
    AND validly HMAC-signed, no <chameleon-context> is added.

    Defenses (v0.5.15-17): the marker is HMAC-signed so an out-of-process
    attacker can't plant a forgery. The repo must have a trust grant
    before disable_session will write. Sessions whose session_id has never
    invoked another chameleon tool for this repo are REFUSED by default;
    pass `force=True` for legitimate first-time-disable cases from a
    brand-new session (the response still surfaces a warning).
    """
    return tools.disable_session(repo, session_id, force=force)


@mcp.tool()
def pause_session(repo: str, minutes: int = 15) -> dict:
    """Pause chameleon advisory injections for `minutes` minutes (default 15).

    Per /chameleon-pause-15m. Auto-expires when the timestamp passes.
    """
    return tools.pause_session(repo, minutes)


@mcp.tool()
def propose_archetype_renames(repo: str, top_n: int = 8) -> dict:
    """Suggest better names for the top-N largest archetypes in the profile.

    Drives the /chameleon-init interview's rename step. For each archetype
    the response carries the current name, cluster_size, canonical file,
    and 3-5 candidate alternative names derived from the canonical filename,
    paths_pattern tail, and top-level node kinds.

    The MCP is stateless — the chameleon-init skill collects user choices
    and submits them as a single mapping via apply_archetype_renames.
    """
    return tools.propose_archetype_renames(repo, top_n)


@mcp.tool()
def apply_archetype_renames(repo: str, renames: dict) -> dict:
    """Apply an archetype rename mapping atomically.

    Rewrites archetypes.json + canonicals.json + rules.json keys (where
    keyed by archetype) and regenerates profile.summary.md. Wrapped in
    atomic_profile_commit so a crash mid-write leaves the previous profile
    untouched.

    Returns {status, renames_applied, new_profile_sha256}.
    """
    return tools.apply_archetype_renames(repo, renames)


@mcp.tool()
def teach_profile_structured(
    repo: str,
    slug: str,
    rationale: str,
    example: str | None = None,
    counterexample: str | None = None,
    archetype: str | None = None,
    status: str = "active",
) -> dict:
    """Structured-form idiom capture for /chameleon-teach.

    Validates slug regex (`^[a-z][a-z0-9-]{2,63}$`), enforces a 50KB cap on
    rationale + example + counterexample combined, renders to idioms.md in
    the same format as free-form teach_profile, and delegates to
    teach_profile for the downstream protections (advisory lock,
    sanitization, placeholder strip).
    """
    return tools.teach_profile_structured(
        repo,
        slug=slug,
        rationale=rationale,
        example=example,
        counterexample=counterexample,
        archetype=archetype,
        status=status,
    )


@mcp.tool()
def daemon_status() -> dict:
    """Return current chameleon-mcp daemon status.

    Phase 4.5 long-lived daemon: returns alive flag, PID, socket path,
    uptime (seconds) and ISO 8601 last_request_at when the daemon
    answered a ping. Read-only — does not start/stop the daemon.
    """
    return tools.daemon_status()


@mcp.tool()
def doctor() -> dict:
    """Triage report for chameleon installation health.

    Returns a structured envelope with subsystem checks. Each check has a
    status (ok | warn | error) and a brief detail string. Subsystems checked:
    python version, bash on PATH, timeout(1) on PATH, plugin data dir
    writable, all 5 hook scripts present and executable, HMAC key sane,
    daemon liveness, last 5 hook error log lines, per-known-repo
    profile/trust state.

    Use /chameleon-doctor or inspect `data.overall` to get the overall
    health status.
    """
    return tools.doctor()


def main() -> None:
    """Entry point for `chameleon-mcp` CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
