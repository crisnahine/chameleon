# chameleon docs

Start with the repo-root [README.md](../README.md) — what chameleon is, why it
exists, and the honest line on what it does and doesn't resolve. This folder
holds everything deeper. Reading order by intent:

## I want to use it

- [install.md](install.md) — prerequisites, the two-command install, first
  profile, opt-out layers, updating, uninstalling, troubleshooting by symptom.

## I want to understand how it works

- [architecture.md](architecture.md) — the full design: profile derivation,
  the hook stack, the trust and enforcement model, the turn-end review
  pipeline, cross-file indexes, storage layout, and the security posture.
  The single most complete document in the repo.
- [language-support-matrix.md](language-support-matrix.md) — the per-language
  capability parity matrix (TypeScript/JavaScript, Ruby, Python): every
  capability, per language, with ✅/⚠️/❌/n-a marks and code-grounded notes.
- [hot-path-budget.md](hot-path-budget.md) — the latency budget for the
  per-edit hot path and every layer's timeout ceiling.

## I want to work on it

- [../.github/CONTRIBUTING.md](../.github/CONTRIBUTING.md) — dev setup, test
  suites, change procedures per surface (hooks, MCP tools, schema, skills),
  commit/PR conventions, CI.
- [qa-team.md](qa-team.md) — the standing QA agent roster and dispatch
  protocol used for release verification.

## Verification program (the "prove it works" track)

- [chameleon-goal.md](chameleon-goal.md) — the standing goal document:
  what "works correctly" means, the support matrix, and the acceptance
  criteria for all 15 subsystems.
- [verification-matrix.md](verification-matrix.md) — the cell-by-cell
  sign-off tracker against that goal.
- [verification-runbook.md](verification-runbook.md) — turnkey, copy-paste
  steps for a human verifying each matrix cell.
- [gap-log.md](gap-log.md) — the numbered log of gaps found during the
  v2.38.x verification campaign and their outcomes. Later gaps are recorded
  in [CHANGELOG.md](../CHANGELOG.md) release entries.

## Historical snapshots

- [parity-progress.md](parity-progress.md) — point-in-time log of the June
  2026 language-parity campaign. Superseded for current status by
  language-support-matrix.md.
