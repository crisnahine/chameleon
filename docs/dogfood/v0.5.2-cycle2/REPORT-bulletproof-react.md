# Dogfood report: bulletproof-react

- App path: `/Users/crisn/Documents/Projects/Testing Apps/bulletproof-react`
- Total wall time: 0.1s
- PASS: 0
- FAIL: 2
- FINDING: 0
- NOTE: 4

## Phase 0 — Pre-flight survey
_Detect app shape before bootstrap._
Duration: 0.0s

- `[.]` **shape=monorepo-ts** — file_count_approx=798
- `[.]` **has_gemfile=False**
- `[.]` **has_pkgjson=True**
- `[.]` **has_monorepo=True**

## Phase 1 — Bootstrap from scratch
_Remove any .chameleon/, bootstrap_repo, inspect outputs._
Duration: 0.0s

- `[x]` **bootstrap_repo status='failed_unsupported_language'** — No TypeScript signals (tsconfig.json / package.json TS deps) and no Ruby signals (Gemfile / *.gemspec) detected

## ABORT
_bootstrap_repo failed; skipping later phases_
Duration: 0.0s

- `[x]` **Cannot proceed without successful bootstrap**
