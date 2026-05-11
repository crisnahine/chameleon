# Contributing to chameleon

This document is for **developers working on chameleon itself**. For users
of the plugin, see [README.md](README.md) and [INSTALL.md](INSTALL.md).
For the design, start with [docs/chameleon/OVERVIEW.md](docs/chameleon/OVERVIEW.md)
(5 minutes) before diving into [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

chameleon is open source under the [MIT License](LICENSE). It went public
on 2026-05-11 (v0.2.0). Solo-maintained by Cris Nahine. External issues
and PRs are welcome — read this file end-to-end before opening one.

By contributing, you agree your contributions are licensed under MIT.

## Reporting bugs / requesting features

Use the GitHub issue templates:

- **Bug report** — incorrect behavior, crashes, or unexpected output.
- **Feature request** — new languages, new MCP tools, new slash commands.

Search existing issues (open AND closed) for duplicates before opening one.
For anything that touches the architecture surface (hook stack, MCP tool
shape, profile schema, skill bodies), open an issue or discussion **before**
writing code — see [Architecture changes](#architecture-changes) below.

## Dev prerequisites

- macOS or Linux. Windows via Git Bash; see [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md).
- Python ≥ 3.11
- Node ≥ 20
- Ruby ≥ 3.0 with the `prism` gem (ships by default in Ruby ≥ 3.3)
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- `jq` (for `scripts/bump-version.sh`)

## First-time local setup

Local contributor install uses `claude --plugin-dir` — the only place this
project documents a local clone install. Marketplace users follow
[INSTALL.md](INSTALL.md).

```bash
git clone https://github.com/crisnahine/chameleon
cd chameleon/mcp && uv sync && npm install && cd ..

# Launch Claude Code with the working tree mounted as a plugin
claude --plugin-dir "$(pwd)"
```

Smoke-check the MCP server resolves cleanly:

```bash
cd mcp
.venv/bin/python -c "from chameleon_mcp.server import mcp; print('mcp ok')"
```

## Running tests

All test commands run from `mcp/` with `PYTHONPATH=.:../tests`.

| Suite | Command | Notes |
|---|---|---|
| Full suite, 4 randomized orderings | `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py` | Order-independence check. Run before every PR. |
| Comprehensive | `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/comprehensive_test.py` | Broad coverage; fastest single-file run. |
| MCP protocol | `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/mcp_protocol_test.py` | Tool surface contract. |
| v0.2 regression | `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_2_regression_test.py` | 25 assertions covering the v0.2.0 audit fixes. Don't regress. |
| Real Claude Code acceptance | `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/claude_code_acceptance_test.py` | Spends ~$0.20 per run. Required for PRs that touch hooks or skills. |
| Bash skill triggering | `bash tests/skill_triggering_test.sh` | Spends ~$0.35 per run. |

Real-Claude-Code tests need a TypeScript repo and/or a Ruby on Rails repo
to exercise. Set in `.env` (gitignored):

```
CHAMELEON_TEST_TS_REPO=/abs/path/to/typescript/repo
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/rails/repo
```

Tests skip gracefully when these are unset.

## How to make a change

### Architecture changes

For changes that touch more than one of: hook stack, MCP tool surface,
profile schema, skill bodies — **author an ADR first**. ADRs live at
[docs/chameleon/decisions/](docs/chameleon/decisions/). Use `0000-template.md`
as the starting point. Existing ADRs (0001–0003) are short, scoped, and
focused on the decision rather than the implementation; match that voice.

The ADR documents the *decision* and rejected alternatives, not the *code*.
Once accepted, the implementation can proceed.

### Hook stack changes

Hooks live in `hooks/`. Four hooks: `session-start`, `preflight-and-advise`,
`posttool-recorder`, `callout-detector`. They are subprocess-per-call today.

- Preserve the fail-open / fail-closed split: safety hard-denies block;
  advisory MCP calls fail open with a `<chameleon-context>` warning.
- Preserve the 2-second MCP timeout in `preflight-and-advise`. Increasing
  it slows every edit; decreasing it raises fail-open rate.
- Run `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/pretooluse_hook_test.py`
  and the real-Claude-Code acceptance suite.

Test a hook locally:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/to/file.ts"},"session_id":"test"}' \
  | CLAUDE_PLUGIN_ROOT="$(pwd)" hooks/preflight-and-advise
```

### MCP tool changes

MCP tools (`mcp/chameleon_mcp/tools.py`) are a public API surface.
Compatibility contract per [ARCHITECTURE.md](ARCHITECTURE.md):

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
`mcp/chameleon_mcp/bootstrap/orchestrator.py` and `CURRENT_SCHEMA_VERSION`
in `mcp/chameleon_mcp/profile/schema.py`.

- **Non-breaking** (additive only) → ship.
- **Breaking** → bump both version anchors, write a migration at
  `mcp/chameleon_mcp/profile/migrations/v<old>_to_v<new>.py` with a fixture
  pair `(input_v<old>.json, expected_output_v<new>.json)`, update
  `SUPPORTED_SCHEMA_RANGE`, write an ADR. See
  [docs/chameleon/MAINTAINER.md#schema-migration-authoring](docs/chameleon/MAINTAINER.md#schema-migration-authoring)
  for the full procedure. The migration MUST be idempotent, atomic, and
  no-op when already at target.

v0.2.0 bumped schema v4 → v5 (`paths_pattern` semantics changed in
`archetypes.json`). It's the most recent example to study.

### Skill changes

Skill bodies are behavior-shaping prose, not docs. Treat changes the way
you'd treat changes to a production rule engine.

Procedure:
1. Author adversarial pressure scenarios for the skill in question
   (time pressure, sunk cost, "I know this codebase already", etc.).
2. Run scenarios WITHOUT your change — document the failures verbatim.
3. Write/edit the skill body addressing those specific rationalizations.
4. Re-run scenarios WITH the change — verify compliance across ≥5 sessions.
5. Iterate until the failure mode doesn't reappear.

The acceptance bar is "the rationalization doesn't reappear under pressure,"
not "the prose reads cleanly." PRs that rewrite skill voice without
before/after eval evidence will be sent back.

## Commit conventions

- **Subject line**: 50 chars max, imperative mood ("Add X" not "Added X" or
  "Adds X").
- **Body**: explain *why*, not *what* (the diff shows what).
- **Reference issues / ADRs**: `Closes #42` / `Per ADR-0003`.
- **English only**: all code, comments, docs, error messages, commit
  messages are in English.

## Pull request conventions

1. Fork the repo, branch from `main`.
2. Read the section above that matches your change area.
3. Run the full test suite. Real-Claude-Code acceptance is required for
   PRs touching hooks, skills, or the MCP tool surface.
4. Open a PR using the [PR template](.github/PULL_REQUEST_TEMPLATE.md).
   Fill in every section with real, specific answers.
5. One problem per PR. Bundled unrelated changes will be sent back.
6. Update `CHANGELOG.md` under `## [Unreleased]` (add the section if
   missing). Match the v0.2.0 entry's style (severity-tagged for fixes,
   `### Breaking` for breaking changes).

## Version bumps

`scripts/bump-version.sh <new-version>` keeps seven manifest files in sync:
`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`.cursor-plugin/plugin.json`, `.codex-plugin/plugin.json`,
`gemini-extension.json`, `package.json`, `mcp/package.json`.

**Known gap:** the Python-side version anchors are NOT in the script's
declared list — `mcp/pyproject.toml` (`[project] version = ...`) and
`mcp/chameleon_mcp/__init__.py` (`__version__ = "..."`) drift unless
updated manually. Bump them in the same commit. Fixing the script to cover
both is a welcome PR.

Run `scripts/bump-version.sh --check` before tagging to catch drift.

## Continuous integration

Three workflows live under [.github/workflows/](.github/workflows/):

- **`ci.yml`** — fires on every PR against `main` and every push to `main`.
  Runs the Python test matrix (3.11 + 3.12 on Ubuntu + macOS), ruff lint,
  `bump-version.sh --check`, and a one-shot `hooks/session-start` smoke
  test. The real-Claude-Code acceptance suite is intentionally excluded.
- **`release.yml`** — fires on tag pushes matching `v*.*.*`. Verifies all
  seven manifests + `mcp/pyproject.toml` + `mcp/chameleon_mcp/__init__.py`
  agree with the tag, that `CHANGELOG.md` has an entry for the version,
  re-runs the full test matrix, builds a release tarball, and publishes
  a GitHub Release with the CHANGELOG entry as the body.
- **`real-claude-code-acceptance.yml`** — manual (`workflow_dispatch`)
  plus a weekly cron. Runs `tests/claude_code_acceptance_test.py`
  (~$0.20/run). Trigger manually from the Actions tab; requires the
  maintainer to have configured `CLAUDE_CODE_OAUTH_TOKEN`,
  `CHAMELEON_TEST_TS_REPO`, and `CHAMELEON_TEST_RUBY_REPO` secrets.
  Fails soft with a SKIP message when those secrets aren't present.

Workflow run logs live under the repo's Actions tab on GitHub.

## Decision-making

Solo maintainer. Architecture-touching changes are captured in ADRs.
The maintainer reserves the right to decline PRs that don't fit the
project's design goals — open an issue first for any non-trivial change
so we can agree on the approach before you invest time.

## Data handling

- Never commit hardcoded credentials.
- Profile artifacts in `.chameleon/` may contain code excerpts; treat as
  sensitive even though committed.
- `drift.db`, `index.db`, `value_attrib.db`, `.trust` files in
  `~/.local/share/chameleon/` are local-only. Never committed, never
  exfiltrated.

## License

MIT. See [LICENSE](LICENSE). By contributing, you grant the project the
right to distribute your contribution under MIT.
