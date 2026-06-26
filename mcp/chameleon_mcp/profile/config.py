"""``.chameleon/config.json`` reader for per-repo UX features.

Earlier versions had no per-repo configuration file; behavior was hard-coded.
These features (branch pinning, auto-refresh,
trust-friction reduction, auto-rename) need a place to be
configured per repo. This module loads + validates the file.

Schema (all fields optional, all have safe defaults):

```jsonc
{
  "$schema": "chameleon-config-0.9.0",
  "canonical_ref": "origin/main",          // branch pinning (reads)
  "production_ref": "production",          // branch the profile derives from
                                           // (explicit null = opt out of auto-locking)
  "auto_refresh": {                         // drift-triggered refresh
    "enabled": true,
    "drift_threshold": 0.2,
    "max_age_hours": 168
  },
  "trust": {
    "auto_preserve_when": "always"  // null | "pulled_from_remote" | "always"
                                    // "always" re-grants trust after any refresh
  },
  "auto_rename": true                       // skip rename interview in /chameleon-init
}
```

Absent file → all defaults (auto_refresh on, auto_rename on,
trust.auto_preserve_when="always"). The loader never
raises on a missing file; it raises ``ChameleonConfigError`` only when
a present file is malformed (unrecognized type, unknown key under a
strict section).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"
# 0.9.0: production_ref + unknown-top-level-key tolerance. Older schema
# strings remain accepted; $schema is informational, never a gate.
CURRENT_SCHEMA = "chameleon-config-0.9.0"


class ChameleonConfigError(ValueError):
    """Raised when ``.chameleon/config.json`` is present but malformed."""


@dataclass(frozen=True)
class AutoRefreshConfig:
    enabled: bool = True
    drift_threshold: float = 0.2
    max_age_hours: int = 168
    # Default-ON: before a refresh (manual or auto) resolves the locked
    # production tip, fetch origin/<branch> so derivation sees the genuinely
    # latest production, not the user's last fetch. The one network-default-ON
    # path; kill with CHAMELEON_FETCH_PRODUCTION_REF=0 or this flag = false, and
    # it self-suppresses under CI. Non-interactive + hard-timeout, fails open.
    fetch_production_ref: bool = True


@dataclass(frozen=True)
class TrustConfig:
    # Controls only whether a refresh re-stamps the stored grant hash. Re-prompting
    # is gated separately by CHAMELEON_TRUST_REVALIDATE (default off); with it unset,
    # trust persists across changes and null/pulled_from_remote have no user-visible
    # effect. "always" re-stamps after any refresh; null skips the re-stamp.
    auto_preserve_when: str | None = "always"


@dataclass(frozen=True)
class EnforcementConfig:
    # mode master switch: "off" = advisory only; "shadow" = log would-have-blocked
    # but never block; "enforce" = real deny/block. Default enforce. All blocking
    # requires a trusted profile and is overridable inline (CHAMELEON_ENFORCE=0
    # forces advisory). The per-block guard differs by class: the per-edit
    # convention denies (naming/import/jsx/file-naming) additionally require
    # per-repo zero-false-positive calibration against the repo's own committed
    # files AND a high- or medium-confidence archetype match; the
    # archetype-independent security facts (hard-kind secrets, eval/exec sinks)
    # block on deterministic detection with no confidence gate; the turn-end idiom
    # review blocks once per session when idioms/principles are present. So enforce
    # is a safe default for the calibrated convention rules without a measure-first
    # shadow period. It is NOT a blanket "every block is calibrated" guarantee (see
    # the security and idiom-review paths).
    mode: str = "enforce"
    stop_backstop: bool = True
    stop_block_cap: int = 3
    # idiom_review: at turn end, when the session edited files governed by team
    # idioms/principles, block once (enforce) to force a self-review of the
    # changes against those idioms/principles. On by default so enforce repos get
    # the reflexive check; the once-per-session marker keeps it from nagging.
    idiom_review: bool = True
    # idiom_judge: opt-in. When True, the idiom-review directive is strengthened
    # to demand a thorough review (an independent judge is enabled). The judge
    # spawn itself is not wired into the hook; the flag only hardens the directive.
    idiom_judge: bool = False
    # correctness_judge: on by default. When True and mode is shadow/enforce, turn
    # end spawns a separate reviewer model that reads the turn's reconstructed
    # diffs for correctness bugs (logic errors, missing guards, dropped awaits,
    # error-handling gaps) and surfaces its findings as advisory context. It never
    # blocks the turn and runs at most once per session, like the idiom gate.
    # Safe as a default because every stage fails open (missing CLI, timeout,
    # non-zero exit all collapse to no findings) and the spawn is bounded: no
    # tools, one turn, hard wall-clock budget, throwaway config dir. The cost is
    # one bounded reviewer spawn per session at the first governed turn end; set
    # false to opt out.
    correctness_judge: bool = True
    # stale_test_advisory: on by default. At turn end, when the session edited a
    # source file whose archetype's siblings overwhelmingly ship a paired test but
    # the existing paired test was not touched this turn, surface an advisory
    # naming that test and the exports the edit may have moved out from under it.
    # Advisory only, never a block: the pairing floor admits many legitimately
    # untested files, so this is a hint to confirm coverage, not a hard gate.
    stale_test_advisory: bool = True
    # changeset_completeness: on by default. At turn end, when the session created
    # a NEW file whose framework convention demands a companion (a Rails model its
    # migration, a new controller its route) but the change-set carries none,
    # surface an advisory naming the file and the missing companion. Driven by a
    # hand-curated framework pair table, gated to new files only, and silenced per
    # rule for a repo whose own committed files already break the pairing. Advisory
    # only, never a block: a partial edit may defer its companion to a later commit.
    changeset_completeness: bool = True
    # crossfile_existence_advisory: on by default. At turn end, for each TypeScript
    # source the session touched, check the prebuilt reverse index for exports that
    # disappeared while indexed importers still reference them, and surface an
    # advisory naming the broken call sites. Reuses the persisted index plus a cheap
    # regex presence check (no parse at Stop). Advisory only, never a block: a
    # mid-rename turn may legitimately leave a call site for a follow-up edit.
    crossfile_existence_advisory: bool = True
    # duplication_review: on by default. At turn end, when the session introduced a
    # function whose body hash matches an existing one in the catalog or earlier
    # this session, surface an advisory naming the original so the author can reuse
    # it. Confirmed by a bounded judge spawn; advisory only, never a block.
    duplication_review: bool = True
    # intent_scope_advisory: on by default. At turn end, when the session's captured
    # request named specific identifiers, surface an advisory naming any changed file
    # that shares nothing with them -- a possibly-unrequested change. Advisory only,
    # never a block; stays silent unless the turn plausibly IS the captured work (at
    # least one changed file matched what was named), to keep false positives low.
    intent_scope_advisory: bool = True
    # judge_crossfile_facts: on by default. When True, the correctness judge's
    # prompt carries a bounded block of committed caller facts (deterministic
    # grades only, from the calls_index.json snapshot) for the callables the
    # turn changed, so the reviewer reads each diff with its consumers in view.
    # Purely additive prompt grounding: a missing or stale index just means the
    # judge reviews without the block. Set false to opt out.
    judge_crossfile_facts: bool = True
    # signature_contract_diff: on by default. When True, the auto-pass router (and
    # pr-review) compute a DETERMINISTIC caller-contract signature diff: a changed
    # callable whose POSITIONAL contract narrowed (a new required positional arg,
    # or an optional positional flipped required) is flagged when it has committed
    # callers, so a narrowing in a low-importer file no longer slides under the
    # blast-radius gate with no signal. git show + AST re-parse, tool-time only,
    # never a hook hot path; advisory (routes a human / FIX), never blocks. A
    # missing index or git failure just means the check does not fire. Set false
    # to opt out.
    signature_contract_diff: bool = True
    # judge_imported_definitions: on by default. When True, the correctness
    # judge's prompt carries the SIGNATURES of the symbols the changed files
    # import (resolved through the committed symbol-signature index), so the
    # reviewer reads each call site with the contract it must satisfy in view --
    # the forward complement to the reverse caller facts. Additive prompt
    # grounding from a static index; a missing index just means the block is
    # absent. Set false to opt out.
    judge_imported_definitions: bool = True
    # judge_transitive_impact: on by default. When True, the correctness judge's
    # prompt carries a bounded MULTI-HOP transitive caller-impact block: for each
    # changed callable, the chain of callers-of-callers (changed_fn <- service <-
    # controller) walked from the committed calls index. This is the cross-module
    # context LLMs are documented to be weakest at. Hard-bounded (depth/fanout/
    # total/char caps), deterministic, fails open; a missing index just means the
    # block is absent. Set false to opt out.
    judge_transitive_impact: bool = True
    # test_integrity_review: on by default. At turn end, when the turn changed
    # live source AND weakened tests (added skip markers, dropped assertions, net
    # test deletion -- the deterministic signals the auto-pass router computes),
    # surface an advisory naming what was weakened. Deterministic, zero model
    # spawn; advisory only, never a block. Set false to opt out.
    test_integrity_review: bool = True
    # multi_lens_review: OFF by default. When True (and mode is shadow/enforce),
    # the turn-end review runs a coordinated multi-lens pass (correctness +
    # duplication today) merged through lens_synthesis instead of the separate
    # correctness-judge and duplication gates, so duplication is no longer starved
    # by the single-spawn defer. Opt-in because it lifts the per-turn reviewer
    # spawn budget above one; advisory only, never a block. Measure in shadow,
    # then enable.
    multi_lens_review: bool = False


@dataclass(frozen=True)
class ChameleonConfig:
    schema_version: str = CURRENT_SCHEMA
    canonical_ref: str | None = None
    # Branch the profile DERIVES from: bootstrap/refresh analyze this ref's
    # tree (materialized, no network) instead of the checked-out working tree,
    # so the profile reflects the canonical line regardless of which feature
    # branch is checked out. Distinct from canonical_ref, which only redirects
    # profile READS to a committed .chameleon snapshot at a ref.
    production_ref: str | None = None
    auto_refresh: AutoRefreshConfig = field(default_factory=AutoRefreshConfig)
    trust: TrustConfig = field(default_factory=TrustConfig)
    enforcement: EnforcementConfig = field(default_factory=EnforcementConfig)
    auto_rename: bool = True
    # Stable identity for repos without a git remote. Bootstrap persists this so
    # moving/renaming the working tree on disk does not orphan the trust grant.
    # Repos with a remote ignore it; the remote URL is the stronger signal.
    repo_uuid: str | None = None

    @property
    def branch_pinning_enabled(self) -> bool:
        return bool(self.canonical_ref)


# Controls only whether a refresh re-stamps the stored grant hash. Re-prompting is
# gated by CHAMELEON_TRUST_REVALIDATE (default off), NOT by this knob: with it unset,
# trust persists across changes and none of these values re-prompt the user.
# "always" (default) -> re-stamp the grant hash after ANY refresh (manual or auto)
# "pulled_from_remote" -> re-stamp only when the change came from a teammate's git pull
# null  -> skip the re-stamp (under CHAMELEON_TRUST_REVALIDATE=1 this is what surfaces
#          a material refresh as "stale"; with the env unset it has no visible effect)
_VALID_AUTO_PRESERVE = frozenset({None, "pulled_from_remote", "always"})

_VALID_ENFORCE_MODES = frozenset({"off", "shadow", "enforce"})


def _coerce_enforcement(raw: Any) -> EnforcementConfig:
    if raw is None:
        return EnforcementConfig()
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"`enforcement` must be an object, got {type(raw).__name__}")
    # Unknown keys under `enforcement` are tolerated (ignored), not rejected.
    # config.json is committed and trust-hashed, so it travels via git to
    # teammates who may run a different chameleon version. A newer version that
    # adds an enforcement key must not brick auto-refresh / branch-pinning for a
    # teammate still on an older engine (which would hard-reject the unknown key
    # and surface a scary ChameleonConfigError in /chameleon-doctor). Known keys
    # are still type-validated below, so a typo in a known key's VALUE still
    # raises; only a key this engine does not recognize is skipped.
    mode = raw.get("mode", "enforce")
    if not isinstance(mode, str) or mode not in _VALID_ENFORCE_MODES:
        raise ChameleonConfigError(
            f"`enforcement.mode` must be one of {sorted(_VALID_ENFORCE_MODES)}, got {mode!r}"
        )
    stop_backstop = raw.get("stop_backstop", True)
    if not isinstance(stop_backstop, bool):
        raise ChameleonConfigError(
            f"`enforcement.stop_backstop` must be bool, got {type(stop_backstop).__name__}"
        )
    cap = raw.get("stop_block_cap", 3)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ChameleonConfigError(
            f"`enforcement.stop_block_cap` must be a non-negative int, got {cap!r}"
        )
    idiom_review = raw.get("idiom_review", True)
    if not isinstance(idiom_review, bool):
        raise ChameleonConfigError(
            f"`enforcement.idiom_review` must be bool, got {type(idiom_review).__name__}"
        )
    idiom_judge = raw.get("idiom_judge", False)
    if not isinstance(idiom_judge, bool):
        raise ChameleonConfigError(
            f"`enforcement.idiom_judge` must be bool, got {type(idiom_judge).__name__}"
        )
    correctness_judge = raw.get("correctness_judge", True)
    if not isinstance(correctness_judge, bool):
        raise ChameleonConfigError(
            f"`enforcement.correctness_judge` must be bool, got {type(correctness_judge).__name__}"
        )
    stale_test_advisory = raw.get("stale_test_advisory", True)
    if not isinstance(stale_test_advisory, bool):
        raise ChameleonConfigError(
            "`enforcement.stale_test_advisory` must be bool, got "
            f"{type(stale_test_advisory).__name__}"
        )
    changeset_completeness = raw.get("changeset_completeness", True)
    if not isinstance(changeset_completeness, bool):
        raise ChameleonConfigError(
            "`enforcement.changeset_completeness` must be bool, got "
            f"{type(changeset_completeness).__name__}"
        )
    crossfile_existence_advisory = raw.get("crossfile_existence_advisory", True)
    if not isinstance(crossfile_existence_advisory, bool):
        raise ChameleonConfigError(
            "`enforcement.crossfile_existence_advisory` must be bool, got "
            f"{type(crossfile_existence_advisory).__name__}"
        )
    duplication_review = raw.get("duplication_review", True)
    if not isinstance(duplication_review, bool):
        raise ChameleonConfigError("enforcement.duplication_review must be a boolean")
    judge_crossfile_facts = raw.get("judge_crossfile_facts", True)
    if not isinstance(judge_crossfile_facts, bool):
        raise ChameleonConfigError(
            "`enforcement.judge_crossfile_facts` must be bool, got "
            f"{type(judge_crossfile_facts).__name__}"
        )
    signature_contract_diff = raw.get("signature_contract_diff", True)
    if not isinstance(signature_contract_diff, bool):
        raise ChameleonConfigError(
            "`enforcement.signature_contract_diff` must be bool, got "
            f"{type(signature_contract_diff).__name__}"
        )
    judge_imported_definitions = raw.get("judge_imported_definitions", True)
    if not isinstance(judge_imported_definitions, bool):
        raise ChameleonConfigError(
            "`enforcement.judge_imported_definitions` must be bool, got "
            f"{type(judge_imported_definitions).__name__}"
        )
    judge_transitive_impact = raw.get("judge_transitive_impact", True)
    if not isinstance(judge_transitive_impact, bool):
        raise ChameleonConfigError(
            "`enforcement.judge_transitive_impact` must be bool, got "
            f"{type(judge_transitive_impact).__name__}"
        )
    test_integrity_review = raw.get("test_integrity_review", True)
    if not isinstance(test_integrity_review, bool):
        raise ChameleonConfigError(
            "`enforcement.test_integrity_review` must be bool, got "
            f"{type(test_integrity_review).__name__}"
        )
    multi_lens_review = raw.get("multi_lens_review", False)
    if not isinstance(multi_lens_review, bool):
        raise ChameleonConfigError(
            f"`enforcement.multi_lens_review` must be bool, got {type(multi_lens_review).__name__}"
        )
    intent_scope_advisory = raw.get("intent_scope_advisory", True)
    if not isinstance(intent_scope_advisory, bool):
        raise ChameleonConfigError(
            "`enforcement.intent_scope_advisory` must be bool, got "
            f"{type(intent_scope_advisory).__name__}"
        )
    return EnforcementConfig(
        mode=mode,
        stop_backstop=stop_backstop,
        stop_block_cap=cap,
        idiom_review=idiom_review,
        idiom_judge=idiom_judge,
        correctness_judge=correctness_judge,
        stale_test_advisory=stale_test_advisory,
        changeset_completeness=changeset_completeness,
        crossfile_existence_advisory=crossfile_existence_advisory,
        duplication_review=duplication_review,
        judge_crossfile_facts=judge_crossfile_facts,
        signature_contract_diff=signature_contract_diff,
        judge_imported_definitions=judge_imported_definitions,
        judge_transitive_impact=judge_transitive_impact,
        test_integrity_review=test_integrity_review,
        multi_lens_review=multi_lens_review,
        intent_scope_advisory=intent_scope_advisory,
    )


def _coerce_auto_refresh(raw: Any) -> AutoRefreshConfig:
    if raw is None:
        return AutoRefreshConfig()
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"`auto_refresh` must be an object, got {type(raw).__name__}")
    allowed = {"enabled", "drift_threshold", "max_age_hours", "fetch_production_ref"}
    unknown = set(raw.keys()) - allowed
    if unknown:
        raise ChameleonConfigError(
            f"unknown key(s) under auto_refresh: {sorted(unknown)!r}; allowed: {sorted(allowed)!r}"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ChameleonConfigError(
            f"`auto_refresh.enabled` must be bool, got {type(enabled).__name__}"
        )
    fetch_production_ref = raw.get("fetch_production_ref", True)
    if not isinstance(fetch_production_ref, bool):
        raise ChameleonConfigError(
            "`auto_refresh.fetch_production_ref` must be bool, got "
            f"{type(fetch_production_ref).__name__}"
        )
    threshold = raw.get("drift_threshold", 0.2)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not (0.0 <= float(threshold) <= 1.0)
    ):
        raise ChameleonConfigError(
            f"`auto_refresh.drift_threshold` must be a number in [0, 1], got {threshold!r}"
        )
    max_age = raw.get("max_age_hours", 168)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
        raise ChameleonConfigError(
            f"`auto_refresh.max_age_hours` must be a positive int, got {max_age!r}"
        )
    return AutoRefreshConfig(
        enabled=enabled,
        drift_threshold=float(threshold),
        max_age_hours=max_age,
        fetch_production_ref=fetch_production_ref,
    )


def _coerce_trust(raw: Any) -> TrustConfig:
    if raw is None:
        return TrustConfig()
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"`trust` must be an object, got {type(raw).__name__}")
    allowed = {"auto_preserve_when"}
    unknown = set(raw.keys()) - allowed
    if unknown:
        raise ChameleonConfigError(
            f"unknown key(s) under trust: {sorted(unknown)!r}; allowed: {sorted(allowed)!r}"
        )
    # Absent key defaults to "always" (auto-trust on refresh); an explicit null
    # opts back into re-prompting.
    apw = raw.get("auto_preserve_when", "always")
    if not (apw is None or isinstance(apw, str)) or apw not in _VALID_AUTO_PRESERVE:
        raise ChameleonConfigError(
            f"`trust.auto_preserve_when` must be one of {sorted(s for s in _VALID_AUTO_PRESERVE if s)} or null, got {apw!r}"
        )
    return TrustConfig(auto_preserve_when=apw)


def load_config(profile_dir: Path) -> ChameleonConfig:
    """Return the parsed config for ``<profile_dir>/config.json``.

    Returns a default ``ChameleonConfig`` when the file is missing
    (repos without a config get the built-in defaults: auto_refresh
    on, auto_rename on, trust.auto_preserve_when="always"). Raises
    ``ChameleonConfigError`` only when a present
    file is malformed; never crashes on a missing file.
    """
    path = profile_dir / CONFIG_FILENAME
    if not path.is_file():
        return ChameleonConfig()
    # config.json is a trust-hashed artifact, so give it the same read-path
    # hardening as the others: O_NOFOLLOW + size cap + duplicate-key/depth.
    from chameleon_mcp.profile.schema import SchemaError, _check_depth, _no_duplicate_keys
    from chameleon_mcp.safe_open import UnsafeFileError, safe_read_profile_artifact

    try:
        text = safe_read_profile_artifact(path)
    except FileNotFoundError:
        return ChameleonConfig()
    except (UnsafeFileError, OSError) as exc:
        raise ChameleonConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        _check_depth(raw)
    except (json.JSONDecodeError, SchemaError) as exc:
        raise ChameleonConfigError(f"{path} is not valid/safe JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"{path}: top-level must be an object, got {type(raw).__name__}")

    # Unknown TOP-LEVEL keys are tolerated (ignored), not rejected — the same
    # compat posture as the enforcement section: config.json is committed and
    # trust-hashed, so it travels via git to teammates who may run a different
    # chameleon version. A newer engine that adds a top-level key must not
    # brick branch pinning / auto-refresh / enforcement reads for a teammate
    # still on an older engine. Known keys are still strictly type-validated
    # below, so a typo in a known key's VALUE raises; only a key this engine
    # does not recognize is skipped.

    schema = raw.get("$schema", CURRENT_SCHEMA)
    if not isinstance(schema, str):
        raise ChameleonConfigError(f"`$schema` must be a string, got {type(schema).__name__}")

    canonical_ref = raw.get("canonical_ref")
    if canonical_ref is not None and not (isinstance(canonical_ref, str) and canonical_ref.strip()):
        raise ChameleonConfigError(
            f"`canonical_ref` must be a non-empty string or null, got {canonical_ref!r}"
        )

    production_ref = raw.get("production_ref")
    if production_ref is not None and not (
        isinstance(production_ref, str) and production_ref.strip()
    ):
        raise ChameleonConfigError(
            f"`production_ref` must be a non-empty string or null, got {production_ref!r}"
        )
    # SECURITY: the refresh-time fetch passes production_ref positionally into
    # `git fetch origin <value>`. Only a plain branch name is safe: a leading
    # '-' is an option (--upload-pack=<cmd> -> RCE), and a ':'/'+'/'*' makes it a
    # refspec that writes a local ref. Refuse anything else here too (the fetch
    # itself also refuses + uses --end-of-options).
    if isinstance(production_ref, str) and production_ref.strip():
        from chameleon_mcp.production_ref import is_safe_branch_name

        if not is_safe_branch_name(production_ref):
            raise ChameleonConfigError(
                f"`production_ref` must be a plain branch name (got {production_ref!r}): "
                "values with '-' prefix, ':', '+', '*', or whitespace are a git "
                "argument/refspec-injection risk"
            )

    auto_rename = raw.get("auto_rename", True)
    if not isinstance(auto_rename, bool):
        raise ChameleonConfigError(f"`auto_rename` must be bool, got {type(auto_rename).__name__}")

    repo_uuid = raw.get("repo_uuid")
    if repo_uuid is not None and not (isinstance(repo_uuid, str) and repo_uuid.strip()):
        raise ChameleonConfigError(
            f"`repo_uuid` must be a non-empty string or null, got {repo_uuid!r}"
        )

    return ChameleonConfig(
        schema_version=schema,
        canonical_ref=canonical_ref.strip() if canonical_ref else None,
        production_ref=production_ref.strip() if production_ref else None,
        auto_refresh=_coerce_auto_refresh(raw.get("auto_refresh")),
        trust=_coerce_trust(raw.get("trust")),
        enforcement=_coerce_enforcement(raw.get("enforcement")),
        auto_rename=auto_rename,
        repo_uuid=repo_uuid.strip() if repo_uuid else None,
    )


def load_config_enforcement_only(profile_dir: Path) -> EnforcementConfig:
    """Parse ONLY the ``enforcement`` section of ``config.json``.

    The enforcement GATES (PreToolUse deny, PostToolUse block, Stop backstop)
    need exactly ``enforcement.mode`` and nothing else. Routing them through the
    full :func:`load_config` couples them to every other section: a typo in an
    UNRELATED section (``auto_refresh``, ``trust``, ...) makes ``load_config``
    raise :class:`ChameleonConfigError`, which the gates swallow fail-open --
    silently disabling credential / import blocking over a typo that has nothing
    to do with enforcement. Reading the enforcement section in isolation breaks
    that coupling: an unrelated-section typo can no longer disable the deny,
    while a genuinely malformed enforcement section (or an unreadable /
    invalid-JSON file) still raises and is still handled fail-open WITH a
    degraded signal at the gate.

    Reuses the same hardened read path as :func:`load_config` (O_NOFOLLOW + size
    cap + duplicate-key + depth) so it is not a weaker parser. Returns the
    default :class:`EnforcementConfig` for a missing file. Raises
    :class:`ChameleonConfigError` only when the file is unreadable, is not
    valid/safe JSON, is not an object, or its ``enforcement`` section is
    malformed -- never for a malformed sibling section.
    """
    path = profile_dir / CONFIG_FILENAME
    if not path.is_file():
        return EnforcementConfig()
    from chameleon_mcp.profile.schema import SchemaError, _check_depth, _no_duplicate_keys
    from chameleon_mcp.safe_open import UnsafeFileError, safe_read_profile_artifact

    try:
        text = safe_read_profile_artifact(path)
    except FileNotFoundError:
        return EnforcementConfig()
    except (UnsafeFileError, OSError) as exc:
        raise ChameleonConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        _check_depth(raw)
    except (json.JSONDecodeError, SchemaError) as exc:
        raise ChameleonConfigError(f"{path} is not valid/safe JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"{path}: top-level must be an object, got {type(raw).__name__}")
    return _coerce_enforcement(raw.get("enforcement"))
