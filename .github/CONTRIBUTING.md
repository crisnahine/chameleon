# Contributing to chameleon

This document is for **developers working on chameleon itself**. For users
of the plugin, see [README.md](../README.md) and [INSTALL.md](../docs/install.md).
For the design, see [architecture.md](../docs/architecture.md).

## Status

chameleon is open source under the [MIT License](../LICENSE). It went public
on 2026-05-11 (v0.2.0). Solo-maintained by Cris Nahine. External issues
and PRs are welcome - read this file end-to-end before opening one.

By contributing, you agree your contributions are licensed under MIT.

## Reporting bugs / requesting features

Use the GitHub issue templates:

- **Bug report** - incorrect behavior, crashes, or unexpected output.
- **Feature request** - new languages, new MCP tools, new slash commands.

Search existing issues (open AND closed) for duplicates before opening one.
For anything that touches the architecture surface (hook stack, MCP tool
shape, profile schema, skill bodies), open an issue or discussion **before**
writing code - see [Architecture changes](#architecture-changes) below.

## Dev prerequisites

- macOS or Linux. Windows via Git for Windows or WSL2 (the lock layer is cross-platform; `bash` is required for the hooks).
- Python >= 3.11
- Node >= 20
- Ruby >= 3.0 with the `prism` gem (ships by default in Ruby >= 3.3)
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- `jq` (for `scripts/bump-version.sh`)

## First-time local setup

Local contributor install uses `claude --plugin-dir` - the only place this
project documents a local clone install. Marketplace users follow
[INSTALL.md](../docs/install.md).

```bash
git clone https://github.com/crisnahine/chameleon
cd chameleon
plugin/scripts/setup.sh --dev    # verify prerequisites + warm Python/Node deps (incl. pytest, ruff)

# Launch Claude Code with the working tree's plugin surface mounted as a plugin
claude --plugin-dir "$(pwd)/plugin"
```

`plugin/scripts/setup.sh --dev` replaces the older `cd plugin/mcp && uv sync && npm install` one-liner. The `--dev` flag matters: a plain `uv sync` installs runtime deps only and prunes the dev extras, leaving you without pytest and ruff. Run `plugin/scripts/setup.sh --check` any time to re-verify your toolchain.

Smoke-check the MCP server resolves cleanly:

```bash
cd plugin/mcp
.venv/bin/python -c "from chameleon_mcp.server import mcp; print('mcp ok')"
```

## Running tests

| Suite | Command | Notes |
|---|---|---|
| Unit tests | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/unit/ -v` | Fast, no external deps. Run before every PR. |
| Harness library self-tests | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/journey/harness/tests/ -v` | Verifies the journey harness itself. |
| Effectiveness eval self-tests | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/effectiveness/tests/ -v` | Verifies the effectiveness eval harness. CI's `test-python` job runs these alongside unit + harness tests. |
| Journey harness (full) | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner` | Drives real `claude -p` subprocesses. ~$33, ~65 min. |
| Journey harness (dry-run) | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner --dry-run` | Preflight only, no Claude spawn. |
| Journey harness (list acts) | `PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner --list` | Lists available acts. |

All commands run from the repo root.

## How to make a change

### Architecture changes

For changes that touch more than one of: hook stack, MCP tool surface,
profile schema, skill bodies - open a discussion or issue first to align
on approach. Document the *decision* and rejected alternatives in the
issue, not the code, before implementation proceeds.

### Hook stack changes

Hooks live in `plugin/hooks/`. Six hook scripts wired across six events:
`session-start` (SessionStart), `preflight-and-advise` (PreToolUse),
`posttool-recorder` and `posttool-verify` (PostToolUse), `callout-detector`
(UserPromptSubmit), and `stop-backstop` (Stop and SubagentStop). They are
subprocess-per-call today.

- Preserve the fail-open / fail-closed split: safety hard-denies block;
  advisory MCP calls fail open with a `<chameleon-context>` warning.
- Preserve the per-edit timeout budget in `preflight-and-advise`: the shell
  wrapper caps each hook at 3s and the daemon socket call defaults to 1.5s.
  Raising them slows every edit; lowering them raises the fail-open rate.
- Run `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/unit/ -v`
  and the journey harness dry-run (`PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner --dry-run`).

Test a hook locally:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/to/file.ts"},"session_id":"test"}' \
  | CLAUDE_PLUGIN_ROOT="$(pwd)/plugin" plugin/hooks/preflight-and-advise
```

### MCP tool changes

MCP tools are a public API surface. The 46 `@mcp.tool()` registrations (the
public surface) live in `plugin/mcp/chameleon_mcp/server.py`;
`plugin/mcp/chameleon_mcp/tools.py` holds the implementations `server.py` wraps.
Compatibility contract per [architecture.md](../docs/architecture.md):

**Non-breaking** (no major version bump):
- Adding new MCP tools
- Adding new optional input fields with safe defaults
- Adding new optional output fields
- Loosening a validation rule

**Breaking** (major version bump required):
- Renaming a tool
- Removing a tool
- Reordering positional arguments
- Tightening a validation rule
- Changing the type or meaning of an existing field

For breaking changes: ADR + major bump + migration notes in `CHANGELOG.md`
under a `### Breaking` heading (mirror the v0.2.0 entry).

### Profile schema changes

Schema files: `archetypes.json`, `rules.json`, `canonicals.json`,
`profile.json`. Schema version anchors: `PROFILE_SCHEMA_VERSION` in
`plugin/mcp/chameleon_mcp/bootstrap/orchestrator.py` and `CURRENT_SCHEMA_VERSION`
in `plugin/mcp/chameleon_mcp/profile/schema.py`.

- **Non-breaking** (additive only) -> ship.
- **Breaking** -> bump both version anchors, write a migration at
  `plugin/mcp/chameleon_mcp/profile/migrations/v<old>_to_v<new>.py` with a fixture
  pair `(input_v<old>.json, expected_output_v<new>.json)`, bump
  `MAX_SUPPORTED_SCHEMA_VERSION` in `plugin/mcp/chameleon_mcp/profile/loader.py`,
  and document the decision in an issue. The
  migration MUST be idempotent, atomic, and no-op when already at target.

No migration scripts exist yet: the engine is at `schema_version` 8 and
every bump so far has been load-compatible (an older-schema profile still
loads and re-bootstrap re-derives it). The first migration script gets
authored when a breaking bump actually needs one. For the CHANGELOG.md
format on a breaking change, see the v0.2.0 entry.

### Skill changes

Skill bodies are behavior-shaping prose, not docs. Treat changes the way
you'd treat changes to a production rule engine.

Procedure:
1. Author adversarial pressure scenarios for the skill in question
   (time pressure, sunk cost, "I know this codebase already", etc.).
2. Run scenarios WITHOUT your change - document the failures verbatim.
3. Write/edit the skill body addressing those specific rationalizations.
4. Re-run scenarios WITH the change - verify compliance across >=5 sessions.
5. Iterate until the failure mode doesn't reappear.

The acceptance bar is "the rationalization doesn't reappear under pressure,"
not "the prose reads cleanly." PRs that rewrite skill voice without
before/after eval evidence will be sent back.

## Commit conventions

- **Subject line**: imperative mood ("Add X" not "Added X" or "Adds X"),
  aim for concise.
- **Body**: explain *why*, not *what* (the diff shows what).
- **Reference issues**: `Closes #42`.
- **English only**: all code, comments, docs, error messages, commit
  messages are in English.

## Pull request conventions

1. Fork the repo, branch from `main`.
2. Read the section above that matches your change area.
3. Run the full test suite. Real-Claude-Code acceptance is required for
   PRs touching hooks, skills, or the MCP tool surface.
4. Open a PR using the [PR template](PULL_REQUEST_TEMPLATE.md).
   Fill in every section with real, specific answers.
5. One problem per PR. Bundled unrelated changes will be sent back.
6. Update `CHANGELOG.md` under `## [Unreleased]` (add the section if
   missing). Match the v0.2.0 entry's style (severity-tagged for fixes,
   `### Breaking` for breaking changes).

