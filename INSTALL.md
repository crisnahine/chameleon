# Installing Chameleon

Chameleon is a Claude Code plugin that gives the model archetype-aware context for TypeScript and Ruby on Rails repos.

## Prerequisites

- macOS or Linux. Windows works via Git Bash; see [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md).
- [Claude Code](https://docs.claude.com/claude-code) 2.x.
- [uv](https://docs.astral.sh/uv/) for the Python venv.
- [Node.js](https://nodejs.org/) ≥ 18 (TypeScript extractor).
- [Ruby](https://www.ruby-lang.org/) ≥ 3.0 with the `prism` gem (Ruby extractor).
- `jq` (for `scripts/bump-version.sh`).

## One-time setup

Clone the repo somewhere stable:

```bash
git clone https://github.com/crisnahine/chameleon
cd chameleon
```

Build the MCP server's Python venv and install the TypeScript extractor's Node dependencies:

```bash
cd mcp
uv sync          # Python venv for the MCP server
npm install      # Node deps for the TypeScript AST extractor
cd ..
```

> `npm install` is required even if you only plan to use Ruby support — it installs the `typescript` package that `scripts/ts_dump.mjs` resolves via `require("typescript")`. Skipping it makes TS bootstrap fail at runtime.

Verify the entry point and Node deps:

```bash
ls -l mcp/.venv/bin/chameleon-mcp        # Python entry point
ls -d mcp/node_modules/typescript        # TypeScript package
```

Done. Skip ahead to **Wiring chameleon into Claude Code**.

## Wiring chameleon into Claude Code

Three options, ordered most-recommended → least:

### Option A — Per-session via `--plugin-dir`

Pass the chameleon repo path on every `claude` invocation:

```bash
claude --plugin-dir ~/path/to/chameleon
```

Pros: scoped, easy to enable/disable. Cons: have to remember the flag.

### Option B — Permanent install via marketplace add

```bash
# Inside Claude Code:
/plugin marketplace add ~/path/to/chameleon
/plugin install chameleon
```

Restart Claude Code. Verify by asking: *"What chameleon tools do you have?"*

### Option C — Symlink into `~/.claude/plugins/`

```bash
mkdir -p ~/.claude/plugins
ln -s ~/path/to/chameleon ~/.claude/plugins/chameleon
```

Restart Claude Code.

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

```bash
cd ~/path/to/chameleon
git pull
cd mcp
uv sync
npm install      # in case the TypeScript pin changed
```

Restart Claude Code if it's running.

## Uninstalling

If you used Option C (symlink):

```bash
rm ~/.claude/plugins/chameleon
```

If you used Option B (marketplace install):

```
# Inside Claude Code:
/plugin uninstall chameleon
```

Also remove your trust state and drift cache:

```bash
rm -rf ~/.local/share/chameleon
```

## Troubleshooting

### "chameleon-mcp not found" or MCP server doesn't connect

Build the venv: `cd mcp && uv sync`.

### TypeScript bootstrap fails with `Cannot find module 'typescript'`

The Node deps were never installed. Run `cd mcp && npm install`, then retry `/chameleon-init`.

### `detect_repo` returns `trust_state: untrusted` after `/chameleon-trust`

Check `~/.local/share/chameleon/<repo_id>/.trust` exists. If not, re-run `/chameleon-trust`.

### Slash commands don't show up

Verify `--plugin-dir` is on the command line, OR that `/plugin list` shows chameleon.

### Hook latency feels high

The `PreToolUse` hook spawns a Python subprocess per invocation. Expect 200–500 ms on warm cache. Use `/chameleon-pause-15m` for latency-sensitive sessions.

### Profile bootstrap fails with `failed_unsupported_language`

The repo has no TypeScript (`tsconfig.json` / `package.json`) AND no Ruby (`Gemfile`) signals. Chameleon currently supports only those two languages.

## Related docs

- [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md) — Windows hook setup
- [ARCHITECTURE.md](ARCHITECTURE.md) — design + invariants
- [tests/](tests/) — test files double as living documentation
