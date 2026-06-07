"""``.chameleon/config.json`` reader for per-repo UX features.

Earlier versions had no per-repo configuration file; behavior was hard-coded.
These features (branch pinning, auto-refresh,
trust-friction reduction, auto-rename) need a place to be
configured per repo. This module loads + validates the file.

Schema (all fields optional, all have safe defaults):

```jsonc
{
  "$schema": "chameleon-config-0.8.0",
  "canonical_ref": "origin/main",          // branch pinning
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
CURRENT_SCHEMA = "chameleon-config-0.8.0"


class ChameleonConfigError(ValueError):
    """Raised when ``.chameleon/config.json`` is present but malformed."""


@dataclass(frozen=True)
class AutoRefreshConfig:
    enabled: bool = True
    drift_threshold: float = 0.2
    max_age_hours: int = 168


@dataclass(frozen=True)
class TrustConfig:
    # "always" by default: a refresh (manual or auto) re-grants trust so the user
    # is not re-prompted on their own repo. Opt out with auto_preserve_when=null
    # to re-prompt on any non-structurally-identical change.
    auto_preserve_when: str | None = "always"


@dataclass(frozen=True)
class EnforcementConfig:
    # mode master switch: "off" = advisory only; "shadow" = log would-have-blocked
    # but never block; "enforce" = real deny/block. Default shadow so a newly
    # enabled repo measures before it enforces.
    mode: str = "shadow"
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


@dataclass(frozen=True)
class ChameleonConfig:
    schema_version: str = CURRENT_SCHEMA
    canonical_ref: str | None = None
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


# "always" (default) -> re-grant trust after ANY refresh (manual or auto), so the user
#             is not re-prompted on their own repo.
# "pulled_from_remote" -> re-grant only when the change came from a teammate's git pull
# null  -> opt out: re-prompt for trust on any non-structurally-identical refresh
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
    mode = raw.get("mode", "shadow")
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
    correctness_judge = raw.get("correctness_judge", False)
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
    )


def _coerce_auto_refresh(raw: Any) -> AutoRefreshConfig:
    if raw is None:
        return AutoRefreshConfig()
    if not isinstance(raw, dict):
        raise ChameleonConfigError(f"`auto_refresh` must be an object, got {type(raw).__name__}")
    allowed = {"enabled", "drift_threshold", "max_age_hours"}
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

    allowed_top = {
        "$schema",
        "canonical_ref",
        "auto_refresh",
        "trust",
        "enforcement",
        "auto_rename",
        "repo_uuid",
    }
    unknown_top = set(raw.keys()) - allowed_top
    if unknown_top:
        raise ChameleonConfigError(
            f"{path}: unknown top-level key(s): {sorted(unknown_top)!r}; "
            f"allowed: {sorted(allowed_top)!r}"
        )

    schema = raw.get("$schema", CURRENT_SCHEMA)
    if not isinstance(schema, str):
        raise ChameleonConfigError(f"`$schema` must be a string, got {type(schema).__name__}")

    canonical_ref = raw.get("canonical_ref")
    if canonical_ref is not None and not (isinstance(canonical_ref, str) and canonical_ref.strip()):
        raise ChameleonConfigError(
            f"`canonical_ref` must be a non-empty string or null, got {canonical_ref!r}"
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
        auto_refresh=_coerce_auto_refresh(raw.get("auto_refresh")),
        trust=_coerce_trust(raw.get("trust")),
        enforcement=_coerce_enforcement(raw.get("enforcement")),
        auto_rename=auto_rename,
        repo_uuid=repo_uuid.strip() if repo_uuid else None,
    )
