# chameleon

> *"Code that blends in."*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-7C3AED.svg)](https://docs.claude.com/claude-code)

chameleon learns your repo's actual conventions and injects archetype-aware guidance on every edit, so AI-generated code matches your existing style on the first try. It supports TypeScript and Ruby on Rails.

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
/chameleon-init    # build a profile (a few seconds for repos under 5k files)
/chameleon-trust   # approve it for your user
```

After that, every Edit/Write in that repo gets archetype-aware context automatically.

## Why

AI-generated code in established codebases routinely violates local conventions: wrong file location, off-pattern naming, missed team idioms, divergent error handling. Reviewer time gets spent on style and shape instead of logic and security.

chameleon clusters your actual code patterns (AST plus statistical analysis), captures team-specific idioms (`/chameleon-teach`), and injects archetype-keyed guidance per edit so the model writes code that fits.

## How it works

1. **Bootstrap.** `/chameleon-init` locks the repo's production branch (auto-detected from the origin default branch; you are asked only when the signal is ambiguous), materializes that branch's tree from local git objects, runs an AST scan over it, clusters files into archetypes by their structural signature, picks a canonical example per archetype, and writes the `.chameleon/` profile, committed to git and team-shared. The profile reflects the production line no matter which feature branch you happen to have checked out.

2. **Trust.** `/chameleon-trust` is a per-user, per-repo approval gate. Same mental model as `git config --get user.signingkey`: the profile lives in the repo, the trust grant lives on your machine.

3. **Per-edit context.** Before every Edit/Write/NotebookEdit, the `PreToolUse` hook asks the chameleon MCP server for the matched archetype's canonical excerpt, rules, and idioms, then injects them as a `<chameleon-context>` block.

4. **Teach.** `/chameleon-teach` captures idioms an AST cannot infer: banned imports, mandatory wrappers, custom HTTP clients, internal conventions. They persist to `.chameleon/idioms.md` and surface through the trust gate so reviewers see them before granting trust.

5. **Drift detection.** Per-edit confidence is tracked in `~/.local/share/chameleon/<repo_id>/drift.db`. When the profile no longer matches reality, or the locked production branch's tip moves past the commit the profile was derived from, `/chameleon-status` escalates and recommends `/chameleon-refresh`.

Because the skills trigger automatically, you do not need to do anything special after install. Edits in a trusted, profiled repo just blend in.

## Install

The [Quickstart](#quickstart) above has the two commands for Claude Code. For the full guide (per-OS prerequisite setup for macOS, Linux, and Windows, verification, updating, uninstall, and troubleshooting) see **[docs/install.md](docs/install.md)**.

## Workflow

1. **`/chameleon-init`** bootstraps a profile. It runs the AST scan, clusters files into archetypes, selects canonical examples, and writes the `.chameleon/` profile. Commit the result.

2. **`/chameleon-trust`** reviews and approves the committed profile for your user. It asks you to type the repo's basename to confirm. Trust state lives at `~/.local/share/chameleon/<repo_id>/.trust`.

3. **Edit normally.** The `PreToolUse` hook looks up the target file's archetype, fetches the canonical excerpt plus rules plus active idioms, and injects them as `<chameleon-context>` before the edit lands. The model references the canonical example and writes code that fits.

4. **`/chameleon-teach`** captures missed patterns as team idioms. They persist to `.chameleon/idioms.md` and survive `/chameleon-refresh`.

5. **`/chameleon-status`** checks profile state, drift, enforcement calibration, and plugin health. High drift recommends `/chameleon-refresh`.

6. **`/chameleon-refresh`** re-derives the profile from the production branch's current tip. No need to checkout or pull that branch first: refresh reads your local `origin/<branch>` ref (after a default-on, non-interactive `git fetch` of that branch), materializes its tree, and re-derives, leaving your feature-branch checkout untouched. Tip unchanged means an instant noop. Existing idioms are preserved; the trust grant is re-issued only if your config asks for it, since the profile SHA changes.

## What's inside

### Slash commands

Thirteen user-invocable commands, plus the `using-chameleon` skill that auto-fires on `SessionStart` and orients the model.

| Command | Purpose |
|---|---|
| `/chameleon-init` | Bootstrap a new profile |
| `/chameleon-refresh` | Re-analyze and update the profile after drift or team changes |
| `/chameleon-status` | View profile state, drift, enforcement calibration, and plugin health |
| `/chameleon-teach` | Capture a missed pattern as a team idiom |
| `/chameleon-auto-idiom` | Derive novel team idioms from repo evidence (append-only, deduplicated against the profile) |
| `/chameleon-trust` | Approve a committed profile for your user |
| `/chameleon-disable` | Suppress chameleon for the rest of this session |
| `/chameleon-pause-15m` | Pause for 15 minutes (auto-resume) |
| `/chameleon-doctor` | Run health checks on the installation |
| `/chameleon-journey` | Run the end-to-end journey test harness |
| `/chameleon-pr-review` | Review a branch or PR against the repo's conventions and task intent |
| `/chameleon-receiving-code-review` | Apply reviewer feedback with judgment: verify claims, surface tradeoffs, decide and explain |
| `/chameleon-explain` | Reconstruct what chameleon knew and did about a file at its last edit, or drill into one enforcement rule |

### Hooks

Six hook scripts drive the runtime, wired across six Claude Code events:

- **SessionStart** (`session-start`) detects the repo, loads the profile, injects the convention primer, surfaces any drift banner, and runs the default-on auto-refresh.
- **PreToolUse** (`preflight-and-advise`) fires on Edit/Write/NotebookEdit; injects `<chameleon-context>` with the archetype's canonical excerpt, rules, and idioms, and applies the pre-write deny gates.
- **PostToolUse recorder** (`posttool-recorder`) fires on Bash/Edit/Write/NotebookEdit; records drift signals and the HMAC-signed Bash exec log, and re-lints single-target Bash file writes.
- **PostToolUse verify** (`posttool-verify`) fires on Edit/Write/NotebookEdit; runs archetype conformance lint on the written file.
- **UserPromptSubmit** (`callout-detector`) captures checkable intent tokens for the turn-end review and surfaces disable/pause/teach options when it detects frustration.
- **Stop and SubagentStop** (`stop-backstop`) runs the turn-end gates: the enforcement backstop, the idiom review, and the advisory reviewers (correctness judge, duplication, cross-file existence, and more).

### MCP server

`chameleon-mcp` (Python, FastMCP, stdio transport) exposes 41 tools. The hooks call them for you; you rarely call them directly. They group into:

- **Detection and context:** `detect_repo`, `get_archetype`, `get_pattern_context`, `get_canonical_excerpt`, `get_rules`, `lint_file`.
- **Lifecycle:** `bootstrap_repo`, `refresh_repo`, `list_profiles`, `merge_profiles`, `propose_archetype_renames`, `apply_archetype_renames`.
- **Teaching:** `teach_profile`, `teach_profile_structured`, `teach_competing_import`, `unteach_competing_import`, `get_idiom_coverage`, `check_idiom_candidates`, `get_drift_antipatterns`.
- **Trust and opt-out:** `trust_profile`, `disable_session`, `pause_session`.
- **Observability:** `get_status`, `get_drift_status`, `get_shadow_report`, `get_override_audit`, `get_longitudinal_signals`, `daemon_status`, `doctor`, `explain_edit`.
- **Review gate:** `get_autopass_verdict`, `get_crossfile_context`, `query_symbol_importers`, `get_callers`, `get_contract_breaks`, `get_duplication_candidates`, `scan_dependency_changes`, `dep_audit`, `refute_finding`, `record_review_verdict`, `get_review_history`.

The full per-tool reference lives in [docs/architecture.md](./docs/architecture.md#mcp-server-chameleon-mcp).

### Opt-out hierarchy

Most-permanent to most-temporary:

```
.chameleon/.skip          per-repo, all users (committed)
CHAMELEON_DISABLE=1       per-user globally (in your shell rc)
CHAMELEON_VERIFY=0        disable post-edit verification only
/chameleon-disable        this session only
/chameleon-pause-15m      next 15 minutes (auto-resume)
```

## Philosophy

- **Evidence over assumption.** Cluster what the repo actually does; never assume a framework's defaults match this team's style.
- **Atomic and reviewable.** Profile writes use a flock-serialized `COMMITTED` sentinel; the trust gate surfaces idioms verbatim so reviewers see what they are approving.
- **Fail open on advisories, fail closed on safety.** The canonical-selection pipeline rejects candidates that fail secret, injection, or poisoning scans, so the profile ships with no example before it ships a poisoned one; when the advisor is unreachable, the edit still proceeds.
- **Opt out at every layer.** Five ways to silence the plugin, from a committed repo flag to a 15-minute pause.

See [docs/architecture.md](docs/architecture.md) for the full design: the bootstrap pipeline, the cluster signature function, the atomic profile commit, the drift model, the enforcement gate, and the security model.

## Precision

The loudest complaint about AI code review is noise: false positives and nitpick fatigue. chameleon is built the other way. Most feedback is advisory and shapes the code without ever blocking. A rule blocks only after calibration proves it flags near-zero of the repo's own committed files, and `/chameleon-pr-review` findings pass an independent round-3 refuter that drops anything it cannot ground, so a green review means something.

That precision is measured, not asserted. `/chameleon-status` surfaces the calibration headline: how many block rules are active and the false-positive ceiling they clear against this repo's own committed code. A separate live `overrides` axis tracks real-world contention on actual edits, so a rule that calibrates clean but fights the team stays visible.

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for dev setup, test workflows, and the change conventions used in this repo. Contributors hacking on the plugin itself should use `--plugin-dir`, not the marketplace install above.

## License

MIT. See [LICENSE](LICENSE).

## Author

Cris Nahine, [crisjosephnahine@gmail.com](mailto:crisjosephnahine@gmail.com)
