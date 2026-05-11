"""Opt-out enforcement for the chameleon advisory hook.

Per ARCHITECTURE.md "Opt-out hierarchy" (most-permanent → most-temporary):

  1. .chameleon/.skip                    per-repo, all users (committed)
  2. CHAMELEON_DISABLE=1                 per-user, globally (env var)
  3. .session_disabled.<session_id>      per-session (this Claude Code session)
  4. .pause_until                        timestamped, auto-expires

`is_chameleon_suppressed()` checks all four; preflight-and-advise calls it
before deciding whether to inject canonical context. Returns the FIRST
matching reason for diagnostic logging.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from chameleon_mcp.profile.trust import repo_data_dir


def is_chameleon_suppressed(
    repo_root: Path | None,
    repo_id: str | None,
    session_id: str | None = None,
) -> str | None:
    """Return reason string if chameleon is suppressed, else None.

    Reasons:
      - "repo_skip" — .chameleon/.skip file present in repo
      - "user_disable" — CHAMELEON_DISABLE=1 in env
      - "session_disable" — .session_disabled.<session_id> marker exists
      - "pause" — .pause_until file with future timestamp
    """
    if repo_root is not None and (repo_root / ".chameleon" / ".skip").is_file():
        return "repo_skip"

    if os.environ.get("CHAMELEON_DISABLE") == "1":
        return "user_disable"

    if repo_id and session_id:
        marker = repo_data_dir(repo_id) / f".session_disabled.{session_id}"
        if marker.is_file():
            return "session_disable"

    if repo_id:
        pause_path = repo_data_dir(repo_id) / ".pause_until"
        if pause_path.is_file():
            try:
                expiry_iso = pause_path.read_text(encoding="utf-8").strip()
                expiry = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
                if expiry.timestamp() > time.time():
                    return "pause"
                # Expired — clean up so future calls don't re-read
                try:
                    pause_path.unlink()
                except OSError:
                    pass
            except (ValueError, OSError):
                # Malformed pause file → treat as not paused, but don't crash
                pass

    return None


def write_session_disable(repo_id: str, session_id: str) -> Path:
    """Write the .session_disabled.<session_id> marker. Returns the path."""
    marker = repo_data_dir(repo_id) / f".session_disabled.{session_id}"
    marker.write_text(
        f"disabled-at={time.time()}\nsession_id={session_id}\n",
        encoding="utf-8",
    )
    return marker


def clear_session_disable(repo_id: str, session_id: str) -> bool:
    """Remove the marker. Returns True if it existed."""
    marker = repo_data_dir(repo_id) / f".session_disabled.{session_id}"
    if marker.is_file():
        marker.unlink()
        return True
    return False


def write_pause(repo_id: str, minutes: int = 15) -> str:
    """Write a .pause_until file with expiry = now + minutes. Returns ISO timestamp."""
    expiry = datetime.now(UTC).timestamp() + minutes * 60
    expiry_iso = datetime.fromtimestamp(expiry, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    pause_path = repo_data_dir(repo_id) / ".pause_until"
    pause_path.write_text(expiry_iso, encoding="utf-8")
    return expiry_iso


def clear_pause(repo_id: str) -> bool:
    """Remove the .pause_until file. Returns True if it existed."""
    pause_path = repo_data_dir(repo_id) / ".pause_until"
    if pause_path.is_file():
        pause_path.unlink()
        return True
    return False
