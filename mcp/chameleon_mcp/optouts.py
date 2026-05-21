"""Opt-out enforcement for the chameleon advisory hook.

Per docs/architecture.md "Opt-out hierarchy" (most-permanent → most-temporary):

  1. .chameleon/.skip                    per-repo, all users (committed)
  2. CHAMELEON_DISABLE=1                 per-user, globally (env var)
  3. .session_disabled.<session_id>      per-session (this Claude Code session)
  4. .pause_until                        timestamped, auto-expires

`is_chameleon_suppressed()` checks all four; preflight-and-advise calls it
before deciding whether to inject canonical context. Returns the FIRST
matching reason for diagnostic logging.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Polyfill for Python <3.11 (system python3 on macOS Command Line Tools
# is 3.9; Debian-stable ships 3.11+ only). The hook-side bash wrapper
# falls back to system python3 when no venv is bundled with the plugin,
# so this module MUST import cleanly on 3.9.
try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised on Py<3.11 only
    UTC = timezone.utc  # type: ignore[assignment]  # noqa: UP017

from chameleon_mcp.profile.trust import repo_data_dir


def _sign_marker(repo_id: str, session_id: str, disabled_at: float) -> str:
    """Compute the HMAC signature for a session-disable marker.

    Returns an empty string when the local HMAC key cannot be loaded
    (the caller then writes the marker unsigned and signature
    verification short-circuits to "valid" for back-compat).
    """
    import hmac as _hmac

    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key
    except Exception:
        return ""
    try:
        key = _ensure_hmac_key()
    except Exception:
        return ""
    msg = f"{repo_id}|{session_id}|{disabled_at}".encode()
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def _marker_has_valid_signature(
    marker: Path, repo_id: str, session_id: str
) -> bool:
    """Verify the HMAC signature on a session-disable marker.

    Returns True when the signature is present AND verifies, OR when
    no signature is present (back-compat for v0.5.13 markers and for
    systems that can't load the HMAC key). Returns False ONLY when
    a signature is present but invalid — i.e. a third-party process
    planted a marker without the key.
    """
    import hmac as _hmac

    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    sig_line = ""
    disabled_at_line = ""
    for line in text.splitlines():
        if line.startswith("sig="):
            sig_line = line[len("sig=") :].strip()
        elif line.startswith("disabled-at="):
            disabled_at_line = line[len("disabled-at=") :].strip()
    if not sig_line:
        # Unsigned marker: honor it (back-compat). Pre-v0.5.14
        # markers and any system whose HMAC key is unavailable land
        # here, so refusing them would break legitimate disables.
        return True
    try:
        disabled_at = float(disabled_at_line)
    except ValueError:
        return False
    expected = _sign_marker(repo_id, session_id, disabled_at)
    if not expected:
        # Key unavailable AT VERIFICATION TIME → fail-open (back-compat
        # with the original honor-the-marker behavior).
        return True
    return _hmac.compare_digest(sig_line, expected)


def _safe_session_marker(session_id: str | None) -> str:
    """Return a filesystem-safe identifier derived from session_id.

    Uses sha256 of utf-8 bytes, truncated to 16 hex chars. Stable across
    calls for the same session_id but contains no path-traversal chars.
    Returns 'unknown' for None / empty input.
    """
    if not session_id:
        return "unknown"
    raw = session_id.encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


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
        marker = repo_data_dir(repo_id) / f".session_disabled.{_safe_session_marker(session_id)}"
        if marker.is_file() and _marker_has_valid_signature(marker, repo_id, session_id):
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
    """Write the .session_disabled.<session_id> marker, HMAC-signed.

    v0.5.14 bug 8: the marker is HMAC-signed with the local HMAC key
    (the same key the exec_log uses) over `repo_id|session_id|disabled-at`.
    A third-party process that learns a session_id cannot forge a valid
    marker without the HMAC key, so `is_chameleon_suppressed` won't
    honor a planted marker.

    On a system where the HMAC key cannot be created (very unusual —
    only happens when /dev/urandom is unavailable AND no override path
    is writable) the marker is still written but without a signature.
    `_marker_has_valid_signature` treats unsigned markers as valid for
    back-compat with v0.5.13 and earlier — the security gate only
    rejects markers whose signature is PRESENT BUT WRONG.
    """
    marker = repo_data_dir(repo_id) / f".session_disabled.{_safe_session_marker(session_id)}"
    disabled_at = time.time()
    sig = _sign_marker(repo_id, session_id, disabled_at)
    sig_line = f"sig={sig}\n" if sig else ""
    marker.write_text(
        f"disabled-at={disabled_at}\nsession_id={session_id}\n{sig_line}",
        encoding="utf-8",
    )
    try:
        os.chmod(marker, 0o600)
    except OSError:
        pass
    return marker


def clear_session_disable(repo_id: str, session_id: str) -> bool:
    """Remove the marker. Returns True if it existed."""
    marker = repo_data_dir(repo_id) / f".session_disabled.{_safe_session_marker(session_id)}"
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
