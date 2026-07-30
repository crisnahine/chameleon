---
name: chameleon-deep-work
argument-hint: <task or ticket-URL>
description: "Use when the user explicitly invokes /chameleon-deep-work <task> to execute a substantive coding task end to end: dig the code and external unknowns first (no clarifying questions — parallel expert subagents work independent unknowns), present a 100%-understanding brief, then implement and verify in an isolated git worktree under chameleon's per-edit guardrails."
---

# Deep Work with Chameleon Context

Execute one substantive task end to end, the way a senior engineer who owns the
task does: absorb everything first, dig until nothing important is unknown,
state the plan once, then build in isolation with the repo's own conventions
enforced per edit. The user hands over a task and gets back a working,
verified change plus the decisions that were taken along the way - not a
stream of questions.

## The contract

Four rules, in priority order. They come from how the skill is meant to feel:
the user should be able to walk away.

1. **Understand the whole task before touching anything.** No edit, no
   scaffold, no "let me just start with the easy part" until Step 4's brief
   exists.
2. **Do not ask questions.** An unknown is either (a) answerable by digging -
   so dig; (b) a decision - so take the best default, name it in the brief,
   and keep building so it stays flippable; or (c) a missing hard dependency
   (an API key that does not exist, a service that is not deployed) - the only
   case that blocks, and it blocks with a one-line statement of exactly what
   is missing, not a question list.
3. **Dig all the code and do the deep research first - and staff the dig.**
   Chameleon's comprehension tools plus reading the real files, and external
   documentation for any library or framework behavior you are not certain
   of. Never guess an API you could verify. Digging is hired work, not a
   solo grind: two or more independent unknowns means expert subagents
   working them in parallel (see "Hire experts").
4. **Come back at 100%, then implement in a worktree.** The comeback is the
   Understanding Brief (Step 4) - a report, not a permission request. Then
   the implementation happens in a linked git worktree, never on the user's
   checked-out branch.

**With superpowers installed:** this skill is self-contained and asks no
questions, so `superpowers:brainstorming` is NOT in its path in either
direction - brainstorming is question-driven, which contradicts rule 2 above.
The dig (Steps 2-3) replaces it. `writing-plans` is likewise not a step here:
the Understanding Brief IS the plan, and it is written from a dig this skill
already performed rather than for an engineer assumed to have no context.

That contract is also stated in the SessionStart routing paragraph, but do not
rely on reading it there. That paragraph is optional prose appended only to a
digest that survived its budget fit whole, so it is absent whenever the digest
was squeezed - and it is squeezed hardest in a session already running inside a
linked worktree, which carries neither `CLAUDE.local.md` nor the
`.chameleon/conventions.md` it imports, so the conventions block cannot dedup
against the memory channel and is charged in full. Shedding it there is
deliberate. Stating the contract here is what makes it reliable regardless.

## Input formats

```
/chameleon-deep-work <task description>          → the task, in prose
/chameleon-deep-work <ticket key / URL>          → gather the task from the tracker first
```

Anything the user adds mid-flight (constraints, corrections) joins the brief
and, if it flips a taken default, is applied at the point the plan reaches it.

## Step 1: Absorb the task

Restate, to yourself, before any tool call:

- the goal (what exists after that does not exist now)
- the acceptance criteria (how the user will judge it done; if the task names
  none, derive them from the goal and put them in the brief)
- explicit constraints (stack, style, performance, compatibility)
- what is out of scope (say so in the brief; scope creep is a silent default
  nobody approved)

Then enumerate every unknown and classify each one as dig / default / hard
dependency per the contract. Worktree feasibility belongs in this triage: a
workspace that cannot host a linked worktree (not a git repo) is a rule-2c
hard dependency, surfaced here - not discovered after the dig. This list
drives Steps 2-3; the brief reports where each item landed.

## Hire experts (dispatch discipline)

