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
        key = toggle.removeprefix("enforcement.")
        keys = _toggleable_keys()
        if key not in keys:
            raise ArmError(
                f"--toggle {toggle!r} is not a boolean enforcement key; valid: {sorted(keys)}"
            )
        non_off = [s for s in specs if not s.disable_env]
        if not non_off:
            raise ArmError("--toggle needs a non-off base arm (default shadow)")
        base = next((s for s in non_off if s.name == "shadow"), non_off[0])
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
    """Per-arm session env: copy of the run env, plus the off arm's kill switch."""
    env = dict(base_env)
    if spec.disable_env:
        env["CHAMELEON_DISABLE"] = "1"
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
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
