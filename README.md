# chameleon

> *"Code that blends in."*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-7C3AED.svg)](https://docs.claude.com/claude-code)

chameleon learns your repo's actual conventions and injects archetype-aware guidance per-edit, so AI-generated code matches your existing style on the first try. Supports TypeScript and Ruby on Rails.

## Quickstart

**Before you start**, you need `uv` and Node.js 20+ on your `PATH`. Ruby 3.0+ is also needed if you edit Rails repos. On a fresh machine, [docs/install.md](docs/install.md) has copy-paste setup for macOS, Linux, and Windows, plus a check for each tool. Skip ahead if you already have them.

**1. Install the plugin.** In any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Confirm it loaded by asking *"What chameleon tools do you have?"*

**2. Profile a repo.** Open a TypeScript or Ruby on Rails repo, then:

```
/chameleon-init    # build a profile (3-10s for repos under 5k files)
/chameleon-trust   # approve it for your user
```

After that, every Edit/Write in that repo gets archetype-aware context automatically.

Using Cursor, Codex CLI, or Gemini CLI instead? Install steps for each are in [docs/install.md](docs/install.md).

## Why

AI-generated code in established codebases routinely violates local conventions: wrong file location, off-pattern naming, missed team idioms, divergent error handling. Reviewer time gets spent on style and shape instead of logic and security.

chameleon clusters your actual code patterns (AST + statistical analysis), captures team-specific idioms (`/chameleon-teach`), and injects archetype-keyed guidance per-edit so the model writes code that fits.

## How it works

1. **Bootstrap.** `/chameleon-init` runs an AST scan, clusters files into archetypes by a 7-tuple signature, picks a canonical example per archetype, and writes `.chameleon/profile.json` — committed to git, team-shared.

2. **Trust.** `/chameleon-trust` is a per-user, per-repo approval gate. Same mental model as `git config --get user.signingkey`: the profile lives in the repo, the trust grant lives on your machine.

3. **Per-edit context.** Before every Edit/Write/NotebookEdit, the `PreToolUse` hook calls the chameleon MCP server, which returns the matched archetype's canonical excerpt, rules, and idioms. The hook injects `<chameleon-context>` into the model's context.

4. **Teach.** `/chameleon-teach` captures idioms an AST can't infer — banned imports, mandatory wrappers, custom HTTP clients, internal conventions. Persisted to `.chameleon/idioms.md` and surfaced through the trust gate so reviewers see them before granting trust.

5. **Drift detection.** Per-edit confidence is tracked in `~/.local/share/chameleon/<repo_id>/drift.db`. When the profile no longer matches reality, `/chameleon-status` escalates and recommends `/chameleon-refresh`.

Because the skills trigger automatically, you don't need to do anything special after install. Edits in a trusted, profiled repo just blend in.

## Install

The [Quickstart](#quickstart) above has the two commands for Claude Code. For the full guide - per-OS prerequisite setup (macOS, Linux, Windows), the other harnesses (Cursor, Codex, Gemini), verification, updating, uninstall, and troubleshooting - see **[docs/install.md](docs/install.md)**.

## Workflow

1. **`/chameleon-init`** — bootstrap a profile. Runs the AST scan, clusters files into archetypes, selects canonical examples, writes `.chameleon/profile.json`. Commit the result.

2. **`/chameleon-trust`** — review and approve the committed profile for your user. Asks you to type the repo's basename to confirm. Trust state lives at `~/.local/share/chameleon/<repo_id>/.trust`.

3. **Edit normally.** The `PreToolUse` hook looks up the target file's archetype, fetches the canonical excerpt + rules + active idioms, and injects them as `<chameleon-context>` before the edit lands. The model references the canonical example and writes code that fits.

4. **`/chameleon-teach`** — capture missed patterns as team idioms. Persists to `.chameleon/idioms.md` and survives `/chameleon-refresh`.

5. **`/chameleon-status`** — check profile state, drift score, and plugin health. High drift recommends `/chameleon-refresh` to re-cluster against the current code.

6. **`/chameleon-refresh`** — re-run the bootstrap after meaningful team changes. Existing idioms are preserved; the trust grant must be re-issued because the profile SHA changes.

## What's Inside

### Slash commands

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze and update profile after team changes |
| `/chameleon-status` | View profile state, drift score, plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |
| `/chameleon-doctor` | Run health checks on the chameleon installation |
| `/chameleon-journey` | Run the end-to-end journey test harness |

All commands accept `/cham-<name>` short aliases. `using-chameleon` is the tenth skill — it auto-fires on `SessionStart` and orients the model.

### Hooks

Five hooks drive the runtime:

- **SessionStart** — detects the repo, loads the profile, and announces archetype awareness to the model.
- **PreToolUse** — fires on Edit/Write/NotebookEdit; injects `<chameleon-context>` with the archetype's canonical excerpt, rules, and idioms.
- **PostToolUse (recorder)** — fires on Bash/Edit/Write/NotebookEdit; records drift signals for `/chameleon-status`.
- **PostToolUse (verify)** — fires on Edit/Write/NotebookEdit; runs archetype conformance lint on the written file.
- **UserPromptSubmit** — detects frustration phrases and surfaces disable/pause/teach options.

### MCP server

`chameleon-mcp` (Python, FastMCP, stdio transport) exposes 20 tools: `detect_repo`, `get_archetype`, `get_pattern_context`, `get_canonical_excerpt`, `get_rules`, `lint_file`, `get_drift_status`, `refresh_repo`, `bootstrap_repo`, `list_profiles`, `merge_profiles`, `teach_profile`, `teach_profile_structured`, `trust_profile`, `disable_session`, `pause_session`, `propose_archetype_renames`, `apply_archetype_renames`, `daemon_status`, `doctor`.

### Opt-out hierarchy

Most-permanent → most-temporary:

```
.chameleon/.skip          per-repo, all users (committed)
CHAMELEON_DISABLE=1       per-user globally (in your shell rc)
CHAMELEON_VERIFY=0        disable post-edit verification only
/chameleon-disable        this session only
/chameleon-pause-15m      next 15 minutes (auto-resume)
```

Set `CHAMELEON_ENFORCEMENT_MODE=additionalContext` to revert post-edit violations from `updatedToolOutput` (v0.7.0 default) to the v0.6.x advisory style.

## Philosophy

- **Evidence over assumption** — cluster what the repo actually does; don't assume a framework's defaults match this team's style.
- **Atomic and reviewable** — profile writes use a flock-serialized `COMMITTED` sentinel; the trust gate surfaces idioms verbatim so reviewers see what they're approving.
- **Fail closed** — the canonical-selection pipeline rejects candidates that fail secret, injection, or poisoning scans; the profile is shipped with no example before it is shipped with a poisoned one.
- **Opt-out at every layer** — four ways to silence the plugin, from committed repo flag to a 15-minute pause.

See [docs/architecture.md](docs/architecture.md) for the full design: bootstrap pipeline, cluster signature function, atomic profile commit, drift model, and security mitigations.

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for dev setup, test workflows, and the change conventions used in this repo. Contributors hacking on the plugin itself should use `--plugin-dir`, not the marketplace install above.

## License

MIT — see [LICENSE](LICENSE).

## Author

Cris Nahine — [crisjosephnahine@gmail.com](mailto:crisjosephnahine@gmail.com)
