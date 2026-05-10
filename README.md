# chameleon

> *"Code that blends in."*

A Claude Code plugin that learns your repo's actual conventions and injects archetype-aware guidance so AI-generated code matches your existing style on the first try.

## Status

`v0.1.0-alpha` — under active development. Architecture complete (5 review rounds, 27 reviewer perspectives). Implementation through Phase 4 of 7 complete.

Private to Empire Flippers, LLC. Not yet ready for external use.

### Implementation progress

- [x] Architecture (v5, 5 review rounds + EF dogfood verification)
- [x] **Phase 1A**: Core repo scaffold (plugin manifest, hooks, skills shell, MCP scaffold, ADRs)
- [x] **Phase 1B**: Hook stubs + skill stubs + first 3 ADRs
- [x] **Phase 1C**: MCP server scaffold (FastMCP, 12 tools, security-critical helpers)
- [x] **Phase 2A**: TS extractor + cluster signature function + drift.db schema
- [x] **Phase 2B**: Bootstrap engine main flow (discovery, clustering, canonical selection)
- [x] **Phase 2C**: Workspace detection + tool config reading
- [x] **Phase 2D**: chameleon-init MCP wiring + trust + teach
- [x] **Phase 3**: Foundation skill bodies (using-chameleon, init, trust, teach)
- [x] **Phase 4**: Security mitigations + hook wiring (secret scanner, sanitization, hook_helper)
- [ ] **Phase 5**: EF dogfood — populate `docs/chameleon/REAL-PROBLEM-EVIDENCE.md` (CI-gated)
- [ ] **Phase 6**: Conformance benchmarking + calibration target evaluation
- [ ] **Phase 7**: Documentation + v1.0 release

## Why?

AI-generated code in established codebases routinely violates local conventions: wrong file location, off-pattern naming, missed team idioms, divergent error handling. Reviewer time gets spent on style and shape, not logic and security.

chameleon clusters your actual code patterns (via AST + statistical analysis), captures team-specific idioms (via interview + iterative `/chameleon-teach`), and injects archetype-keyed guidance per-edit so Claude writes code that fits.

## How it works

1. **Bootstrap** — `/chameleon-init` runs an AST scan over your repo, clusters files into archetypes, picks canonical examples, asks ≤3 confirmation questions, and writes `.chameleon/profile.json` (committed; team-shared via git).
2. **Trust** — `/chameleon-trust` per-user, per-repo approval (non-blocking, mirrors `git config --get user.signingkey` model).
3. **Per-edit** — PreToolUse hook calls the chameleon MCP server; the server returns archetype-keyed canonical excerpt + rules; injected as `<chameleon-context>` in Claude's context.
4. **Iterate** — `/chameleon-teach` captures idioms AST can't infer (banned imports, mandatory wrappers, custom HTTP clients, etc.).
5. **Drift detection** — per-edit confidence tracking surfaces when the profile no longer matches reality; primer escalates to suggest `/chameleon-refresh`.

## Quick install (when ready)

```sh
claude plugin install chameleon@empire-flippers-marketplace
```

## First use

```sh
# In your TypeScript repo
cd /path/to/your/repo

# Bootstrap a profile (≤3 prompts, ~$0.50–$2 one-time)
claude
> /chameleon-init

# Approve the profile for your user
> /chameleon-trust

# Edit code as normal — chameleon injects per-edit context automatically
> Add a new endpoint at /api/v1/widgets that returns a list of widgets.
```

## Why not just write a CLAUDE.md?

| Need | CLAUDE.md | chameleon |
|---|---|---|
| One repo, simple conventions | Adequate | Overkill |
| Multiple repos with different stacks | Manual sync per repo | Automatic per-repo |
| Discovers patterns you didn't realize you had | No | Yes (clustering surfaces them) |
| Surfaces drift over time | No | Yes (confidence tracking) |
| Per-edit "did I follow the pattern?" | No | Yes (advisory injection) |
| Auto-derived from real code, not aspirational rules | No | Yes (Tier 1: AST + statistical) |
| Captures hand-curated team idioms | Yes | Yes (Tier 2: idioms.md) |

CLAUDE.md is the right tool for one repo with a clear, well-articulated convention. chameleon is the right tool for multiple repos, evolving conventions, or teams who want Claude grounded in patterns derived from what they actually wrote.

## Documentation

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — full design (~16,000 words across 5 review rounds + EF dogfood verification)
- [`CHANGELOG.md`](./CHANGELOG.md) — version history
- [`docs/chameleon/decisions/`](./docs/chameleon/decisions/) — Architecture Decision Records
- [`docs/chameleon/MAINTAINER.md`](./docs/chameleon/MAINTAINER.md) — runbook (Phase 7)
- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — dev setup (Phase 7)

Round-by-round review reports:
- [`docs/chameleon/ROUND-1-REVIEWS.md`](./docs/chameleon/ROUND-1-REVIEWS.md) — 6 lens-based reviewers
- [`docs/chameleon/ROUND-2-REVIEWS.md`](./docs/chameleon/ROUND-2-REVIEWS.md) — 5 adversarial reviewers
- [`docs/chameleon/ROUND-3-FINAL-VERIFICATION.md`](./docs/chameleon/ROUND-3-FINAL-VERIFICATION.md) — Jesse Vincent perspective final verification
- [`docs/chameleon/ROUND-4-ELITE-VERIFICATION.md`](./docs/chameleon/ROUND-4-ELITE-VERIFICATION.md) — 5 elite-tier reviewers (25+ years)
- [`docs/chameleon/ROUND-5-EXPERT-VERIFICATION.md`](./docs/chameleon/ROUND-5-EXPERT-VERIFICATION.md) — 10 expert reviewers (25+ years)

## Support

GitHub Issues path TBD. For now, internal Empire Flippers Slack.

## License

`UNLICENSED` — proprietary to Empire Flippers, LLC. See [LICENSE](./LICENSE).
