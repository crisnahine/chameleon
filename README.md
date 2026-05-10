# chameleon

> *"Code that blends in."*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-7C3AED.svg)](https://docs.claude.com/claude-code)

A Claude Code plugin that learns your repo's actual conventions and injects archetype-aware guidance so AI-generated code matches your existing style on the first try.

## Why

AI-generated code in established codebases routinely violates local conventions: wrong file location, off-pattern naming, missed team idioms, divergent error handling. Reviewer time gets spent on style and shape, not logic and security.

chameleon clusters your actual code patterns (via AST + statistical analysis), captures team-specific idioms (via `/chameleon-teach`), and injects archetype-keyed guidance per-edit so Claude writes code that fits.

## How it works

1. **Bootstrap** — `/chameleon-init` runs an AST scan over your repo, clusters files into archetypes, picks canonical examples, and writes `.chameleon/profile.json` (committed to git; team-shared).
2. **Trust** — `/chameleon-trust` is a per-user, per-repo approval gate. Mirrors the `git config --get user.signingkey` mental model.
3. **Per-edit** — the `PreToolUse` hook calls the chameleon MCP server, which returns the archetype's canonical excerpt + rules + idioms. The hook injects `<chameleon-context>` into the model's context before each Edit/Write/NotebookEdit.
4. **Iterate** — `/chameleon-teach` captures idioms AST can't infer (banned imports, mandatory wrappers, custom HTTP clients, etc.).
5. **Drift detection** — per-edit confidence tracking surfaces when the profile no longer matches reality; the status command escalates to suggest `/chameleon-refresh`.

## Supported languages

- TypeScript (via the TypeScript Compiler API)
- Ruby on Rails (via the [Prism](https://github.com/ruby/prism) parser)

## Install

See [INSTALL.md](INSTALL.md) for the full setup. Quick path on macOS / Linux:

```sh
git clone https://github.com/crisnahine/chameleon
cd chameleon/mcp && uv sync && cd ..
claude --plugin-dir "$(pwd)"
```

In a Claude Code session inside any repo:

```
/chameleon-init    # bootstrap a profile (10s for ~5k files)
/chameleon-trust   # approve the profile for your user
```

After that, every Edit/Write in that repo gets archetype-aware context automatically.

## Slash commands

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze after team changes |
| `/chameleon-status` | View profile state, drift score, plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |

All commands accept `/cham-<name>` short aliases.

## Opt-out hierarchy

Most-permanent → most-temporary:

```
.chameleon/.skip          per-repo, all users (committed)
CHAMELEON_DISABLE=1       per-user globally (in your shell rc)
/chameleon-disable        this session only
/chameleon-pause-15m      next 15 minutes (auto-resume)
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design: bootstrap pipeline, cluster signature function, atomic profile commit pattern, security mitigations (sanitization, secret scanning, poisoning scanner), and the trust + drift model.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, test workflows, and the change conventions used in this repo.

## License

MIT — see [LICENSE](LICENSE).

## Author

Cris Nahine — [crisjosephnahine@gmail.com](mailto:crisjosephnahine@gmail.com)
