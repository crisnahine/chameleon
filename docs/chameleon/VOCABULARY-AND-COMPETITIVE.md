# chameleon — Vocabulary firewall and competitive analysis

A reference for reviewers, contributors, and adopters. chameleon shares
surface vocabulary with linters, formatters, AI rules files, and
codebase-aware retrievers — but the underlying mechanism differs. This
document fixes the terms and locates chameleon against adjacent tools.

It is not a marketing piece. Where chameleon's scope is narrower than a
reader might assume, the doc says so.

---

## Section 1 — Vocabulary firewall

chameleon's vocabulary collides with industry-standard terms in
predictable places. Use the chameleon term inside this codebase, the
industry term outside. The two are not interchangeable.

| chameleon term | industry near-synonym | why they differ | where it lives |
|---|---|---|---|
| **archetype** | rule | An archetype is a discovered cluster of files with shared shape, picked out by the 7-tuple signature function. A lint rule is a single hand-authored boolean assertion ("no `var` declarations"). Archetypes are derived from the repo; rules are written for it. | `mcp/chameleon_mcp/signatures.py`; ARCHITECTURE.md "Cluster signature function" (~line 862) |
| **canonical** | example | The canonical is the selected witness file for an archetype — chosen by recency weighting, secret scan, injection scan, and poisoning scan. A generic "example" is handwritten prose. Canonicals are AST-derived and gated by `canonical_scanner.py`. | `.chameleon/canonicals/`; `mcp/chameleon_mcp/bootstrap/` canonical-selection step |
| **idiom** | convention | An idiom is the encoded subset of conventions captured via `/chameleon-teach`. "Convention" is the broader software-engineering term for team practices, most of which never get written down. Idioms are the part that does — and gets surfaced through the trust gate before injection. | `.chameleon/idioms.md`; `mcp/chameleon_mcp/tools.py::teach_profile` |
| **profile** | config | A profile is the committed `.chameleon/` directory tree (`profile.json`, `canonicals/`, `idioms.md`, `profile.summary.md`). A config is a single file like `.eslintrc` or `.rubocop.yml`. The profile is multi-file, derived from a repo's actual code, and re-emitted through atomic commit. | `.chameleon/` (committed); `mcp/chameleon_mcp/profile/schema.py` |
| **trust** | install | Trust is the per-user, per-repo approval gate. Install is the plugin-level mechanism that puts chameleon on your machine. You install chameleon once per harness; you grant trust once per (user, repo, profile SHA) and re-grant after material changes. | `~/.local/share/chameleon/<repo_id>/.trust`; `mcp/chameleon_mcp/profile/trust.py` |
| **drift** | divergence | Drift is the measured `observed_drift_score` computed from `drift.db`'s recent low-confidence-edit ratio. Divergence is the colloquial term reviewers use in PRs. Drift is a number returned by `get_drift_status`; divergence is a vibe. | `~/.local/share/chameleon/<repo_id>/drift.db`; `mcp/chameleon_mcp/drift/observations.py` |
| **bucketing** | glob | `path_pattern_bucket` reduces a path to a 2–3-segment signature (e.g., `app/api/v1`) used as one of the 7 cluster-signature dimensions. A glob (e.g., `app/**/*.rb`) is a file-match pattern. Bucketing groups files into archetypes; globs filter files in or out. | `mcp/chameleon_mcp/signatures.py::path_pattern_bucket_for` |
| **shape** | structure | Shape is the cluster signature itself — a 7-tuple of (path_pattern_bucket, content_signal, top_level_node_kinds, default_export_kind, named_export_count_bucket, import_hash, jsx_present). Structure is the colloquial term reviewers reach for. Shape is an exact-match equivalence class; structure is fuzzy. | `mcp/chameleon_mcp/signatures.py`; ARCHITECTURE.md "Cluster signature function" |

### Why this matters

Most of the confusion in PR review and skeptical onboarding traces to
one of these collisions. A reviewer reads "archetype" and pattern-matches
to "lint rule." They read "canonical" and pattern-match to "example in
the docs." They read "profile" and assume it's a single `.chameleonrc`
file. None of those mental models survive contact with the codebase, so
the firewall above is load-bearing.

Two collisions are sharper than the others and worth flagging:

- **archetype is derived; rule is authored.** chameleon cannot "add a
  rule." It can only re-run the bootstrap and let the cluster signatures
  fall where they fall. The `/chameleon-teach` channel is the closest
  thing to rule authoring, and it writes prose to `idioms.md` — not a
  predicate to a rule engine.
- **trust is per (user, repo, profile SHA).** A team member who clones
  the repo sees the committed profile but has not granted trust on their
  machine. Until they run `/chameleon-trust`, the SessionStart primer
  surfaces the profile as ungated and downstream advisory injections are
  suppressed. This is the same mental model as `git config --get
  user.signingkey`: the artifact lives in the repo, the grant lives on
  your machine.

---

## Section 2 — Competitive analysis

chameleon operates next to several established tools. The comparisons
below describe scope differences, not winners. In most cases the right
answer is "use both."

### ESLint / RuboCop / linters

