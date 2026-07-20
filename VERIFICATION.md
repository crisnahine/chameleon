# Chameleon — Independent Verification of the Full-Matrix Campaign

**What this is.** A skeptical, from-scratch audit of the claims in `TESTING.md`, run as a separate
exercise by a reviewer who did not perform the original campaign. `TESTING.md` closes with
*"the full 7,680-cell matrix passes with correct, effective output and zero failures"* and
*"the plugin is proven to work under real usage across every supported language and framework."*
Every such claim was treated as unproven until reproduced here.

**Baseline audited:** `b79b15c` (v4.4.50), branch `plugin-testing-fixes`, working tree clean.
**Shipped by this verification:** v4.4.51 → v4.4.55 (five releases, all tagged, deployed and re-verified).
**Date:** 2026-07-20 · host `darwin 25.5.0` arm64 · claude CLI 2.1.215.

---

## 1. Headline verdict

**The plugin works, and it is substantially better than the campaign's own critics would suggest —
but `TESTING.md`'s final claim is overstated in three specific, demonstrable ways.**

| Claim in `TESTING.md` | Verdict |
|---|---|
| 7,680 cells driven by real invocation, 0 FAIL | **Overstated.** The ledger is real and the fixes are real, but ~1,600 cells carry evidence tagged to a plugin version older than the fixes that followed, and 270 skill cells were filled by reading `SKILL.md` files and calling the underlying MCP tool rather than invoking the slash command. |
| Every shipped fix holds | **Mostly true, one false.** 33 fixes independently re-audited: 32 hold for their reported scenario; **GAP-005 was never actually fixed** and is repaired here. ~30 carry a demonstrated adjacent gap. |
| Fresh-repo clean-room confirms every fix | **Directionally true, not sufficient.** 10 brand-new repos built here reproduce a defect class the campaign declared resolved (opaque `cluster-<hash>` archetype names in 4 of 10). Fixed here as v4.4.52. |
| Robustness / fail-open holds under hostile input | **Confirmed.** 21 hostile payloads × 6 hooks: zero crashes, zero malformed output. This claim held up completely. |
| Framework classification correct in all 10 columns | **Confirmed** on 10 brand-new repos the fixes were never tuned against. 10/10. |

**Bottom line:** the plugin is genuinely effective and unusually robust. It is *not* "proven to work
100%", and the one place the campaign's own methodology was blind — a fix that reached a byte-perfect
cache directory no running session ever loaded — is exactly where the one genuinely false claim was hiding.

---

## 2. Critical findings

### F-1 — `GAP-005` was never fixed. The turn-end test-run advisory could never fire. (CRITICAL, fixed → v4.4.51)

`TESTING.md` lists GAP-005 as resolved in v4.4.18: *"turn-end test-run advisory unsatisfiable (wrong
payload key)"*, fixed by reading `exit_code` instead of `returnCode`.

**The Bash `PostToolUse` payload contains neither key.** Captured from a live session:

```json
{"stdout": "ok-one", "stderr": "", "interrupted": false,
 "isImage": false, "noOutputExpected": false}
```

So the v4.4.18 change swapped one absent key for another. Every command kept logging the `-1`
absent-value sentinel, and `session_test_run_seen` — which requires a zero exit — could never
return true.

**Red evidence** (this repo's own exec log, on the plugin's own development machine):

```
command rows: 37293    exit_code counts: {-1: 37291, 0: 2}
```

37,291 of 37,293 rows recorded `-1`, *after* the fix shipped. The advisory was unsatisfiable no
matter how much the user tested — which is precisely the user-visible symptom GAP-005 was filed for.

**Why the unit suite stayed green.** `tests/unit/test_exec_log_exit_code_contract.py` asserted the
broken behaviour (*"a payload with no exit code must not be read as success"*) because it was written
from the same misreading of the docs it was meant to guard. A fixture built from the implementation
encodes the implementation's assumption and passes against the bug.

**Root cause.** Per the official docs, `PostToolUse` *"fires only after a tool call succeeds"*; a
failed call raises the separate `PostToolUseFailure` event, which chameleon does not register for.
Confirmed empirically: `sh -c 'exit 3'` produced **no** `PostToolUse` invocation at all, while `echo`
did. The event itself is the status signal.

**Fix (v4.4.51).** An uninterrupted Bash `tool_response` records exit 0. An explicit
`exit_code`/`returnCode` still wins when a host sends one; an absent or non-dict response keeps the
sentinel rather than inferring success from a malformed payload. The contract test now uses the
captured payload shape as its fixture.

