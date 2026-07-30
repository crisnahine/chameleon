# deep-work reference — Step 5: detecting, placing, and creating the worktree

Read this once, at the moment you create the worktree. Everything here is
one-time setup mechanics; the rules that bind for the whole of Step 5 (never
implement on the user's checked-out branch, the baseline, the build log) stay
in the skill body.

## Detect before you create

The session may ALREADY be inside a linked worktree. The test is that
`git rev-parse --path-format=absolute --git-dir` differs from
`git rev-parse --path-format=absolute --git-common-dir` — compare them as
absolute paths, because the raw outputs differ spuriously when run from a
subdirectory. Both claims in the guidance this was adapted from were refuted
live on git 2.50.1, so use exactly this form rather than a plainer
`rev-parse`.

A plain submodule does NOT produce that mismatch. There,
`git rev-parse --show-superproject-working-tree` prints the superproject path
instead of nothing — that is how you tell the two apart.

Being in a linked worktree is not enough on its own. Use the current one only
when it is DEDICATED to this task: the harness created it for this session, or
`git status --porcelain` is clean with no user work parked there. Otherwise it
is the user's workspace like any other checkout, and you create a SIBLING
worktree per the placement rules below. "Never nest" means never place the new
worktree inside the current one; a sibling is fine.

## Placement, in priority order

1. **An explicit user placement wins over every tool default.** When the
   user's instructions (or the project's `CLAUDE.md`) declare where worktrees
   live, hand that path to the harness's native worktree tool if it accepts
   one, else use the git fallback at that location.
2. **With no declared placement, prefer the harness's native worktree tool**
   when one exists. A manual `git worktree add` beside a native tool leaves
   phantom state the harness can neither see nor clean up. Report the branch
   it creates as-is in the Step 7 worktree slot — do not rename it to match a
   convention it did not follow.
3. **Only without a native tool, fall back to git**, placing the worktree by
   priority:
   - an existing `.worktrees/` or `worktrees/` directory at the repo root,
     but ONLY if `git check-ignore` confirms it is ignored. If it is not
     ignored, do NOT edit the user's `.gitignore` — that edits their
     checked-out branch, which Step 5's hard boundary forbids. Fall through
     instead.
   - the sibling default `../<repo>-deep-<slug>`.

   Every git-fallback placement creates the same branch — from the repo root,
   `git worktree add <dir> -b deep/<slug>` — only the directory differs.

## Make it runnable before you baseline

A fresh linked worktree shares the repo's HISTORY, not its installed state:
`node_modules`, `.venv`, and every other gitignored build artifact are absent
even though the tree looks complete. Run the repo's own dependency setup first
(the lockfile's install command) or the baseline you take will measure a
missing install rather than the code.

No lockfile present — a vendored-dependency repo, a stdlib-only script
collection — means there is no install step, not that there is nothing to
check: confirm the gates run at all (import the entry point, invoke the
linter) before treating the tree as ready.

A gate failure that traces to a missing install is a SETUP artifact, not an
inherited failure. Fix it by installing, then re-baseline. Only a failure that
survives a correct install is inherited, and only an inherited one belongs in
the Step 7 report as pre-existing.
