# Chameleon — Full-Matrix Real-Usage Test Campaign

**Status:** IN PROGRESS — Phase 1 (inventory + environment)
**Branch:** `plugin-testing-fixes`
**Baseline commit:** `27fd8d3` (Release v4.4.15) — clean tree, no uncommitted changes
**Plugin version under test:** 4.4.15
**Started:** 2026-07-18

This file is the source of truth for the campaign. On any interruption, compaction, or
restart: re-read this file and `git log --oneline`, then resume from the first cell that is
not marked PASS with evidence. Never restart the campaign. Never mark a cell PASS without
fresh evidence captured in this run.

---

## 1. Environment

Verified on 2026-07-18, host `darwin 25.5.0` (arm64, Apple Silicon).

| Component | Version | Status | Notes |
|---|---|---|---|
| macOS / kernel | Darwin 25.5.0 | OK | arm64 |
| git | 2.50.1 (Apple Git-155) | OK | |
| node | v22.22.3 | OK | TypeScript/JS extractor host (`ts_dump.mjs`) |
| npm | 10.9.8 | OK | |
| pnpm | 11.12.0 | OK | |
| npx | present | OK | scaffolding |
| ruby | 3.4.9 (+PRISM) | OK | Ruby extractor host (`prism_dump.rb`) |
| prism gem | 1.9.0 / 1.5.2 (default) / 1.4.0 | OK | Ruby AST parser |
| bundler | 4.0.15 | OK | |
| rails | 8.1.3 | OK | Rails cell scaffolding |
| python3 (system) | 3.9.6 | BELOW FLOOR | `/usr/bin/python3`; below chameleon's >=3.11 floor — exercises the `_resolve-python.sh` ladder rather than blocking |
| plugin venv python | 3.13.13 | OK | `plugin/mcp/.venv/bin/python` — rung 1 of the interpreter ladder |
| uv / uvx | 0.11.7 | OK | MCP server launcher (`.mcp.json` uses `uvx`) |
| uv-managed pythons | 3.11.15, 3.12.13, 3.13.13 | OK | rung 2/3 of the ladder |
| sqlite3 | 3.51.0 | OK | `drift.db`, `index.db` |
| claude CLI | 2.1.214 | OK | real slash-command / hook invocation |
| network | reachable (npm registry 200) | OK | dependency scaffolding only; chameleon itself is offline |
| free disk | 95 GiB | OK | |

**BLOCKED items:** none. Every runtime and toolchain the plugin requires is installed and
working.

**Environment note (not a blocker, but under test):** the system `python3` is 3.9.6, below
the plugin's documented `>=3.11` floor. `plugin/hooks/_resolve-python.sh` exists precisely
for this and resolves via a validated ladder (bundled venv -> version-named binaries -> `uv
run` -> probed `python3`). This host therefore exercises the ladder's rung-1 path for real,
which is a feature of this environment, not a gap. Ladder behaviour is itself a matrix item.

### How the plugin under test is actually loaded (fix-deploy protocol)

Verified end-to-end, not assumed. The plugin that really executes in a Claude Code session is
**not** the dev working tree. There are **three** hops:

| Hop | Path | Role | State at campaign start |
|---|---|---|---|
| 1. Dev tree | `/Users/crisn/Documents/Projects/chameleon` | where fixes are authored | branch `plugin-testing-fixes` @ `16a0638` |
| 2. Marketplace clone | `~/.claude/plugins/marketplaces/chameleon` | install source | branch `main` @ `27fd8d3`, clean |
| 3. **Version-keyed cache** | `~/.claude/plugins/cache/chameleon/chameleon/4.4.15/` | **what hooks + MCP actually execute** | materialized from hop 2 |

Hop 3 was confirmed by a real `chameleon_telemetry(action="doctor")` call, which reported the
hook interpreter as:

```
hooks resolve `uv run --project /Users/crisn/.claude/plugins/cache/chameleon/chameleon/4.4.15/mcp python`
```

The cache directory is keyed by the version string in `plugin.json` — 46 historical version
dirs are present (`2.39.0` … `4.4.15`). At campaign start all three copies of
`secret_scanner.py` are byte-identical (`diff -q` clean), confirming the chain is in sync.

**Consequence — the single most important operational rule of this campaign:** editing
`plugin/` in the dev tree changes *nothing* a hook, skill, or MCP tool does. A fix only
reaches the running plugin after it is committed, propagated to the marketplace clone, **and
given a new version** so a fresh cache dir is materialized. A campaign that skipped this
would test v4.4.15 while believing it had tested its own fixes, and every post-fix "green"
would be false. This is also why the project's own `CLAUDE.md` says *"Always bump the version
— the plugin cache is version-keyed."*

