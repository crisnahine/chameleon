# Round 5 — 10-Expert Comprehensive Verification

> 10 parallel expert reviewers, each 25+ years experience, channeling distinct deep expertise. Reviews collected 2026-05-10.
> Source architecture: `/Users/crisn/Documents/Projects/chameleon/ARCHITECTURE.md` (v4, post-EF dogfood verification).

## Verdict summary

| Reviewer | Verdict |
|---|---|
| SRE / Production Operations | APPROVED WITH NOTES |
| **Compiler / Static Analysis Theorist** | **NEEDS REVISION** |
| UX Researcher | APPROVED WITH NOTES |
| Application Security Specialist | APPROVED WITH NOTES (internal-only) |
| **Database / Consistency Architect** | **NEEDS REVISION** |
| API Designer / Protocol Engineer | APPROVED WITH NOTES |
| OSS Maintainer | APPROVED WITH NOTES (private) / NEEDS REVISION (if public) |
| **Performance Engineer** | **NEEDS REVISION** |
| **Technical Writer / Documentation Engineer** | **NEEDS REVISION** |
| **Engineering Manager / Tech Lead** | **NEEDS REVISION** |

5 NEEDS REVISION, 5 APPROVED WITH NOTES.

## The dominant cross-cutting message

The engineering manager said it most directly: **architecture has stopped being a design document and become a substitute for shipping.** Five rounds of review with no code is the wrong direction. v3 was shippable. Each round adds 30-50% more requirements — most addressing speculative threats nobody has experienced because the system has never run.

This is consistent across the verdicts: every reviewer flagged real concerns, but **collectively none are blocking — together they show that more iteration would surface more refinements indefinitely**. The diminishing returns inflection happened around Round 3.

## Critical findings (5 NEEDS REVISION)

### 1. Compiler / Static Analysis Theorist — undefined signature function

The architecture never specifies the signature function `f: file → cluster_key`. "AST shape" appears as a slogan but has no implementation referent. Round 4 PL theorist named the equivalence relation; Round 5 compiler theorist names the engineering substrate gap.

**Critical fix needed:**
```
sig(file) = (
  path_pattern_bucket,
  content_signal_match,
  top_level_node_kinds,         # tuple of ts.SyntaxKind
  default_export_kind,
  named_export_count_bucket,
  import_module_set_hash,
  jsx_present
)
```

Plus: commit to `ts.createSourceFile` (syntax-only); commit to recompute-all-from-cached-signatures incremental algorithm; document parser-error tolerance contract (skip at >N parse diagnostics).

### 2. Database / Consistency Architect — zero SQLite schemas specified

Three SQLite databases mentioned (`drift.db`, `index.db`, `value_attrib.db`); ZERO schemas, indices, or query patterns specified. Migration correctness contract covers JSON only, not SQLite.

**Critical fix needed:**
- Full DDL for all three databases in architecture
- Hashing function: xxhash64 (non-crypto, fast, "_hint" suffix already implies)
- merge_profiles algorithm: pick one strategy and document (recommended: "reproposed profile from union, user reviews via summary, runs /chameleon-refresh")
- Double-fstat loader pattern with generation counter for cross-file consistency
- Drift.db migration policy: "cache, drop-and-recreate permitted"

### 3. Performance Engineer — bootstrap math impossible without daemonization

Bootstrap on 5,000 files at "300ms startup × 5,000 = 29 minutes" if `ts_dump.mjs` invoked per-file. The architecture never specifies long-lived process. Per-Edit subprocess fork (~50ms × 30 edits = 1.5s overhead) before MCP call.

**Critical fix needed:**
- Daemonize MCP server invocation path (UNIX socket, not per-call subprocess)
- `ts_dump.mjs` MUST be persistent process consuming file paths from stdin
- Parallelism: cpu_count/2 workers
- Throughput floor: ≥50 files/sec/core in CI
- Memory bounds: 100 MB RSS hard cap on MCP server, LRU eviction on AST cache (N=16)
- Replace "200ms is the cost of correctness" in Red Flags table — number will be wrong day one

### 4. Technical Writer — documentation packaging fails readers

10,914-word monolith. No README. No TOC. No glossary. Round 4 changelog dominates top. 40+ vocabulary terms despite "5-term firewall" claim. Examples sparse.

**Critical fixes needed:**
- Write README before next architecture revision (front door)
- Auto-generated TOC at top
- Glossary as appendix (every non-firewall term, alphabetical)
- Reorder by reader mental model (Concepts → UX → System → Contracts → Operations → Project plan → Appendix)
- Move Round 4 changelog → CHANGELOG.md
- Move dimensions catalog + calibration targets → docs/chameleon/reference/
- Replace ASCII diagrams with Mermaid (renders in GitHub natively)
- Mark certainty levels uniformly (`[VERIFIED]`, `[ESTIMATED]`, `[TBD]`)

### 5. Engineering Manager — scope vs capacity mismatch + stakeholder alignment unverified

**Effort estimate unrealistic:** 390h v1.0 is fabricated. Realistic: 800-1,200h = 9-15 months calendar at sustainable solo pace.

