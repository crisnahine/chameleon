"""Arm construction for the effectiveness eval."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path


class ArmError(Exception):
    pass


VALID_ARM_NAMES = ("off", "shadow", "enforce")


@dataclasses.dataclass(frozen=True)
class ArmSpec:
    name: str
    base_mode: str  # enforcement.mode written to the worktree config
    disable_env: bool  # True only for the "off" arm
    toggle_key: str | None = None
    toggle_value: bool | None = None
    env_key: str | None = None  # env var set for this arm (env-flag toggle)
    env_value: str | None = None  # the value env_key is set to ("1" or "0")


# Feature toggles that are env vars, not config.json enforcement keys. The
# config-key toggle path cannot flip these (they are read from the environment at
# hook time, not the profile), so they get a paired arm that sets the env var for
# its sessions instead. Maps the --toggle name to (env var, value): a default-OFF
# feature is set "1" to turn it on for the paired arm; a default-ON feature is set
# "0" to turn it off, so either way the diff against the base isolates the feature.
_ENV_TOGGLES: dict[str, tuple[str, str]] = {
    "nearby_signatures": ("CHAMELEON_NEARBY_SIGNATURES", "1"),
    "counterexample": ("CHAMELEON_COUNTEREXAMPLE", "0"),
}


def _toggleable_keys() -> dict[str, bool]:
    from chameleon_mcp.profile.config import EnforcementConfig

    defaults = EnforcementConfig()
    return {
        f.name: getattr(defaults, f.name)
        for f in dataclasses.fields(EnforcementConfig)
        if isinstance(getattr(defaults, f.name), bool)
    }


def parse_arms(arms_csv: str, toggle: str | None) -> list[ArmSpec]:
    names = [a.strip() for a in arms_csv.split(",") if a.strip()]
    if not names:
        raise ArmError("--arms must name at least one arm")
    bad = [n for n in names if n not in VALID_ARM_NAMES]
    if bad:
        raise ArmError(f"unknown arm(s) {bad}; valid: {VALID_ARM_NAMES}")
    if len(set(names)) != len(names):
        raise ArmError("duplicate arm names")
    specs = [
        ArmSpec(name=n, base_mode=("shadow" if n == "off" else n), disable_env=(n == "off"))
        for n in names
    ]
    if toggle:
        non_off = [s for s in specs if not s.disable_env]
        if not non_off:
            raise ArmError("--toggle needs a non-off base arm (default shadow)")
        base = next((s for s in non_off if s.name == "shadow"), non_off[0])
        env_toggle = _ENV_TOGGLES.get(toggle)
        if env_toggle is not None:
            # Env-flag feature (e.g. nearby_signatures, counterexample): a paired
            # arm identical to the base except the env var is flipped, so the diff
            # isolates the feature. "1" turns a default-OFF feature on; "0" turns a
            # default-ON feature off.
            env_key, env_value = env_toggle
            direction = "on" if env_value == "1" else "off"
            specs.append(
                ArmSpec(
                    name=f"{base.name}~{toggle}={direction}",
                    base_mode=base.base_mode,
                    disable_env=False,
                    env_key=env_key,
                    env_value=env_value,
                )
            )
            return specs
        key = toggle.removeprefix("enforcement.")
        keys = _toggleable_keys()
        if key not in keys:
            raise ArmError(
                f"--toggle {toggle!r} is not a boolean enforcement key or env toggle; "
                f"valid keys: {sorted(keys)}; valid env toggles: {sorted(_ENV_TOGGLES)}"
            )
        flipped = not keys[key]
        specs.append(
            ArmSpec(
                name=f"{base.name}~{key}={str(flipped).lower()}",
                base_mode=base.base_mode,
                disable_env=False,
                toggle_key=key,
                toggle_value=flipped,
            )
        )
    return specs


def arm_env(spec: ArmSpec, base_env: dict[str, str]) -> dict[str, str]:
    """Per-arm session env: copy of the run env, plus the off arm's kill switch
    and any env-flag toggle this arm turns on."""
    env = dict(base_env)
    if spec.disable_env:
        env["CHAMELEON_DISABLE"] = "1"
    if spec.env_key is not None:
        env[spec.env_key] = spec.env_value or "1"
    return env


def apply_arm_config(spec: ArmSpec, worktree: Path) -> None:
    """Write the arm's enforcement mode (and toggle flip) into the worktree config.

    Read-modify-write so every other committed key (production_ref,
    canonical_ref, auto_refresh, repo_uuid) is preserved. MUST run before the
    worktree's trust grant: config.json is part of the trust hash, so flipping
    it afterwards would de-trust the profile mid-cell.
    """
    cfg_path = worktree / ".chameleon" / "config.json"
    data: dict = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            data = {}
    enforcement = data.get("enforcement")
    if not isinstance(enforcement, dict):
        enforcement = {}
        data["enforcement"] = enforcement
    enforcement["mode"] = spec.base_mode
    if spec.toggle_key is not None:
        enforcement[spec.toggle_key] = spec.toggle_value
    # Cells must be hermetic: the arm-setup commit makes the cloned profile
    # look stale, and a mid-session auto-refresh then re-derives it, polluting
    # the session diff with profile churn and charging one arm a re-derivation
    # the other never pays. Same-cell comparability beats realism here.
    auto_refresh = data.get("auto_refresh")
    if not isinstance(auto_refresh, dict):
        auto_refresh = {}
        data["auto_refresh"] = auto_refresh
    auto_refresh["enabled"] = False
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