**Green evidence** — live headless session, real `pytest` run:

```
exit_code=0  test_seen=True      (v4.4.51)
exit_code=-1 test_seen=True      (v4.4.50, same session shape)
```

### F-2 — The deploy protocol verified a plugin no session was running. (CRITICAL, fixed → v4.4.51)

This is the methodological hole that let F-1 survive a 7,680-cell campaign.

`TESTING.md` §"fix-deploy protocol" documents three hops (dev tree → marketplace clone →
version-keyed cache) and states the rule *"a fix is never marked green against a stale plugin."*
`scripts/qa-deploy.sh verify` enforces it by diffing the cache directory against the dev tree.

**There is a fourth hop it never checked:** `~/.claude/plugins/installed_plugins.json`. Claude Code
resolves *which* copy to load from that registry. `qa-deploy.sh` materialized the new cache directory
and diffed it byte-for-byte — passing — while the registry still pinned the previous version, so
every newly started session loaded the **old** plugin.

**Caught for real:** after deploying v4.4.51 and seeing `verify` report
`OK: hooks and skills run the dev tree byte-for-byte`, a live `claude -p` session still produced the
v4.4.50 behaviour. The registry read `"version": "4.4.50"`. Only after correcting the pin did the
fix appear.

**Consequence for the original campaign:** any cell re-run through a *newly spawned session* after a
version bump could have been scored against a plugin that was never running. Cells driven by direct
hook/MCP invocation with an explicit `--plugin-root` are unaffected (most of the ledger), so this
does not invalidate the campaign — but it is the exact blind spot that allowed a never-fixed bug to
be recorded as fixed and re-verified.

**Fix (v4.4.51).** `deploy` now rewrites the registry pin (with a backup); `verify` fails when the
pin disagrees with the version under test:

```
OK: new sessions load v4.4.52 (installed-plugin registry agrees)
```

### F-3 — Opaque `cluster-<hash>` archetypes reproduce on brand-new repos. (HIGH, fixed → v4.4.52)

`TESTING.md` records four separate CRITICAL/HIGH fixes for hash-named archetypes (GAP-009a, 009b,
009b-ii, 022) and reports the class resolved.

**It is not resolved.** Bootstrapping 10 brand-new fixture repos (built for this audit, in domains no
fix was ever tuned against) on the deployed plugin:

| | hashed archetypes |
|---|---|
| before (v4.4.50) | **5** across 4 repos — `app/core`, `common/common`, `telematics/common`, `app/app` ×2 |
| after (v4.4.52) | **2**, both cohorts sitting directly in an `app/` source root |

The campaign's *own* ten fixtures still carried these hash names at sign-off (`py-django`, `py-drf`,
`py-flask`, `py-fastapi` — one each), while the matrix recorded 0 FAIL.

**What a user actually saw** (real PreToolUse hook fire on `app/core/audit.py`):

```
[🦎 chameleon: archetype=cluster-b2ee7e53, confidence=high, match_quality=exact, sub_buckets=1]
```

**Severity nuance, found by driving the real user path afterwards.** `/chameleon-init` runs a
rename pass that clears a hash when it can: on a fresh `py-fastapi` bootstrap it reported
*"Renames applied: 2 — `class-repositories` → `repositories`, `cluster-9aed0445` → `app-py`"*,
leaving zero hashed archetypes. So a user who onboards through the slash command is better off than
the raw bootstrap suggests. The defect is still real and worth fixing — the campaign's own ten
fixtures, bootstrapped through the MCP tool directly, carried the hash names into their committed
profiles, and every consumer that reads `archetypes.json` without going through `init` sees them —
but the user-visible blast radius is smaller than the raw derivation numbers imply.

