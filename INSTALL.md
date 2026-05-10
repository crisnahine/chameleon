# Installing Chameleon

Chameleon is a Claude Code plugin that gives the model archetype-aware context for TypeScript and Ruby on Rails repos.

For Cursor, Codex CLI, and Gemini CLI install commands, see the harness sections in [README.md](README.md#install). The rest of this document is the deep walkthrough for Claude Code (the recommended harness).

## Prerequisites

- macOS or Linux. Windows works via Git Bash; see [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md).
- [Claude Code](https://docs.claude.com/claude-code) 2.x.
- [uv](https://docs.astral.sh/uv/) for the Python venv.
- [Node.js](https://nodejs.org/) ≥ 20 (TypeScript extractor).
- [Ruby](https://www.ruby-lang.org/) ≥ 3.0 with the `prism` gem (Ruby extractor; `prism` ships by default in Ruby ≥ 3.3).

## Install

Inside any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Verify by asking: *"What chameleon tools do you have?"*

> Contributors hacking on the plugin itself should follow [CONTRIBUTING.md](CONTRIBUTING.md) instead — `--plugin-dir` is the right tool for local development.

## How dependencies are resolved (no manual setup)

Chameleon ships a Python MCP server and a Node-based TypeScript extractor. Starting with **v0.1.1**, both are resolved automatically:

- **Python side:** the plugin's `.mcp.json` invokes `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`. The first time Claude Code starts the MCP server, `uv` builds an isolated venv in its own cache (~5–10s); subsequent starts are instant.
- **Node side:** the first `/chameleon-init` against a TypeScript repo runs `npm install` lazily inside `${CLAUDE_PLUGIN_ROOT}/mcp/` (~10s, one-time per plugin install). Ruby-only users never trigger this.

You only need `uv`, Node.js ≥ 20, and (optionally) Ruby ≥ 3.0 on your `PATH`. No `uv sync`, no `npm install` to run by hand.

## Verifying chameleon works

In a fresh Claude Code session inside any TypeScript or Ruby on Rails repo:

1. **Run `/chameleon-init`** if `.chameleon/` doesn't exist yet:

   ```
   /chameleon-init
   ```

   Bootstraps the profile in 3–10 seconds for repos under 5,000 files.

2. **Run `/chameleon-trust`** to approve the committed profile for this user:

   ```
   /chameleon-trust
   ```

   You'll be asked to type the repo's basename to confirm.

3. **Edit any file** in the repo. The model should mention the archetype and reference the canonical example before writing code.

## Slash commands

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze + update profile after team changes |
| `/chameleon-status` | View profile state, drift score, plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |

All commands accept `/cham-<name>` short aliases.

## Opt-out hierarchy

```
Most-permanent →    .chameleon/.skip          per-repo, all users (committed)
                ↓   CHAMELEON_DISABLE=1        per-user globally (in shell rc)
                ↓   /chameleon-disable         this session only
                ↓   /chameleon-pause-15m       next 15 minutes (auto-resume)
Most-temporary
```

## Updating chameleon

```
/plugin marketplace update chameleon
```

Restart Claude Code. `uv` and the lazy `npm install` pick up the new versions on next launch / next `/chameleon-init`.

## Uninstalling

```
/plugin uninstall chameleon
/plugin marketplace remove chameleon
```

Then remove your trust state and drift cache:

```bash
rm -rf ~/.local/share/chameleon
```

## Troubleshooting

### "chameleon-mcp not found" or `uvx: command not found`

Install `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh` (see [uv docs](https://docs.astral.sh/uv/)). Then restart Claude Code.

### "npm not found on PATH" during `/chameleon-init` against a TS repo

Install Node.js ≥ 20 and make sure `npm` is on your shell's PATH. Then retry `/chameleon-init`.

### MCP server slow on first start

Expected. The first launch builds a venv via `uv` (~5–10s). Subsequent starts are instant. The first `/chameleon-init` against a TS repo also runs `npm install` once (~10s).

### `detect_repo` returns `trust_state: untrusted` after `/chameleon-trust`

Check `~/.local/share/chameleon/<repo_id>/.trust` exists. If not, re-run `/chameleon-trust`.

### Slash commands don't show up

Run `/plugin list` inside Claude Code and confirm `chameleon` is listed. If not, re-run the install commands above.

### Hook latency feels high

The `PreToolUse` hook spawns a Python subprocess per invocation. Expect 200–500 ms on warm cache. Use `/chameleon-pause-15m` for latency-sensitive sessions.

### Profile bootstrap fails with `failed_unsupported_language`

The repo has no TypeScript (`tsconfig.json` / `package.json`) AND no Ruby (`Gemfile`) signals. Chameleon currently supports only those two languages.

## Related docs

- [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md) — Windows hook setup
- [ARCHITECTURE.md](ARCHITECTURE.md) — design + invariants
- [tests/](tests/) — test files double as living documentation
