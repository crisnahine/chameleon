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
    allowed = {"mode", "stop_backstop", "stop_block_cap", "idiom_review", "idiom_judge"}
    unknown = set(raw.keys()) - allowed
    if unknown:
        raise ChameleonConfigError(
            f"unknown key(s) under enforcement: {sorted(unknown)!r}; allowed: {sorted(allowed)!r}"
        )
    mode = raw.get("mode", "shadow")
    if mode not in _VALID_ENFORCE_MODES:
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
    return EnforcementConfig(
        mode=mode,
        stop_backstop=stop_backstop,
        stop_block_cap=cap,
        idiom_review=idiom_review,
        idiom_judge=idiom_judge,
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
    if apw not in _VALID_AUTO_PRESERVE:
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
