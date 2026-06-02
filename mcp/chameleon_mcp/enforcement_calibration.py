"""Per-repo block-rule calibration artifact (``.chameleon/enforcement.json``).

A block rule is only allowed to block in a repo if it produces (near) zero
violations against that repo's own committed files. This module persists and
reads that decision; the measurement lives in ``calibrate_block_rules``.
Fail-open: a missing/corrupt artifact means no rule is active (advisory only).
"""

from __future__ import annotations

import json
from pathlib import Path

ARTIFACT = "enforcement.json"


def write_block_rules(profile_dir: Path, data: dict) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {"block_rules": data}
    tmp = profile_dir / (ARTIFACT + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.rename(profile_dir / ARTIFACT)


def load_block_rules(profile_dir: Path) -> dict:
    path = profile_dir / ARTIFACT
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rules = raw.get("block_rules")
    return rules if isinstance(rules, dict) else {}


def active_block_rules(profile_dir: Path) -> set[str]:
    out = set()
    for rule, meta in load_block_rules(profile_dir).items():
        if isinstance(meta, dict) and meta.get("active") is True:
            out.add(rule)
    return out