## Version bumps

`scripts/bump-version.sh <new-version>` keeps six manifest files in sync
(see `.version-bump.json` for the full list): `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `package.json`,
`plugin/mcp/package.json`, `plugin/mcp/pyproject.toml`, `plugin/mcp/chameleon_mcp/__init__.py`.

Run `scripts/bump-version.sh --check` before tagging to catch drift.

## Continuous integration

Four workflows live under [.github/workflows/](workflows/):

- **`ci.yml`** - fires on every PR against `main` and every push to `main`.
  Ten jobs: the Python test matrix (3.11, 3.12, and 3.13 on Ubuntu and macOS,
  running unit + harness + effectiveness tests), `test-windows` (native Windows
  import + cross-platform locking, py3.11-3.13), `runtime-windows` (drives the
  hook stack plus a bootstrap/trust/refresh lifecycle on native Windows), ruff
  lint, `bump-version.sh --check`, `check-no-personal-paths.sh`, a `hook-smoke`
  matrix over 5 hooks (`session-start`, `preflight-and-advise`,
  `posttool-recorder`, `posttool-verify`, `callout-detector`) each fed synthetic
  stdin and asserted to emit valid JSON, `dependency-audit` (`pip-audit` +
  `npm audit`), `shellcheck`, and `hooks-manifest-check`.
- **`release.yml`** - fires on tag pushes matching `v*.*.*`. Verifies all
  six manifests agree with the tag, that `CHANGELOG.md` has an entry for
  the version, re-runs the full test matrix, builds a release tarball, and
  publishes a GitHub Release with the CHANGELOG entry as the body.
- **`real-claude-code-acceptance.yml`** - manual (`workflow_dispatch`)
  plus a weekly cron. Runs `tests.journey.runner --dry-run` (preflight
  check). Trigger manually from the Actions tab; requires the maintainer
  to have configured `CLAUDE_CODE_OAUTH_TOKEN`, `CHAMELEON_TEST_TS_REPO`,
  and `CHAMELEON_TEST_RUBY_REPO` secrets. Fails soft with a SKIP message
  when those secrets aren't present.
- **`calibration.yml`** - manual only (`workflow_dispatch`). Runs the
  calibration harness against a corpus of repos to measure parameter
  defaults. Requires `CHAMELEON_CALIBRATION_CORPUS_JSON` secret; without
  it, emits `no_corpus_configured` and exits cleanly.

Workflow run logs live under the repo's Actions tab on GitHub.

## Decision-making

Solo maintainer. The maintainer reserves the right to decline PRs that
don't fit the project's design goals - open an issue first for any
non-trivial change so we can agree on the approach before you invest time.

## Data handling

- Never commit hardcoded credentials.
- Profile artifacts in `.chameleon/` may contain code excerpts; treat as
  sensitive even though committed.
- `drift.db`, `index.db`, `.trust` files in
  `~/.local/share/chameleon/` are local-only. Never committed, never
  exfiltrated.

## License

MIT. See [LICENSE](../LICENSE). By contributing, you grant the project the
right to distribute your contribution under MIT.