The unknowns list from Step 1 is a work queue, and one context grinding
through it serially is the slowest and shallowest way to drain it. The
posture is proactive: whenever the list holds two or more independent
unknowns - different subsystems, different files, an internal question next
to an external one - hire expert subagents, one owned question each,
dispatch the batch concurrently, and work the remaining unknown yourself
while they run. This is a default with a stated exception, not a
judgment call to quietly wave off: on a repo over ~100 source files with 2+
independent unknowns, dispatch; declining to dispatch is legitimate only at
fixture scale or when every unknown is genuinely sequential, and the brief
then says so in one line ("experts: none - <reason>"). Three kinds of
expert, matched to the work:

- **Code scouts** (read-only): "map every call path into the gateway
  wrapper", "find how this repo does soft-deletion everywhere", "list every
  file the checkout flow touches". Dispatch the packaged `chameleon:code-scout`
  plugin agent (Task tool `subagent_type: "chameleon:code-scout"`); its
  definition carries the role and the read-only tool limits, so the dispatch
  prompt carries only the one question, the context, and the answer shape.
  When the harness does not expose that agent type, use its read-only explore
  agent type instead; a digging expert never edits anything.
- **Web researchers**: "what changed in this library between the lockfile's
  version and the latest docs", "the exact contract of this API at the
  pinned version" - resolved per Step 3's rules, never from memory. Dispatch
  the packaged `chameleon:web-researcher` plugin agent
  (`subagent_type: "chameleon:web-researcher"`; WebSearch/WebFetch only -
  include the pinned version in the prompt, the researcher cannot read the
  repo). When the harness does not expose it, dispatch a general-purpose
  subagent under the same rules.
- **Verifiers** (Step 6): a fresh-context pass over the finished diff -
  either a convergence-round review against the brief, or an adversarial
  bypass hunt on one surface. Fresh eyes catch what the author's context has
  gone blind to. Dispatch the packaged `chameleon:verifier` plugin agent
  (Task tool `subagent_type: "chameleon:verifier"`); its definition carries
  the two modes, the tool limits, and the output schema, so the dispatch
  prompt carries only the mode, this task's brief and acceptance criteria,
  the current diff, and the declined-findings log. When the harness does not
  expose that agent type, read `${CLAUDE_PLUGIN_ROOT}/agents/verifier.md` and
  dispatch a read-only explore agent with that file's body (everything after
  the frontmatter) prepended to the prompt - one source of truth, never a
  from-memory retelling of the role. That fallback does NOT reproduce the
  no-shell guarantee: a harness explore agent typically HAS Bash, so there the
  state discipline below is a rule the agent must follow rather than one the
  tool grants make impossible. Disclose it on the report's convergence line
  (slot 4), the way a self-review is disclosed, so a reader can tell which
  guarantee was in force.
  The packaged agent has NO shell on purpose: a verifier that runs the suite
  or drives the app mutates state that outlives it - a graded run handed over
  a branch whose suite was RED because a review subagent wrote two rows
  outside a transaction and the repo's whole-table ordering assertions then
  failed. Removing the shell makes that class impossible rather than
  forbidden, and running the gates is YOUR job at Step 6 anyway.

The dispatch recipe - every expert prompt carries three things:

1. ONE question, precisely scoped. A scout given five questions answers
   each at a fifth of the depth.
2. The context the expert cannot discover alone: the task's constraint, the
   paths already found, the pinned version, the repo root.
3. The required shape of the answer: file:line evidence for code claims,
   the doc URL and version for external claims, a verdict with reasoning
   for review findings.

An expert's answer is input, not truth. Before it enters the brief,
first-hand-verify every claim a decision rests on: read the cited line,
rerun the cited search, fetch the cited doc. Experts inherit the contract -
they answer their question and never ask the user one - and their claims
pass through Step 2's honesty gates like any other tool result. Solo
digging stays right when the task is one file, one subsystem, one question:
a dispatch that costs more than the dig it replaces is theater, not
thoroughness.

## Step 2: Dig the codebase (comprehension pass)

Ground the plan in the repo as it actually is, not as it is remembered. The
ladder, cheapest first - stop at any rung only when the remaining unknowns for
the files you will touch are zero. On a TRUSTED profile, rungs 1-4 are not
optional garnish before rung 5: skipping straight to raw reads forfeits the
derived call graph and archetype data this plugin exists to provide, and what
feels fine on a 30-file fixture silently degrades on a 3,000-file repo. Climb
in order; the only sanctioned shortcut is a repo small enough to read WHOLE
(under ~40 source files), and taking it must be said in the brief ("dig:
read all N files directly; comprehension rungs skipped as the repo is
smaller than the ladder").

1. `describe_codebase(repo=<repo_id>)` - language, framework, archetypes,
   scale. `detect_repo` first if the repo id is not yet known.
2. `search_codebase(repo, query=<symbol or concept>)` - find the functions
   AND classes the task touches, ranked, with signature and caller count.
   Search for what the task would DUPLICATE too: the helper you are about to
   write may already exist.
3. `get_pattern_context(file_path=<abs path>)` per file you expect to touch -
   its archetype, confidence, canonical witness. This is the shape the new
   code must blend into.
4. `get_callers` / `get_callees` per symbol you will modify, and
   `get_blast_radius` for anything whose signature or behavior changes - the
   recorded call sites are the contract you must not silently break.
   `query_symbol_importers` for any export you will move or remove.
5. Read the real files. The tools locate and rank; the plan is grounded in
   code you actually read, never in a tool summary alone.

On a wide surface - three or more subsystems in play, or an unfamiliar area
of a large repo - do not climb the ladder alone: hire one code scout per
subsystem (per "Hire experts") in a single concurrent batch, keep the
cheapest rungs for yourself, and let the scouts' file:line answers point
rung 5's reading. Scout claims pass through the same honesty gates below
before the brief cites them.

**The dig terminates on a fixpoint, not on the original list.** "Remaining
unknowns are zero" is graded against a list you wrote before you knew
anything - a list that only ever shrinks is a recall ceiling, and an unknown
never written down is trivially "resolved". So the dig (Steps 2 AND 3) may end
only after a re-enumeration pass: walk what the dig itself surfaced - a file
that referenced a subsystem not on the list, a tool result that contradicted
an assumption, a collaborator you now know you will touch - and list every NEW
unknown. Work the new items, then re-enumerate again. The dig ends when a
re-enumeration adds zero items; cap at 2 re-enumerations, and anything still
open at the cap enters the brief as a named default or risk, never silently.

Honesty gates on this pass:

- The comprehension tools are trust-gated: on an untrusted profile the graph
  and search tools return nothing, and `get_pattern_context` withholds the
  value-bearing content (canonical witness, idioms, rules) AND the archetype
  itself — the envelope comes back with a null archetype, not a name-only
  summary. If `trust_state` is `untrusted` (or no profile/grant exists), say
  so in the brief, suggest `/chameleon-trust`, and fall back to manual
  reading (grep + Read). A `stale` grant (rare — revalidation is opt-in) is
  different: content still flows, so use it, note the staleness in the brief,
  and suggest `/chameleon-trust`; never degrade to manual digging over
  staleness alone. Degraded digging is stated, never hidden.
- An EMPTY `get_callers` / importer result is absence of evidence, not
  evidence of dead code - dynamic and unindexed call paths are invisible.
  Never plan a removal on an empty result alone; grep before you conclude.
- If no `.chameleon/` profile exists at all, note once that `/chameleon-init`
  would arm the conformance layer, then proceed immediately with manual
  digging (do not wait for an answer - the no-questions contract holds here
  too) and say in the brief that the conformance layer is off.

## Step 3: Deep research (external unknowns)

For every unknown that lives outside the repo - a framework contract, a
library API, a protocol detail:

- Resolve it against the VERSION the repo actually uses: read the manifest /
  lockfile first, then that version's documentation. An API remembered from
  training data is a guess until verified.
- Prefer official docs and the installed package's own source/types over blog
  posts.
- Research is bounded by the task: stop when the unknowns list is empty, not
  when the topic is exhausted.
- Search deep, not wide-and-shallow: official docs for the pinned version
  first, then the changelog or release notes across the exact version
  window, then the installed package's own source. A blog post or a single
  search hit is a lead to verify, never an answer to cite.
- External unknowns are prime expert work: hire one web researcher per
  independent unknown (per "Hire experts"), dispatched in the same batch as
  the code scouts, so external answers land while the code dig runs.

## Step 4: The 100% Understanding Brief (the comeback)

The gate between digging and building. Like the Step 7 report, the brief is
a FIXED template, not a free-form summary: render every slot below in order,
an empty slot as `<slot>: none - <reason>`, never silently dropped. A slot
that does not appear in the rendered brief was not done:

1. **Goal & criteria** - restated, numbered.
2. **Files** - every file to create or change, each with its archetype and
   canonical (or "unprofiled - manual conformance" honestly noted).
3. **Contracts** - every symbol whose contract changes, its callers / blast
   radius mapped, the update plan for each call site.
4. **Unknowns** - each one resolved (with where it was verified, file:line)
   or defaulted (with the chosen default and the reason). Every expert
   answer a decision rests on was verified first-hand or is marked
   unverified here.
5. **Re-audit line** - "Unknowns re-audit: 0 new" or the leftovers named.
6. **Plan** - the ordered steps, each step ON ITS OWN LINE in the shape
   `<n>. <action> -> verify: <the specific check for THIS step>`. EVERY step,
   including the last ones (adversarial probes, the commit step) - a line
   without its `-> verify:` clause is not a step, and the steps that lose the
   clause are empirically the ones that later go undone. This slot
   is the one most often collapsed into slot 2 - a files list is WHERE the
   work happens, a plan is the ORDER it happens in and how each increment is
   proven; "verify: covered by the final test suite" on every line is a
   collapsed plan, not a plan.
7. **Risks & rollback** - the worktree makes rollback trivial; say what
   else, if anything, is hard to undo.
8. **Ladder line** - the fixed shape
   "Ladder: used <rungs by name> | skipped <rungs> - <reason> | read N of M
   source files", where N is your own count of files opened this session and
   M is the repo's source-file count from the glob/find you ran. Both numbers
   or the line is unfinished: "every file", "all of app/", and a bare "the
   repo is small" are not renderable in this shape, which is the point - a
   graded run wrote "I read every app/ file" over 15 of 19 with its own
   19-file find output in context. When N < M, the skipped-rung reason must
   survive the real M, not the read subset (the ~40-file whole-read shortcut
   is judged against M).
9. **Experts line** - "Experts: <N> dispatched (<one per unknown>)" or
   "Experts: none - <reason>" (over ~100 files with 2+ independent
   unknowns, "none" needs the reason).

Before presenting, run the same completeness pass as Step 7: all 9 slots
present in order, file:line for every evidence claim (a bare filename is a
pointer, not evidence), slot 6 in the literal per-step shape, and every
UNIVERSAL claim counted, not recalled: "all N files read", "every caller
updated", "throughout" are transcriptions from this session's tool calls -
count them before writing the word, or weaken to the honest subset ("the 24
files on the touched surface; the 15 framework stubs unopened"). A graded
run wrote "read all 39 files" over a transcript showing 24 - the honest
smaller claim would have scored; the padded universal failed it. Present it
compact, then PROCEED - do not end the turn with "shall I continue?". The
contract forbids question-stalling, the worktree makes every implementation
step reversible, and the user interrupts if the direction is wrong. The one
thing that pauses the skill is a hard dependency (contract rule 2c), stated
in one line.

## Step 5: Implement in a worktree

- **Never implement on the branch the user has checked out**: their working
  tree, stash, and half-staged files are not yours to disturb. If a worktree
  cannot be created (not a git repo, `git worktree add` fails, the sandbox
  denies it), that is a missing hard dependency of implementation - contract
  rule 2c: STOP and report it in one line, never fall back to implementing on
  the checked-out branch.
- Detect before you create (the session may already be in one), then place it.
  Read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-deep-work/references/worktree-setup.md`
  NOW, before creating anything. Do not run the detection or the placement
  priority from memory: both `rev-parse` claims a plainer form would rest on
  were refuted live on git 2.50.1, and the placement order carries a rule
  about the user's `.gitignore` that a reasonable-looking shortcut violates.
- Make it runnable, then baseline it - the reference above covers the install;
  this is the part that binds afterward. Run the gates for the surface you are
  about to touch once, BEFORE the first edit, and record the result on the
  build log's baseline line below. A pre-existing failure that survives a
  correct install is inherited, not yours to fix - name it for the Step 7
  report (scope holds) and keep building. The baseline is what keeps Step 6
  attributable: any new failure after it is yours.
- Chameleon follows you in. A linked worktree inherits the main checkout's
  profile and trust (`worktree.py` resolves the profile root through the
  `.git` file pointer), so the per-edit injection, the deny gates, and the
  turn-end review stay live on every edit you make there.
- Build one plan step at a time, in the plan's order. Run the step's own
  verification before moving on.
- **Keep a build log, and render it as you go.** This step is the plan's only
  execution record, and until it has one the plan is a promise nobody is
  holding. Emit one line the moment each plan step lands - not a batch
  reconstructed at delivery:

  ```
  Baseline: <the install command run | none needed - <reason>> | gates: <command> -> <result>
            | inherited failures: <named, N> | none
  Step <n>/<N>: <action> -> verify: <this step's own check> -> <the observed result>
  ```

  Three things fall out of the shape that exhortation has not held. A step you
  skipped has no line, so the gap is visible on the page instead of surviving
  to a report slot that asks you to remember it. A step whose verify you did
  not actually run cannot be filled in without writing an observed result you
  did not observe, which is the same fabrication class as an invented evidence
  cell. And the Step 7 report's not-verified slot becomes a TRANSCRIPTION -
  read the log, list the steps whose lines are missing or whose result was not
  clean - rather than a recollection of a plan you read many turns ago.
  `<N>` is the plan's step count from the brief; if the plan grew or shrank
  mid-flight, say so on the line where it changed.
- **The brief stays binding mid-flight.** When a premise the brief relied on
  turns out false during implementation, or a mid-flight user instruction
  materially changes the scope (not just flips a named default), STOP building
  on the broken premise - go back to Steps 2-4, re-dig what changed, and
  re-issue the brief before continuing. Sunk work is not a reason to push
  through on a falsified assumption; the worktree keeps the abandoned steps
  cheap to discard. When a chameleon advisory or block fires,
  fix the code to conform - the conventions are the repo's, not an obstacle;
  an inline `chameleon-ignore` override needs a reason the brief can defend.
- Commit as the repo's conventions dictate (imperative subject, why-not-what
  body), in reviewable units.

## Step 6: Verify like it ships

- Run the repo's own gates for the touched surface: its tests, its linter,
  its typechecker - whatever the repo itself uses - and compare against
  Step 5's baseline: an inherited failure is reported, not fixed (scope
  holds); any failure the baseline does not show is yours and blocks done.
- Drive the change end to end at least once - the real flow, not only the
  unit tests. A feature that has never run is not done.
- **Build the per-criterion evidence table.** One row per acceptance criterion
  from the brief: criterion | how it was exercised (the exact command run or
  flow driven that hits THIS criterion) | observed output (pasted, not
  paraphrased) | met / not met. "Re-read the diff", "covered by the suite",
  and "should work" are not evidence and may not fill a cell; a criterion
  whose row you cannot fill is not met yet. This table goes into the Step 7
  report verbatim.
- **Verify adversarially, not just affirmatively.** Passing gates prove the
  change works on the inputs it was built for; now try to BREAK it: drive the
  changed flow with hostile/edge inputs (empty, missing, oversized, the wrong
  type, the unauthorized caller), and for any new test, check it actually
  guards - a test that passes with the change reverted verifies nothing. The
  guard check is git-level, worktree-only, and it flips the SOURCE while
  keeping the TEST: with the work committed (Step 5),
  `git revert --no-commit <the commit(s)>`, then
  `git checkout HEAD -- <test paths>` to re-materialize the new test the
  revert just took out (test and code committed together is the normal
  reviewable unit, so this is the common shape); for uncommitted work,
  `git stash push -u -- <source paths>` (`-u`, so a brand-new source file
  stashes too). Run the new test and confirm it FAILS. Then restore - the
  committed path with `git reset --hard HEAD` plus `git revert --quit` (clears
  the in-progress revert state), confirming `git status --porcelain` is clean;
  the stash path with `git stash pop`, confirming the source edits are back
  and `git stash list` no longer holds them (porcelain is NOT clean here - the
  restored uncommitted work is the modifications). Prefer git for the flip:
  the live deny gates watch edits, and re-introducing pre-fix code by hand can
  be blocked halfway. An editor flip is acceptable ONLY when a git flip cannot
  isolate the behavior (a brand-new file the test imports: reverting the file
  fails the test on import, proving nothing) - and then the flip's named
  reason goes in the report's guard-check line, not just the transcript. When the test and the code it guards
  genuinely cannot be separated (they live in the same file), skip the check
  with that named reason in the report. Scale to risk: a task
  touching authorization, money, or data deletion hires adversarial experts
  (per "Hire experts") for a bypass hunt on the changed surface. This is the
  evidence a doubting "one more round, just to be sure" exists to demand -
  produce it in round 1.
- **Review to convergence, not once.** Round N: hire a NEW fresh-context
  reviewer (read-only, per "Hire experts" - never the round N-1 reviewer,
  never yourself) over the CURRENT diff, given the brief's acceptance
  criteria, the repo's conventions, and the declined-findings log - one
  (file:line, defect class, one-line decline reason) row per prior decline,
  not full reasoning - so rounds never relitigate a reasoned decline; a
  reviewer may re-raise a declined finding only by refuting the recorded
  reason with new evidence. Verify its load-bearing findings first-hand
  before acting; apply or decline each with a reason. Declining is not free
  convergence: a declined finding the reviewer rated blocking goes to the
  engine for independent adjudication of the decline —
  `chameleon_review(action="refute_finding", params={"repo": <repo_id>, "findings": [{id, kind, severity, file, line, claim, evidence}, ...], "base_ref": <the branch's merge base, or the locked production_ref, else "main">})`.
  Every key earns its place: `id` maps verdicts back, `file`/`line` is the
  excerpt the refuter prefetches (omit them and it silently degrades to the
  whole branch diff), `kind` is rendered VERBATIM into the adjudication prompt
  (omit it and the refuter is told `kind: None` about the claim it is judging),
  and `severity` picks the refuter's model — only `block` / `high` / `critical`
  escalate, so an unset severity adjudicates your highest-stakes declines with
  the base model. At most 8 findings per call (the per-invocation spawn cap; an
  over-cap send returns "unverified" for the tail). A
  `confirmed` verdict means the decline was wrong: apply the finding, and the
  round is non-converged. `refuted` upholds the decline. `unverified` — or a
  `refuter` envelope of `disabled` / `unavailable` / `untrusted` — upholds
  nothing: hold that decline as unadjudicated and say so in the convergence
  line, never present it as vindicated. If ANY finding was applied, the diff changed - the
  fixes are new unreviewed code, so run round N+1; also re-run the specific
  verification each applied finding's criterion or gate describes. The cap
  may be EXTENDED one round at a time past 3 only while the latest round
  still applied a finding (stopping mid-application is worse than one more
  look), each extension disclosed in the convergence line; a round that
  applies nothing ends the loop wherever it stands. This has
  no small-fix exemption: a test-only or 20-line fix is still unreviewed
  code, and "converged (0 findings after the fix)" without an actual
  post-fix reviewer run in the record is a mis-claim. The report's
  convergence line therefore names each round with its applied count -
  "Review convergence: N round(s) (r1: 3 applied, r2: 0) - converged" -
  so a final round with a nonzero applied count is self-evidently
  non-converged. Terminate
  when a round applies zero findings (converged); cap at 3 rounds, and a
  cap-hit with findings still being applied is reported as such, never
  silently. When expert dispatch is unavailable (you are yourself a
  subagent), run each round as a structured self-review against the brief and
  the conventions instead - degraded, and disclosed. The Step 7 report
  carries one line: "Review convergence: N round(s), <converged | cap hit>
  <, self-reviewed - no dispatch>."
- **Persist each finding's fate.** As the convergence loop resolves each
  reviewer finding, record its outcome to the local finding-fate ledger so
  per-lens precision accrues over time (`get_finding_fate_stats`): a declined
  finding (the declined-findings log) is `declined`, an applied one is
  `accepted`, a runtime-state one converted to a check is `converted`. Call
  `chameleon_review(action="record_finding_fate", params={"repo": <repo_id>, "fate": <accepted | declined | converted>, "message": <the finding's one-line gist>, "file": <file>, "line": <line>, "lens": <the finding's defect class>, "surface": "deep-work"})`
  once per finding. Only a digest of the text is stored, never the prose;
  best-effort, never blocks - on any failure, skip it. This is not optional
  bookkeeping to shed under time pressure: the Step 7 report carries one
  mandatory line - "Finding fates recorded: N accepted / M declined / K
  converted" (or "fate recording failed: <reason>") - and a report without it
  is incomplete, exactly like a missing evidence-table row. Likewise every
  DECLINED finding gets its own recorded one-line reason (the
  declined-findings log rows the next round's reviewer receives); "the others
  are non-issues" covering several findings at once records nothing and
  starves both the next round and the ledger.
- **Read the ledger back (calibration, advisory).** Before the convergence
  loop's first `refute_finding` send, call
  `chameleon_telemetry(action="get_finding_fate_stats", params={"repo": <repo_id>})`
  once — fail-open: on any error or an empty ledger, skip silently — and read
  `surfaces["deep-work"].lenses`. The `surfaces` map holds ONLY surfaces that
  have rows, so a repo that has never run deep-work simply has no `"deep-work"`
  key; that absence is the empty-ledger case, not an error, and it is the
  normal first-run state. Each lens bucket carries
  `{accepted, declined, converted, total, precision}` from this repo's own
  adjudication history. Use it to ORDER the refuter queue only: a decline
  that contradicts history (the finding's lens has high `precision` with
  `total` >= 5 — its findings are usually applied here) is the riskiest
  decline, so send it first. History never decides a finding's fate, never
  substitutes for first-hand verification, and a lens with a thin ledger
  (`total` < 5 or `precision` null) contributes nothing.

## Step 7: Deliver and integrate

The report is a FIXED template, not a free-form summary. Render every slot
below in order; a slot with nothing to say is rendered as
`<slot>: none - <reason>`, never silently dropped. An omitted slot makes the
report incomplete exactly like an unfilled evidence-table cell:

1. **Built** - what was delivered, one paragraph.
2. **Evidence table** - one row per acceptance criterion (criterion | exact
   command/flow | observed output pasted | met/not met). Fill this table by
   COPYING from tool results earlier in this session, then re-read each row
   asking "did I actually run this, this session?" - a cell describing an
   action not performed (a flow imagined from the code, a click never made)
   is fabrication, strictly worse than an honest "not driven". Any NUMBER in
   a cell (test counts, file counts, timings) must come from a tool result
   you can point at; a plausible-looking count written from memory is the
   same defect ("4 tests" for a 5-test file, never run alone).
3. **Guard checks** - which flips ran (git or editor-with-reason), which
   tests failed, restored-clean confirmation.
4. **Review convergence** - the per-round line ("N round(s) (r1: X applied,
   r2: 0) - converged | cap hit | self-reviewed"), plus ", fallback verifier -
   no packaged agent type" when Step 6 dispatched the fallback rather than the
   packaged agent, since that path does not carry the no-shell guarantee.
5. **Finding fates recorded** - "N accepted / M declined / K converted" or
   "fate recording failed: <reason>" or "none - zero findings". The numbers
   are a TRANSCRIPTION, never a recollection: derive them by re-counting
   your own `record_finding_fate` calls this session (or reading
   `get_finding_fate_stats` back) immediately before rendering - a graded
   run wrote "9 accepted" from memory over a ledger holding 8, with its own
   convergence line summing to 8 two lines up.
6. **Defaults taken** - one line each (contract rule 2b).
7. **Not verified** - what was not driven, and why. This includes any
   PLAN STEP from the brief that was not carried out: the plan is a promise,
   so a step silently dropped (the hostile-input probes that never ran) is a
   broken one unless it is named here. Derive this slot from the Step 5 build
   log, not from memory: every plan step with no log line, and every logged
   step whose observed result was not clean, belongs here. Reconciling "what
   you actually did" from recollection is the exact move that lost the
   hostile-input probes.
8. **Worktree** - path, branch, commit state, integration options: merge
   locally, push the branch and open a PR, or discard. The integration
   decision belongs to the user - pushing, merging into a shared branch, or
   opening a PR happens only on their explicit go. Every STATE claim in this
   slot is a pasted command result, never a characterization: run
   `git status --porcelain` at write time and paste its output (empty output
   IS the clean claim), same for the commit list (`git log --oneline`). "The
   tree is clean" written from impression is the defect - a graded run wrote
   "tracked files clean" with two tracked files dirty in a `git status` it
   had run one tool call earlier. The rule generalizes: any sentence about
   repo, process, or environment state carries the command output that
   proves it, or it is not made.
9. **Proactive follow-ups** - the dig and the implementation read a lot of
   code; what did that reading SURFACE beyond the task? Three lists, each
   entry one line: (a) adjacent issues observed near the changed surface
   (each with file:line - a latent bug, a stale pattern, a missing test the
   task did not cover), (b) the ranked next tasks this change sets up (the
   follow-up feature, the cleanup now unblocked), (c) risks worth watching
   (the fragile seam a future change will trip on). "none - <reason>" per
   empty list. Proactive means NAMING, never doing: acting on any of these
   is a new task on the user's go - scope discipline holds (contract rule
   2a), and this slot is where the discipline's byproduct lands instead of
   being silently discarded.

Before sending, run the completeness pass: all 9 slots present in order,
every evidence cell traceable to a real tool result this session, the
convergence and fates lines byte-shaped as specified, every universal
claim ("all", "every", "throughout") counted from the session record
rather than recalled - the same transcription discipline as the fates
line, and the graded failure mode both times - and NO process you
started still running - every dev server, watcher, or REPL spawned for
verification is killed before delivery (a graded run left its Flask dev
server serving on a high port after the report went out). The same discipline
applies to the Step 4 brief's checklist - a rendered brief or report missing
a slot is unfinished work, not a style choice.
- This applies on FAILURE too: a task that blocked on a hard dependency or
  could not pass verification still reports the worktree path and branch with
  whatever partial work it holds. Leave the worktree in place - removing it
  is the user's call, same as merging it. If the block hit before the
  worktree existed, there is no path to report and nothing to leave - say
  that instead.

## Integrity rules

- **No questions is not no communication.** The brief, the defaults, and the
  delivery report are the communication. Silence about a taken decision is a
  violation; a question that digging could have answered is too.
- **Never claim understanding you cannot cite.** Every "I know how X works"
  in the brief traces to a file you read, a tool result you received, or a
  doc you fetched - not to memory of similar codebases.
- **Experts answer; the brief decides.** Hired agents return evidence, never
  take decisions or make edits of their own. A defaulted decision stays
  yours to name and defend, whoever gathered the facts under it.
- **Empty results are not clearance.** An empty caller list, an empty search,
  an empty importer set - each means "the index sees nothing", never "it is
  safe". Grep before concluding.
- **Degradation is disclosed.** Untrusted profile, missing index, unsupported
  language: the skill keeps working with manual digging, and the brief says
  which layer was manual.
- **The worktree boundary is hard.** No edits to the user's checked-out
  branch, no `git checkout`/`reset` on their working tree, no force-push
  anywhere, and no push/PR/merge without their explicit instruction.
- **Scope holds.** The task in the brief is the task delivered. A discovered
  adjacent problem becomes one line in the report ("found, out of scope"),
  not silent extra work.

## Honesty Rules

- Restate the task in your own words and hold the delivery to that
  restatement; do not quietly redefine done.
- Say which unknowns were defaulted and which were verified, per item. A
  defaulted decision presented as a verified fact is a false claim.
- Report verification results faithfully: the command, the actual output,
  failures included. "Tests pass" without having run them is a lie.
- When digging hits a wall (unreadable code, missing docs, an ambiguous
  contract), the brief says so plainly instead of papering over it.
- The final report distinguishes what the change does from what it should do
  but was not exercised. Unverified paths are named, not implied to work.
