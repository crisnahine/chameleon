"""Trust state management for committed profiles.

Per docs/architecture.md "Profile schema" → `.trust` file format + Round 4
trust model with cooldown.

Trust is per-user, per-repo. Stored at `${PLUGIN_DATA}/<repo_id>/.trust`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.safe_open import (
    UnsafeFileError,
    safe_read_profile_artifact_bytes,
)


def plugin_data_dir() -> Path:
    """Resolve where chameleon stores per-user state (trust DB, drift.db).

    Trust state is per-user, not per-plugin-instance: the same user editing
    the same repo from a Claude Code plugin invocation, a Cursor plugin
    invocation, or a direct CLI call must see the SAME trust record.
    Therefore we deliberately do NOT honor CLAUDE_PLUGIN_DATA — Claude Code
    sets that to a per-plugin sandbox path, which would partition trust
    records across launchers and leave Claude Code-spawned MCP calls
    unable to see trust granted by direct tool calls (real bug observed
    in production).

    CHAMELEON_PLUGIN_DATA exists for tests that need to isolate state to a
    tmpdir; it is the only supported override.
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA")
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
    """Contents of `.trust` file.

    Schema:
        granted_at: ISO-8601 timestamp of the (first) grant.
        granted_by_user: best-effort username for the audit trail.
        profile_sha256: hash of the "root" profile_dir at grant time. This
            is what older trust records carried alone; v0.5.1 keeps it as
            the fallback hash for any repo_root not present in the new
            ``repo_root_specific_hashes`` map (backward compat).
        repo_root: filesystem path of the profile_dir's parent recorded
            on the FIRST grant. For non-monorepo repos this is the only
            trusted root. For monorepos with multiple workspace-internal
            trust grants the value reflects whichever root was trusted
            first; subsequent workspace grants land in the per-root map.
        repo_root_specific_hashes: optional map of resolved-repo_root path
            → profile_sha256, populated when a workspace-internal profile
            is trusted alongside (or instead of) the root. Lookups for a
            given repo_root prefer this map; absence falls back to
            ``profile_sha256``. v0.5.0 records that lack this field still
            load (defaults to ``{}``).
    """

    granted_at: str
    granted_by_user: str
    profile_sha256: str
    repo_root: str = ""
    repo_root_specific_hashes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> TrustRecord:
        raw_map = data.get("repo_root_specific_hashes") or {}
        # Defensive: only accept str→str entries. Anything else gets dropped
        # so a corrupted record still loads (the worst case is a fallback
        # to ``profile_sha256``, i.e., the pre-v0.5.1 behavior).
        specific: dict[str, str] = {}
        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                if isinstance(k, str) and isinstance(v, str):
                    specific[k] = v
        return cls(
            granted_at=str(data.get("granted_at", "")),
            granted_by_user=str(data.get("granted_by_user", "")),
            profile_sha256=str(data.get("profile_sha256", "")),
            repo_root=str(data.get("repo_root", "")),
            repo_root_specific_hashes=specific,
        )

    def to_dict(self) -> dict:
        out: dict = {
            "granted_at": self.granted_at,
            "granted_by_user": self.granted_by_user,
            "profile_sha256": self.profile_sha256,
            "repo_root": self.repo_root,
        }
        # Only persist the map when it carries data — keeps v0.5.0 records
        # byte-identical to before so the diff stays minimal on disk.
        if self.repo_root_specific_hashes:
            out["repo_root_specific_hashes"] = dict(self.repo_root_specific_hashes)
        return out

    def hash_for_root(self, repo_root: Path | str) -> str:
        """Return the most-specific trusted hash for ``repo_root``.

        Lookup order:
            1. ``repo_root_specific_hashes[str(repo_root.resolve())]`` when
               a workspace-internal grant has been recorded.
            2. ``profile_sha256`` — the "root" hash recorded on the first
               grant. This is the backward-compat path for v0.5.0 records
               and for repos where only the top-level was trusted.

        Always returns a string (possibly empty when the record is malformed).
        """
        try:
            key = str(Path(repo_root).resolve())
        except OSError:
            key = str(repo_root)
        specific = self.repo_root_specific_hashes.get(key)
        if specific:
            return specific
        return self.profile_sha256


# The artifact ordering for hash_profile. Listed alphabetically so a hash
# value can be reproduced offline (e.g., from a git diff) without needing
# to consult the source. Each entry is hashed only when present on disk.
_HASHED_ARTIFACTS: tuple[str, ...] = (
    "archetypes.json",
    "canonicals.json",
    "idioms.md",
    "profile.json",
    "rules.json",
)