**Root cause.** `core`, `common` and `shared` sat in `_STRUCTURAL_DIRS` next to the source roots, so
`_dominant_layer_name` refused to name the cohort and it fell through to a hash. The rationale ("a
location, not a purpose") is right for `src`/`app` and wrong for these three: they are the utility
layer a real repo actually names, and a hash carries *strictly less* information than the directory
it replaced.

**Fix (v4.4.52).** The three are removed from the exclusion set; source roots stay excluded and keep
a dedicated regression test. Verified across all 10 languages/frameworks (§4).

### F-4 — `/chameleon-journey` could start a ~$38 billed run with no confirmation. (HIGH, fixed → v4.4.53)

Found by driving the command for real rather than reading its `SKILL.md`. The skill documented the
cost (~$38, ~65 min, $40 cap) but its Run section went straight to the spawning command, with no
step putting the decision to the user. The project's own testing policy says of this exact harness:
*"Run before a release, not on every `/qa`. **Ask before spending.**"*

Reading the skill file — which is how the original campaign scored this cell — shows the cost
prominently and looks fine. Only invoking it exposes that nothing ever asks. Fixed: a free
`--dry-run` preflight, a stated projection, and an affirmative answer are now required;
`--dry-run` and `--list` stay ungated.

### F-5 — An idiomatic RSpec spec produced no canonical witness. (HIGH, fixed → v4.4.54)

The GAP-024 re-audit claimed plain-RSpec Ruby still ships a witnessless test archetype. I built a
paired A/B to isolate it: two gems identical except that one's specs carry `require "spec_helper"`.

```
bare         test witness = []                                    per-edit ctx = 657 B, no witness block
withrequire  test witness = [spec/clients/invoice_client_spec.rb]  per-edit ctx = 1067 B, witness block present
```

One line of difference. And the bare form is the *standard* layout — `rspec --init` writes `.rspec`
with `--require spec_helper`, so an idiomatic spec has no top-level require.

**Root cause.** The Ruby dimension extractor recognized only `class` / `module` / `def` / `require`
at column 0. A spec whose sole top-level node is `RSpec.describe … do` reported
`top_level_node_kinds == []`, canonical selection scored every member `trivial`, and the archetype
shipped with no witness.

**Fix.** A top-level receiver call opening a block (`do` / `{`) now counts as a `CallNode`. The block
opener is required, so an ordinary statement (`puts x`, a bare `Foo.bar`) still does not read as a
declaration. Green: the bare variant now yields the same witness as the require variant.

### F-6 — Every flat ESLint config reported a cause that was not true. (MEDIUM, fixed → v4.4.55)

Both TypeScript fixtures produced:

```
"parse_warning": "eslint.config.mjs: no top-level module.exports assignment found"
```

The file has no `module.exports` precisely because it correctly uses `export default` — the
canonical flat-config shape in ESLint's own documentation (an array, usually via `defineConfig([…])`
or `tseslint.config(…)`), and the default since ESLint 9. The message sent a reader hunting for a
CommonJS export that was never meant to exist, and named no way forward.

**I deliberately did not change the parse behaviour.** Reading a flat config means resolving spreads,
imports and helper calls — i.e. executing it — which is gated behind `CHAMELEON_ALLOW_ESLINT_EVAL`
so an untrusted repo's code never runs during bootstrap. That boundary is correct and tested. The
warning now states exactly that and points at the flag.

---

## 3. Re-audit of every claimed fix

33 of 37 claimed fixes were independently re-audited by agents instructed to **refute by default**,
each building its own minimal repro repo, bootstrapping it for real, and driving the deployed plugin.
(4 remain unaudited — see §6.)

| Verdict | Count | Meaning |
|---|---:|---|
| HOLDS | 2 | could not be broken at all |
| PARTIAL | 30 | **the reported scenario genuinely holds**; an adjacent variant of the same mechanism is still imperfect |
| NEVER-FIXED | 1 | GAP-005 (§F-1) |

**Calibration — this matters, and a naive reading gets it backwards.** 30 PARTIAL does *not* mean 30
broken fixes. In nearly every case the auditor verified the original defect is genuinely repaired,
often with a differential against the pre-fix build, and then found a *new* adjacent gap. Examples in
the auditors' own words:

- GAP-009b-ii — *"proved it with a differential rather than by trusting the claim: the identical repo
  bootstrapped against the pre-fix build yields `cluster-1e6c7e1d`… and against the deployed build
  yields `repository`."*
- GAP-023 — *"v4.4.39 produced layering `{}`; v4.4.50 produces 3 forbidden-upward edges. This is not
  NEVER-FIXED or REGRESSED."*
- GAP-011 — *"Every shape I threw at the comment-stripping mechanism survived intact, including the
  two exact symptoms in the report."*

So `TESTING.md`'s per-fix claims are **substantially accurate**. What it gets wrong is the leap from
"each reported case is fixed" to "the plugin works 100%".

**The dominant residual pattern, in one sentence:** almost every fix is a *hand-curated list or a
hardcoded position* that is correct for the layout it was measured against and silently wrong for a
common alternative of the same ecosystem. This is the same root cause `TESTING.md` itself identifies
in its effectiveness assessment — the campaign diagnosed the disease correctly and then treated the
symptoms one at a time.

Highest-value confirmed residuals (each demonstrated with a real invocation by its auditor):

