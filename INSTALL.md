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

Two supported methods. Pick one:

### Option A — Per-session via `--plugin-dir` (recommended for local dev)

Pass the chameleon repo path on every `claude` invocation:

```bash
claude --plugin-dir ~/path/to/chameleon
```

You can stack multiple `--plugin-dir` flags:

```bash
claude --plugin-dir ./chameleon --plugin-dir ./other-plugin
```

When a `--plugin-dir` plugin shares a name with an installed marketplace plugin, the local copy wins for that session.

Pros: picks up local edits without reinstalling. Cons: have to remember the flag.

### Option B — Permanent install via the GitHub marketplace

Inside any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Verify by asking: *"What chameleon tools do you have?"*

> `/plugin marketplace add` accepts a GitHub `owner/repo` slug or a full HTTPS URL — **not** a local filesystem path. Use Option A for local development.

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

If you used Option A (`--plugin-dir`): just drop the flag from your `claude` invocation. Nothing else to undo.

If you used Option B (marketplace install):

```
# Inside Claude Code:
/plugin uninstall chameleon
/plugin marketplace remove chameleon
```

Either way, also remove your trust state and drift cache:

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

Verify either:
- `--plugin-dir ~/path/to/chameleon` is on the `claude` command line, OR
- `/plugin list` (inside Claude Code) shows `chameleon` as installed.

If neither: the plugin isn't loaded. Re-run Option A or Option B from above.

### Hook latency feels high

The `PreToolUse` hook spawns a Python subprocess per invocation. Expect 200–500 ms on warm cache. Use `/chameleon-pause-15m` for latency-sensitive sessions.

### Profile bootstrap fails with `failed_unsupported_language`

The repo has no TypeScript (`tsconfig.json` / `package.json`) AND no Ruby (`Gemfile`) signals. Chameleon currently supports only those two languages.

## Related docs

- [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md) — Windows hook setup
- [ARCHITECTURE.md](ARCHITECTURE.md) — design + invariants
- [tests/](tests/) — test files double as living documentation
