# Installing Chameleon

Chameleon is a private Empire Flippers plugin for Claude Code. It gives the model archetype-aware context for both EF api (Ruby on Rails) and EF client (TypeScript) repos.

## Prerequisites

- macOS or Linux. (Windows works via Git Bash but isn't tested in production yet — see [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md).)
- [Claude Code](https://docs.claude.com/claude-code) 2.x.
- [uv](https://docs.astral.sh/uv/) for the Python venv (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`).
- [Node.js](https://nodejs.org/) ≥ 18 (used by the TypeScript extractor).
- [Ruby](https://www.ruby-lang.org/) ≥ 3.0 with the `prism` gem (used by the Ruby extractor).
- `jq` (for `scripts/bump-version.sh`).

## One-time setup

Clone the repo somewhere stable (any path works — pick something you won't move):

```bash
cd ~/Documents/Projects
git clone <repo-url> chameleon
cd chameleon
```

Build the MCP server's Python venv:

```bash
cd mcp
uv sync
cd ..
```

Verify the MCP entry point exists:

```bash
ls -l mcp/.venv/bin/chameleon-mcp
```

That's all the build steps. Skip ahead to **Wiring chameleon into Claude Code**.

## Wiring chameleon into Claude Code

You have three options, ordered most-recommended → least:

### Option A — Per-session via `--plugin-dir` (recommended for first try)

Pass the chameleon repo path on every `claude` invocation:

```bash
claude --plugin-dir ~/Documents/Projects/chameleon
```

Pros: scoped to one session, easy to enable/disable, no config drift.
Cons: have to remember the flag every time.

### Option B — Permanent install via marketplace add (recommended for daily use)

Once you have a private marketplace set up (or use the local path directly), add chameleon as a plugin:

```bash
# Inside Claude Code:
/plugin marketplace add /Users/<you>/Documents/Projects/chameleon
/plugin install chameleon
```

Restart Claude Code. Verify by asking: *"What chameleon tools do you have?"* — the model should list `detect_repo`, `get_pattern_context`, etc.

### Option C — Symlink into `~/.claude/plugins/`

Manual install for users without marketplace tooling:

```bash
mkdir -p ~/.claude/plugins
ln -s ~/Documents/Projects/chameleon ~/.claude/plugins/chameleon
```

Restart Claude Code.

## Verifying chameleon works

In a fresh Claude Code session inside an EF repo (e.g. `cd ~/Documents/Projects/empire-flippers/client`), check:

1. **The session starts with chameleon context.** The model has read `using-chameleon` SKILL.md if it knows about `chameleon-mcp::get_pattern_context`. Ask: *"List your chameleon-related slash commands."*

2. **Run `/chameleon-init`** (or `/cham-init`) if `.chameleon/` doesn't exist yet:

   ```
   /chameleon-init
   ```

   This bootstraps the profile in 3–10 seconds for repos under 5,000 files.

3. **Run `/chameleon-trust`** to approve the committed profile for this user:

   ```
   /chameleon-trust
   ```

   You'll be asked to type the repo's basename (e.g. `client` or `api`) to confirm.

4. **Edit any file** in the repo. Watch for the chameleon context block: the model should mention the archetype name and reference the canonical example before writing code.

## Slash commands

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile (≤10s for typical repos) |
| `/chameleon-refresh` | Re-analyze + update profile after team changes |
| `/chameleon-status` | View profile state, drift score, plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |

All commands accept `/cham-<name>` short aliases.

## Opt-out hierarchy

If chameleon is unhelpful, opt out at the level that matches the situation:

```
Most-permanent →    .chameleon/.skip          (per-repo, all users, committed → team-wide)
                ↓   CHAMELEON_DISABLE=1        (per-user globally; in shell rc)
                ↓   /chameleon-disable         (this session only)
                ↓   /chameleon-pause-15m       (next 15 minutes)
Most-temporary
```

## Updating chameleon

```bash
cd ~/Documents/Projects/chameleon
git pull
cd mcp
uv sync   # picks up any new Python dependencies
```

Restart Claude Code if it's already running.

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

To also remove your trust state and drift cache:

```bash
rm -rf ~/.local/share/chameleon
```

## Troubleshooting

### "chameleon-mcp not found" or MCP server doesn't connect
The Python venv hasn't been built. Run `cd mcp && uv sync`.

### `detect_repo` returns `trust_state: untrusted` after `/chameleon-trust`
You may have a stale trust cache from an earlier `CLAUDE_PLUGIN_DATA` env override. Check `~/.local/share/chameleon/<repo_id>/.trust` exists and contains your user. If not, re-run `/chameleon-trust`.

### Slash commands don't show up in `/help`
Claude Code didn't load the plugin. Verify `--plugin-dir` is passed on the command line, OR that the marketplace install completed (`/plugin list` should show chameleon).

### Hook latency feels high
The PreToolUse hook spawns a Python subprocess per invocation. Expect 200–500 ms on a warm cache. The Phase 4 daemon model will reduce this; until then, use `/chameleon-pause-15m` for latency-sensitive sessions.

### Profile bootstrap fails with `failed_unsupported_language`
The repo has no TypeScript (`tsconfig.json` / `package.json`) AND no Ruby (`Gemfile`) signals. Chameleon currently supports only those two languages.

## Related docs

- [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md) — Windows hook setup (when ready to test)
- [ARCHITECTURE.md](ARCHITECTURE.md) — design + invariants
- [tests/](tests/) — every test file is also documentation by example