| Gap | Residual still live |
|---|---|
| GAP-001 | The FP mechanism is untouched: `key`, `access`, `private`, `secret` are ordinary English words on the allow-list, so an ordinary path in prose is still reported as a leaked AWS key. It also introduced a **false negative** — `Authorization: Bearer <blob>` no longer opens the gate, contradicting the CHANGELOG's "loses no real coverage". |
| GAP-008 | Disclosure is gated on the answer being *empty*. One resolved caller flips the response to an affirmative `found:true, total:1` with real call sites missing — more dangerous than the reported `total:0`. |
| GAP-022 | Verified by me directly (§5): a dashed gem name (`acme-widgets` → `lib/acme/widgets/`, RubyGems' own convention, as in `aws-sdk-s3`, `rack-attack`) reproduces the original bug **verbatim** — 5 layers, 30 files, 1 archetype. |
| GAP-013 | The ten new role names were added to the filename map but not the *directory* map, so `repositories/`, `handlers/`, `clients/` as packages still miss. |
| GAP-016 | The widened gemspec regex requires whitespace before the quote, so `spec.add_dependency("x")` (no space) still parses to zero findings. |

### Fixes I re-verified myself, not via an agent

Five claims were checked directly by me with real invocations on brand-new fixtures, to avoid
relaying an agent's verdict on load-bearing points:

| Gap | My verdict | Evidence |
|---|---|---|
| GAP-019 (generic bases normalized) | **HOLDS** | `py-plain` fixture has `Repository[Detection]`, `Repository[Waveform]`, `Repository[Network]`… → derived `dominant_base: "Repository"` at frequency 0.875 over 8 samples. Without normalization each parameterization is a distinct base and no convention clears the floor. |
| GAP-028 (pairing floor) | **HOLDS** | `TEST_PAIRING_MIN_SAMPLE=5` now matches `MIN_SAMPLE_SIZE=5`, and `test_pairing` is genuinely derived on 5 fresh fixtures across all 3 languages (2–6 archetypes each). |
| GAP-034 (torn `.eslintrc.json`) | **HOLDS** | Purpose-built repro with a truncated `.eslintrc.json` → `rules.json` records `"parse_warning": "malformed JSON in .eslintrc.json: Expecting ',' delimiter: line 4 column 1"`, not a silent bare pass. |
| GAP-004 (engine floor) | **HOLDS** | A fresh bootstrap stamps `engine_min_version: "3.0.0"` (a static floor) alongside `engine_version: "4.4.52"` — not the self-orphaning "own version" the bug described. |
| GAP-008 (call-graph honesty) | **PARTIAL — residual confirmed** | See below. |

**GAP-008, verified both directions.** Accuracy is excellent: `get_callers` on a module-level
function returned `total: 7` with five caller records and exact line numbers `[41] [75] [33]
[32,35] [95,98]` — an independent grep found exactly those seven sites, so 100% precision and
recall. The instance-dispatch case is handled honestly: `get_by_email`, called as
`self._applicants.get_by_email(...)`, returns `total: 0` *with* the blind-spot note.

But the residual the fix re-audit reported is real. The note is gated on the answer being **empty**:
the 7-caller response carried `truncated: false` and **no note at all**. A function whose callers mix
module-level and instance dispatch therefore returns an affirmative, complete-looking answer that
silently omits the instance calls — which is more dangerous than the `total: 0` case the fix
addressed, because zero at least invites suspicion.

---

## 4. Re-verified matrix — first-party evidence

Ten **brand-new** repositories were built for this audit (55–190 real source files each, real git
history of 6–12 commits, real tool configs and lockfiles, deliberate outliers), in domains no fix was
developed against: freight tracking, recipe sharing, event ticketing, PDF invoicing, veterinary
clinic, seismic processing, community gardens, fleet telematics, podcast aggregation, insurance
quoting. Each was bootstrapped from zero on the deployed plugin over the real MCP stdio transport.

### Framework classification — 10/10 correct

| Col | Repo | Classified | Archetypes | Conventions | Hashed |
|---|---|---|---:|---:|---:|
| C1 | ts-plain | `None` ✓ | 12 | 7 | 0 |
| C2 | ts-nextjs | `nextjs` ✓ | 13 | 7 | 0 |
| C3 | ts-nestjs | `nestjs` ✓ | 10 | 9 | 0 |
| C4 | rb-plain | `None` ✓ | 8 | 9 | 0 |
| C5 | rb-rails | `rails` ✓ | 14 | 8 | 0 |
| C6 | py-plain | `None` ✓ | 8 | 12 | 0 |
| C7 | py-django | `django` ✓ | 18 | 11 | 0 |
| C8 | py-drf | `django` ✓ (DRF folds, as documented) | 12 | 11 | 0 |
| C9 | py-flask | `flask` ✓ | 9 | 10 | 1 · by design |
| C10 | py-fastapi | `fastapi` ✓ | 8 | 10 | 1 · by design |

The three agnostic columns (`None`) are the only place the framework-agnostic claim is actually under
test, and all three are correct. Derived archetype names are genuinely useful — `controller`, `dto`,
`entity`, `guard`, `interceptor`, `module`, `pipe`, `repository`, `service` for NestJS; `selector`,
`view`, `form`, `admin`, `migration`, `urls` for Django. This is the plugin's core value proposition
and it demonstrably works on unfamiliar code.

### Per-edit conformance — the core value proposition, driven on all 10

A real `PreToolUse` fire for a NEW file in each fixture's dominant layer, on the trusted profile:

| Col | Archetype resolved | Confidence / match | Context |
|---|---|---|---:|
| C1 ts-plain | `service` | high / exact | 6,655 B |
| C2 ts-nextjs | `lib-module-2` | high / exact | 6,253 B |
| C3 ts-nestjs | `service` | medium / ast | 2,258 B |
| C4 rb-plain | `class-renderers` | low / fallback | 3,079 B |
| C5 rb-rails | `service` | high / exact | 3,721 B |
| C6 py-plain | `service` | high / ast | 2,572 B |
| C7 py-django | `service` | high / exact | 8,069 B |
| C8 py-drf | `serializer` | medium / ast | 2,519 B |
| C9 py-flask | `service` | high / exact | — |
| C10 py-fastapi | `service` | high / exact | 6,160 B |

**9 of 10 resolve to the correct layer**, 8 of them carrying a canonical witness and a
reuse-before-create list. The one weak name is C2's `lib-module-2` — a numeric disambiguator, and
`lib/` in that repo really is a grab-bag of unrelated subdirectories, so the name is uninformative
but not wrong.

**A false alarm I raised against myself, worth recording.** My first C9 probe used
`app/services/digest_service.py` and got `class-clients, confidence=low, match_quality=fallback` —
which looks like a wrong-layer match. It was my error: that repo co-locates services as
`app/<feature>/services.py` and has no `app/services/` directory at all. Re-probed at the repo's
real convention, C9 returns `service, confidence=high, match_quality=exact`. Chameleon did the right
thing on a path that matches no cohort: it fell back and **said so** via `confidence=low,
match_quality=fallback` rather than asserting a confident wrong answer. That honesty signal is
load-bearing, and it worked.

### Enforcement — driven in all three languages, all four contracts hold

| Probe | Result |
|---|---|
| `eval()` / dynamic exec — Python, Ruby, TypeScript | **DENY** in all three, citing the exact line |
| Real-shaped AWS key (`AKIA2X7QFJ4NPLZW8VRT`) | **DENY** — "hardcoded credential in the proposed content" |
| AWS *documentation* example key (`AKIAIOSFODNN7EXAMPLE`) | **ALLOW** — correctly treated as a known placeholder |
| Bare `# chameleon-ignore` over an `eval` | **DENY** — a blanket-immune rule is not suppressed |
| Named `# chameleon-ignore eval-call`, on or above the line | **ALLOW** — the escape hatch works |

**Two false alarms I raised and then disproved.** My first pass reported "credential deny never
fires in any language" and "the named ignore does not work" — both were my own test errors. The key
I used was AWS's published documentation example, which the scanner is *supposed* to allow; and I
had placed the ignore comment at the top of the file rather than on or above the violating line.
Re-probed correctly, both behave exactly as documented. Recording this because an audit that reports
its first failing probe as a finding produces false criticism, and two of my five apparent findings
in this section did not survive a second look.

### Hook robustness — confirmed, no exceptions

21 hostile payloads × all 6 hooks: empty stdin, 300 random bytes, null fields, missing keys,
5,000-deep nesting, unicode / null-byte / `../` traversal / `/etc/passwd` paths, 4 MB content,
garbage text, truncated JSON, wrong-typed exit code.

**Result: every invocation `rc=0`, valid JSON or empty output, zero stderr, no traceback.** No path
escaped the repo boundary. `TESTING.md`'s robustness claim holds completely.

### Coverage actually re-driven here

`tests/verify/cells.jsonl` is this audit's own ledger (separate file, so its verdicts can never be
merged into the run it audits; driven via `qa-matrix.py --ledger`).

