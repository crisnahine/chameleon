---
name: chameleon-explain
argument-hint: "[rule | file-path]"
description: Use when the user explicitly invokes /chameleon-explain to drill down on one enforcement rule (its calibration, would-block frequency, inline-override rate) OR to replay what chameleon knew and did the last time a file was edited (post-incident gap analysis)
---

# /chameleon-explain

Two drill-downs share this command, dispatched on the argument shape:

- `/chameleon-explain <rule>` — explain one enforcement rule: why it's active or demoted, how often it would have blocked real edits, how often the team overrode it. The drill-down behind the `/chameleon-status` enforcement summary.
- `/chameleon-explain <file>` — replay what chameleon knew and did the last time that file was edited, and classify why the gate stayed silent. The recovery loop for a postmortem.

Dispatch: if the argument names an existing file (or looks like a path — has a `/`, a known source extension, or matches a file in the repo), run the **file** flow. Otherwise treat it as a rule name and run the **rule** flow. With no argument, explain every rule that has activity (rule flow).

---

# Rule flow: `/chameleon-explain <rule>`

Explain a single enforcement rule for the active repo: why it's active or demoted, how often it would have blocked real edits, and how often the team overrode it with inline `chameleon-ignore`.

Usage: `/chameleon-explain <rule>` (e.g. `/chameleon-explain import-preference-violation`). With no rule, explain every rule that has activity.

## The two axes — keep them separate

A rule's health is two independent measurements. Do not collapse them into one number.

1. **Bootstrap calibration (`fp_rate`)** — a one-shot measurement taken when the profile was built, running the rule against the repo's own committed files. A demoted rule carries the `fp_rate` that demoted it. This is frozen until the next refresh.
2. **Live override rate** — measured on real AI edits since then. Of the edits where the rule fired, what fraction got waved through with an inline `chameleon-ignore`. A rule can read `fp_rate` 0.000 and still be overridden on most edits — that means it passes on committed code but fights the team on new code.

An override is a **contention** signal, not a false-positive count. An override can mean the rule is wrong, OR that the edit was a genuine, documented intentional deviation (which is exactly what the escape hatch is for). The override rate tells you the rule is contested; the *sample* tells you whether the contest is the rule's fault.

## The flow

1. Resolve the repo via `chameleon-mcp::detect_repo(<file-path>)`.
2. Call `chameleon-mcp::chameleon_telemetry(action="get_status", params={"repo": <repo>})` — read `enforcement.active` / `enforcement.demoted` to state whether the rule is holding and, if demoted, its bootstrap `fp_rate`.
3. Call `chameleon-mcp::chameleon_telemetry(action="get_override_audit", params={"repo": <repo>})` (defaults to `CHAMELEON_OVERRIDE_AUDIT_WINDOW_DAYS`, 21). Read `rules[<rule>]`:
   - `overrides` / `would_blocks` — the two event counts.
   - `override_rate` — overrides / (overrides + would_blocks), or null below the event floor (too few events to read a rate).
   - `blanket` — how many overrides came from a bare `chameleon-ignore` with no rule name.
   - `high_override_rate` / `blanket_abuse` — the flags.
4. Call `chameleon-mcp::chameleon_telemetry(action="get_shadow_report", params={"repo": <repo>})` and print the `sample` rows for this rule (`rule  file:line  ts`) so the user can open the actual edits and judge whether the overrides were genuine deviations or the rule misfiring.

## What to recommend

- **`high_override_rate`** — the rule is fighting the team. Read the sample. If the overridden edits are genuinely off-pattern (the rule is right, the team is wrong), leave it. If the rule is wrong: the convention itself is wrong → `/chameleon-teach`; the rule is miscalibrated for this repo → `/chameleon-refresh`, which re-runs calibration and rewrites the enforcement verdict **before** the trust-hash snapshot, so the demotion lands inside the trusted profile instead of de-trusting it.
- **`blanket_abuse`** — overrides are mostly bare directives. Someone is stamping `chameleon-ignore` with no rule name, downgrading every block-eligible rule at once. Flag it: a bare directive is a blunt instrument, and a high bare share reads as routing around the gate wholesale rather than annotating one deviation.
- **Never auto-demote.** This surface only reports. Demotion is a human decision routed through refresh-time recalibration; nothing here mutates `enforcement.json` at runtime (that artifact is in the trust hash, so a runtime flip would de-trust the profile and silently disable blocking repo-wide). Refresh-time recalibration auto-demotes a contested rule only when its override evidence spans at least `CHAMELEON_OVERRIDE_DEMOTION_MIN_SESSIONS` distinct sessions (default 2); below that floor, and always for security-class rules (`eval-call`, `secret-detected-in-content`), the demotion is recorded as `demotion_proposed` in `enforcement.json` and surfaced by `/chameleon-status` as `proposed_demotions` while the rule keeps blocking. `chameleon-mcp::chameleon_telemetry(action="get_status", params={"repo": <repo>})` returns the pending-proposal list under `enforcement.proposed_demotions` **only when it is non-empty** — the key is ABSENT (not `None`, not `[]`) on the common repo with no pending proposals, so read it with `.get("proposed_demotions", [])` and treat absence as "no pending proposals", never as an error.

