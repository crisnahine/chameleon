---
name: chameleon-doctor
description: Use when the user explicitly invokes /chameleon-doctor to get a triage report on their chameleon installation health
---

# /chameleon-doctor

Run the chameleon-mcp `doctor` MCP tool. It returns a structured envelope:

- `overall`: ok | warn | error
- `checks`: a list of subsystem checks (python version, `hook_interpreter_deps` (a dep-capable Python >= 3.11 resolves for the hooks — when this errors, hook enforcement and guidance are OFF), `mcp_server_launcher` (`uvx` resolves so the bundled MCP server can launch — when this errors, the MCP tools like `/chameleon-init`/`refresh`/`status` are unavailable), bash on PATH, timeout(1) on PATH, plugin data writable, hook scripts present and executable, HMAC key sane, daemon liveness, recent hook errors, per-repo profile/trust state, config_json validation, production_ref resolvability when a lock is set, plus three dead-install detectors for the current repo: `profile_artifacts` (generated artifacts exist and parse), `judge_spawn_health` (turn-end reviewer spawns are not all failing), `advisory_emission` (trusted edits actually resolve archetypes))
- `summary`: counts

## The flow

1. Call `mcp__plugin_chameleon_chameleon-mcp__doctor` (no arguments).
2. Display the result to the user as a compact table, highlighting any check with status != ok.
   Include the actionable detail text for each non-ok check.
3. Roll-up interpretation:
   - If `overall` is `ok`: confirm the install is healthy.
   - If `overall` is `warn`: surface the warn-status checks as informational, note that they typically don't block operation.
     - `profile_artifacts` warn: a generated artifact is missing or corrupt; suggest `/chameleon-refresh` to regenerate.
     - `judge_spawn_health` warn: every recent correctness-judge spawn failed; the turn-end review layer is dead. Check the `claude` binary and auth.
     - `advisory_emission` warn: trusted edits are not resolving archetypes, so per-edit advisories are silent; suggest `/chameleon-refresh` then `/chameleon-status`.
   - If `overall` is `error`: call out each error-status check as a blocker and suggest the relevant fix:
     - `python_version` error: upgrade Python to >= 3.11.
     - `hook_interpreter_deps` error: no dep-capable Python >= 3.11 resolves for the hooks, so hook enforcement and guidance are OFF — the single most consequential failure. Install/point to a Python >= 3.11 (or `uv`); the `detail` names what was found.
     - `mcp_server_launcher` error: neither `uvx` nor `uv` is on PATH, so the bundled MCP server cannot launch and every MCP tool (`/chameleon-init`, `refresh`, `status`, and the codebase queries) is dead even if the hooks resolve a Python. Install `uv` (which provides `uvx`): https://docs.astral.sh/uv/getting-started/installation/.
     - `bash_on_path` error: install bash or ensure it is on `$PATH` for the hooks to work.
     - `plugin_data_writable` error: check directory permissions for the chameleon data dir shown in `detail`.
     - `hook_*` error: re-install the plugin or run `chmod +x` on the listed hook script.
4. Always include the platform info and chameleon_version from `data` for copy-paste when filing a bug.

## Honesty Rules

- Report the real health of the installation: name each check, its actual result (pass / degraded / fail), and the evidence from `data`. Never claim a check passed that did not run, and never hide a degraded state behind a green summary.
- When a fix is uncertain, say so and give the safest next step; don't assert a remedy you have not verified.
- Always include the real `chameleon_version` and platform info from `data`; don't paraphrase or guess them.