| | cells |
|---|---:|
| Re-driven with first-party evidence | **651** (8.5%) |
| Covered by the 33-fix adversarial re-audit | in addition, not cell-mapped |
| Not re-driven | 7,027 |

**I did not re-drive all 7,680 cells, and I am not going to claim I did.** Doing so with genuine real
invocations is a multi-session exercise. What I re-drove is the load-bearing subset: the full
bootstrap and classification path in all 10 columns on fresh repos, hook robustness across all six
hooks, the per-edit injection path, and 33 of 37 claimed fixes adversarially.

### Slash commands driven for real — C3 (NestJS) complete

The single largest evidence gap in the original campaign (M-4). All 14 commands driven as real
headless `claude -p` sessions against a brand-new NestJS fixture: **12 PASS, 1 PARTIAL, 1 FAIL**,
all 14 genuinely driven. The remaining nine columns were re-launched after a session limit.

The output quality is high and, notably, *honest*. `/chameleon-init` volunteered a 23-file coverage
gap and a bimodal cluster unprompted rather than claiming full coverage. `/chameleon-status` led
with "untrusted means the gates don't fire … armed on paper only" instead of burying it.
`/chameleon-doctor` explicitly separated two vacuously-green checks from genuinely exercised ones.
`/chameleon-teach` did not transcribe what it was told — it verified the claim against the code,
found a real counterexample the author had missed (`health.controller.ts` injects `DatabaseService`
directly), and recorded the exception instead of writing a false absolute.

