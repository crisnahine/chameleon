# Contributing

This document is for **developers working on chameleon itself**. For users
of the plugin, see `README.md`.

## Status

Private to Empire Flippers. External contributions not solicited at this
stage. (Decision pending: see ADR-0002 + open question in ARCHITECTURE.md.)

## Dev setup

### Prerequisites

- macOS or Linux (Windows via WSL2 supported but untested in v1)
- Python ≥ 3.11
- Node ≥ 20
- `uv` for Python dependency management (https://github.com/astral-sh/uv)
- `pnpm` ≥ 10 for the EF client repo testing

### First-time setup

```bash
git clone <chameleon repo>
cd chameleon

# Python side (MCP server)
cd mcp
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Node side (TS Compiler subprocess)
npm install

# Verify both halves
python -c "from chameleon_mcp.server import mcp; print('mcp ok')"
node ../scripts/ts_dump.mjs <<< /dev/null  # exits cleanly
```

### Running tests

```bash
# Unit tests (Python)
cd mcp && uv run pytest

# Skill-triggering behavioral tests (TBD Phase 3 + after Phase 5 dogfood)
tests/skill-triggering/run-all.sh

# Integration tests (TBD Phase 4)
tests/integration/run-all.sh

# Acceptance tests (TBD Phase 3)
tests/acceptance/run.sh
```

Currently CI gates documented in `docs/chameleon/MAINTAINER.md` are
aspirational — actual CI workflows arrive in Phase 7.

## How to make a change

### 1. Architecture changes

For changes that affect more than one of: hook stack, MCP tool surface,
profile schema, skill bodies — **author an ADR first**. ADRs live at
`docs/chameleon/decisions/` with the template at `0000-template.md`.

The ADR documents the *decision*, not the *implementation*. Once the ADR
is accepted, the implementation can proceed.

### 2. Skill changes

Per `superpowers:writing-skills` Iron Law: **NO SKILL WITHOUT A FAILING
TEST FIRST.**

Procedure:
1. Author pressure scenarios at `skills/<skill-name>/tests/baseline.md`
   capturing rationalizations agents use to skip the rule
2. Run scenarios WITHOUT the skill — document the failures verbatim
3. Write/edit the skill body addressing those specific rationalizations
4. Re-run scenarios WITH the updated skill — verify compliance
5. Iterate (REFACTOR phase) until rationalizations don't appear in 5 runs

CI enforces the presence of `tests/baseline.md` for every skill. PRs
that modify skill content without an updated baseline are blocked.

### 3. MCP tool changes

Tool signatures are an API surface (per `ARCHITECTURE.md#mcp-server`).
Treat changes per the API compatibility contract:

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
- Changing the type of an existing field
- Changing the meaning of an existing field

### 4. Schema changes

See `docs/chameleon/MAINTAINER.md` "Schema migration authoring" for the
detailed procedure. Short version:

- Non-breaking → ship
- Breaking → bump `PROFILE_SCHEMA_VERSION`, write migration script with
  test fixture pair, ADR explaining why

## Commit conventions

- **Subject line**: 50 chars max, imperative mood ("Add X" not "Added X")
- **Body**: explain *why*, not *what* (the diff shows what)
- **Reference issues / ADRs**: `Closes #42` / `Per ADR-0003`
- **Co-author trailer**: when AI-assisted, include `Co-Authored-By:
  Claude Opus 4.7 (1M context) <noreply@anthropic.com>` at the end

## Phase status

See `CHANGELOG.md` and `ARCHITECTURE.md#phase-plan` for current state.

The architecture is at v5 (post-Round-5 verification, 27 reviewer
perspectives). Implementation is in progress through Phase 4.

## Decision-making

Solo maintainer (Cris) decides. Architecture changes captured in ADRs.
External contributions through PR are subject to maintainer review.

## Ethics & data handling

- Never commit code that scrapes EF customer data
- Never commit hardcoded credentials (CI secret scanner enforces this)
- Profile artifacts in `.chameleon/` may contain code excerpts that
  include internal hostnames, employee names, customer references —
  treat as sensitive even though committed (Round 4/5 AppSec reviewer)