Linters author rules and check files against them. chameleon does not
author rules; it discovers cluster signatures and advises shape
alignment to the matched archetype's canonical excerpt. The two
mechanisms answer different questions: a linter answers "does this file
violate any of the configured rules?" chameleon answers "does this file
look like the other files in its archetype?" Use both — linters for
rule violations, chameleon for archetype conformance — and expect the
overlap to be small. The shape-only `lint_file` engine (see
`mcp/chameleon_mcp/lint_engine.py`) checks the five cluster-signature
dimensions, not the rule taxonomy a linter operates on.

### Prettier / formatters

Formatters normalize whitespace, quote style, and line breaks. chameleon
does not touch whitespace and explicitly defers to `.prettierrc`,
`.editorconfig`, and `.rubocop.yml` for those concerns (ARCHITECTURE.md
tracked dimensions 15–16). Different problem entirely. There is no
overlap to manage and no choice to make.

### `.cursorrules` / `CLAUDE.md` / handwritten AI rules files

Handwritten AI rules files encode what the team wants the AI to do.
chameleon derives what the code already does. Both coexist; chameleon
answers "what does this repo look like?" and handwritten rules answer
"what do I want it to look like?" The two layers do not compete — a
team can have both, and a `CLAUDE.md` directive ("always prefer Result
types") will sit beside a chameleon archetype ("controllers in this
repo look like this canonical") without conflict. chameleon does not
read or modify handwritten rules files.

### superpowers (obra/superpowers)

[superpowers](https://github.com/obra/superpowers) is process-shaping:
brainstorming → spec → plan → subagent-driven TDD → review → merge. It
governs the workflow the agent follows. chameleon is output-shaping:
canonical excerpt + shape rules + active idioms injected into the
model's context per-edit. It governs the shape of what the agent
writes. The two compose: superpowers covers the *how* of an
implementation session, chameleon covers the *what* a given edit looks
like. A team running superpowers with chameleon installed gets both
process discipline and per-repo shape conformance.

### Cody / Copilot / generic AI coding assistants

Generic AI coding assistants draw on broad pretraining corpora plus
some form of retrieval — public-internet code, project files in
context, fuzzy code search. chameleon adds a committed per-repo profile
and a deterministic per-edit context-injection hook. The injected
content is the repo's own canonical example, not generic best practices
for the framework. The mechanisms are complementary: a Copilot-style
assistant can suggest a function body; chameleon ensures the surrounding
file matches the repo's controllers, services, or models.

### Codebase-aware retrievers (Continue.dev, embedding-based search)

Codebase-aware retrievers embed files and retrieve the top-K nearest
neighbors at query time. chameleon clusters once at bootstrap, picks a
canonical per archetype, commits the result, and queries the *committed*
profile per-edit. The profile is a stable team artifact reviewable in
PRs — it does not change between edits in the same session. Retrievers
are good at "find me a similar file"; chameleon is good at "give me the
agreed canonical for this archetype." A team that wants both can run
them side by side: retrieval for exploration, chameleon for shape
conformance during edit-time generation.

---

## When NOT to use chameleon

chameleon's value scales with two things: how well the codebase
clusters under the 7-tuple signature function, and how much team
curation flows through `/chameleon-teach`. Both can fail.

- **Repos with no consistent archetypes.** Early prototypes, single-file
  projects, and codebases where every file is its own snowflake will
  produce many small, low-confidence clusters with weak canonicals.
  chameleon will run, but the injected context will be near-noise. Wait
  until the codebase has at least a few archetypes with 3+ members each.
- **Languages chameleon doesn't support.** v1 ships TypeScript and Ruby
  on Rails extractors only (see `mcp/chameleon_mcp/extractors/`).
  Python, Go, Rust, Java, C#, PHP, and Swift repos have no path through
  the bootstrap pipeline. There is no plan to add languages without a
  contributor who knows the AST surface well — the cluster signature
  function depends on language-specific parser semantics.
- **Codebases that intentionally have no canonical examples.** Research
  code, exploratory notebooks, and one-off scripts are *supposed* to
  diverge. The canonical-selection mechanism will pick witnesses anyway,
  but the resulting archetype guidance will fight the work. Add
  `.chameleon/.skip` (committed) or set `CHAMELEON_DISABLE=1` for these
  repos.
- **Codebases where conformance is not yet a problem.** chameleon
  targets the failure mode described in
  [REAL-PROBLEM-EVIDENCE.md](REAL-PROBLEM-EVIDENCE.md): AI-generated
  code routinely violates local conventions, and reviewer time gets
  spent on shape instead of logic. If your reviewers are not burning
  cycles on shape comments, the engine's value is lower. The
  conformance-rate claim is not yet measured (Phase 6 work,
  outstanding).

---

## See also

- [OVERVIEW.md](OVERVIEW.md) — 5-minute architecture orientation
- [REAL-PROBLEM-EVIDENCE.md](REAL-PROBLEM-EVIDENCE.md) — what the engine
  is verified to do today and what remains unmeasured
- [THREAT-MODEL.md](THREAT-MODEL.md) — sanitization, trust gate,
  poisoning scanner, and what they defend against
- [ARCHITECTURE.md](../../ARCHITECTURE.md) — full design including the
  cluster signature function (~line 862) and "what chameleon is and is
  not computing" (~line 206)
- [decisions/0001-best-effort-clustering-vs-framework-aware.md](decisions/0001-best-effort-clustering-vs-framework-aware.md)
  — the framing that explains why archetypes are derived, not authored