**The one FAIL is a real defect, fixed here.** `/chameleon-journey` documented its cost (~$38,
~65 min, $40 cap) but its Run section went straight to the spawning command with no step putting
the decision to the user — so a bare `/chameleon-journey` could start an hour-long billed run
unprompted. This contradicts the project's own testing policy, which says of this exact harness:
*"Run before a release, not on every `/qa`. **Ask before spending.**"* Fixed in **v4.4.53**: a free
`--dry-run` preflight, a stated projection, and an affirmative confirmation are now required;
`--dry-run` and `--list` stay ungated.

**C2 (Next.js) corroborates C3 and adds two findings.** `/chameleon-init` auto-renamed 7 of 13
archetypes to human-readable names (`lib-module` → `repository`, `test` → `service-test`), left zero
hashes, and disclosed 17 below-floor files and a bimodal cluster unprompted. `/chameleon-teach`
again re-verified the taught claim against the tree instead of trusting it, and named the exact file
and line (`legacy-import-service.ts:7`) that enabling enforcement would break — then stopped rather
than editing source. `/chameleon-trust` refused to grant in a `--permission-mode bypassPermissions`
headless session without a typed token, which is the correct security posture.

Its two new findings: the flat-ESLint warning (F-6, fixed), and a **critical** claim that
`/chameleon-pr-review` produced no review across three real invocations on a diff with 8+ planted
defects, wedging past an 870 s timeout. **I could not verify that claim first-party** — reproducing
it needs the `claude -p` sessions the account limit blocked. It is recorded as *unverified*, not as
a confirmed defect, and is the single highest-priority item for a follow-up session. Note the same
agent also reported a harness defect of its own (parallel columns sharing one scratchpad, so capture
files clobbered each other), which is a plausible alternative explanation for an empty capture.

**A second C3 claim I narrowed.** The auditor reported that the `test` archetype covers only 3 of
14 spec files, so *"11 domain spec files get no archetype guidance."* The cluster count is right
(`cluster_size: 3`, pattern `src/common:ts`), but the conclusion is not: driving the real hook on
`src/customers/customers.service.spec.ts` returns `archetype=test, confidence=low,
match_quality=fallback`. Domain specs do get the test archetype — flagged honestly as a fallback
match, exactly as the design intends. The real (milder) issue is witness representativeness: the
canonical comes from `src/common/`, which may not model a domain service spec well.

### A claimed regression I could not reproduce as user harm

The GAP-024 re-audit reported a **regression**: that v4.4.41 lets a test file become the canonical
witness of a *source* archetype in colocated layouts, so editing a source file injects a `.test.ts`
under *"mirror the canonical witness below closely"*.

**The artifact-level leak is real.** I built a colocated repro (6 services + 6 sibling `.test.ts`)
and A/B'd it: on v4.4.40 the `service` archetype carries one canonical witness; on v4.4.52 it
carries two, and the second is `src/services/billing-service.test.ts`.

