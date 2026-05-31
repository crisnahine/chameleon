"""HMAC-signed exec log for the posttool-recorder hook.

Per docs/architecture.md "Hook stack" PostToolUse Bash + "Security mitigations" #5
(per-repo HMAC log directory, mode 0700, owner-checked).

Design notes:
- Writes AND reads use ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/.
- Only command_sha256 is stored, never the command body: the only consumer
  needs "was this session seen", and Bash command lines routinely carry secrets
  (Authorization headers, AWS keys, db URLs) that must not sit in plaintext.
- Fail-loud HMAC key: raises if /dev/urandom is unavailable (no silent
  unsigned mode).
- GC: logs older than RETENTION_DAYS days are purged opportunistically when a
  new session's log file is first created.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

_DEFAULT_HMAC_KEY_PATH = Path.home() / ".claude" / "hooks" / ".exec_hmac.key"


def _hmac_key_path() -> Path:
    """Resolve the HMAC key path, honoring CHAMELEON_HMAC_KEY_PATH override.

    The override exists for tests; production always uses the default
    path under the user's home directory.
    """
    override = os.environ.get("CHAMELEON_HMAC_KEY_PATH")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_HMAC_KEY_PATH


class HMACKeyError(Exception):
    """Raised when HMAC key generation or read fails. Fail-loud per Round 4 #15."""


def _ensure_hmac_key() -> bytes:
    """Load the per-user HMAC key, generating it on first use.

    Mode 0600 enforced. Raises HMACKeyError if:
    - the key file is owned by a different uid than the calling process,
    - /dev/urandom is unavailable (containerized env without /dev mount),
    - or another writer wins the create race and the resulting file is
      unreadable for any reason.
    """
    key_path = _hmac_key_path()
    if key_path.is_file():
        st = os.stat(key_path)
        if st.st_uid != os.geteuid():
            raise HMACKeyError(
                f"HMAC key {key_path} owned by uid {st.st_uid}, "
                f"expected {os.geteuid()}"
            )
        if st.st_mode & 0o077:
            os.chmod(key_path, 0o600)
        return key_path.read_bytes()

    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_path.parent, 0o700)
    except OSError:
        pass
    try:
        key = secrets.token_bytes(32)
    except Exception as e:
        raise HMACKeyError(f"failed to read /dev/urandom: {e}") from e

    tmp_path = key_path.with_suffix(".key.tmp")

    def _create_excl() -> int:
        return os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    try:
        fd = _create_excl()
    except FileExistsError:
        import time as _time
        for _ in range(20):
            _time.sleep(0.05)
            if key_path.is_file():
                return key_path.read_bytes()
        # The final key never appeared: the tmp is an orphan from a writer that
        # crashed between O_EXCL create and os.replace. Without reclaiming it,
        # key generation is bricked forever (and markers get written unsigned).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        try:
            fd = _create_excl()
        except FileExistsError:
            raise HMACKeyError(
                f"HMAC key tmp file {tmp_path} exists and final {key_path} "
                f"never appeared after retries"
            ) from None
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp_path, key_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if key_path.is_file():
            return key_path.read_bytes()
        raise
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


RETENTION_DAYS = 30


def _gc_old_logs(exec_dir: Path) -> None:
    """Best-effort purge of session logs older than RETENTION_DAYS.

    Called only when a new session's log file is first created, so the
    per-command append path rarely pays for a directory scan.
    """
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        entries = list(exec_dir.glob("*.jsonl"))
    except OSError:
        return
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


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
        "command_sha256": str (hex sha256 of the command; the body is never stored),
        "exit_code": int,
        "duration_ms": int | null,
        "hmac": "<hex sha256-hmac>"
      }
    """
    key = _ensure_hmac_key()
    from chameleon_mcp.optouts import _safe_session_marker
    log_path = _exec_log_dir(repo_id) / f"{_safe_session_marker(session_id)}.jsonl"
    new_session = not log_path.exists()

    # Store only the SHA-256 of the command, never the body: the consumer just
    # needs to know a session was seen, and command lines routinely carry secrets.
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()

    payload = {
        "ts": time.time(),
        "session_id": session_id,
        "command_sha256": command_sha256,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    payload["hmac"] = sig

    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)

    if new_session:
        _gc_old_logs(log_path.parent)


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


