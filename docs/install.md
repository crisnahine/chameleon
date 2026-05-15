# Installing Chameleon

Chameleon is a Claude Code plugin that auto-derives codebase conventions and injects archetype-aware guidance per-edit for TypeScript and Ruby on Rails repos.

This is the deep-dive install guide: prerequisites, every supported harness, the zero-touch dependency story, verification, opt-out, updates, uninstall, and troubleshooting. The [README](../README.md) links here for the long form.

Contributors hacking on the plugin itself want [CONTRIBUTING.md](../.github/CONTRIBUTING.md), not this file — `--plugin-dir` and local clones live there.

## Prerequisites

- **OS**: macOS or Linux. Windows works via Git Bash.
- **[Claude Code](https://docs.claude.com/claude-code) 2.x** (or any supported harness — see [Other harnesses](#other-harnesses)).
- **[uv](https://docs.astral.sh/uv/)** on `PATH`. Installs the MCP server's Python venv on first launch.
- **[Node.js](https://nodejs.org/) ≥ 20** with `npm` on `PATH`. Powers the TypeScript extractor.
- **[Ruby](https://www.ruby-lang.org/) ≥ 3.0** with the `prism` gem. Optional — only needed for Rails repos. Ruby ≥ 3.3 ships `prism` by default.

You do not need to run `uv sync` or `npm install` by hand. See [How dependencies are resolved](#how-dependencies-are-resolved).

## Install (Claude Code)

Inside any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. That is the entire user-facing install.

The plugin lands at `~/.claude/plugins/cache/chameleon/chameleon/<version>/`.

## Other harnesses

Chameleon also targets Cursor, Codex CLI, and Gemini CLI. Install per harness — settings do not cross over.

### Cursor

```
/add-plugin chameleon
```

> Pending marketplace listing. Until listed, install via local clone — see [CONTRIBUTING.md](../.github/CONTRIBUTING.md).

### Codex CLI

```
/plugins
```

Search `chameleon` and select **Install Plugin**.

> Pending marketplace listing. Until listed, install via local clone — see [CONTRIBUTING.md](../.github/CONTRIBUTING.md).

### Gemini CLI

```bash
gemini extensions install https://github.com/crisnahine/chameleon
```

Update later with `gemini extensions update chameleon`.

## How dependencies are resolved

Chameleon ships a Python MCP server and a Node-based TypeScript extractor. Both are resolved automatically — there is no manual `uv sync` or `npm install` step after marketplace install.

- **Python (MCP server).** The plugin's [`.mcp.json`](../.mcp.json) invokes `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`. On first launch, `uv` builds an isolated venv in its own cache (~5–10s). Subsequent starts are instant.
- **Node (TypeScript extractor).** The first `/chameleon-init` against a TypeScript repo lazily runs `npm install` inside `${CLAUDE_PLUGIN_ROOT}/mcp/` (~10s, one-time per plugin install). Ruby-only users never trigger this.

Only `uv`, Node.js ≥ 20, and (optionally) Ruby ≥ 3.0 need to be on your `PATH`. The rest builds itself.

## Verify the install

In a Claude Code session, ask:

> *What chameleon tools do you have?*

The model should list MCP tools like `detect_repo`, `get_archetype`, `bootstrap_repo`, etc. If it does not, see [Slash commands don't show up](#slash-commands-dont-show-up).

## First run inside a project

Open a TypeScript or Ruby on Rails repo in Claude Code, then:

1. **Bootstrap the profile** (skip if `.chameleon/` already exists and you trust its source):

   ```
   /chameleon-init
   ```

   Builds `.chameleon/` (archetypes, canonical excerpts, rules) in 3–10 seconds for repos under 5,000 files. Commit the result.

2. **Trust the profile** for your user:

   ```
   /chameleon-trust
   ```

   You will be asked to type the repo's basename to confirm. Trust state lives in `~/.local/share/chameleon/<repo_id>/.trust` and is per-user, not committed.

3. **Edit any file.** The `PreToolUse` hook fires; the model should mention the archetype and reference the canonical example before writing code.

## Slash commands

Seven user-invocable commands plus one auto-fired skill (`using-chameleon`). All commands accept `/cham-<name>` short aliases.

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze and update profile after team changes |
| `/chameleon-status` | View profile state, drift score, plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |

## Opt-out hierarchy

Four layers, ordered most-permanent to most-temporary:

```
.chameleon/.skip          per-repo, all users (committed)
CHAMELEON_DISABLE=1        per-user globally (shell rc)
/chameleon-disable         this session only
/chameleon-pause-15m       next 15 minutes (auto-resume)
```

Use `.chameleon/.skip` for repos chameleon should never analyze (e.g., docs-only). Use the env var to opt a user out across all repos. Use the slash commands for ad-hoc, scoped overrides.

## Updating

```
/plugin marketplace update chameleon
```

Restart Claude Code. `uv` resolves the new Python deps on next MCP launch; the lazy `npm install` reruns the next time you invoke `/chameleon-init` against a TS repo.

**v0.2.0 schema bump (one-time):** if you are upgrading from v0.1.x, the profile schema bumped from v4 → v5 and the loader refuses old profiles. Run `/chameleon-refresh` in each repo to rebuild, then re-run `/chameleon-trust` (the new profile has a new SHA). See [CHANGELOG.md](../CHANGELOG.md#020--2026-05-11) for the full v0.2.0 audit.

## Uninstalling

```
/plugin uninstall chameleon
/plugin marketplace remove chameleon
```

Then remove your trust state and drift cache:

```bash
rm -rf ~/.local/share/chameleon
```

Committed `.chameleon/` directories in your repos are unaffected; delete them per-repo if you want.

## Troubleshooting

### `uvx: command not found` / "chameleon-mcp not found"

`uv` isn't on your `PATH`. Install it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

See the [uv docs](https://docs.astral.sh/uv/) for alternatives. Restart Claude Code afterward.

### "npm not found on PATH" during `/chameleon-init` against a TS repo

The TypeScript extractor needs Node.js ≥ 20 with `npm` reachable. Install Node, confirm `npm --version` works in the same shell Claude Code launched from, then retry `/chameleon-init`.

### MCP server slow on first start

Expected. The first launch builds a venv via `uv` (~5–10s); subsequent starts are instant. The first `/chameleon-init` against a TS repo also runs `npm install` once (~10s, one-time per plugin install).

### `detect_repo` returns `trust_state: untrusted` after `/chameleon-trust`

Check that `~/.local/share/chameleon/<repo_id>/.trust` exists. If not, re-run `/chameleon-trust` and confirm the repo basename when prompted.

If the trust state is `stale`, the committed profile changed after you granted trust — re-run `/chameleon-trust` to re-approve.

### Slash commands don't show up

Run `/plugin list` and confirm `chameleon` is installed and enabled. If missing, re-run the install commands at the top. If listed but inactive, restart Claude Code.

### Hook latency feels high

The `PreToolUse` hook spawns a Python subprocess per invocation (200–500 ms on warm cache). For latency-sensitive editing sessions, use `/chameleon-pause-15m` or `/chameleon-disable`.

### Profile bootstrap fails with `failed_unsupported_language`

The repo has no TypeScript signals (`tsconfig.json` / `package.json`) and no Ruby signals (`Gemfile`). Chameleon currently supports only those two languages.

### Windows: hooks open in an editor or `bash` is not recognized

Hooks go through a polyglot `.cmd` wrapper that needs Git for Windows installed at the default path.

## Related docs

- [README.md](../README.md) — quickstart and overview
- [architecture.md](architecture.md) — design and invariants
- [CHANGELOG.md](../CHANGELOG.md) — release notes
- [CONTRIBUTING.md](../.github/CONTRIBUTING.md) — local dev setup, test suite, PR process
