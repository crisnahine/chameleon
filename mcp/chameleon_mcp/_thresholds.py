"""Central, env-overridable threshold module.

An audit flagged that ~10 hardcoded numeric thresholds
(_WORKSPACE_FANOUT_CAP, _EDIT_OBS_HARD_CAP, _MAX_EXTENDS_HOPS, etc.)
were spread across the codebase with no env-override path. Operators
who hit a threshold cap had to fork the code.

This module declares each threshold once with a default value and a stable
env-var name (``CHAMELEON_<NAME>``).

Existing modules keep their local constants (for back-compat), but new
readers should use ``threshold('NAME')`` to pick up env overrides.
"""

from __future__ import annotations

import math
import os
from typing import Final

DEFAULTS: Final[dict[str, int | float]] = {
    "WORKSPACE_FANOUT_CAP": 500,
    "WARNING_SAMPLE_PATHS": 3,
    "SPARSE_WARNING_LIMIT": 50,
    "MAX_EXTENDS_HOPS": 8,
    "EDIT_OBS_HARD_CAP": 50_000,
    "EDIT_OBS_SOFT_CAP": 10_000,
    "EDIT_OBS_AGE_DAYS": 90,
    # Row caps for the durable per-edit decision log. It is not wiped on refresh,
    # so it grows for the life of the repo; the same two-stage trim as
    # edit_observations bounds it (shed rows past the age window first, then
    # hard-cap by recency). Sized larger than the drift cache because each row is
    # postmortem history a lead may reach back for, not a rolling drift signal.
    "DECISION_LOG_HARD_CAP": 100_000,
    "DECISION_LOG_SOFT_CAP": 50_000,
    "DECISION_LOG_AGE_DAYS": 180,
    "STRUCTURED_TOTAL_CAP": 50_000,
    "SPAWN_WAIT_SECONDS": 3.0,
    "LISTEN_BACKLOG": 16,
    "MAX_CONCAT_FOLDS_PER_FILE": 1000,
    "CLUSTER_SHAPE_JACCARD_THRESHOLD": 0.7,
    "CLUSTER_PATH_BUCKET_DEPTH": 2,
    "RENAMES_OVERLAY_CAP": 256,
    "DRIFT_BANNER_THRESHOLD": 0.4,
    "DRIFT_BANNER_MIN_OBSERVATIONS": 10,
    "DRIFT_BANNER_TTL_SECONDS": 7 * 24 * 3600,
    # Calibration corpus bounds. The file cap and epsilon move together: keeping
    # the cap below 1/epsilon means a single flagged file already exceeds the
    # tolerance, so the gate stays "zero false positives" in practice. 1200/20
    # lets a gitlabhq-sized repo's witness+sibling corpus through (a 600-file
    # head sample masked real FPs that only surfaced past file 600).
    "CALIBRATION_MAX_FILES": 1200,
    "CALIBRATION_MAX_SIBLINGS": 20,
    "CALIBRATION_FP_EPSILON": 0.0005,
    # Degraded-parse gate: a bootstrap/refresh whose extractor child died
    # mid-run surfaces as mass parse skips. Healthy repos parse at ~100%, so a
    # skip rate past the ratio (with at least the floor of skipped files, to
    # spare tiny repos) aborts the run instead of committing a thin profile
    # over a healthy one.
    "EXTRACTOR_DEGRADED_MIN_SKIPPED": 10,
    "EXTRACTOR_DEGRADED_RATIO": 0.5,
    # Body-shape norms need a thicker witness pool than the generic sample gate:
    # a p90 drawn from 10 functions is too noisy to ground an outlier claim.
    "BODY_SHAPE_MIN_FUNCTIONS": 18,
    # How far past the archetype's p90 a function must reach before its branch
    # count or nesting depth reads as an outlier. Line span and parameter count
    # are secondary and never trigger a finding on their own.
    "BODY_SHAPE_OUTLIER_MULT": 1.5,
    # Default lookback for the shadow would_block report when the caller does not
    # pass an explicit window. Two-to-three weeks of real editing is the volume a
    # lead reads before deciding whether to flip shadow -> enforce.
    "SHADOW_REPORT_WINDOW_DAYS": 21,
    # Cap on the sampled file:line list the report returns per call, so a noisy
    # rule cannot flood the output the human has to eyeball.
    "SHADOW_REPORT_SAMPLE_CAP": 20,
    # A rule reads as promotion-ready only when zero would-blocks occurred across
    # at least this many edits in a non-truncated window. Below the floor the
    # window is "insufficient data" rather than "safe to enforce".
    "SHADOW_PROMOTION_MIN_EDITS": 100,
    # Minimum number of member basenames an archetype must contribute before a
    # file-naming convention (dominant casing / suffix token) is derived. A
    # casing call drawn from three filenames is too thin to ground a rule that
    # can block a new file's name.
    "FILE_NAMING_MIN_SAMPLE": 8,
    # Lookback for the inline-override audit. Same horizon as the shadow report
    # so a lead reads both surfaces over the same recent-edit window.
    "OVERRIDE_AUDIT_WINDOW_DAYS": 21,
    # A rule overridden in at least this fraction of the edits where it would
    # block reads as fighting the team rather than catching bugs. Surfaced
    # loudly so the lead reconciles it via refresh/teach; never auto-mutates the
    # trust-hashed enforcement verdict. As a fraction of (overrides + would_blocks).
    "OVERRIDE_RATE_HIGH": 0.5,
    # Below this many combined override + would-block events a rate is too thin
    # to read as contention; one override out of one event is not a signal.
    "OVERRIDE_AUDIT_MIN_EVENTS": 5,
    # A rule whose overrides are mostly bare blanket directives is being stamped
    # past wholesale rather than annotated per intentional deviation. Flagged
    # separately at or above this blanket share.
    "OVERRIDE_BLANKET_HIGH": 0.5,
    # Refresh-time auto-demotion bar. A calibrated-active block rule the team
    # overrides above this share of its fires (over OVERRIDE_AUDIT_MIN_EVENTS) is
    # fighting the team, not catching bugs, so the next refresh demotes it to
    # advisory. This is recomputed at refresh BEFORE the trust hash is taken, so
    # the demotion lives in the trust-hashed enforcement.json and is never a
    # runtime mutation of it. As a fraction of (overrides + would_blocks).
    "RULE_FP_DEMOTE_THRESHOLD": 0.5,
    # Minimum distinct sessions the override evidence must span before a
    # refresh-time auto-demotion applies. Override telemetry is author-
    # generated, so a single session's overrides must never be able to demote
    # a correct block rule on their own; below the floor (and always for
    # security-class rules) the demotion is recorded as a proposal for the
    # status surface instead of being applied.
    "OVERRIDE_DEMOTION_MIN_SESSIONS": 2,
    # Auto-pass router bounds (advisory). A change stays auto-pass-eligible only
    # while it is small and low-fan-out; past any of these it routes to a human.
    # Conservative defaults for a "routine" change; an auth/payment/migration
    # surface or a grounded block finding routes to a human regardless of size.
    "AUTOPASS_MAX_FILES": 10,
    "AUTOPASS_MAX_LINES": 150,
    "AUTOPASS_MAX_BLAST_RADIUS": 10,
    # Hard wall-clock budget for the opt-in repo-local `tsc --noEmit` grounding
    # run (CHAMELEON_ALLOW_TSC). Tool-time only, never on a hook hot path; on
    # timeout the typecheck reads "unavailable" — a recorded fact, never a
    # failure and never a synthetic clean.
    "AUTOPASS_TSC_TIMEOUT_SECONDS": 120,
    # Cap on the unified-diff text scanned for the deterministic content
    # signals (removed-guard lexicon, in-diff ignore directives, test skip
    # markers, assertion delta). Past the cap the scan truncates and says so;
    # a diff that large already routes needs-human on size alone.
    "AUTOPASS_MAX_DIFF_BYTES": 2_000_000,
    # Net removed lines across changed test files before the change reads as
    # net test deletion in the test-integrity gate. Small consolidation
    # refactors stay quiet; gutting a spec does not.
    "AUTOPASS_TEST_DELETION_NET_LINES": 10,
    # Assertion-count delta (added minus removed assertion tokens in changed
    # test files) at or below this floor reads as test weakening. One or two
    # consolidated assertions are the normal refactor shape; tighten to -1 via
    # the env override for stricter repos.
    "AUTOPASS_ASSERTION_DELTA_FLOOR": -3,
    # Lookback for the combined longitudinal health section (structural conformance
    # plus enforcement-outcome rates). Same horizon as the shadow / override
    # surfaces so a lead reads all three over the same recent-edit window.
    "LONGITUDINAL_WINDOW_DAYS": 21,
    # Fraction of an archetype's files that must share an error-handling shape
    # (TS function bodies that try/catch, Ruby controller bases that rescue_from)
    # before it reads as the archetype's contract. Shares the 60% floor the
    # inheritance and required-guard derivations use: a choice the clear majority
    # makes is the established norm, not noise.
    "ERROR_HANDLING_FREQUENCY": 0.60,
    # Fraction of an archetype's files whose imports follow the same external-vs-
    # relative grouping order before it reads as the archetype's import layout.
    # Shares the 60% floor the inheritance and error-handling derivations use: a
    # layout the clear majority follows is the established norm, not noise.
    "IMPORT_ORDERING_FREQUENCY": 0.60,
    # Minimum files that must carry at least one import before an import-ordering
    # convention is derived. A partition pattern drawn from a handful of files is
    # too thin to ground an advisory that an edit's imports are out of order.
    "IMPORT_ORDERING_MIN_SAMPLE": 10,
    # Fraction of an archetype's public declarations that must carry a leading doc
    # comment before "siblings document their public surface" reads as the norm.
    # Shares the 60% floor the other dominant-ratio conventions use.
    "DOC_COVERAGE_FREQUENCY": 0.60,
    # Fraction of an archetype's non-test source files that must have a paired test
    # at the derived path before "siblings ship a test alongside the source" reads
    # as the norm. Shares the 60% floor the other dominant-ratio conventions use;
    # advisory only, never block-eligible (up to 40% of files legitimately lack a
    # test at this floor, so the near-zero-FP calibration gate would demote a block
    # rule on nearly every real repo).
    "TEST_PAIRING_FREQUENCY": 0.60,
    # Minimum non-test source files an archetype must contribute before a
    # test-pairing rate is trustworthy. A pairing fraction drawn from three files
    # swings wildly on one missing test.
    "TEST_PAIRING_MIN_SAMPLE": 10,
    # Minimum public declarations an archetype must contribute before a doc-
    # coverage fraction is trustworthy. A coverage figure drawn from three
    # declarations swings wildly on one undocumented addition.
    "DOC_COVERAGE_MIN_DECLS": 12,
    # Default number of most-recent PR-review records get_review_history returns
    # when the caller does not ask for more. A reviewer scanning the recent
    # verdict trail wants the last couple dozen, not the whole repo history.
    "REVIEW_HISTORY_DEFAULT_LIMIT": 25,
    # Hard cap on records kept in a repo's review ledger. The file is append-only
    # and never wiped by refresh, so it grows for the life of the repo; once it
    # crosses this many lines the oldest are dropped on the next append. One
    # record per review run keeps this small in practice.
    "REVIEW_LEDGER_MAX_RECORDS": 5_000,
    # Cap on the number of distinct callable names recorded in an archetype's
    # signature consensus. Names are kept most-frequent-first so the cap drops the
    # long tail of one-off helpers, not the methods every sibling shares. Wide
    # archetypes (a fat Rails controller base, a broad service module) legitimately
    # share more than 80 method names, so the cap admits them with margin.
    "CALLABLE_SIGNATURE_MAX_NAMES": 120,
    # A callable name must appear in at least this many of an archetype's member
    # files before its parameter shape is treated as the archetype's contract. A
    # shape drawn from one file is an instance, not a convention.
    "CALLABLE_SIGNATURE_MIN_FILES": 2,
    # An import edge A->B between two clusters must appear in at least this many
    # member files before the unanimous-direction check considers it. A single
    # crossing is too thin to read as the intended layering direction.
    "LAYERING_MIN_EDGE_FILES": 3,
    # Cap on the number of forbidden-upward edges recorded in layering.json, kept
    # most-frequent-first so a pathological repo cannot bloat the artifact.
    "LAYERING_MAX_FORBIDDEN_EDGES": 60,
    # Cap on the number of import cycles recorded in the static cycle report. A
    # repo with hundreds of cycles only needs a representative sample surfaced.
    "LAYERING_MAX_CYCLES": 40,
    # Hard wall-clock budget for the turn-end correctness judge subprocess. It
    # runs once per session at Stop, so a single slow spawn must never trap the
    # turn; on timeout the judge fails open to no findings. Kept short because
    # the user is waiting on the turn to end.
    "CORRECTNESS_JUDGE_TIMEOUT_SECONDS": 45,
    # Wall-clock budget for the reviewer spawn inside the DETACHED async judge
    # child when bare auth is known failed. The plain (non --bare) spawn pays
    # the full session primer before it can review, which cannot fit the short
    # sync budget above; the child runs detached from the Stop hook, so the
    # budget is generous. Synchronous spawns always keep the short budget.
    "CORRECTNESS_JUDGE_FALLBACK_TIMEOUT_SECONDS": 180,
    # Cap on the total bytes of reconstructed diff the judge prompt carries. A
    # large refactor can produce a huge diff; past this the diff is truncated so
    # the prompt stays bounded and the spawn stays within its time budget. Sized
    # so ~5 per-file diffs at the 12KB per-file cap fit, keeping the file cap
    # below meaningful on multi-file turns (~1 in 5 real turns touches >8 files).
    "CORRECTNESS_JUDGE_MAX_DIFF_BYTES": 60_000,
    # Cap on the number of touched files the judge inspects in one turn. Covers
    # the p90 of real multi-file turns; the most-recently-edited files are kept
    # and the rest are dropped. Moves together with the diff-bytes cap above:
    # raising one without the other leaves the loop bound by the other cap.
    "CORRECTNESS_JUDGE_MAX_FILES": 12,
    # Cap on the number of findings surfaced from a single judge run. The judge
    # is advisory, so a long list is noise; the highest-confidence findings are
    # kept and the remainder dropped.
    "CORRECTNESS_JUDGE_MAX_FINDINGS": 5,
    # Cap on the source files the turn-end stale-test advisory names. When a turn
    # edits many paired sources without their tests, the list is truncated so the
    # advisory stays a short nudge rather than a wall of paths.
    "STALE_TEST_ADVISORY_MAX_FILES": 8,
    # Cap on the changed export names cited per file in the stale-test advisory.
    # A file can export many symbols; the advisory only needs enough to point the
    # reader at what the paired test may now be missing.
    "STALE_TEST_ADVISORY_MAX_EXPORTS": 6,
    # Fraction of a co-change rule's committed trigger files that may already lack
    # the rule's companion before the rule is treated as inapplicable to this repo
    # and silenced. A pair like new-model -> migration is only worth surfacing when
    # the repo overwhelmingly keeps the two together; a repo where many committed
    # models already ship without a co-changed companion is not following the rule,
    # so firing it on a new file would nag. Kept loose relative to the block-rule
    # epsilon because this is an advisory nudge, not a calibrated blocker.
    "COCHANGE_MAX_VIOLATION_RATE": 0.02,
    # Minimum committed trigger files a co-change rule needs before its repo
    # violation rate is trustworthy. A rate drawn from two models swings wildly on
    # one exception; below the floor the rule stays silent rather than guess.
    "COCHANGE_MIN_TRIGGER_FILES": 8,
    # Lines longer than this skip the detect-secrets per-line pass. On a
    # token-dense single line (minified bundle, generated const map) the keyword
    # detector yields thousands of candidates and each one re-scans the whole
    # line through the allowlist regex set, turning one 100KB line into tens of
    # seconds. Hand-written code stays far below this length, and the
    # deterministic fallback patterns (the only source of block-eligible secret
    # kinds) still scan the full content linearly, so hard secrets on long lines
    # remain caught.
    "SECRET_SCAN_MAX_LINE_LEN": 2_000,
    # Upper bound on committed files scanned when measuring a co-change rule's repo
    # violation rate, so the disable check on a huge repo stays a bounded glob walk.
    "COCHANGE_MAX_FILES_SCANNED": 8000,
    # Cap on the change-set-completeness items surfaced at turn end. When a turn
    # creates many trigger files with no companion, the list is truncated so the
    # advisory stays a short nudge rather than a wall of paths.
    "COCHANGE_ADVISORY_MAX_ITEMS": 8,
    # Cap on the number of files recorded in the cross-file function catalog. A
    # large monorepo has tens of thousands of functions; past this the artifact
    # is truncated (sorted-path order) so it stays a bounded, committable size.
    # The catalog IS the duplication detector's search space, so a low cap
    # silently blinds it on exactly the repos where duplication is most likely;
    # 8000 keeps the biggest measured artifact (~9.4MB) under the loader's 16MB
    # ceiling — do not raise past that without lifting the loader cap too.
    "DUPLICATION_CATALOG_MAX_FILES": 8_000,
    # Cap on the functions recorded per file in the function catalog, so one
    # generated or machine-emitted file cannot crowd out the rest of the repo
    # under the file cap. High enough to admit a real hand-written wide module
    # (a util grab-bag, a wide Rails concern) without losing its tail.
    "DUPLICATION_CATALOG_MAX_FNS_PER_FILE": 120,
    # Minimum shared domain-word tokens between a new function's name and a
    # catalog candidate before the candidate is surfaced. One shared token (date,
    # slug, total) is enough to be worth the judge's look; zero overlap means the
    # names point at different intents and pairing them would be noise.
    "DUPLICATION_MIN_SHARED_TOKENS": 1,
    # Cap on the candidate functions surfaced per new function. The prefilter
    # only narrows the search for the LLM judge, so a short ranked list is the
    # goal; the highest-overlap candidates are kept and the tail dropped.
    "DUPLICATION_MAX_CANDIDATES_PER_FN": 5,
    # Cap on how many of the queried file's functions (matches) get_duplication_
    # candidates returns. A large file (hundreds of functions x 5 candidates x a
    # body excerpt) would otherwise blow the MCP response token cap and become
    # undeliverable; over the cap the result is truncated and flagged.
    "DUPLICATION_MAX_MATCHES": 15,
    # Caps for the turn-end body-hash duplication review gate. The file and
    # findings caps bound the parse fan-out and advisory length per turn. The
    # prompt-bytes cap keeps the judge prompt within the session budget. The
    # spawns cap limits how many judge spawns can fire across an entire session
    # (one per significant editing burst is the intent; raising it too high
    # turns every turn into a billable spawn).
    "DUPLICATION_REVIEW_MAX_FILES": 12,
    "DUPLICATION_REVIEW_MAX_FINDINGS": 8,
    "DUPLICATION_REVIEW_MAX_PROMPT_BYTES": 60_000,
    "DUPLICATION_REVIEW_MAX_SPAWNS_PER_SESSION": 2,
    # Lines of a candidate function's body read from disk as a citation aid for
    # the duplication judge. Enough to show the function's intent without
    # inlining a whole large method into the tool result: covers the median TS
    # function body (~14 lines) with margin and the Ruby p90, while staying a
    # bounded prompt-side slice (this excerpt flows verbatim into tool results).
    "DUPLICATION_BODY_EXCERPT_LINES": 20,
    # Minimum normalized-body length (chars) before a function gets a body-hash
    # fingerprint in the catalog. The hash pairs body-exact clones whose names
    # share no tokens; trivial one-expression bodies collide across half a
    # codebase, so they carry no identity worth fingerprinting.
    "DUPLICATION_BODY_HASH_MIN_CHARS": 40,
    # Cap on the modules scanned for cross-file existence breaks in one
    # get_crossfile_context call. Each scanned module reads its own source to
    # recompute its current export set, so this bounds the read fan-out on a
    # large monorepo; modules are taken in sorted-key order so the scan is
    # deterministic when truncated.
    "CROSSFILE_MAX_MODULES_SCANNED": 4_000,
    # Cap on the existence-break findings returned by get_crossfile_context, so a
    # mass-rename turn produces a short actionable list rather than a wall.
    "CROSSFILE_MAX_FINDINGS": 50,
    # Cap on the importer sites listed per existence-break finding. The first
    # sites in sorted (path, line) order are kept; the count still reports the
    # true total so the reader knows the full blast radius.
    "CROSSFILE_MAX_SITES_PER_FINDING": 10,
    # Separate, smaller cap on low-confidence (open-set/barrel) existence rows.
    # They are transparency output the PR-review consumer must not relay, so
    # they get their own budget instead of crowding high-confidence findings
    # out of CROSSFILE_MAX_FINDINGS; overflow is counted, not silently lost.
    "CROSSFILE_MAX_LOW_CONFIDENCE": 10,
    # Cap on the source files touched this turn that the Stop existence-break
    # advisory inspects, bounding the turn-end read fan-out.
    "CROSSFILE_STOP_ADVISORY_MAX_FILES": 8,
    # Hard wall-clock budget for the opt-in dependency-audit subprocess
    # (npm audit / bundler-audit). The audit hits the network, so a slow or
    # hung registry must never trap the caller; on timeout the tool fails open
    # to an "unavailable" result. Tool-time only, never on the hook hot path.
    "DEP_AUDIT_TIMEOUT_SECONDS": 90,
    # Cap on style-rule-violation emissions per file. The style baseline checks
    # indent / quote / line-length against the repo's own declared formatter
    # config and can match on every line of a misformatted file, so a single
    # paste of foreign-style code must not flood the advisory list. Past the cap
    # a summary row reports the remainder, mirroring the secret-scan cap.
    "STYLE_RULE_VIOLATIONS_PER_FILE": 20,
    # Cap on proposed-content characters the PreToolUse hard-secret deny
    # scans, consistent with the 100KB ceiling every existing content scan
    # shares. A secret placed past the cap escapes the pre-write deny but
    # still meets the PostToolUse and Stop scans.
    "PREWRITE_SECRET_SCAN_MAX_CHARS": 100_000,
    # Hard per-session budget for the per-turn-routed correctness judge (the
    # old behavior was exactly one spawn per session). Bounds cost and the
    # worst-case cumulative Stop latency; once spent, further routed turns
    # record a skipped check instead of spawning.
    "CORRECTNESS_JUDGE_MAX_SPAWNS_PER_SESSION": 4,
    # Cap on extracted assertion tokens persisted per user prompt by intent
    # capture. Enough to carry the spec constants of a detailed prompt without
    # storing the prompt itself.
    "INTENT_MAX_TOKENS_PER_PROMPT": 40,
    # Size cap on the per-session intent file; oldest entries trim first so a
    # long session cannot grow the artifact unbounded.
    "INTENT_FILE_MAX_BYTES": 32_768,
    # Age at which session-start sweeps reap intent and judge session files,
    # matching the session-marker reaper horizon.
    "INTENT_RETENTION_DAYS": 7,
    # Deadlines for the two cross-process profile locks. Both were unbounded
    # blocking flocks: one daemon grinding through a slow git-show extraction
    # held .materialize.lock while every other reader of the same repo wedged
    # behind it for the holder's whole lifetime (observed: a 68-minute session
    # stall). The materialize waiter fails OPEN to the working-tree profile on
    # timeout; the trust waiter raises LockHeldError, which trust_profile
    # surfaces as an error envelope and refresh-time trust preservation
    # swallows.
    "CANONICAL_MATERIALIZE_LOCK_TIMEOUT_SECONDS": 30.0,
    "TRUST_LOCK_TIMEOUT_SECONDS": 10.0,
    # Caps for the turn-end session attestation. Files bounds governed +
    # ungoverned entries (and their digest reads) per record; overrides and
    # check events bound the embedded evidence lists, with the remainder
    # reported as truncated counts rather than dropped silently; the ledger
    # cap is the recency trim for session_attestations.ndjson, mirroring
    # REVIEW_LEDGER_MAX_RECORDS.
    "ATTESTATION_MAX_FILES": 200,
    "ATTESTATION_MAX_OVERRIDES": 100,
    "ATTESTATION_MAX_CHECK_EVENTS": 200,
    "ATTESTATION_LEDGER_MAX_RECORDS": 2_000,
    # Cap on caller rows stored per callee in calls_index.json. The true total
    # is always stored separately; the judge shows at most JUDGE_FACTS_MAX_SITES
    # sites per callable, so stored rows only need to cover display and
    # pr-review sampling -- 100 is ample for that, unlike the reverse index's
    # 500, which must also cover existence checks across all importers.
    "CALLS_INDEX_MAX_CALLERS_PER_CALLEE": 100,
    # Hard cap on total edges in calls_index.json; past this the builder stops
    # adding rows and every further-affected entry reads as truncated.
    "CALLS_INDEX_MAX_TOTAL_EDGES": 200_000,
    # Caps for the judge's cross-file caller-facts block: callables listed,
    # sites shown per callable, and total characters. The block is grounding
    # context, not the review itself, so it stays small.
    "JUDGE_FACTS_MAX_CALLABLES": 5,
    "JUDGE_FACTS_MAX_SITES": 5,
    "JUDGE_FACTS_CHAR_CAP": 1200,
}


def _env_name(name: str) -> str:
    return f"CHAMELEON_{name}"


def threshold(name: str) -> int | float:
    """Return the current value of threshold ``name``.

    Resolves ``$CHAMELEON_<NAME>`` first; falls back to the documented
    default. Returns the default on KeyError or non-numeric env input.
    """
    if name not in DEFAULTS:
        raise KeyError(f"unknown threshold: {name!r}")
    default = DEFAULTS[name]
    raw = os.environ.get(_env_name(name))
    if raw is None:
        return default
    try:
        if isinstance(default, float):
            value = float(raw)
            # NaN/inf would make every threshold comparison meaningless, and a
            # negative threshold is never a sensible cap; reject all three.
            if not math.isfinite(value) or value < 0:
                return default
            return value
        return int(raw)
    except ValueError:
        return default


def threshold_int(name: str) -> int:
    """Convenience: ``threshold(name)`` cast to int."""
    return int(threshold(name))


def threshold_float(name: str) -> float:
    """Convenience: ``threshold(name)`` cast to float."""
    return float(threshold(name))
