---
name: chameleon-deep-work
description: "Use when the user explicitly invokes /chameleon-deep-work <task> to execute a substantive coding task with the deep-work discipline: understand the whole task first, ask no clarifying questions (resolve unknowns by digging and by defaulting open decisions), map the code with chameleon's comprehension tools until understanding is complete, present a 100%-understanding brief, then implement in an isolated git worktree under chameleon's per-edit guardrails."
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
3. **Dig all the code and do the deep research first.** Chameleon's
   comprehension tools plus reading the real files, and external documentation
   for any library or framework behavior you are not certain of. Never guess
   an API you could verify.
4. **Come back at 100%, then implement in a worktree.** The comeback is the
   Understanding Brief (Step 4) - a report, not a permission request. Then
   the implementation happens in a linked git worktree, never on the user's
   checked-out branch.

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
dependency per the contract. This list drives Steps 2-3; the brief reports
where each item landed.

## Step 2: Dig the codebase (comprehension pass)

Ground the plan in the repo as it actually is, not as it is remembered. The
ladder, cheapest first - stop at any rung only when the remaining unknowns for
the files you will touch are zero:

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

Honesty gates on this pass:

- The comprehension tools are trust-gated: the graph and search tools return
  nothing on an untrusted profile, and `get_pattern_context` withholds the
  value-bearing content (the canonical witness, idioms, rules), returning only
  the archetype name and confidence. If `trust_state` is not `trusted`, say so
  in the brief, suggest `/chameleon-trust`, and fall back to manual reading
  (grep + Read). Degraded digging is stated, never hidden.
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

## Step 4: The 100% Understanding Brief (the comeback)

The gate between digging and building. Every box checked, or back to Steps
2-3:

- [ ] Goal and acceptance criteria restated
- [ ] Every file to create or change is listed, each with its archetype and
      canonical (or "unprofiled - manual conformance" honestly noted)
- [ ] Every symbol whose contract changes has its callers / blast radius
      mapped, with the update plan for each call site
- [ ] Every unknown is resolved (with where it was verified) or defaulted
      (with the chosen default and the reason)
- [ ] The step plan exists, ordered, each step with its own verification
- [ ] Risks named, with the rollback (the worktree makes rollback trivial;
      say what else, if anything, is hard to undo)

Present the brief to the user, compact. Then PROCEED - do not end the turn
with "shall I continue?". The contract forbids question-stalling, the
worktree makes every implementation step reversible, and the user interrupts
if the direction is wrong. The one thing that pauses the skill is a hard
dependency (contract rule 2c), stated in one line.

## Step 5: Implement in a worktree

- Create it: the harness's native worktree tool when one exists, else
  `git worktree add ../<repo>-deep-<slug> -b deep/<slug>` from the repo root.
  Never implement on the branch the user has checked out: their working tree,
  stash, and half-staged files are not yours to disturb. If a worktree cannot
  be created (not a git repo, `git worktree add` fails), STOP and report that
  in one line - never fall back to implementing on the checked-out branch.
- Chameleon follows you in. A linked worktree inherits the main checkout's
  profile and trust (`worktree.py` resolves the profile root through the
  `.git` file pointer), so the per-edit injection, the deny gates, and the
  turn-end review stay live on every edit you make there.
- Build one plan step at a time, in the plan's order. Run the step's own
  verification before moving on.
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
  its typechecker - whatever the repo itself uses.
- Drive the change end to end at least once - the real flow, not only the
  unit tests. A feature that has never run is not done.
- Re-read the whole diff against the brief's acceptance criteria, one final
  pass, before declaring done: every criterion either demonstrably met or
  explicitly reported as not met and why.
- Chameleon's turn-end gates have been reviewing each turn; anything they
  surfaced is addressed or consciously carried into the report.

## Step 7: Deliver and integrate

Report back:

- What was built, against each acceptance criterion - met / not met, with
  evidence (the command run, the output observed).
- Every default taken (contract rule 2b), one line each, so any of them can
  be flipped cheaply now.
- What was verified clean, and what was NOT verified (and why).
- The worktree path and branch, with the integration options: merge locally,
  push the branch and open a PR, or discard. The integration decision belongs
  to the user - pushing, merging into a shared branch, or opening a PR
  happens only on their explicit go.
- This applies on FAILURE too: a task that blocked on a hard dependency or
  could not pass verification still reports the worktree path and branch with
  whatever partial work it holds. Leave the worktree in place - removing it
  is the user's call, same as merging it.

## Integrity rules

- **No questions is not no communication.** The brief, the defaults, and the
  delivery report are the communication. Silence about a taken decision is a
  violation; a question that digging could have answered is too.
- **Never claim understanding you cannot cite.** Every "I know how X works"
  in the brief traces to a file you read, a tool result you received, or a
  doc you fetched - not to memory of similar codebases.
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
