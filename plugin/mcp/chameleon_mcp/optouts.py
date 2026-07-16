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
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _marker_has_valid_signature(marker: Path, repo_id: str, session_id: str) -> bool:
    """Verify the HMAC signature on a session-disable marker.

    Defense against an attacker who knows another user's session_id and
    writes a marker file directly (bypassing /chameleon-disable):

    1. If the local HMAC key IS available, the marker MUST carry a
       valid `sig=` line. No sig = REJECT (closes the downgrade attack
       where an attacker writes an unsigned marker and the back-compat
       path honors it).
    2. If the local HMAC key is NOT available (very unusual — only
       happens when /dev/urandom is unreachable AND no override path
       is writable, which is itself a major system compromise), we
       fail-open and honor the marker. Without the key the system
       can't verify ANY marker, including legitimate ones, so refusing
       them would break the disable flow on systems without an HMAC
       key. The attacker scenario here already requires the attacker
       to have caused the key to be unavailable.
    """
    import hmac as _hmac

    try:
        text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Undecodable bytes are a planted marker the reject path must reject,
        # not crash on: is_chameleon_suppressed's disable branch has no local
        # guard, so an escaping UnicodeDecodeError would ride to the hook's
        # fail-open and skip that root's gates with no valid signature.
        return False
    sig_line = ""
    disabled_at_line = ""
    for line in text.splitlines():
        if line.startswith("sig="):
            sig_line = line[len("sig=") :].strip()
        elif line.startswith("disabled-at="):
            disabled_at_line = line[len("disabled-at=") :].strip()

    try:
        disabled_at = float(disabled_at_line)
    except ValueError:
        return False
    expected = _sign_marker(repo_id, session_id, disabled_at)
    if not expected:
        return True
    if not sig_line:
        return False
    # Compare as bytes: compare_digest raises TypeError on a non-ASCII str,
    # which a planted marker's sig= line controls — the reject path must not
    # be crashable by the very input it exists to reject.
    return _hmac.compare_digest(sig_line.encode("utf-8"), expected.encode("utf-8"))


def _sign_pause(repo_id: str, expiry_iso: str) -> str:
    """Compute the HMAC signature for a `.pause_until` marker.

    Same key and posture as `_sign_marker`: empty string when the local
    HMAC key cannot be loaded (the marker is then written unsigned and
    verification short-circuits to "valid").
    """
    import hmac as _hmac

    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key

        key = _ensure_hmac_key()
    except Exception:
        return ""
    msg = f"pause|{repo_id}|{expiry_iso}".encode()
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def _pause_has_valid_signature(repo_id: str, expiry_iso: str, sig_line: str) -> bool:
    """Verify the HMAC signature on a `.pause_until` marker.

    Same threat model and policy as `_marker_has_valid_signature`: a
    third-party process with write access to the data dir must not be able
    to plant a pause that silently suppresses every advisory. With the HMAC
    key available a marker MUST carry a valid `sig=` line (a bare timestamp
    is rejected); without the key nothing can be verified, so the marker is
    honored — a pause is a bounded, low-privilege state and the no-key case
    already implies a bigger compromise.
    """
    import hmac as _hmac

    expected = _sign_pause(repo_id, expiry_iso)
    if not expected:
        return True
    if not sig_line:
        return False
    # Bytes compare — see _marker_has_valid_signature: a non-ASCII str makes
    # compare_digest raise instead of reject.
    return _hmac.compare_digest(sig_line.encode("utf-8"), expected.encode("utf-8"))


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
                lines = pause_path.read_text(encoding="utf-8").splitlines()
                expiry_iso = lines[0].strip() if lines else ""
                sig_line = ""
                for line in lines[1:]:
                    if line.startswith("sig="):
                        sig_line = line[len("sig=") :].strip()
                expiry = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
                if expiry.timestamp() > time.time():
                    # Same planted-marker defense as the session-disable
                    # marker: an unverifiable pause is ignored, not honored.
                    if _pause_has_valid_signature(repo_id, expiry_iso, sig_line):
                        return "pause"
                else:
                    try:
                        pause_path.unlink()
                    except OSError:
                        pass
            except (ValueError, OSError, UnicodeDecodeError):
                pass

    return None


def write_session_disable(repo_id: str, session_id: str) -> Path:
    """Write the .session_disabled.<session_id> marker, HMAC-signed.

    Bug 8: the marker is HMAC-signed with the local HMAC key
    (the same key the exec_log uses) over `repo_id|session_id|disabled-at`.
    A third-party process that learns a session_id cannot forge a valid
    marker without the HMAC key, so `is_chameleon_suppressed` won't
    honor a planted marker.

    On a system where the HMAC key cannot be created (very unusual —
    only happens when /dev/urandom is unavailable AND no override path
    is writable) the marker is still written but without a signature.
    `_marker_has_valid_signature` then honors it only because the key is
    equally unavailable at verify time (nothing can be verified, so
    refusing would break the disable flow entirely). When the key IS
    available, an unsigned marker is REJECTED — see that function's
    docstring for the downgrade-attack rationale.
    """
    marker = repo_data_dir(repo_id) / f".session_disabled.{_safe_session_marker(session_id)}"
    disabled_at = time.time()
    sig = _sign_marker(repo_id, session_id, disabled_at)
    sig_line = f"sig={sig}\n" if sig else ""
    content = f"disabled-at={disabled_at}\nsession_id={session_id}\n{sig_line}"
    tmp = marker.parent / (marker.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(marker))
    return marker


def write_pause(repo_id: str, minutes: int = 15) -> str:
    """Write a .pause_until file with expiry = now + minutes. Returns ISO timestamp.

    The expiry is rounded UP to the next whole second before formatting: the
    ISO format is second-precision, and flooring the fractional part would
    make the honored pause window always a hair shorter than requested.
    Ceiling ensures the caller-requested duration is never under-delivered.

    Line 1 is the bare ISO timestamp (the statusline's renderer and other
    display readers parse only that line); a `sig=` line follows so
    `is_chameleon_suppressed` can reject a marker planted directly on disk,
    mirroring the session-disable marker's HMAC defense.
    """
    expiry = datetime.now(UTC).timestamp() + minutes * 60
    expiry_iso = datetime.fromtimestamp(math.ceil(expiry), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = expiry_iso
    sig = _sign_pause(repo_id, expiry_iso)
    if sig:
        content += f"\nsig={sig}"
    pause_path = repo_data_dir(repo_id) / ".pause_until"
    tmp = pause_path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(pause_path))
    return expiry_iso


def reap_stale_session_markers(repo_id: str, max_age_seconds: int = 604_800) -> int:
    """Best-effort removal of stale ``.session_disabled.<sid>`` markers.

    These per-session opt-out markers have no SessionEnd cleanup path, so they
    accumulate (~120 bytes each). A marker older than ``max_age_seconds``
    (default 7 days, far beyond any Claude Code session) is from a dead session;
    because it is keyed by ``sha256(session_id)[:16]`` it could only ever match
    its own session, so removing it is safe. Returns the count removed; never
    raises (called best-effort from SessionStart).
    """
    try:
        data_dir = repo_data_dir(repo_id)
        markers = list(data_dir.glob(".session_disabled.*"))
    except Exception:  # noqa: BLE001 - best-effort housekeeping
        return 0
    now = time.time()
    removed = 0
    for marker in markers:
        if marker.name.endswith(".tmp"):
            continue
        try:
            if now - marker.stat().st_mtime <= max_age_seconds:
                continue
            marker.unlink()
            removed += 1
        except OSError:
            continue
    return removed