**The user-visible harm is not.** Across four real `preflight-and-advise` fires — a new file, an
edit to an existing service, a Write, and an unrelated module — the witness delivered to the model
was the **source** file every time. Selection scores the candidates rather than taking the list
head, and the source witness wins.

So this is a latent artifact-level issue worth cleaning up, not an active regression harming users.
Recording the distinction because "a test file is injected as the canonical witness" and "a test
file is stored as a secondary canonical that never gets served" have very different severities, and
only the second is supported by evidence.

---

## 5. Mismatches between `TESTING.md` and observed reality

| # | `TESTING.md` says | I observed | Severity |
|---|---|---|---|
| M-1 | GAP-005 resolved in v4.4.18 | Never fixed; 37,291/37,293 rows still `-1` after the fix | **CRITICAL** — fixed here |
| M-2 | "a fix is never marked green against a stale plugin"; `qa-deploy.sh verify` enforces it | `verify` passed while every new session loaded the previous version | **CRITICAL** — fixed here |
| M-3 | Archetype hash-naming resolved (4 fixes) | 4 of the campaign's own 10 fixtures, and 4 of 10 brand-new repos, still hashed | **HIGH** — fixed here |
| M-4 | 270 skill cells PASS | Filled by reading `SKILL.md` and calling the underlying MCP tool. Classified: 136 static file inspection, 53 MCP-tool-only, 79 other, **2** real slash-command invocations | **HIGH** — re-driving in progress (§6) |
| M-5 | "1,658 executed with real evidence, 0 FAIL, 0 BLOCKED" (§7.2) vs "6,184 PASS … 74 BLOCKED" (final report) | Two incompatible numbers in the same document | MEDIUM — documentation |
| M-6 | Cells re-verified against the current plugin | ~1,601 cells carry evidence tagged to a pre-4.4.33 build, 544 of them on language-sensitive items | MEDIUM |
| M-7 | GAP-022 (RubyGems layer collapse) resolved | Reproduces verbatim on dashed gem names — my own repro: 5 layers → **1** archetype, 30 files | **HIGH** — open, see below |
| M-8 | Inventory anchors verified (`file:line`) | 66 of 768 anchors name a file that does not exist (e.g. `plugin/hooks/hooks.js` — the real file is `hooks.json`); 0 point past EOF | LOW |
| M-9 | 74 BLOCKED cells are Windows-only, unreachable | Mostly correct, but `wrapper.run-hook.missing-arg` is marked BLOCKED in 7 columns and PASS in 3 — with *contradictory* evidence for the same behaviour. The Unix path is reachable: `rc=126` no-arg, `rc=127` bad name | LOW |

### M-7 in detail — reproduced by me, and left open deliberately

```
lib/acme/widgets/{renderers,formatters,validators,clients,models}/  (30 files, 5 layers)
  → 1 archetype: class-widgets (30 files)          # dashed gem name

lib/acmegem/{renderers,formatters,validators,clients,models}/       (30 files, 5 layers)
  → 5 archetypes, one per layer                    # undashed control
```

Root cause: `signatures.py` hardcodes the layer at `parts[2]`, but RubyGems maps a dashed gem name
onto a directory chain, shifting the layer to `parts[3]`.

**I attempted a fix and reverted it.** Anchoring on the file's immediate parent fixes the dashed case
but breaks `lib/gem/services/billing/invoice.rb`, which has an explicit passing test asserting that a
file nested below a layer stays in that layer's cohort. The two shapes are *structurally
indistinguishable from the path alone*; a correct fix needs the gem name from the `.gemspec`,
threaded through both the bootstrap write path and `tools.py:1664`'s read path. Getting that
threading wrong would create derivation-vs-enforcement drift — the exact bug class this campaign
identified as its own dominant defect source.

Shipping a fix that trades one real behaviour for another is the pattern this audit exists to catch,
so I stopped and documented it instead. Root cause, reproduction, and fix design are recorded above.

---

## 6. What is unresolved or unverified — stated plainly

- **7,027 of 7,680 cells were not re-driven by me.** They may well be correct; I did not verify them.
- **The 14 slash commands were driven in only 2 of 10 columns** (C2 Next.js, C3 NestJS). Three
  attempts were made; the account's session limit stopped the rest. **C1 and C4–C10 are unverified
  for the skills surface** — this remains the largest gap in the original campaign's evidence and I
  am not inferring those columns from the two that ran.
- **One critical claim is unverified:** that `/chameleon-pr-review` produces no review at all on a
  real diff (C2, three attempts). Reproducing it needs the sessions the limit blocked, and the same
  agent reported a harness bug that could explain an empty capture. Highest-priority follow-up.
