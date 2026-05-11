# chameleon

> *"Code that blends in."*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-7C3AED.svg)](https://docs.claude.com/claude-code)

chameleon learns your repo's actual conventions and injects archetype-aware guidance per-edit, so AI-generated code matches your existing style on the first try. Supports TypeScript and Ruby on Rails.

## Quickstart

Install chameleon for your harness: [Claude Code](#claude-code), [Cursor](#cursor), [Codex CLI](#codex-cli), [Gemini CLI](#gemini-cli).

Then, inside any TypeScript or Ruby on Rails repo:

```
/chameleon-init    # bootstrap a profile (3–10s for repos under 5k files)
/chameleon-trust   # approve the profile for your user
```

After that, every Edit/Write in that repo gets archetype-aware context automatically.

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

Install differs by harness. If you use more than one, install separately for each.

**Prerequisites (all harnesses):** [uv](https://docs.astral.sh/uv/), Node.js ≥ 20. Ruby ≥ 3.0 with the `prism` gem is only needed for Ruby on Rails repos (`prism` ships by default in Ruby ≥ 3.3).

Both the Python MCP server and the Node-based TypeScript extractor are resolved automatically. `.mcp.json` invokes `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp` (uv builds the venv on first launch, ~5–10s); the first `/chameleon-init` against a TypeScript repo lazy-runs `npm install` inside the plugin dir (~10s, one-time). No `uv sync` or `npm install` to run by hand.

### Claude Code

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Verify by asking *"What chameleon tools do you have?"*

See [INSTALL.md](INSTALL.md) for the deep walkthrough, troubleshooting, and uninstall instructions.

### Cursor

In Cursor Agent chat:

```
/add-plugin chameleon
```

> Pending listing on Cursor's plugin marketplace.

### Codex CLI

Open the plugin search interface and install:

```
/plugins
```

Search for `chameleon`, then select **Install Plugin**.

> Pending listing on Codex's plugin marketplace.

### Gemini CLI

```sh
gemini extensions install https://github.com/crisnahine/chameleon
```

Update later:

```sh
gemini extensions update chameleon
```

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

All commands accept `/cham-<name>` short aliases. `using-chameleon` is the eighth skill — it auto-fires on `SessionStart` and orients the model.

### Hooks

Four hooks drive the runtime:

- **SessionStart** — detects the repo, loads the profile, and announces archetype awareness to the model.
- **PreToolUse** — fires on Edit/Write/NotebookEdit; injects `<chameleon-context>` with the archetype's canonical excerpt, rules, and idioms.
- **PostToolUse** — fires on Bash; records drift signals for `/chameleon-status`.
- **UserPromptSubmit** — surfaces profile state and any active opt-out at the start of each turn.

### MCP server

`chameleon-mcp` (Python, FastMCP, stdio transport) exposes 15 tools: `detect_repo`, `get_archetype`, `get_pattern_context`, `get_canonical_excerpt`, `get_rules`, `lint_file`, `get_drift_status`, `refresh_repo`, `bootstrap_repo`, `list_profiles`, `merge_profiles`, `teach_profile`, `trust_profile`, `disable_session`, `pause_session`.

### Opt-out hierarchy

Most-permanent → most-temporary:

```
.chameleon/.skip          per-repo, all users (committed)
CHAMELEON_DISABLE=1       per-user globally (in your shell rc)
/chameleon-disable        this session only
/chameleon-pause-15m      next 15 minutes (auto-resume)
```

## Philosophy

- **Evidence over assumption** — cluster what the repo actually does; don't assume a framework's defaults match this team's style.
- **Atomic and reviewable** — profile writes use a flock-serialized `COMMITTED` sentinel; the trust gate surfaces idioms verbatim so reviewers see what they're approving.
- **Fail closed** — the canonical-selection pipeline rejects candidates that fail secret, injection, or poisoning scans; the profile is shipped with no example before it is shipped with a poisoned one.
- **Opt-out at every layer** — four ways to silence the plugin, from committed repo flag to a 15-minute pause.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design: bootstrap pipeline, cluster signature function, atomic profile commit, drift model, and security mitigations.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, test workflows, and the change conventions used in this repo. Contributors hacking on the plugin itself should use `--plugin-dir`, not the marketplace install above.

## License

MIT — see [LICENSE](LICENSE).

## Author

Cris Nahine — [crisjosephnahine@gmail.com](mailto:crisjosephnahine@gmail.com)
