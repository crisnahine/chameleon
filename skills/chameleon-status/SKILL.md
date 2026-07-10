---
name: chameleon-status
argument-hint: "[--shadow]"
description: Use when the user explicitly invokes /chameleon-status to view profile state, drift indicators, and trust state for the current repo
---

# /chameleon-status

Surface the current state of chameleon for the active repo. The user-facing observability surface — like `git status` for chameleon profiles.

## What it reports

What's plumbed today (read straight from `.chameleon/` and `drift.db`):

1. **Profile summary** — language, archetype count + names, schema version, generation, last bootstrap timestamp.
2. **Trust state** — `trusted | untrusted | stale | n/a`, with the trusting user and grant timestamp when present.
3. **Structural conformance (NOT a quality bar)** — `days_since_refresh`, the drift score (now surfaced as `structural_conformance_score`), and a `recommended_action` string from `get_drift_status`. This score is 1 - mean structural-match confidence: it measures how closely recent edits match their archetype's *shape*, nothing more. **Print the `conformance_disclaimer` line verbatim, immediately under the heading and above the number**, so the reader can never read a low score as a correctness or quality bar. `get_drift_status` returns the disclaimer string and the `blind_spots` list (`logic, dataflow, cross-file, auth checks`) — echo the disclaimer as-is. A perfectly off-pattern-but-on-shape edit (right shape, wrong logic, null deref, missing auth check) scores as zero drift, so a green conformance number says nothing about whether the code is safe.
4. **Language hint** — when a Rails-with-frontend (or TS-with-Ruby-sidecar) was detected, name the secondary tree so the user can bootstrap it separately.
5. **Version coherence** — call `daemon_status` to get `running_version` (also returns `alive`, `pid`, `socket`, `uptime_s`, `last_request_at`). If the running version differs from the installed plugin version, surface "Running v<X>, installed v<Y> — restart Claude Code to pick up the new MCP."
6. **Config** — surface the active config.json settings (or the built-in defaults when there is no file):
   - `canonical_ref` (and whether materialize is currently working, via `branch_pinning_enabled`)
   - `production_ref` — the locked production branch the profile DERIVES from. When set, also surface `get_drift_status`'s `production_ref` block: `derived_sha` vs `tip_sha`, and when `tip_moved` is true print "Production branch <ref> moved N commit(s) past the profile — run /chameleon-refresh". Doctor's `production_ref` check reports whether the lock still resolves.
   - `auto_refresh.enabled` + `drift_threshold` + `max_age_hours`
   - `trust.auto_preserve_when`
   - `auto_rename`

   Read these via `chameleon-mcp::doctor` — its `config_json` check returns the parsed config. On the healthy path `config_json.detail` is a **parsed-config object** (a dict of the fields above); it is a **string** only on the malformed / no-file / generic-error paths. Surface the detail verbatim (render the object's fields, or print the error string) rather than improvising the defaults. Pass the statused repo to `doctor(repo=<abs-repo-path>)` so `config_json` / `production_ref` describe THAT repo, not the process cwd. When there is no `.chameleon/config.json`, the built-in defaults are: `auto_refresh` ON (drift_threshold=0.2, max_age_hours=168), `auto_rename` ON, `trust.auto_preserve_when="always"` (a refresh — manual or auto — re-grants trust, so the user is **not** re-prompted on their own repo), `enforcement.mode` `enforce` (calibrated block rules deny for real on a trusted repo; set `shadow` to log-only or `off` for advisory), `canonical_ref` OFF (opt-in). Do **not** label this block with a version number ("v0.6.0" is the release that introduced config.json, not the current version — read the real version from `daemon_status`). When the file is malformed, doctor surfaces a clear error and config.json features fall back to those defaults; show the error prominently so the user can fix the typo.

7. **Enforcement** — call `chameleon-mcp::get_status(repo)` and surface its `enforcement` block:
   - `mode` — `off | shadow | enforce`. `enforce` (default) blocks on calibrated rules, plus deterministic security facts and the once-per-session idiom review; `shadow` logs would-have-blocked events but never blocks. A team that wants to measure first can opt into `shadow`, read `/chameleon-status --shadow`, then switch back to `enforce`. By default a mode switch is a single edit to `config.json` — trust persists across the change, so it takes effect immediately. (Only under `CHAMELEON_TRUST_REVALIDATE=1` is it a two-step action: edit `config.json`, then re-run `/chameleon-trust`, because the trust-hashed config edit flips the profile to `stale` and disables enforcement until re-granted.)
   - `active` — block rules armed for this repo. Two kinds ride this one list: MEASURED rules calibration kept active (near-zero false positives against the repo's own committed files), and the two calibration-exempt security rules (`secret-detected-in-content`, `eval-call`), which are always listed on a profiled repo because calibration runs no content scans and a witness-count floor must not disarm the credential/eval deny (their artifact entries carry `exempt_reason: "security-rule"`). Don't describe the security pair as "measured near-zero-FP" — they are deterministic content facts, active by design.
   - `precision` — the headline calibration number: `active_block_rules` rules are active, the measured ones each flagging at most `max_fp_rate` of this repo's own committed files (`sampled_files` sampled). Surface it as the one-line low-noise guarantee, e.g. "Block precision: 4 rules active, <=0.0% false-positive on 12 sampled committed files." The count includes the two exempt security rules (unmeasured; deterministic-kind). It is the measured ceiling against frozen committed code, not a promise about future edits (the `overrides` axis below is the live signal).
   - `demoted` — block rules calibration kept advisory, each with the `fp_rate` that demoted it. Surface these so the user can see why a rule that blocks elsewhere is silent here.
   - `proposed_demotions` — present when refresh found a rule the team overrides above the demotion bar but the evidence came from fewer than `CHAMELEON_OVERRIDE_DEMOTION_MIN_SESSIONS` distinct sessions (default 2), or the rule is security-class (`security_rule: true` — `eval-call`, `secret-detected-in-content`), which never auto-demotes on override pressure. Each entry carries `rule`, `reason`, `override_rate`, `events`, `distinct_sessions`, `security_rule`. Surface these **loudly**: the rule is **STILL BLOCKING**. Tell the user the path forward — if the rule is genuinely wrong, fix the convention via `/chameleon-teach` then `/chameleon-refresh`; for a non-security rule, a second session's overrides will auto-apply the demotion at the next refresh; a security-class rule never auto-demotes, so the override evidence is the lead's to act on. Per-edit inline `chameleon-ignore <rule>` remains the documented escape hatch meanwhile.
   - `idiom_review` — default on in enforce mode. At turn end, when the turn edited files governed by team idioms/principles, the Stop hook blocks once per session to force a self-review of those changes against the idioms/principles.
   - `idiom_judge` — default on in enforce mode. Strengthens the idiom-review directive to demand a thorough review.
   - `correctness_judge` — default on (advisory, never blocks). At turn end an independent reviewer model reads the turn's diffs for correctness bugs; findings arrive as a context block to verify against the code.
   - `config_malformed` — when true, `config.json` is present but its enforcement section could not be parsed, so enforcement is OFF (the gates fail open) until it is fixed. Surface this **loudly**: a typo silently disabled enforcement; it is not a deliberate opt-out. `active` is empty in this state because the mode that would arm the rules is unreadable.
   - `enforcement_artifact_unreadable` — when true, `enforcement.json` is present but unparseable (or its `block_rules` is not a dict), so every MEASURED block rule silently drops and only the calibration-exempt security pair stays armed, even though `mode` still reads `enforce`. Surface this **loudly**: the enforcement panel otherwise reads healthy while the measured blocking is neutered. `/chameleon-refresh` re-derives the artifact.
   - `overrides` — the inline-override section (present when there is drift.db override history). This is a **different axis** from `demoted.fp_rate`: `fp_rate` is one-shot bootstrap calibration against frozen committed files; `overrides` is live team contention measured on real AI edits. A rule can show `fp_rate` 0.000 and still be overridden on most edits. Surface it:
     - `total_overrides` — the headline: "import-preference-violation overridden in N edits".
     - `rules[rule]` — `overrides`, `would_blocks`, `override_rate` (overrides / fired edits, or null below the event floor), `blanket` (bare-directive overrides), `high_override_rate`, `blanket_abuse`.
     - `flagged` — rules with a high override rate or blanket abuse. Surface these **loudly**. A high override rate means the rule is fighting the team: either the convention is wrong (suggest `/chameleon-teach`) or the rule is miscalibrated (suggest `/chameleon-refresh`, which re-runs calibration and rewrites the verdict before the trust-hash snapshot). Bare blanket `chameleon-ignore` (no rule name) is flagged separately — it stamps past every block-eligible rule at once, which signals someone routing around the gate wholesale. Never auto-demote a flagged rule; this surface only reports. `/chameleon-explain <rule>` reads the same data in depth.

8. **PR-review ledger** — when `get_status` returns a `review_ledger` block (present once `/chameleon-pr-review` has recorded at least one verdict for this repo), surface it. It is the persisted trail of review verdicts, a **different surface** from enforcement: enforcement is per-edit shape rules; this is the record of what `/chameleon-pr-review` decided on a diff.
   - `total` — review records on file.
   - `last` — the most-recent `{ts, commit_sha, verdict}`.
   - `shipped_over_block` — BLOCK verdicts whose commit is now an ancestor of HEAD, i.e. **merged despite a BLOCK**. Surface this **loudly** when non-empty: it is the one accountability case the ledger exists to catch. Print each `commit_sha ts` so the lead can open it.
   - `unverified` — records whose HMAC no longer matches. State the scope honestly: a verified record only proves no *other* local user silently edited the line. It does **not** prove the reviewed developer did not re-run and re-sign their own APPROVE (they hold the signing key), and **CI cannot verify these records** (no shared key). Present the ledger as an honest audit trail, never as a merge gate. If `unverified > 0`, say a record was tampered or written unsigned and name the count. Full per-record detail (the profile each verdict pinned, findings by severity) is in `chameleon-mcp::get_review_history(repo)`.

9. **Degraded delivery** — `get_status` returns a `degraded` block: how often chameleon's guidance silently failed to reach the session over the last `window_days` (default 7). A **different surface** again — not what the rules decided, but whether chameleon ran at all.
   - `total` — degraded hook fires in the window. When `0`, say guidance was delivered on every recent hook call (one line, not alarming). When `> 0`, surface it plainly and point at `/chameleon-doctor`.
   - Breakdown: `no_interpreter` (no Python >=3.11 / uv resolved — enforcement and guidance were OFF), `spawn_failed` (the helper crashed), `advisor_unavailable` (Python ran but the advisor raised). `last_ts` is the most recent degraded event.
   - These are **counts, not a ratio**: the no-interpreter/spawn-failed classes have no matching success rows, so don't compute or print an "N of M" fraction. A non-zero `no_interpreter` is the loud one — it means chameleon was effectively off, so recommend `/chameleon-doctor` and a Python >=3.11 / uv install.

## `--shadow`: would-block evidence for a repo running in shadow mode

`/chameleon-status --shadow` is for a repo a team has set to `shadow` to measure before enforcing (enforce is the default, so this is the opt-in measurement path). It answers: over the last few weeks of real edits, how often would each block rule have fired, and were those would-blocks genuine off-pattern code? `get_status` only returns the one-shot bootstrap calibration (frozen committed files); `--shadow` reads the live accumulating real-edit record. This record accrues under `enforce` too — a blocking rule (a security fact like `eval-call` / `secret-detected-in-content`, or a calibrated convention rule) logs a `would_block` row every time it fires, whether or not the repo is in shadow mode — so the report is typically POPULATED on an enforce repo, not empty. The real distinction is purpose: shadow mode is where a team MEASURES a rule's would-block behavior before promoting it, reading this same surface; on an enforce repo it reads as an after-the-fact audit of what the active rules caught.

Call `chameleon-mcp::get_shadow_report(repo, window_days)`. `window_days` is optional (default `CHAMELEON_SHADOW_REPORT_WINDOW_DAYS`, 21). Surface its block:

- **Per rule** (`rules`): `would_blocks`, `distinct_files`, `distinct_sessions`, `advisory_only`, and a `verdict`:
  - `safe_to_enforce` — zero would-blocks across enough real edits in a non-truncated window.
  - `would_block` — the rule fired; the lead must read the sample to decide whether those instances were genuinely off-pattern before enforcing.
  - `insufficient_data` — zero would-blocks but the window is truncated or saw too few edits to trust "never fires"; leave it in shadow longer.
- **`total_edits`** — the edit volume the verdict is measured against.
- **`window_truncated`** — when True, log rotation dropped rows older than the window. Say so plainly: a "0 would-blocks" verdict over a truncated window is NOT full coverage; tell the user to shorten `window_days` or treat the result as a lower bound.
- **`idiom_review`** — a turn-level would-block counter for the once-per-session idiom/principle self-review gate. It has no single rule, so it is reported on its own, never as a per-rule promotion candidate.
- **`sample`** — up to 20 `{rule, file, line, ts}` instances. Print them as `rule  file:line  ts` so the lead can open each one. This is the false-positive check: there is **no** computed FP fraction (the rows carry no accept/override outcome signal), so the human reads the sample and decides. If `sample_truncated` is True, note that more instances exist than shown.

Do not switch a repo back to `enforce` on the user's behalf; this surface only reports. Recommend the switch only when every candidate rule reads `safe_to_enforce` and the window is not truncated. By default that is a single `config.json` edit (trust persists across it), a two-step action (edit `config.json`, then `/chameleon-trust`) only under `CHAMELEON_TRUST_REVALIDATE=1`.

## Longitudinal health: two honestly-labelled tracks

A lead watching a rollout asks "is the AI code staying healthy without humans?" and historically had one trailing number, the drift score, to answer it. That number measures structural mimicry, not correctness, so reading it as a health bar over-trusts it. Call `chameleon-mcp::get_longitudinal_signals(repo, window_days)` (default `CHAMELEON_LONGITUDINAL_WINDOW_DAYS`, 21) and present the two tracks it returns — but **print the `disclaimer` line first, above both tracks**, so neither track is read as a correctness guarantee:

- **`structural_conformance`** (Track 1) — `score` (drift), `conformance` (1 - score, the on-shape reading), and `observations`. It carries `is_quality_bar: false`: structural conformance measures mimicry, not correctness. State that an off-pattern-but-on-shape edit scores clean here.
- **`enforcement_outcomes`** (Track 2) — `block_rate` (would-blocking rule fires / real edits) and `idiom_review_rate` (idiom/principle would-blocks / real edits) over the window, plus `total_edits`, `would_block_edits`, `idiom_review_blocks`, and `window_truncated`. These count how often chameleon's **own** shape/idiom rules fired. Rates are `null` (not zero) when `total_edits` is 0 — say "no edits in window", don't print 0%.

The trap is an **all-zeros enforcement-outcome result reading as health**. It does not mean the code is safe; it means the shape rules never caught anything, and those rules are blind to exactly the classes in `blind_spots` (`logic, dataflow, cross-file, auth checks`). Keep the `disclaimer` line visible above the output every time. When `window_truncated` is True, note the rates are a lower bound (rotation dropped older rows).

## The flow

1. Call `chameleon-mcp::detect_repo(<file-path>)` to get the current repo_id and trust_state.
2. Read `.chameleon/profile.json` and `archetypes.json` to enumerate archetypes (or call `get_pattern_context` if more convenient).
3. Call `chameleon-mcp::get_drift_status(repo)` for `days_since_refresh` / `structural_conformance_score` / `conformance_disclaimer` / `recommended_action`. When `structural_conformance_score` is `null` (no edits observed since bootstrap), render "no edits observed yet" in place of a numeric drift and report `recommended_action` (typically "none; profile is fresh") — never a literal "None".
4. Format the result for the terminal. Keep the conformance disclaimer above the score.

## Output format

```
chameleon profile: <repo-name>
  Language:        typescript
  Schema:          <schema-version> (engine min: <engine-min>)
  Last bootstrap:  47 days ago
  Trust state:     trusted (granted 2026-05-10 by <user>)
  Structural conformance (NOT a quality bar; does NOT cover logic, dataflow, cross-file, auth checks):
    Drift:         0.12 (engine upgraded since this profile was built; run /chameleon-refresh)
  Archetypes:      17
    - react-component (89 files): src/components/base
    - query (12 files): src/queries
    - utility (7 files): src/utils
    [...]
```

The conformance disclaimer line stays above the number. If the longitudinal section (`get_longitudinal_signals`) is shown, lead it with the same disclaimer and label Track 2 as "how often chameleon's own rules fired", never as a safety reading.

When `trust_state` is `untrusted` or `stale`, the line should be highlighted and accompanied by the corresponding remediation (`/chameleon-trust` for untrusted, `/chameleon-refresh` for stale).

## Slash command surface

- `/chameleon-status` — default summary
- `/chameleon-status --shadow` — per-rule would-block evidence + promotion verdict (see above)

## Out of scope

The earlier draft of this skill listed several telemetry surfaces ("value attribution", "p99 hook latency") that aren't implemented yet — there is no `value_attrib.db`, no hook-latency surface. Cumulative hook-degradation IS now surfaced (the `degraded` block above). The rest have moved to **future work**:

- `--health` flag with operator-grade SLO compliance dashboard (Round 5 SRE recommendation).
- `--diff` flag with profile-poisoning scan + semantic diff for PR review.
- `--json` flag for CI integration (machine-readable output).
- Value attribution: edits matching archetype over last N sessions, deviations flagged, corrections via /chameleon-teach.

Until those land, do not invent values for them; print only what the MCP data layer actually returns.

## Honesty Rules

- Report the real profile state: the active block-rule set, demoted rules with their measured `fp_rate`, drift indicators, and trust state, read from the MCP data layer, never estimated.
- Surface degraded or stale state plainly; never present a profile as healthy when a check says otherwise.
- Don't invent values for fields the data layer does not yet return; print only what it actually returns.
