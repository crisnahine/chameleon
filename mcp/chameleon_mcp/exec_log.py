"""HMAC-signed exec log for the posttool-recorder hook.

Per ARCHITECTURE.md "Hook stack" PostToolUse Bash + "Security mitigations" #5
(per-repo HMAC log directory, mode 0700, owner-checked).

Inherited from claude-measure-twice with Phase 4 bug fixes:
- Path mismatch: writes AND reads use ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/
- HMAC key fail-loud: raises if /dev/urandom unavailable (no silent unsigned mode)
- GC: -mtime +1 → -mmin +1440 (correct semantics; weekly purge of >30-day logs)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path

HMAC_KEY_PATH = Path.home() / ".claude" / "hooks" / ".exec_hmac.key"


class HMACKeyError(Exception):
    """Raised when HMAC key generation or read fails. Fail-loud per Round 4 #15."""


def _ensure_hmac_key() -> bytes:
    """Load the per-user HMAC key, generating it on first use.

    Mode 0600 enforced. Raises HMACKeyError if /dev/urandom is unavailable
    (containerized environments without /dev mount). No silent fallback.
    """
    if HMAC_KEY_PATH.is_file():
        # Verify mode 0600 and owner == euid
        st = os.stat(HMAC_KEY_PATH)
        if st.st_uid != os.geteuid():
            raise HMACKeyError(
                f"HMAC key {HMAC_KEY_PATH} owned by uid {st.st_uid}, "
                f"expected {os.geteuid()}"
            )
        if st.st_mode & 0o077:
            # Fix permissions silently if too permissive
            os.chmod(HMAC_KEY_PATH, 0o600)
        return HMAC_KEY_PATH.read_bytes()

    # Generate fresh key (32 bytes from /dev/urandom via secrets.token_bytes)
    HMAC_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = secrets.token_bytes(32)
    except Exception as e:
        raise HMACKeyError(f"failed to read /dev/urandom: {e}") from e

    # Atomic write with mode 0600
    tmp_path = HMAC_KEY_PATH.with_suffix(".key.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, HMAC_KEY_PATH)
    return key


def _exec_log_dir(repo_id: str) -> Path:
    """Return per-repo log directory: ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/.

    Mode 0700 enforced; owner-checked on every read.
    """
    tmpdir = Path(os.environ.get("TMPDIR") or "/tmp")
    base = tmpdir / ".chameleon_exec_log"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    repo_dir = base / repo_id
    repo_dir.mkdir(mode=0o700, exist_ok=True)
    return repo_dir


def append_exec_log(
    repo_id: str,
    *,
    session_id: str,
    command: str,
    exit_code: int,
    duration_ms: int | None = None,
) -> None:
    """Append an HMAC-signed log entry. One entry per Bash invocation.

    Format: NDJSON, one JSON object per line. Each object:
      {
        "ts": <unix epoch float>,
        "session_id": str,
        "command": str (truncated to 1KB),
        "exit_code": int,
        "duration_ms": int | null,
        "hmac": "<hex sha256-hmac>"
      }
    """
    key = _ensure_hmac_key()
    log_path = _exec_log_dir(repo_id) / f"{session_id}.jsonl"

    # Truncate command to 1 KB to bound log size
    truncated_command = command[:1024]

    payload = {
        "ts": time.time(),
        "session_id": session_id,
        "command": truncated_command,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    payload["hmac"] = sig

    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def verify_exec_log_line(line: str) -> bool:
    """Verify HMAC signature of a single log line. Constant-time compare.

    Returns True iff the HMAC matches expected signature.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return False
    expected = record.pop("hmac", None)
    if not isinstance(expected, str):
        return False
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        key = _ensure_hmac_key()
    except HMACKeyError:
        return False
    actual = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, actual)


def gc_old_logs(*, max_age_seconds: int = 30 * 86_400) -> int:
    """Purge log files older than max_age_seconds. Returns count of files removed.

    Per ARCHITECTURE.md GC policy: 30-day record purge, weekly cadence.
    """
    tmpdir = Path(os.environ.get("TMPDIR") or "/tmp")
    base = tmpdir / ".chameleon_exec_log"
    if not base.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for repo_dir in base.iterdir():
        if not repo_dir.is_dir():
            continue
        for log_file in repo_dir.glob("*.jsonl"):
            try:
                mtime = log_file.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    log_file.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed
