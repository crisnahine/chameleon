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
    "CALIBRATION_MAX_FILES": 600,
    "CALIBRATION_MAX_SIBLINGS": 10,
    "CALIBRATION_FP_EPSILON": 0.001,
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
    # long tail of one-off helpers, not the methods every sibling shares.
    "CALLABLE_SIGNATURE_MAX_NAMES": 80,
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
    # Cap on the total bytes of reconstructed diff the judge prompt carries. A
    # large refactor can produce a huge diff; past this the diff is truncated so
    # the prompt stays bounded and the spawn stays within its time budget.
    "CORRECTNESS_JUDGE_MAX_DIFF_BYTES": 40_000,
    # Cap on the number of touched files the judge inspects in one turn. Beyond a
    # handful the prompt grows past what a single short-budget read can review,
    # so the most-recently-edited files are kept and the rest are dropped.
    "CORRECTNESS_JUDGE_MAX_FILES": 8,
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
    "COCHANGE_MAX_FILES_SCANNED": 4000,
    # Cap on the change-set-completeness items surfaced at turn end. When a turn
    # creates many trigger files with no companion, the list is truncated so the
    # advisory stays a short nudge rather than a wall of paths.
    "COCHANGE_ADVISORY_MAX_ITEMS": 8,
    # Cap on the number of files recorded in the cross-file function catalog. A
    # large monorepo has tens of thousands of functions; past this the artifact
    # is truncated (sorted-path order) so it stays a bounded, committable size.
    "DUPLICATION_CATALOG_MAX_FILES": 4_000,
    # Cap on the functions recorded per file in the function catalog, so one
    # generated or machine-emitted file cannot crowd out the rest of the repo
    # under the file cap.
    "DUPLICATION_CATALOG_MAX_FNS_PER_FILE": 60,
    # Minimum shared domain-word tokens between a new function's name and a
    # catalog candidate before the candidate is surfaced. One shared token (date,
    # slug, total) is enough to be worth the judge's look; zero overlap means the
    # names point at different intents and pairing them would be noise.
    "DUPLICATION_MIN_SHARED_TOKENS": 1,
    # Cap on the candidate functions surfaced per new function. The prefilter
    # only narrows the search for the LLM judge, so a short ranked list is the
    # goal; the highest-overlap candidates are kept and the tail dropped.
    "DUPLICATION_MAX_CANDIDATES_PER_FN": 5,
    # Lines of a candidate function's body read from disk as a citation aid for
    # the duplication judge. Enough to show the function's intent without
    # inlining a whole large method into the tool result.
    "DUPLICATION_BODY_EXCERPT_LINES": 15,
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
