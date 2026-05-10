# Contributing

This document is for **developers working on chameleon itself**. For users of the plugin, see [README.md](README.md) and [INSTALL.md](INSTALL.md).

## Status

Solo-maintained open-source project (MIT). Issues and PRs are welcome — read this file end-to-end before opening one.

## Reporting bugs / requesting features

Use the GitHub issue templates:

- **Bug report** for incorrect behavior, crashes, or unexpected output.
- **Feature request** for new languages, new MCP tools, or new slash commands.

Before opening an issue, please search existing issues (open AND closed) for duplicates.

## Submitting a PR

1. Fork the repo, branch from `main`.
2. Read the relevant section below (skill change vs MCP tool change vs schema change).
3. Run the full test suite — see [Running tests](#running-tests).
4. Open a PR using the [PR template](.github/PULL_REQUEST_TEMPLATE.md). Fill in **every section** with real, specific answers.
5. Keep one problem per PR. Bundled unrelated changes will be sent back.

## Dev setup

### Prerequisites

- macOS or Linux. Windows via Git Bash; see [docs/windows/polyglot-hooks.md](docs/windows/polyglot-hooks.md).
- Python ≥ 3.11
- Node ≥ 20
- Ruby ≥ 3.0 with the `prism` gem
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management
- `jq` (for `scripts/bump-version.sh`)

### First-time setup

```bash
git clone https://github.com/crisnahine/chameleon
cd chameleon

# Python side (MCP server)
cd mcp
uv sync

# Verify
.venv/bin/python -c "from chameleon_mcp.server import mcp; print('mcp ok')"
node ../scripts/ts_dump.mjs <<< /dev/null  # exits cleanly with no input
```

### Running tests

```bash
# Full suite, four randomized orderings
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py

# Real-Claude-Code acceptance (~$0.20 per run)
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/claude_code_acceptance_test.py

# Bash-driven skill triggering (~$0.35 per run)
bash tests/skill_triggering_test.sh
```

Real-Claude-Code tests require `CHAMELEON_TEST_TS_REPO` and/or `CHAMELEON_TEST_RUBY_REPO` env vars (see `.env.example`). Tests skip when the env is missing.

## How to make a change

### Architecture changes

For changes that touch more than one of: hook stack, MCP tool surface, profile schema, skill bodies — **author an ADR first**. ADRs live at `docs/chameleon/decisions/`. Use `0000-template.md` as the starting point.

The ADR documents the *decision*, not the *implementation*. Once accepted, the implementation can proceed.

### Skill changes

Skill bodies are behavior-shaping prose. Don't tweak them lightly.

Procedure:
1. Author adversarial pressure scenarios for the skill in question.
2. Run them WITHOUT your change — document the failures.
3. Write/edit the skill body addressing those specific rationalizations.
4. Re-run scenarios WITH the change — verify compliance across 5 sessions.
5. Iterate until the failure mode doesn't reappear.

### MCP tool changes

MCP tools are an API surface (per [ARCHITECTURE.md](ARCHITECTURE.md)). Treat changes per the API compatibility contract:

**Non-breaking** (no major version bump):
- Adding new optional input fields with safe defaults
- Adding new optional output fields
- Adding new MCP tools
- Loosening a validation rule

**Breaking** (major version bump required):
- Renaming a tool
- Removing a tool
- Reordering positional arguments
- Tightening a validation rule
- Changing the type or meaning of an existing field

### Schema changes

- Non-breaking → ship.
- Breaking → bump `PROFILE_SCHEMA_VERSION`, write a migration script with a fixture pair, and write an ADR explaining why.

## Commit conventions

- **Subject line**: 50 chars max, imperative mood ("Add X" not "Added X").
- **Body**: explain *why*, not *what* (the diff shows what).
- **Reference issues / ADRs**: `Closes #42` / `Per ADR-0003`.

## Decision-making

Solo maintainer. Architecture changes captured in ADRs. The maintainer reserves the right to decline PRs that don't fit the project's design goals — please open an issue first for any non-trivial change so we can agree on the approach before you invest time.

## Data handling

- Never commit hardcoded credentials.
- Profile artifacts in `.chameleon/` may contain code excerpts; treat as sensitive even though committed.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
