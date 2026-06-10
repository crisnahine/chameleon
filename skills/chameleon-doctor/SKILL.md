---
name: chameleon-doctor
description: Use when the user explicitly invokes /chameleon-doctor to get a triage report on their chameleon installation health
---

# /chameleon-doctor

Run the chameleon-mcp `doctor` MCP tool. It returns a structured envelope:

- `overall`: ok | warn | error
- `checks`: a list of subsystem checks (python version, bash on PATH, timeout(1) on PATH, plugin data writable, hook scripts present and executable, HMAC key sane, daemon liveness, recent hook errors, per-repo profile/trust state, config_json validation, production_ref resolvability when a lock is set)
- `summary`: counts

## The flow

1. Call `mcp__plugin_chameleon_chameleon-mcp__doctor` (no arguments).
2. Display the result to the user as a compact table, highlighting any check with status != ok.
   Include the actionable detail text for each non-ok check.
3. Roll-up interpretation:
   - If `overall` is `ok`: confirm the install is healthy.
   - If `overall` is `warn`: surface the warn-status checks as informational, note that they typically don't block operation.
   - If `overall` is `error`: call out each error-status check as a blocker and suggest the relevant fix:
     - `python_version` error: upgrade Python to >= 3.11.
     - `bash_on_path` error: install bash or ensure it is on `$PATH` for the hooks to work.
     - `plugin_data_writable` error: check directory permissions for the chameleon data dir shown in `detail`.
     - `hook_*` error: re-install the plugin or run `chmod +x` on the listed hook script.
4. Always include the platform info and chameleon_version from `data` for copy-paste when filing a bug.
