# chameleon

> *"Code that blends in."*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-7C3AED.svg)](https://docs.claude.com/claude-code)
[![Languages](https://img.shields.io/badge/languages-TS%20%7C%20Ruby%20%7C%20Python-2ea44f.svg)](#what-it-actually-supports)
[![Tests](https://img.shields.io/badge/unit%20tests-4%2C777-blue.svg)](#proof-not-promises)
[![Listed on ClaudePluginHub](https://www.claudepluginhub.com/badge/crisnahine-chameleon)](https://www.claudepluginhub.com/plugins/crisnahine-chameleon?ref=badge)

**Your AI writes code that works. It just doesn't write code that looks like yours.**

It reaches for `axios` when your team standardized on `@/lib/http` six months ago. It hand-rolls a date format when `fmt()` already exists. It builds a service that ignores the base class every other service in the repo extends. The code passes. The diff is wrong. And you find out in review, every single time, because the model never saw how *your* repo does it.

Chameleon fixes that before the model writes a line.

---

## The one thing it does

Before Claude edits a file, chameleon hands it context drawn straight from your own codebase:

1. **A real example file** of the same kind it's about to write (the "canonical witness"), derived automatically when you profile the repo.
2. **Your team's idioms** for that kind of file (the wrapper to use, the import that's banned, the guard that's mandatory), as you teach them or let chameleon auto-derive them.
3. **The anti-pattern to avoid**, quoted from a real off-pattern line in your repo and labeled "do NOT write it this way," once your team has taught a competing import.

Here is the full first-touch block the model gets before editing a service. The canonical witness is automatic; the off-pattern line shows up once your team has taught a competing import:

![chameleon injecting archetype-aware guidance before an edit: the resolved archetype and confidence, the canonical witness to mirror, and a "do NOT write it this way" counterexample drawn from the repo's own off-pattern](assets/chameleon-injection.svg)

Every Edit and Write gets convention context. The first edit to a given kind of file gets the full block above; later edits in the same session get a compact one-line pointer, so it doesn't bloat your context window.

No prompt engineering. No rule files to hand-write. No "please follow our conventions" in your CLAUDE.md that the model forgets by the third tool call. The example is real, it's from your repo, and it lands in context at the exact moment the model is deciding what to type.

---

## Why this is different from what you've tried

You already have tools that care about conventions. None of them work like this:

- **Linters and formatters** check your code *after* it's written. The model writes it wrong, then a hook yells, then the model fixes it, then you pay for two round trips. Chameleon shows the right shape *first*, so there's nothing to fix.
- **Hand-written rule files** (`.cursorrules`, `CLAUDE.md` style guides, AGENTS.md) put the work on you. You write the rules. You keep them current. They go stale the day someone refactors. Chameleon derives the conventions from the repo itself, by parsing it, and re-derives them when the code moves.
- **"Add our docs to context"** dumps a wall of prose and hopes. Chameleon gives the model a concrete file to imitate and one anti-pattern to dodge, which is how in-context learning actually works.

The mechanism is the product: **per-repo conventions, auto-derived, shown to the model as a real example at write-time.**

---

## Install in 30 seconds

You need `uv` and Node.js 20+ on your `PATH`. Ruby repos need Ruby with Prism; Python repos need nothing extra (chameleon ships its own parser). Exact version matrix and per-OS setup: [docs/install.md](docs/install.md).

In any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Then point it at a repo:

```
/chameleon-init     # parse the repo, derive conventions (a one-time scan)
/chameleon-trust    # review the profile and approve it for this machine
```

That's it. From here, every edit Claude makes to that repo gets convention context automatically. You don't call anything. The hooks do the work.

---

## Usage

Once a repo is profiled and trusted, chameleon runs on its own. The things you will actually do:

**Edit code normally (zero commands).** Before Claude writes to a file, it sees a block like the one above: the matched archetype, a real example from your repo, your team's idioms, and any anti-pattern to avoid.

**Teach a rule the parser can't infer:**

```
/chameleon-teach
# then describe it, e.g. "use @/lib/http, never raw axios (archetype: api-client)"
```

Every later edit to that archetype gets the rule, paired with a real off-pattern line from your own code as the counterexample.

**Review a PR against your own conventions:**

```
/chameleon-pr-review <PR-URL>
```

A multi-round, self-refuting review grounded in your repo's patterns and cross-file contracts, reported as BLOCK / FIX / NIT.

**Check health, then refresh when the code has moved:**

```
/chameleon-status     # profile state, drift, enforcement mode, rule precision
/chameleon-refresh    # re-derive from the production branch's current tip
```

The full [command reference](#command-reference) has the rest.

---

## How it learns your repo

`/chameleon-init` parses your codebase with real compilers, not regex guesses:

- **TypeScript / JavaScript** via the official TypeScript Compiler API (`typescript` 6.0.3)
- **Ruby** via Prism, Ruby's own parser
- **Python** via libcst (bundled with the plugin, so your repo needs nothing installed)

From those ASTs it clusters your files into **archetypes** (service, controller, React component, worker, model, and whatever else your repo actually contains), picks a **canonical witness** for each (a real, conforming file that exemplifies the shape), and derives the conventions that hold across each cluster:

- preferred imports and the wrappers that dominate over raw libraries
- naming and casing rules (interface prefixes, file naming, class/method casing)
- base classes, mixins, and per-archetype **class contracts** (the DSL macros, decorators, and required methods a cohort shares)
- authorization guards (Rails `before_action` patterns, Django/DRF permission classes)
- test-pairing conventions, doc coverage, error-handling shape, import ordering, layering rules

Two honest details a skeptic should know. First: conventions are derived from **real ASTs**, but the per-edit hot path uses fast string heuristics so it never adds noticeable latency to your edits (the full parser only runs at init and refresh, never on the keystroke path). Second: chameleon profiles your **production branch**, not your dirty feature checkout. For origin-backed repos it locks onto `origin/HEAD` (or `production` / `main`) and derives conventions from a clean worktree of that tip, so a half-finished experiment on your branch never poisons the team's norms. ([mcp/chameleon_mcp/production_ref.py](mcp/chameleon_mcp/production_ref.py))

---

## What you get beyond the per-edit nudge

The convention injection is the headline. Underneath it are layers that catch the mistakes a single edit can't see. Most are advisory; the few that enforce are gated and always overridable inline: a convention rule blocks only after it's calibrated against your own committed code, deterministic facts like a leaked credential block on sight, and a once-per-session nudge asks you to self-review against your team's idioms.

- **Calibrated enforcement.** A block rule never fires until chameleon has measured it against your own committed files and confirmed a near-zero false-positive rate. Rules that fight your team get auto-demoted to advisory. You won't get nagged by a rule your repo disagrees with. ([enforcement_calibration.py](mcp/chameleon_mcp/enforcement_calibration.py))
- **A turn-end correctness judge.** When a turn ends, chameleon can spawn a separate reviewer that reads only your diff for the bugs static analysis misses: inverted conditions, dropped `await`s, off-by-one, missing guards. Advisory, runs on its own budget, never blocks the turn. ([judge.py](mcp/chameleon_mcp/judge.py))
- **Duplication detection.** It notices when the model just re-implemented a function that already exists, grounded in your real call graph ("reuse it, it's already called from 7 sites"), not a fuzzy name match. ([duplication_review.py](mcp/chameleon_mcp/duplication_review.py))
- **Cross-file blast radius and contract breaks.** Change a function's signature and chameleon knows which committed callers you just broke, from a prebuilt import index, with zero re-parsing on the hot path. The `get_blast_radius` tool walks the same index to the transitive callers a change reaches, not just the direct ones. It flags phantom imports (paths that resolve to nothing) and exports you removed that other files still import. ([signature_diff.py](mcp/chameleon_mcp/signature_diff.py), [blast_radius.py](mcp/chameleon_mcp/blast_radius.py), [phantom_imports.py](mcp/chameleon_mcp/phantom_imports.py))
- **Comprehension, not just conformance.** The same committed profile answers questions about code that already exists: `search_codebase` finds a symbol by name or file, ranked, with its signature and caller count; `describe_codebase` gives a structural overview (language, framework, archetypes, the most-called production functions); `get_callees` answers "what does this function call." All offline, deterministic, off one profile, so the model can understand and navigate your repo, not only conform new edits to it. ([comprehension.py](mcp/chameleon_mcp/comprehension.py))
- **Two real review commands.** `/chameleon-pr-review` runs a multi-round, self-refuting review of a PR or branch diff against your repo's own conventions, with a final independent refuter pass to kill findings that can't survive scrutiny. `/chameleon-receiving-code-review` helps you verify a teammate's review against the code before you blindly apply it. ([skills/chameleon-pr-review](skills/chameleon-pr-review/SKILL.md))
- **Teach what AST can't see.** `/chameleon-teach` captures the rules no parser can infer ("use our HTTP wrapper, never raw `fetch`"). `/chameleon-auto-idiom` mines the repo for those rules itself, grounded in occurrence counts, and proposes them for your approval. The `get_prose_rule_candidates` miner reads your CONTRIBUTING / STYLE / AGENTS docs for stated "use X not Y" rules and proposes only the ones your code already backs.

---

## What it truly resolves (and what it doesn't)

Straight, because a tool that overstates what it fixes is not worth trusting. Measured against the real problems of AI-assisted coding in 2026, here is the honest line.

**It resolves:**

- **Code that doesn't fit your repo.** The most-cited comprehension complaint of 2026 ("the AI doesn't know how OUR codebase works") is chameleon's whole reason to exist: the witness, the idioms, the archetype rules, shown before the model types.
- **Hallucinated dependencies and symbols.** The anti-hallucination protocol bans importing a package that is not already in your manifest or lockfile, and inventing a symbol the repo does not define. That is the direct, deterministic counter to "slopsquatting" (about 1 in 5 AI-recommended packages do not exist, and the same fake names recur, so attackers pre-register them).
- **Code duplication.** It catches a re-implemented function against your real call graph ("reuse it, called from 7 sites") and `search_codebase` finds the existing one first. AI's biggest measured maintainability regression is duplication up, reuse down.
- **Leaked secrets.** A hardcoded credential is a deterministic write-time block, on sight, even on a brand-new file.
- **Not understanding the codebase.** The comprehension layer (`search_codebase` / `describe_codebase` / `get_callees`) lets the model query and navigate the repo it is editing.

**It partially helps:**

- **The review bottleneck.** `/chameleon-pr-review` and the cross-file contract checks offload the mechanical part of review (conventions, duplication, broken callers); human judgment on logic still stays.
- **Plausible-but-wrong code.** The turn-end judge reads your diff for inverted conditions, dropped `await`s, off-by-one, and missing guards. A real but bounded set, not all of it.
- **Insecure code.** Secrets and `eval`/`exec` are caught deterministically; taint, SSRF, and authz are advisory in pr-review. This is not a full SAST.

**It does not resolve, and will not pretend to:**

- **The model's correctness and security ceiling.** Roughly 45% of AI-generated code ships an OWASP Top 10 vulnerability regardless of model or tooling (Veracode, 2026), and it is not improving with scale. chameleon catches a slice, never the ceiling.
- **Destructive agent autonomy, token cost, and latency.** chameleon adds some cost and latency; it does not gate an agent from deleting your database, and it does not make agent loops cheaper.
- **Whether your tests actually test anything.**

Those need better models, runtime and dataflow analysis, sandboxing, and team process, not a write-time conformance tool. chameleon owns the conformance and codebase-context slice, and owns it well. That is the pitch, and the whole of it.

---

## You should know the tradeoff

Three honest admissions, because a tool that hides its costs isn't worth trusting:

1. **It spends tokens and adds latency per turn.** Injecting a real example file and running a turn-end judge isn't free. In our committed cost baselines the context-on arm runs longer and, in most categories, costs more per task than the off arm. That's the price of the model seeing your conventions before it writes, instead of you catching the miss in review. If your edits are tiny and your repo has no conventions worth enforcing, you may not want it on.
2. **TypeScript/JavaScript, Ruby, and Python only.** No Go, Rust, Java, or anything else today. If your repo isn't one of those three, this isn't for you yet.
3. **We don't publish a "writes better code by X%" number, and we won't make one up.** The shipped effectiveness baselines track cost and latency for regression detection; they are not a powered efficacy study. We'd rather hand you the harness than a marketing stat.

On that last point, the harness is real and it's in the repo. It runs paired Claude sessions (context off vs. on), scores them, and reports the delta. Reproduce our baseline on the bundled fixtures:

```bash
PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.runner --tier ci --arms off,shadow
```

To measure it on your own code, run `--tier full` with your repo paths set in the `CHAMELEON_TEST_*_REPO` env vars (details in the harness README). We'd rather you check than take our word. ([tests/effectiveness/README.md](tests/effectiveness/README.md))

---

## Your code stays your code

This is a plugin that reads your source. Here is exactly what it does and doesn't do:

- **The hot path is fully offline.** The hooks that fire on every edit make zero network calls. No telemetry, no phone-home, ever. Everything chameleon learns lives on your machine, in `.chameleon/` in the repo and `~/.local/share/chameleon/`.
- **It does not execute your repo's code by default.** Parsing is static. Running your `tsc`, your test suite, or `npm audit` are all separate, opt-in switches, off until you set an environment variable, and even then resolved only from your repo's own `node_modules/.bin`, never your `PATH`.
- **The one default network call is a bounded `git fetch`** of your production branch at refresh time, so conventions track the latest production. It's timeout-capped, suppressed under CI, fails open, and is a single kill switch away (`CHAMELEON_FETCH_PRODUCTION_REF=0`).
- **A committed profile must be trusted before it's used.** Clone a repo with a `.chameleon/` someone else committed and chameleon won't inject any of it until you run `/chameleon-trust`. The trust step scans the profile's prose for prompt-injection and secrets first. Repo-derived content is wrapped so the model treats it as an example to imitate, never as instructions to follow.

If you want it gone for a bit: `/chameleon-pause-15m`, `/chameleon-disable` (this session), or `CHAMELEON_DISABLE=1` (global). It gets out of your way on command.

---

## Proof, not promises

Everything below is checkable in the repo right now:

| What | Count | Verify |
|------|-------|--------|
| Unit tests | **4,777** | `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ --co -q` |
| Released versions | **125** (v0.1.1 to v2.36.1) | `git tag \| wc -l` |
| Changelog | **3,299 lines** | `wc -l CHANGELOG.md` |
| First-class languages | **3** (TS/JS, Ruby, Python) | [extractors/registry.py](mcp/chameleon_mcp/extractors/registry.py) |
| CI matrix | Ubuntu + macOS + **native Windows**, Python 3.11 to 3.13 | [.github/workflows/ci.yml](.github/workflows/ci.yml) |

On top of unit tests, there are real-repo QA batteries per language, a hook-simulation battery, a hot-path latency benchmark, and a journey harness that drives real `claude -p` editing sessions against seed fixtures before each release. This is not a weekend prototype.

---

## What it actually supports

- **TypeScript / JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`), framework-agnostic, with awareness for **Next.js and NestJS** (framework detection, naming roles, and framework-specific anti-hallucination guidance).
- **Ruby**, framework-agnostic, with deeper structural awareness for **Rails** (sidecar detection, controller guards, archetype-aware base classes).
- **Python**, framework-agnostic, with awareness for **Django, DRF, Flask, and FastAPI**.

"Framework-agnostic" is the point: chameleon learns *your* repo's conventions, so it works on any framework. The named frameworks just get extra structural understanding where their conventions are strong.

---

## Command reference

| Command | What it does |
|---------|--------------|
| `/chameleon-init` | Parse the repo and build a profile |
| `/chameleon-trust` | Review and approve a profile for this machine |
| `/chameleon-refresh` | Re-derive after the code has moved |
| `/chameleon-status` | Profile health, drift, enforcement mode, rule precision |
| `/chameleon-teach` | Capture a convention AST can't infer (banned import, mandatory wrapper) |
| `/chameleon-auto-idiom` | Mine the repo for team idioms, evidence-backed, for your approval |
| `/chameleon-pr-review` | Multi-round review of a PR or diff against your conventions |
| `/chameleon-receiving-code-review` | Verify a teammate's review before you apply it |
| `/chameleon-explain` | Why a rule is active, or replay what chameleon knew the last time a file was edited |
| `/chameleon-doctor` | Triage your installation health |
| `/chameleon-journey` | Run the end-to-end journey test harness (release verification) |
| `/chameleon-disable`, `/chameleon-pause-15m` | Turn it off for the session, or pause it briefly |

---

## Built by people who ship with it

Chameleon is built by [Cris Nahine](https://github.com/crisnahine) and [Daniel Lisboa](https://github.com/danlisb). Daniel runs it on real work every day and keeps it honest: rough edges get found and fixed fast. We use it at [Empire Flippers](https://empireflippers.com/), on the code that runs the business. We depend on this tool, not a side project we shipped and forgot.

## Get started

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Then `/chameleon-init` and `/chameleon-trust` on a repo you care about, make an edit, and watch the model write code that already looks like yours.

Architecture and internals: [docs/architecture.md](docs/architecture.md). Install troubleshooting: [docs/install.md](docs/install.md). MIT licensed.