- **1 of 37 claimed fixes is unaudited** (GAP-035). GAP-019, GAP-028 and GAP-034 were re-verified
  first-party after their agents were cut off — all three HOLD.
- **M-7 (dashed-gem layer collapse) is open** with a documented root cause and a rejected naive fix.
- **~30 adjacent residuals** surfaced by the fix re-audit (§3) are recorded but not fixed. Each is a
  real, demonstrated gap; none is a regression of a shipped fix.
- **Not run:** the journey harness (~$33), the effectiveness A/B eval, Windows/Linux cross-platform,
  and visual statusline rendering. All out of scope for this audit; none is claimed as verified.

---

## 7. Changes shipped by this verification

| Version | Change | Evidence |
|---|---|---|
| v4.4.51 | Record a Bash run's real exit status (F-1) | Live session: `exit_code=0 test_seen=True` |
| v4.4.51 | Deploy + verify the installed-plugin registry pin (F-2) | `verify` now fails on a stale pin |
| v4.4.52 | Name `core`/`common`/`shared` cohorts after their directory (F-3) | Hashed archetypes 5 → 2 across 10 fixtures |
| v4.4.53 | `/chameleon-journey` asks before spending (F-4) | Skill had no consent step before a ~$38 run |
| v4.4.54 | Give an idiomatic RSpec spec a canonical witness (F-5) | Paired A/B: witness `[]` → present; context 657 B → 1,044 B |
| v4.4.55 | Name the real cause for an unreadable flat ESLint config (F-6) | Warning no longer cites a `module.exports` the file never had |

Supporting tooling: `scripts/qa-mcp-call.py` (exercise MCP tools over the *real* stdio transport
rather than an in-process import) and `qa-matrix.py --ledger` (a separate cell file for an
independent audit).

Unit suite: **6,267 passed, 3 skipped**, `ruff check` and `ruff format --check` clean. Both releases
tagged, deployed through all four hops, and re-verified on the deployed copy.

---

## 8. Honest overall assessment

**Does the plugin truly work 100% under real usage? No — and no non-trivial plugin does.**

What is true, and well-supported by this audit:

- Framework classification, archetype derivation, and convention extraction work correctly and
  usefully on ten unfamiliar codebases across three languages and six frameworks.
- Fail-open robustness is excellent. 126 hostile invocations produced zero crashes and zero malformed
  output. This is the strongest part of the system.
- 32 of the 33 re-audited fixes genuinely repair what they claim to repair, verified by differential
  against pre-fix builds.
- The original campaign found real, deep, unit-test-invisible bugs. Its diagnosis of its own dominant
  defect class is accurate and impressive.

What is not true:

- "0 FAIL across 7,680 cells" rests on evidence that is uneven in quality — skill cells filled by
  file inspection, ~1,600 cells tagged to superseded builds, and two contradictory coverage counts.
- "Every fix holds" is false in one case (GAP-005) and incomplete in about thirty, where a
  hand-curated list is correct for the tuned layout and wrong for a common alternative.
- The clean-room sign-off could not have been fully load-bearing: brand-new fixtures built here
  immediately reproduced a defect class the report declared resolved, and driving the slash commands
  for real in just two columns produced four more defects worth releasing (F-3 through F-6).

**The pattern across all six findings.** Every one was invisible to the method that scored it green.
GAP-005 was invisible to a unit test written from the same misreading of the docs. F-2 was invisible
to a byte-for-byte directory diff. F-4 was invisible to reading the skill file, where the cost is
documented prominently. F-5 was invisible to any fixture whose specs happened to carry a `require`.
F-6 was invisible to a bootstrap that checks only whether a warning exists, not whether it is true.
**Real invocation is not a more thorough version of static verification — it observes a different
thing**, and the campaign's ~1,600 statically-scored cells are where I would look next.

**The single most important lesson** is F-2. The campaign built a careful protocol to guarantee it
never tested a stale plugin, wrote a script to enforce it, and the script checked three of the four
hops that matter. A fix can be committed, versioned, propagated, byte-verified, and still not be the
code that runs. That is how a never-fixed bug survived thirty-plus releases and a 7,680-cell matrix
while being recorded as fixed twice.

**Recommendation:** the plugin is fit for real use. `TESTING.md`'s final two sentences — *"All
documentation is up to date. The plugin is proven to work under real usage across every supported
language and framework."* — should be softened to match the evidence, and the residuals in §3 and §6
should be worked as a normal backlog rather than treated as closed.
