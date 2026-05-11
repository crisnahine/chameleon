# MAINTAINER.md

Operational runbook for the chameleon plugin maintainer.

This document is for the **maintainer of chameleon itself**, not users of
the plugin. For user-facing setup, see `README.md`. For development setup,
see `CONTRIBUTING.md`.

## Quarterly tasks

Calendar reminder: first Monday of each quarter.

### 1. Dependency bump checklist

For each pinned dependency, verify upstream signatures + diff file lists +
regenerate checksums.

```bash
# TypeScript (mcp/node_modules/typescript)
cd mcp
npm audit signatures
npm outdated typescript
# If bump approved:
npm install typescript@<new-version>
diff -r node_modules/typescript.bak node_modules/typescript | grep -E "^[<>]" | head -50
# Generate fresh SHA-256 checksums
find node_modules/typescript -type f -exec shasum -a 256 {} \; > typescript-checksums.json.new
mv typescript-checksums.json.new typescript-checksums.json
git diff typescript-checksums.json
```

For FastMCP and detect-secrets:

```bash
cd mcp
uv lock --upgrade-package mcp
uv lock --upgrade-package detect-secrets
# Review the diff in uv.lock; verify no unexpected transitive deps
git diff uv.lock | head -100
```

### 2. Calibration review

Run the calibration harness against the internal dogfood corpus + 3 representative
OSS TS repos. Update parameters if measured correlation < 0.5.

Parameters with calibration targets (per `ARCHITECTURE.md#calibration-targets`):
- `recency_weight` (currently 2× for last 90 days)
- `recency_window_days` (currently 90)
- `confidence_function` weights (currently 0.4 / 0.3 / 0.3)
- `cluster_size_log` base (currently natural log)
- `min_cluster_size` (currently 5)
- `bimodal_threshold` (currently 60/40)
- `repo_size_guard` (currently 50,000 files)
- `ast_node_ceiling` (currently 50,000 nodes)
- `MCP timeout` (currently 2 seconds)

Procedure:
1. Run bootstrap on each corpus repo
2. Have a human reviewer label each canonical with "should be in archetype X" / "shouldn't be"
3. Compute precision + recall per archetype
4. If precision or recall < 0.5 on any parameter's affected dimension, propose a calibration update via ADR

### 3. Quarterly model re-baseline

Whenever Anthropic ships a new Sonnet or Opus version, re-run all skill
pressure scenarios:

```bash
bash tests/skill_triggering_test.sh
```

If any rationalizations are not in the existing skill body's Red Flags
table, capture them verbatim and add via PR. Bump `engine_min_version`
in `plugin.json` after CI confirms the regression results.

### 4. HMAC key inspection

Per ARCHITECTURE.md security mitigations, the HMAC key at
`~/.claude/hooks/.exec_hmac.key` is per-user, not per-session. There's no
automated rotation. Recommend manual rotation annually or on suspected
compromise:

```bash
rm ~/.claude/hooks/.exec_hmac.key  # next chameleon session regenerates
```

## Schema migration authoring

When adding fields to `profile.json`, `archetypes.json`, `rules.json`, or
`canonicals.json`:

### Non-breaking changes (no migration needed)

- Adding new optional fields with safe defaults
- Adding new archetype patterns to a profile
- Loosening a validation rule

### Breaking changes (migration required)

- Renaming an existing field
- Changing the type of an existing field
- Removing a field
- Tightening a validation rule

Procedure for breaking changes:

1. Bump `PROFILE_SCHEMA_VERSION` in `mcp/chameleon_mcp/bootstrap/orchestrator.py`
2. Bump `CURRENT_SCHEMA_VERSION` in `mcp/chameleon_mcp/profile/schema.py`
3. Author migration script at `mcp/chameleon_mcp/profile/migrations/v<old>_to_v<new>.py`
4. Per migration correctness contract (ARCHITECTURE.md):
   - Idempotent
   - Round-trip preserved (or document irreversible)
   - Atomic via `bootstrap.transaction.atomic_profile_commit`
   - No-op detection (already at target version)
   - Test fixture pair: `(input_v_<old>.json, expected_output_v_<new>.json)`