## Out of scope

No false-positive *fraction* is computed — the data has no accept/fix outcome signal, only override-vs-would-block frequency. The human reads the sample and decides.

---

# File flow: `/chameleon-explain <file>`

The recovery loop. When a defect escapes (the thing review would have caught), this reconstructs what chameleon knew and did the last time that file was edited, so a postmortem can classify the miss and route the fix instead of letting the same class escape again.

Usage: `/chameleon-explain <file>` (e.g. `/chameleon-explain src/checkout/charge.ts`). The argument can be an absolute path, a `~` path, or a repo-relative path.

## Why this exists

The per-edit context chameleon injects is ephemeral. Once the session ends there's no record of "when this file was edited, chameleon matched archetype X at fallback quality and raised nothing", so a postmortem can't reconstruct why the gate stayed silent. The decision log persists exactly that, keyed by repo-relative path, and survives refresh (closing a gap must not destroy the record of the escape being diagnosed).

## The flow

1. Resolve the repo via `chameleon-mcp::detect_repo(<file-path>)`.
2. Call `chameleon-mcp::explain_edit(repo, file_path)`. It returns the most-recent decision-log row and a `classification`.
3. If `found` is `False`, say so plainly: no edit of this file was ever logged. Either it was edited outside a chameleon session, or before the decision log existed, or the path doesn't match what was stored (check you passed the same path form). There is nothing to reconstruct.
4. When `found` is `True`, read the `decision` block and present `classification` with the right route.

## The classification — coverage gap vs in-scope miss

The single question a postmortem must answer: did chameleon *not see* the file's shape, or did it see the shape and *still miss* the defect? They route to different fixes.

- **`coverage-gap`** — no archetype matched, or it matched at `match_quality` `none`/`fallback`. The per-edit lint never had a calibrated SHAPE to check against, so no shape-specific rule could fire for this file's kind. Note this does NOT mean nothing was raised: the archetype-independent scans (secrets, `eval`, cross-cutting advisories) can still have flagged the edit, so `decision.violations_raised` may be > 0 on a coverage-gap row (it is classified coverage-gap because the SHAPE was uncovered, ordered before the violations check). Say "no calibrated shape for this file's kind; any advisories that fired came from cross-cutting rules, not shape rules" rather than "nothing could have been caught". **Route:** `/chameleon-refresh` to re-derive archetypes (the file's directory may have grown into its own archetype since the last bootstrap), or `/chameleon-teach` to capture the missing convention. This is a hole in shape coverage, not a rule failure.
- **`in-scope-miss`** — an `ast`/`exact` archetype matched and chameleon raised nothing at all. The shape *was* covered; no rule existed to catch this defect class. **Route:** a new rule or idiom, not a refresh. Refreshing re-derives the same archetypes and changes nothing — the gap is the missing check, and the honest answer may be that this defect class is in the "what stays human" set (logic, dataflow, cross-file, auth) that no shape rule reaches.
- **`advised`** — an `ast`/`exact` archetype matched and chameleon *did* raise advisories (or shadow-logged a would-block), but did not block. The shape was covered and the rules fired; they were advisory, so the edit shipped with the warning attached rather than silently. **Route:** not a coverage hole and not a missing rule, so do not `/chameleon-refresh` (it changes nothing here). Ask whether the advisory was surfaced and heeded, and whether this defect class warrants promoting a rule to block-eligible — read `decision.blockable_rules` and the rule's override history via the `/chameleon-explain <rule>` flow above.
- **`blocked`** / **`overridden`** — the gate was *not* silent. It blocked the edit, or it would have blocked and someone waved it through with an inline `chameleon-ignore`. If the defect still escaped, the postmortem question shifts: was the block reverted, or was the override unjustified? Read the `decision.blockable_rules` and `decision.outcome` and follow up on the override (the `/chameleon-explain <rule>` flow above shows the rule's full override history).

## Reading the decision block

`decision` carries: `archetype`, `match_quality` (`none`/`fallback`/`exact`/`ast`, or `null` when the gate fired before a canonical was matched — render that as "n/a", not a literal "None"), `confidence_band`, `violations_raised`, `blockable_rules` (the block-eligible rules that stood on the file), `outcome` (`advised`/`would-block`/`blocked`/`overridden`/`clean`), `session_id`, and `observed_at`. State the archetype and match quality first — that's the coverage half — then what was raised and what the gate did.

## Out of scope

One file, one row: the most-recent edit only. This is a per-edit replay, not a history view; it does not trend a file's edits over time or correlate across files. It reconstructs the last decision so a human can classify and route the miss; it does not itself decide whether the code was wrong.

## Honesty Rules

- Report only the real recorded state: the rule's actual calibration, would-block frequency, and inline-override rate, or the file's actual last-edit replay. Never fabricate a number, a rule, or an event the profile / telemetry does not hold.
- If the data is missing (no telemetry, no recorded edit, an unknown rule), say so plainly; don't infer a plausible answer.
- A post-incident replay names the gap between what chameleon knew and what it could not see honestly; it doesn't excuse the miss or decide whether the code was wrong.