**Stakeholder alignment unverified:** "EF api/client as dogfood" appears 30+ times; ZERO references to documented buy-in from EF engineering manager. CI gate requires Real Problem Evidence transcripts from EF dogfooding — without EF buy-in, v1.0 cannot ship.

**Solo maintainer ambition mismatch:** Architecture quietly assumes a team. Quarterly tasks (model re-baseline, calibration, dep bumps) + ongoing maintenance + active building = 1.3-1.5 FTE for one person with a day job.

**Recommendations:**
1. Have the EF conversation now, before Phase 1. Document champion's name in architecture as named stakeholder.
2. Triple effort estimates. "9-15 months calendar at sustainable solo pace, conditional on no scope additions."
3. Cut v1.0 scope to "v3 minus formal sections." Defer Operational semantics, calibration targets, migration contract to v1.1 or v2.
4. Write a real risk registry (probability × impact × mitigation × owner, max 10).
5. **Declare a review moratorium. No Round 6. Implementation findings replace reviewer findings now.**
6. Define success measurably: "On next 10 EF PRs with AI-generated code, fewer than 2 reviewer comments mention shape/naming/idiom that chameleon should have caught."
7. Pre-commit a "fall-back to v0.5" plan for if Phase 1 takes 12 weeks for 30% scope.
8. Find co-maintainer or document bus factor risk in README.

---

## Notable findings from APPROVED WITH NOTES reviewers

### SRE
- Failure mode matrix is diagnostic, not actionable. Each row needs Detection / Diagnostic / Rollback / Runbook columns
- No SLOs defined — operators can't know if chameleon is healthy or degraded
- `chameleon-status --health` subcommand needed (p99 latency, MCP error rate, fail-open rate)
- `events.jsonl` schema unspecified
- DR test in CI missing
- Plugin rollback runbook missing

### UX Researcher
- Multi-plugin attribution unsolved (which plugin caused this behavior?)
- Vocabulary firewall is leaky (40+ terms in actual use)
- Trust calibration: silent updates vs material changes ambiguous
- Configuration debt: who removes deprecated idioms? When?
- **Recovery / undo gaps**: no `/chameleon-reset` command — users will `rm -rf` when frustrated
- Failure visibility issues — "telemetry log entry" is invisible to users; should surface in next-session primer

### Application Security Specialist
- SAST/DAST integration gaps (no SARIF output, alert fatigue with Semgrep/CodeQL)
- **Vulnerability-pattern-in-canonical attacks** — canonical can be syntactically clean but semantically teach insecure habits
- Python supply chain not equivalent to TypeScript (no `pip install --require-hashes`, no vendored wheels)
- Trust prompt + social engineering (cooldowns by frequency, out-of-band confirmation for security archetypes)
- AI-as-interpreter injection paradigm needs explicit doctrine
- Compliance / regulatory considerations (HIPAA/PCI/GDPR scoping)
- **Auth/crypto archetypes should be excluded from auto-selection** in v1

### API Designer
- Naming inconsistency (mix of verb-noun, get_*, list_*)
- `refine_profile` (MCP) vs `/chameleon-teach` (slash) diverge — same operation, two names
- **Per-edit fan-out should collapse to `get_pattern_context(file_path)`** — one call instead of four
- Idempotency / error contracts unspecified
- API versioning at MCP tool surface missing
- Profile schema needs explicit casing convention (snake_case)
- Unknown-field handling unspecified (preserve on round-trip, ignore on load)

### OSS Maintainer
- License choice not declared
- BC contract not promoted to public document
- Release cadence undefined
- CLA / DCO / IP policy silent
- Issue triage rubric missing
- Good-first-issue pipeline missing
- Public roadmap with priorities/owners/timelines missing
- **Decide path now**: private EF tool vs OSS — current ambiguity is itself the flaw

---

## Convergent themes across all 10 reviewers

1. **Substrate gaps under polished surface** — architecture is rigorous on cost/security/distributed systems but silent on schemas, signatures, performance, documentation packaging, and stakeholder alignment.

2. **Diminishing returns** — every reviewer surfaces specific concerns; collectively, none are blocking, but they show "more iteration would surface more refinements indefinitely."

3. **Solo maintainer ambition vs feasibility** — scope keeps growing, capacity isn't multiplying.

4. **Stakeholder alignment unverified** — EF buy-in is the load-bearing assumption with no documented evidence.

5. **Documentation as a substrate** — the architecture doc itself is unmaintainable as it grows; needs splitting before adding more.

## The strongest single recommendation across all 10 reviewers

**STOP REVIEWING. SHIP SOMETHING.**

Multiple reviewers said this in different language:
- Engineering Manager: "Declare a review moratorium. No Round 6. Implementation findings replace reviewer findings now."
- Engineering Manager: "You cannot review your way to perfection."
- Engineering Manager: "If I am still iterating the architecture in 2 weeks, I am procrastinating."
- Multiple reviewers (perf, db, compiler): "These need to be in the doc before Phase 1" — but each new round produces more such items.

The architecture is mature enough. Continuing review is procrastination. Real learning starts with code.
