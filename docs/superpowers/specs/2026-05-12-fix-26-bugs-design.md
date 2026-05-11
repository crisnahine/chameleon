# Design: fix 26 bugs found in May 12 dogfood run

Source: `/Users/crisn/Documents/Projects/Testing Apps/_chameleon_test_results/_BUGS.md`

## Goal

Land fixes for all 26 bugs documented in the dogfood run, one commit per bug, on main, then ship as v0.5.6.

## Order

1. **Quick wins (1-line / config-shaped)** — BUG-005, 006, 007, 012, 013, 022, 023, 026
2. **Medium API/UX** — BUG-004, 010, 011, 015, 017, 018, 021, 024, 025, 019
3. **Extractors** — BUG-003 (eslintrc.cjs), BUG-020 (eslint flat config), BUG-014 (rubocop)
4. **Critical** — BUG-008/009 (response cap), BUG-002 (clustering quality), BUG-001 (monorepo), BUG-016 (verify v0.5.5 fix)

## Per-bug shape

Each fix:
1. Read the affected file(s)
2. Write a unit test that fails today (per superpowers TDD)
3. Implement the fix
4. Run tests
5. Commit with `Fix BUG-NNN: <one-line>` — no AI attribution

## Test infrastructure

Reuse `tests/run_all_orders.py`. New tests land in `tests/` next to existing suites. For extractor work, add fixture files under `tests/fixtures/`.

## Out of scope this pass

- v0.5.6 release/tag (deferred to a final commit after all fixes land)
- Major clustering rewrite beyond the permissive-merge tier described in BUG-002

## Risks

- The clustering change (BUG-002) could move existing canonicals and break dogfood expectations. Mitigation: leave the old cluster key intact, add the loose-merge tier as a fallback.
- Response-cap changes (BUG-009) change a public schema. Mitigation: additive only (new fields like `sparse_cluster_warnings_truncated`).
