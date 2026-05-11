# ADR-0004: Invoke chameleon-mcp via `uvx` for zero-touch install

> **Status:** Accepted
> **Date:** 2026-05-11
> **Deciders:** Cris Nahine

## Context

The v0.1.0 release shipped with `.mcp.json` pointing at a pre-built
venv path: `mcp/.venv/bin/chameleon-mcp`. This worked for the
maintainer's development install but failed for marketplace users in
two ways:

1. The marketplace install delivers source files only. There is no
   `.venv` at install time, so the path resolves to a non-existent
   binary and Claude Code reports "MCP server failed to start."
2. The documented workaround — "run `uv sync` inside `mcp/` after
   install" — required users to find `${CLAUDE_PLUGIN_ROOT}`, cd
   into it, and run a Python toolchain command. Several users
   bounced off the install at that step.

The MCP server needs a Python venv with `mcp`, `detect-secrets`,
and a handful of small dependencies. Building this on the user's
machine is unavoidable (the dependencies are not pure-Python in
every case). The question is who builds it and when.

## Decision

`.mcp.json` invokes `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`.
uv resolves and builds an isolated venv in its own cache on first
launch (~5–10s); subsequent starts hit the cache and are instant.
Users do not run `uv sync`; uv handles it transparently.

This shipped in v0.1.1 and is the install model going forward.

## Consequences

### Positive consequences

- Zero post-install steps for users who already have `uv` on PATH.
  README is back to "install the plugin, restart, done."
- The venv is uv-managed: cache invalidation, lock-file integrity,
  and platform-specific wheels all become uv's problem, not
  chameleon's.
- No `.venv` ships with the plugin, so the marketplace artifact is
  smaller and platform-neutral.
- Test parameterization is unchanged: `CHAMELEON_PLUGIN_ROOT` env
  override (added in `mcp/chameleon_mcp/plugin_paths.py`, v0.1.1)
  lets the dev install bypass uvx and run against the source venv
  directly.

### Negative consequences / trade-offs

- **First-launch latency.** ~5–10 seconds while uv resolves and
  builds the venv. Visible as a startup pause on the first
  Claude Code session in a fresh shell. Subsequent launches are
  instant (uv cache hit).
- **`uv` is now a hard prerequisite.** Listed at the top of the
  README install section. INSTALL.md troubleshooting covers
  `uvx: command not found` explicitly. We accept this — `uv` is a
  one-line install and has Homebrew / curl / pipx packaging.
- **uv cache location is uv's choice.** Users who want to inspect
  the running venv find it under `~/.cache/uv/`, not under
  `${CLAUDE_PLUGIN_ROOT}/mcp/.venv/`. Surprising at first; not
  load-bearing.
- **`CLAUDE_PLUGIN_ROOT` resolution had to be reworked.** The MCP
  server now runs from uv's isolated cache, not from
  `${CLAUDE_PLUGIN_ROOT}/mcp/`. Path resolution in
  `extractors/typescript.py` and `extractors/ruby.py` was
  rewritten to go through
  `mcp/chameleon_mcp/plugin_paths.py::plugin_root()`, which prefers
  `CLAUDE_PLUGIN_ROOT` (set by Claude Code) over file-relative
  resolution. This is the v0.1.1 fix.

### Risks accepted

- **uv-as-prerequisite cost in onboarding.** A user without uv
  hits an explicit error message and a documented one-line install
  step. Lower friction than a manual `uv sync` step would be.
- **uv upstream stability.** uv is in active development; an
  upstream breaking change between the user's installed version
  and our `pyproject.toml` constraints could break launches.
  Mitigated by uv's strong stability story to date and by uv's
  fallback to PyPI-pinned versions in our lock file.
- **uv cache eviction.** If uv evicts our cached venv, the next
  launch pays the ~5–10s rebuild cost again. Acceptable;
  this is a "first launch after a long absence" event, not a
  per-session cost.

## Alternatives considered

### A. Shell wrapper script

Ship `mcp/bin/chameleon-mcp.sh` that runs `uv sync` (lazy, idempotent)
then `exec .venv/bin/chameleon-mcp`. Rejected because:

- We re-introduce the `.venv` inside the plugin tree, which uv
  intentionally avoids.
- We add a shell-script trampoline on every MCP launch (small but
  non-zero cost, plus a Windows-compatibility headache).
- We have to invent our own cache-validity check; uv already has one.

### B. PyInstaller / shiv / pex bundled binary

Ship a single-file Python binary. Rejected because:

- The marketplace artifact balloons (TypeScript compiler tree is
  already large; doubling that with bundled CPython is hostile).
- Per-platform builds (Linux x86_64, macOS arm64, macOS x86_64,
  Windows) multiply maintenance work and CI cost.
- Updates are heavier — every fix requires a fresh full bundle —
  versus `uvx`'s incremental dependency resolution.

### C. Full TypeScript rewrite of the MCP server

Eliminate Python entirely; ship MCP in Node, reuse the existing Node
TypeScript extractor. Rejected because:

- detect-secrets is the canonical Python implementation; porting or
  vendoring an equivalent in Node is significant new work for
  marginal gain.
- The Python MCP server architecture (FastMCP, stdio transport,
  tool registration) is mature and the test suite is built around
  it. A rewrite resets that maturity.
- The Node already-required prerequisite covers the TypeScript
  extractor; users still need `uv` because the choice is between
  "uv + Node" and "Node + bundled Python," and "uv + Node" is
  smaller.

## References

- `.mcp.json` — the canonical invocation
- `mcp/chameleon_mcp/plugin_paths.py` — `CLAUDE_PLUGIN_ROOT` resolution helper added in v0.1.1
- `mcp/chameleon_mcp/extractors/typescript.py` — uses `plugin_root()` for the long-lived Node subprocess
- `mcp/chameleon_mcp/extractors/ruby.py` — same pattern for the Prism subprocess
- `INSTALL.md#uvx-command-not-found--chameleon-mcp-not-found` — user-facing troubleshooting
- `CHANGELOG.md` — v0.1.1 entry, "Zero-touch install"