Mandatory protocol after every fix cycle:

1. Commit the fix in the dev tree on `plugin-testing-fixes`.
2. `scripts/bump-version.sh <new-version>` (keeps the six manifests in sync).
3. Propagate to the marketplace clone:
   `git -C ~/.claude/plugins/marketplaces/chameleon fetch /Users/crisn/Documents/Projects/chameleon plugin-testing-fixes && git -C ~/.claude/plugins/marketplaces/chameleon reset --hard FETCH_HEAD`
4. Materialize/refresh the version-keyed cache dir for the new version.
5. Clear `~/.local/share/chameleon/interp.cache` when the interpreter ladder is touched.
6. **Assert** the running copy matches the dev tree before re-running any cell — a fix is
   never marked green against a stale plugin.

`scripts/qa-deploy.sh` (added by this campaign, dev-tooling only) implements steps 2-4 so a
cell can never be re-run against a stale plugin.

### Test workspace

Fresh repos are built under `~/Documents/Projects/chameleon-fullmatrix-qa/`, one per
language/framework cell, each `git init`-ed with a real initial commit and **no** `.chameleon/`
directory at start. The developer's own `~/.local/share/chameleon/` is never used as the
campaign's data dir; each run points `CHAMELEON_PLUGIN_DATA` at a campaign-scoped directory
so the host profile store stays untouched.

---

## 2. Inventory

_(populated in Phase 1 — see section 3 for the matrix)_

---

## 3. Coverage Matrix

_(populated in Phase 1)_

---

## 4. Gaps & Effectiveness Log

Running log. Every issue found during real usage, its impact, and its resolution.

### GAP-001 — `possible_aws_secret` fires on an ordinary file path in prose — OPEN

**Cell:** `secret-scan` x (language-agnostic; found on a Markdown doc)
**Severity:** advisory-noise (NOT a block — see impact)
**Found by:** genuine real usage. Chameleon's own PostToolUse hook fired on this campaign's
edit to `TESTING.md` and reported:

```
[🦎 chameleon: 1 violation]
1. detect-secrets flagged a possible_aws_secret at line 57. Never commit credentials —
   rotate the secret and move it to an environment variable or a secret manager.
```

Line 57 was a Markdown table row containing a filesystem path and a git SHA. There is no
credential on it.

**Red evidence (reproduced, not inferred):**

```
$ .venv/bin/python -c "...scan_for_secrets(<exact line 57>)..."
   HIT: possible_aws_secret line 1
Why the context gate passed:
   credential-context match = 'auth' inside the word: 'are authored) '
Why the 40-char pattern matched:
   40-char run = 'Users/crisn/Documents/Projects/chameleon' len 40
```

**Root cause — two independent defects that compound:**

1. `_CREDENTIAL_CONTEXT` (`profile/secret_scanner.py:78`) is a bare alternation with **no word
   boundaries**, so it matches inside ordinary English words. Measured: `authored`, `author`,
   `authority`, `authorize`, `authentic` (via `auth`); `monkey`, `keyboard`, `turkey`,
   `donkey`, `whiskey`, `keynote` (via `key`); `accessible`, `accessory` (via `access`);
   `privately` (via `private`); `secretary`, `secretly` (via `secret`); `tokenize`,
   `passwordless`.
2. The `possible_aws_secret` pattern (`secret_scanner.py:22`) is `\b[A-Za-z0-9/+=]{40}\b` —
   the class includes `/`, so **any 40-character filesystem path matches**. Here
   `Users/crisn/Documents/Projects/chameleon` is exactly 40 chars.

Either alone is harmless; together, a prose line that mentions an *author* next to a
40-char *path* is reported as a leaked AWS credential.

**Impact / effectiveness:** the rule is deliberately **advisory-only** — `possible_aws_secret`
is explicitly excluded from `_DETERMINISTIC_SECRET_KINDS` (`violation_class.py:294`), so it
can never block an edit. The damage is precision, not availability: the user is told to
"rotate the secret" for a file path, which is exactly the false-positive noise the
`_CONTEXT_GATED_KINDS` gate (`secret_scanner.py:69-76`) was introduced to eliminate. The gate
is under-precise, so it is not doing the job its own comment claims. This is a defect in the
mitigation, not accepted behaviour.

**Fix:** deferred to the fix phase (word-boundary the credential-context gate; consider
excluding path-shaped runs from the 40-char pattern). Must be re-verified across all
languages since the scanner is language-agnostic.

---

---

## 5. Fix Log

_(one entry per fix cycle: issue, cell, root cause, red evidence, green evidence, commit)_
