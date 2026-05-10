# Changelog

All notable changes to chameleon will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Phase 1A — Core repo scaffold (2026-05-10)

#### Added
- Initialize git repository
- Plugin manifest: `.claude-plugin/plugin.json` and `marketplace.json`
- LICENSE: `UNLICENSED` (proprietary to Empire Flippers, LLC)
- README.md: front door with tagline, install, first-use, competitive positioning vs CLAUDE.md/Cursor rules/Copilot/paid review services
- CLAUDE.md: development guide for working on this codebase
- `.gitignore`: standard Python + Node + OS + editor exclusions, plus `.chameleon/` per-user state
- `.gitattributes-template`: ships for users to copy into their repos to enable `chameleon-mcp::merge_profiles` as merge driver
- `package.json`: version anchor (mirrors `.claude-plugin/plugin.json` version)
- Directory skeleton: `hooks/`, `skills/`, `mcp/chameleon_mcp/`, `scripts/`, `tests/{skill-triggering,unit,integration,acceptance}/`, `docs/chameleon/{decisions,specs,plans,reference}/`, `assets/`
- Phase 1A placeholder: `hooks/run-hook.cmd` (cross-platform polyglot wrapper, mirroring superpowers' pattern)
- Phase 1A placeholder: `hooks/hooks.json` (hook manifest declaring SessionStart, PreToolUse, PostToolUse Bash, UserPromptSubmit)
- Phase 1A placeholder: `hooks/session-start` (minimal no-op; Phase 1B will implement profile detection + JSON dispatch)
- Phase 1A placeholder: `skills/using-chameleon/SKILL.md` (foundation skill stub; Phase 3 will author full body via RED-GREEN-REFACTOR)

#### Architecture (pre-Phase-1, archived)

- v1 (2026-05-10): Initial draft (3,899 words)
- v2 (2026-05-10): After Round 1 review (6 agents, 14 critical issues addressed)
- v3 (2026-05-10): After Round 2 adversarial review (5 agents, 9 BLOCKING + 25 SIGNIFICANT addressed) + companion plugin pattern removed
- v4 (2026-05-10): After Round 4 elite-tier verification (5 agents, 6 BLOCKING distributed-systems items + 25 HIGH PRIORITY addressed) + EF dogfood verification (51 → 77 dimensions)
- v5 (2026-05-10): After Round 5 10-expert verification (10 agents, 25+ years each; 5 NEEDS REVISION + critical APPROVED-WITH-NOTES items addressed)

Total review investment: 27 unique reviewer perspectives across 5 rounds + EF dogfood verification on /api (Ruby on Rails) and /client (TypeScript). Review moratorium declared after v5.