5. Update `SUPPORTED_SCHEMA_RANGE` in `schema.py`
6. Update `docs/chameleon/decisions/` with ADR explaining the breaking change

Example migration skeleton:

```python
# mcp/chameleon_mcp/profile/migrations/v4_to_v5.py
"""Migrate profile schema v4 → v5.

Reason: <one sentence>
Reversible: yes / no
"""

from __future__ import annotations
from pathlib import Path
from chameleon_mcp.bootstrap.transaction import atomic_profile_commit


def migrate(profile_dir: Path) -> None:
    # Load v4 artifacts
    # Transform to v5 shape
    # Write atomically via atomic_profile_commit
    pass


def can_migrate(profile_dir: Path) -> bool:
    """Idempotence + no-op detection: True iff profile is at v4."""
    pass
```

## Release checklist

Before tagging `v1.0.0` (or any semver-stable):

- [ ] Full test suite passes: `cd mcp && PYTHONPATH=. .venv/bin/python ../tests/run_all_orders.py`
- [ ] Real Claude Code acceptance: `cd mcp && PYTHONPATH=. .venv/bin/python ../tests/claude_code_acceptance_test.py`
- [ ] Bash skill-triggering smoke: `bash tests/skill_triggering_test.sh`
- [ ] All ADRs reviewed for staleness; superseded ones marked
- [ ] CHANGELOG.md updated with Unreleased → vX.Y.Z section
- [ ] `scripts/bump-version.sh --check` reports all manifests in sync
- [ ] README.md and INSTALL.md reviewed for stale references

## Threat model

document the threat model in
`docs/chameleon/THREAT-MODEL.md` (Phase 7-end). Key concerns:

- Adversarial OSS repos shipping malicious `.chameleon/` profiles (defense:
  trust prompt + canonical injection scanner)
- Insider profile poisoning via PR (defense: profile.summary.md for human
  review + CI dangerous-pattern scanner)
- AI-as-interpreter prompt injection via canonical excerpts (defense:
  tag-boundary sanitization + content secret/injection scans)
- Supply chain attacks on vendored TypeScript / FastMCP / detect-secrets
  (defense: SHA-256 checksums, quarterly bumps with `npm audit signatures`)

## Bus factor and succession

Solo maintainer (Cris Nahine, crisjosephnahine@gmail.com). chameleon is
MIT-licensed; anyone may fork and continue the work at any time.

### Inactivity policy

If the primary maintainer is unreachable for **>30 days** on issues,
PRs, and email, the project enters **maintenance-only mode**:

- No new feature work is committed to `main`.
- Critical security fixes (CVEs, data-loss bugs) may be patched by
  any contributor whose PR passes the full test suite + at least one
  independent code review (open a PR; cite this policy in the body).
- The plugin marketplaces (Claude Code, Cursor, Codex, Gemini)
  continue to serve the last tagged release.

If unreachable for **>180 days**, the README and marketplace listings
should be amended to label the project **archived** until a new
maintainer is named.

### Becoming a maintainer

The project is open to a co-maintainer once one exists. Criteria:

- At least 3 merged non-trivial PRs (real features or audit-grade
  fixes, not docs typos).
- Demonstrated familiarity with `ARCHITECTURE.md` + `OVERVIEW.md` +
  the schema migration runbook above.
- Willingness to follow the test-and-review discipline (see
  `CONTRIBUTING.md`).

To propose yourself: open a GitHub Discussion with a one-paragraph
case + your PR history; the primary maintainer responds within 14
days. There is no formal vote — the primary maintainer's call.

### Handoff artifacts

The handoff bundle (for any successor) consists of:

- This repo (full git history).
- The plugin marketplace registration:
  https://github.com/crisnahine/chameleon (public, MIT).
- The contact email above (for marketplace-listing transfer requests
  with Anthropic / Cursor / Codex / Gemini).

There is no hidden infrastructure, no private SaaS, no auth keys.
A fork plus a marketplace-listing transfer is the entire handoff.

## Decision register

ADRs at `docs/chameleon/decisions/`. Pattern:
- `0001-best-effort-clustering-vs-framework-aware.md`
- `0002-companion-plugins-deferred.md`
- `0003-typescript-only-v1-ruby-v15.md`
- `0000-template.md` (start here for new ADRs)