def hash_profile(profile_dir: Path) -> str:
    """SHA-256 over the user-visible profile surface for material-change detection.

    Hashes every artifact in :data:`_HASHED_ARTIFACTS` that exists on disk,
    in alphabetical filename order, with each entry framed by
    ``b"\\x00<filename>\\x00"`` so two artifacts can never collide via
    boundary ambiguity. The fixed ordering plus per-file framing means the
    hash is reproducible byte-for-byte from the profile_dir alone — useful
    for audit reproducibility.

    Included artifacts (alphabetical):

    - ``archetypes.json`` — archetype definitions. ``/chameleon-rename``
      mutates these; v0.5.0 did NOT include this file, so renames slipped
      past trust unchanged (Bug H1).
    - ``canonicals.json`` — canonical witness mappings. Also rewritten by
      ``/chameleon-rename``.
    - ``idioms.md`` — captured team idioms. ``/chameleon-teach`` mutates
      this; included so the user re-reviews new natural-language idioms
      before they reach model context.
    - ``profile.json`` — top-level profile + summary. The original v0.1
      hash input.
    - ``rules.json`` — lint rules; ``/chameleon-rename`` may rewrite
      archetype-keyed entries here.

    Returns an empty string if ``profile.json`` is missing — callers treat
    that as "no trustable profile yet" rather than a real hash. Missing
    optional artifacts (e.g., ``idioms.md`` on a repo that hasn't run
    /chameleon-teach) are simply skipped: their framing bytes never get
    written, so adding the file later produces a distinct hash.
    """
    profile_json = profile_dir / "profile.json"
    if not profile_json.is_file():
        return ""
    h = hashlib.sha256()
    for filename in _HASHED_ARTIFACTS:
        artifact = profile_dir / filename
        try:
            body = safe_read_profile_artifact_bytes(artifact)
        except FileNotFoundError:
            # Genuinely absent: skip framing. Adding the file later produces
            # a distinct hash because the framing bytes appear then.
            continue
        except (OSError, UnsafeFileError) as exc:
            # Symlink, non-regular, or oversized artifact. A teammate-
            # committed 100 MB idioms.md (or a symlink to /etc/passwd)
            # must not balloon trust-check memory — and must not collide
            # with the "absent" hash, otherwise an attacker who plants
            # an unsafe artifact after grant cannot be detected as a
            # material change. Frame a distinguishing sentinel that
            # depends on the failure reason so post-grant swaps trip
            # the trust re-prompt.
            h.update(b"\x01" + filename.encode("utf-8") + b"\x01")
            h.update(b"UNSAFE:" + type(exc).__name__.encode("ascii"))
            continue
        h.update(b"\x00" + filename.encode("utf-8") + b"\x00")
        h.update(body)
    return h.hexdigest()


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
    """Write or update a .trust record for repo_id with current profile hash.

    Uses an atomic write pattern (write tmp then rename) to avoid partial
    files. ``profile_dir.parent`` (the "repo_root" being trusted) is
    recorded so future tool calls can resolve repo_id → repo_root without
    scanning every known repo.

    Per-root storage (v0.5.1, Bug H6):

    Repositories with multiple `repo_root`-equivalent layouts under the
    same git remote (monorepos with per-workspace .chameleon/, hybrid
    Rails+JS, etc.) all share a single repo_id. To keep workspace-internal
    trust grants from being clobbered by a root grant (and vice versa)
    the record now carries an additive map of resolved
    ``repo_root → profile_sha256``.

    - First grant (no existing record): writes the top-level
      ``profile_sha256`` AND seeds ``repo_root_specific_hashes`` with the
      same hash, keyed by the resolved repo_root. The top-level fields
      mirror v0.5.0 semantics for any caller still reading the legacy
      shape.
    - Subsequent grant for the SAME repo_root as the existing record:
      refreshes both the top-level hash and the matching map entry.
    - Subsequent grant for a DIFFERENT repo_root (workspace-internal
      trust under the same repo_id): leaves the top-level
      ``profile_sha256`` + ``repo_root`` alone and only writes/updates
      the per-root map entry. This preserves the original "root" trust
      while extending coverage to the workspace.
    """
    repo_root = profile_dir.parent
    try:
        repo_root_str = str(repo_root.resolve())
    except OSError:
        repo_root_str = str(repo_root)
    new_hash = hash_profile(profile_dir)

    existing = trust_state_for(repo_id)
    if existing is None:
        record = TrustRecord(
            granted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            granted_by_user=_current_user(),
            profile_sha256=new_hash,
            repo_root=repo_root_str,
            repo_root_specific_hashes={repo_root_str: new_hash},
        )
    else:
        # Mutate the existing record additively. Reuse the original
        # granted_at + granted_by_user when we're only extending coverage
        # to a new workspace so the audit trail still points at the first
        # grant; refresh them on a same-root re-grant.
        specific = dict(existing.repo_root_specific_hashes)
        specific[repo_root_str] = new_hash
        if existing.repo_root == repo_root_str or not existing.repo_root:
            # Same root → also refresh the top-level fields.
            record = TrustRecord(
                granted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                granted_by_user=_current_user(),
                profile_sha256=new_hash,
                repo_root=repo_root_str,
                repo_root_specific_hashes=specific,
            )
        else:
            # Different root (workspace-internal grant under same repo_id):
            # keep the original "root" trust intact and only extend the
            # per-root map.
            record = TrustRecord(
                granted_at=existing.granted_at,
                granted_by_user=existing.granted_by_user,
                profile_sha256=existing.profile_sha256,
                repo_root=existing.repo_root,
                repo_root_specific_hashes=specific,
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

    Per docs/architecture.md material-change predicate: hash mismatch → re-prompt
    on next session. (Phase 2D simplification: any hash change is treated as
    material; Phase 4 refines to "any new archetype, new canonical witness file,
    or new active idiom" only.)

    v0.5.1 (Bug H6): consults the per-root hash map first via
    ``record.hash_for_root(current_profile_dir.parent)``. When a workspace
    has its own trust grant, this returns the workspace's hash; when only
    the root was trusted, it falls back to ``record.profile_sha256``. Pre-
    v0.5.1 records (no ``repo_root_specific_hashes``) keep the legacy
    "single hash per repo_id" semantics.
    """
    record = trust_state_for(repo_id)
    if record is None:
        return False  # no trust record → not a "material change" (just untrusted)
    expected = record.hash_for_root(current_profile_dir.parent)
    return expected != hash_profile(current_profile_dir)


def _current_user() -> str:
    """Best-effort current-user identification (for trust audit trail)."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")
