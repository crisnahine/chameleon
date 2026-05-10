"""FastMCP server entry point for chameleon-mcp.

Stdio transport. Long-lived process invoked by hooks via UNIX domain socket
(Phase 4 daemon model) or directly per-call (Phase 1C-2 fallback).

See ARCHITECTURE.md sections:
- "MCP server (`chameleon-mcp`)" — full tool surface
- "Performance characteristics" — daemon model
- "Cluster signature function" — what tools rely on
"""

from mcp.server.fastmcp import FastMCP

from chameleon_mcp import tools

mcp = FastMCP("chameleon-mcp")


# Register all 12 MCP tools (Phase 1C stubs return hardcoded valid-shape values).
# Real implementations land in Phase 2 (bootstrap + extractor) and Phase 4 (security).


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
            "profile_status": str,     # "no_profile" | "profile_present" | "pack_match"
            "trust_state": str,        # "trusted" | "untrusted" | "n/a"
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
            "archetype": {"name": str, "alternatives": [str], "confidence_band": str},
            "canonical_excerpt": {"content": str, "witness_path": str, "truncated": bool, "sha_hint": str},
            "rules": [{"rule": str, "citation": str}],
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
def get_rules(repo: str, archetype: str | None = None) -> dict:
    """Return rules + citations for repo, filtered by archetype if provided."""
    return tools.get_rules(repo, archetype)


@mcp.tool()
def lint_file(repo: str, archetype: str, content: str) -> dict:
    """Validate file content against archetype's rules. Returns violations + canonical confidence."""
    return tools.lint_file(repo, archetype, content)


@mcp.tool()
def get_drift_status(repo: str) -> dict:
    """Report freshness, days_since_refresh, observed_drift_score for a repo."""
    return tools.get_drift_status(repo)


@mcp.tool()
def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile. OS-level locked via flock."""
    return tools.refresh_repo(repo, force)


@mcp.tool()
def bootstrap_repo(path: str, mode: str = "full", paths_glob: str | None = None) -> dict:
    """First-time analysis: AST scan + interview + atomic profile commit."""
    return tools.bootstrap_repo(path, mode, paths_glob)


@mcp.tool()
def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos this user has touched. Cursor-paginated from day 1."""
    return tools.list_profiles(cursor, limit)


@mcp.tool()
def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge: re-cluster from union of ours and theirs.

    Used by `.gitattributes` merge driver registration.
    See ARCHITECTURE.md "SQLite schemas" → "merge_profiles algorithm" subsection.
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


def main() -> None:
    """Entry point for `chameleon-mcp` CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
