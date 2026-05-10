"""Trust state management for committed profiles.

Per ARCHITECTURE.md "Profile schema" → `.trust` file format + Round 4
trust model with cooldown.

Trust is per-user, per-repo. Stored at `${PLUGIN_DATA}/<repo_id>/.trust`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


def plugin_data_dir() -> Path:
    """Resolve `${CLAUDE_PLUGIN_DATA}` (or fall back to a sensible default).

    Phase 2D: respects override env var; falls back to ~/.local/share/chameleon
    when CLAUDE_PLUGIN_DATA is not set.
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "chameleon"


def repo_data_dir(repo_id: str) -> Path:
    """`${PLUGIN_DATA}/<repo_id>/` directory, created if missing."""
    d = plugin_data_dir() / repo_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class TrustRecord:
    """Contents of `.trust` file."""

    granted_at: str
    granted_by_user: str
    profile_sha256: str

    @classmethod
    def from_dict(cls, data: dict) -> "TrustRecord":
        return cls(
            granted_at=str(data.get("granted_at", "")),
            granted_by_user=str(data.get("granted_by_user", "")),
            profile_sha256=str(data.get("profile_sha256", "")),
        )

    def to_dict(self) -> dict:
        return {
            "granted_at": self.granted_at,
            "granted_by_user": self.granted_by_user,
            "profile_sha256": self.profile_sha256,
        }


def hash_profile(profile_dir: Path) -> str:
    """SHA-256 of `profile.json` content for material-change detection."""
    profile_json = profile_dir / "profile.json"
    if not profile_json.is_file():
        return ""
    return hashlib.sha256(profile_json.read_bytes()).hexdigest()


def trust_state_for(repo_id: str) -> TrustRecord | None:
    """Read the .trust file for a repo. Returns None if not trusted."""
    trust_path = repo_data_dir(repo_id) / ".trust"
    if not trust_path.is_file():
        return None
    try:
        return TrustRecord.from_dict(json.loads(trust_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError):
        return None


def grant_trust(repo_id: str, profile_dir: Path) -> TrustRecord:
    """Write a fresh .trust record for repo_id with current profile hash.

    Uses an atomic write pattern (write tmp then rename) to avoid partial files.
    """
    record = TrustRecord(
        granted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        granted_by_user=_current_user(),
        profile_sha256=hash_profile(profile_dir),
    )
    trust_path = repo_data_dir(repo_id) / ".trust"
    tmp_path = trust_path.with_suffix(".trust.tmp")
    tmp_path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp_path, trust_path)
    return record


def revoke_trust(repo_id: str) -> bool:
    """Remove the .trust record. Returns True if a record existed and was removed."""
    trust_path = repo_data_dir(repo_id) / ".trust"
    if not trust_path.exists():
        return False
    trust_path.unlink()
    return True


def is_material_change(repo_id: str, current_profile_dir: Path) -> bool:
    """Return True iff the trusted profile_sha256 no longer matches current.

    Per ARCHITECTURE.md material-change predicate: hash mismatch → re-prompt
    on next session. (Phase 2D simplification: any hash change is treated as
    material; Phase 4 refines to "any new archetype, new canonical witness file,
    or new active idiom" only.)
    """
    record = trust_state_for(repo_id)
    if record is None:
        return False  # no trust record → not a "material change" (just untrusted)
    return record.profile_sha256 != hash_profile(current_profile_dir)


def _current_user() -> str:
    """Best-effort current-user identification (for trust audit trail)."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")
