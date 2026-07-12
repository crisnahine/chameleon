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

Check-event sidecar (``<session>.checks.jsonl``, same directory): a second
per-session NDJSON recording which turn-end checks ran, were skipped, or
degraded; the Stop-path session attestation aggregates it. ``check`` is one of
``correctness_judge``, ``duplication_review``, ``idiom_review``,
``stop_relint``, ``posttool_verify``. ``status`` baseline is ``ran`` /
``skipped`` / ``degraded``; richer per-check statuses are stored verbatim (the
vocabulary is an open set). ``reason`` values defined so far: ``spawn_timeout``,
``spawn_error``, ``spawn_nonzero_exit`` (degraded judge spawns, written by the
judge path), ``in_flight_at_stop`` (an async spawn unfinished at Stop, recorded
as a SKIPPED check), ``cooldown``, ``verify_env_off``, ``enforce_env_off``,
``mode_off``, ``feature_disabled``, ``marker_exists``, ``cap_reached``,
``corr_judge_active``, ``digest_already_judged``, ``suppressed``,
``no_governed_files`` (the idiom review skipped a turn whose edits carry no
recognized source language, without burning its once-per-session marker).
Unknown reasons are stored verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from chameleon_mcp.plugin_paths import secure_chmod

_DEFAULT_HMAC_KEY_PATH = Path.home() / ".claude" / "hooks" / ".exec_hmac.key"


# Broad allow-list of test-runner shapes, matched against the FIRST command word
# of a segment (after wrapper/path/env stripping). The recorder classifies the
# command BEFORE hashing, so only the resulting boolean is persisted and the
# command body (which may carry secrets) is never stored. The list is
# deliberately wide across ecosystems because the only consumer is an advisory
# turn-end nudge: a missed runner costs a missed nudge, never a false block.
#
# Anchored at the start of the stripped segment (^) so `pip install pytest` and
# `cat test.py` do not match: the runner has to BE the command, not an argument.
_TEST_RUNNER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Node test runners, invoked directly or via a binary shim path.
    re.compile(r"^(?:\S*/)?(?:jest|vitest|mocha|ava|tap|jasmine)\b"),
    # Python.
    re.compile(r"^(?:\S*/)?pytest\b"),
    re.compile(r"^(?:\S*/)?python[0-9.]*\s+-m\s+(?:pytest|unittest)\b"),
    re.compile(r"^(?:\S*/)?(?:tox|nox)\b"),
    # Ruby.
    re.compile(r"^(?:\S*/)?rspec\b"),
    re.compile(r"^(?:\S*/)?rails\s+test\b"),
    re.compile(r"^(?:\S*/)?rake\s+(?:test|spec)\b"),
    # `-Itest` needs the explicit whitespace lookbehind: `\b` never matches
    # between a space and `-` (both non-word), so `\b-Itest` is unreachable in
    # the standard `ruby -Itest ...` invocation.
    re.compile(r"^(?:\S*/)?ruby\b.*(?:(?<=\s)-Itest\b|\bminitest\b)"),
    # The minitest CLI invoked directly (or via `bundle exec minitest`, which the
    # wrapper peel above reduces to a bare `minitest`): the `ruby ...` arm only
    # fires when `ruby` leads the segment, so a standalone runner needs its own
    # anchored pattern.
    re.compile(r"^(?:\S*/)?minitest\b"),
    # Go / Rust / Elixir.
    re.compile(r"^(?:\S*/)?go\s+test\b"),
    re.compile(r"^(?:\S*/)?cargo\s+(?:test|nextest)\b"),
    re.compile(r"^(?:\S*/)?mix\s+test\b"),
    # Make / monorepo task runners.
    re.compile(r"^(?:\S*/)?make\s+\S*test\b"),
    re.compile(r"^(?:\S*/)?(?:nx|turbo)\b.*\btest\b"),
    re.compile(r"^(?:\S*/)?bazel\s+test\b"),
    # Package-manager script wrappers: `pnpm test`, `npm run test:unit`,
    # `yarn test`, `npm t`, `bun test`. The script token must look like a test
    # script so `npm install` does not match.
    re.compile(r"^(?:\S*/)?(?:pnpm|yarn|bun)\s+(?:run\s+)?\S*test\S*\b"),
    re.compile(r"^(?:\S*/)?npm\s+(?:run\s+)?\S*test\S*\b"),
    re.compile(r"^(?:\S*/)?npm\s+t\b"),
)

# Leading shell scaffolding stripped off a segment before runner matching:
# `FOO=bar` env assignments, and wrapper invokers that delegate to the real
# runner (`npx jest`, `bundle exec rspec`, `pdm run pytest`, `time make test`).
_LEADING_ENV_ASSIGN_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_WRAPPER_PREFIX_RE = re.compile(
    r"^(?:"
    r"npx\s+(?:--yes\s+|-y\s+)?"  # npx jest
    r"|pnpm\s+(?:exec|dlx)\s+"  # pnpm exec vitest
    r"|yarn\s+(?:exec|dlx)\s+"
    r"|bunx\s+"
    r"|bundle\s+exec\s+"  # bundle exec rspec
    r"|poetry\s+run\s+"  # poetry run pytest
    r"|pdm\s+run\s+"
    r"|hatch\s+run\s+"
    r"|uv\s+run\s+"
    r"|time\s+"  # time make test
    r"|env\s+"  # env pytest (bare env, not env FOO=bar handled above)
    r")"
)


def classify_test_command(command: str) -> bool:
    """True if ``command`` looks like it runs a test suite.

    Privacy-preserving: this runs on the raw command but returns only a boolean;
    the caller persists the bit, never the body. Splits on shell separators so a
    runner chained after a build step (``yarn build && yarn test``) still counts,
    strips leading ``FOO=bar`` env assignments and common wrapper invokers
    (``npx``, ``bundle exec``, ``poetry run``, ``time``), then requires a runner
    at the START of the remaining segment so an argument like ``pip install
    pytest`` does not match. Best-effort and intentionally broad: a false
    negative costs a missed advisory nudge, never a wrong block.
    """
    if not command:
        return False
    # Heuristic split on the common segment separators, not a shell parse:
    # quoted separators are rare in test invocations and a missed split only
    # loses a nudge.
    segments = re.split(r"&&|\|\||[;|&\n]", command)
    for seg in segments:
        seg = _LEADING_ENV_ASSIGN_RE.sub("", seg).strip()
        # Peel wrapper invokers repeatedly (e.g. `time bundle exec rspec`).
        for _ in range(4):
            stripped = _WRAPPER_PREFIX_RE.sub("", seg, count=1)
            if stripped == seg:
                break
            seg = stripped.lstrip()
        if not seg:
            continue
        for pat in _TEST_RUNNER_PATTERNS:
            if pat.search(seg):
                return True
    return False


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
        # POSIX-only: uid-based ownership verification. Windows has no
        # os.geteuid / st_uid concept, so skip the owner check there rather than
        # crashing with AttributeError; file ACLs are the platform's mechanism.
        if hasattr(os, "geteuid"):
            euid = os.geteuid()
            if getattr(st, "st_uid", euid) != euid:
                raise HMACKeyError(f"HMAC key {key_path} owned by uid {st.st_uid}, expected {euid}")
        if st.st_mode & 0o077:
            secure_chmod(key_path, 0o600)
        return key_path.read_bytes()

    key_path.parent.mkdir(parents=True, exist_ok=True)
    secure_chmod(key_path.parent, 0o700)
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


class ExecLogUnsafeError(OSError):
    """Raised when an exec-log directory is a symlink.

    Subclasses OSError so append_exec_log's existing fail-open handlers swallow
    it: a planted symlink means skip logging, never crash the recorder hook.
    """


def _mkdir_checked(path: Path, *, parents: bool) -> None:
    """``mkdir(0o700)`` that refuses a pre-existing symlink or foreign-owned dir.

    ``mkdir(exist_ok=True)`` silently succeeds when ``path`` is already a symlink,
    and a later ``open(..., "a")`` would then FOLLOW it. On a shared ``TMPDIR`` a
    local attacker can pre-plant ``<base>/<repo_id>`` (repo_id is derivable from a
    public clone URL) as a symlink into a directory they control, diverting the
    victim's command log. An ``lstat`` check before the mkdir refuses that,
    mirroring safe_open's symlink discipline.

    ``exist_ok=True`` is also a no-op when the path already exists as a real
    directory owned by another uid: a planted real dir is then written into and
    never re-permed. After the mkdir, an owner check refuses a directory whose
    ``st_uid`` is not the calling euid, so an attacker-owned dir cannot capture
    the victim's command log. POSIX-only (Windows has no ``st_uid`` ownership
    model and relies on directory ACLs), matching the HMAC key file's check.
    """
    if path.is_symlink():
        raise ExecLogUnsafeError(f"refusing symlinked exec-log path: {path}")
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    if hasattr(os, "geteuid"):
        euid = os.geteuid()
        try:
            st = os.stat(path)
        except FileNotFoundError:
            # The dir does not exist after mkdir (mkdir was a no-op or could not
            # create it): there is no foreign-owned directory to capture the log,
            # so there is nothing to refuse. A later open() on the missing path
            # fails and the caller fails open.
            return
        except OSError as e:
            raise ExecLogUnsafeError(f"cannot stat exec-log path {path}: {e}") from e
        if getattr(st, "st_uid", euid) != euid:
            raise ExecLogUnsafeError(
                f"refusing exec-log dir {path} owned by uid {st.st_uid}, expected {euid}"
            )


def _exec_log_dir(repo_id: str) -> Path:
    """Return per-repo log directory: ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/.

    Mode 0700 enforced; symlink-refused; owner-checked on every read.
    """
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    base = tmpdir / ".chameleon_exec_log"
    _mkdir_checked(base, parents=True)
    repo_dir = base / repo_id
    _mkdir_checked(repo_dir, parents=False)
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


def _append_line_nofollow(log_path: Path, line: str) -> bool:
    """Append ``line`` to ``log_path`` without following a symlinked leaf.

    O_NOFOLLOW makes the no-symlink open atomic on POSIX; on platforms without it
    (Windows, where ``getattr`` yields 0) an ``is_symlink`` pre-check refuses an
    existing planted leaf symlink, leaving only a negligible TOCTOU window that
    the per-user 0o700 parent dir already closes. 0o600 keeps the log private; if
    ``os.fdopen`` raises, the fd is closed rather than leaked. Returns True on a
    successful write, False (fail open) on any rejection or open/write error so a
    logging failure never crashes the calling hook.
    """
    if log_path.is_symlink():
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(log_path, flags, 0o600)
    except OSError:
        return False
    try:
        f = os.fdopen(fd, "a", encoding="utf-8")
    except OSError:
        os.close(fd)  # fdopen never took ownership of the fd
        return False
    try:
        with f:
            f.write(line)
    except OSError:
        return False
    return True


def _open_for_read_nofollow(log_path: Path):
    """Open ``log_path`` for reading without following a symlinked leaf.

    The read symmetry of ``_append_line_nofollow``: the write path refuses a
    planted leaf symlink, so the read paths must too, or an attacker who controls
    the (shared-TMPDIR) log dir could plant ``<session>.jsonl`` as a symlink into
    a file they want the victim's Stop path to read. O_NOFOLLOW makes the refusal
    atomic on POSIX; an ``is_symlink`` pre-check covers platforms without it
    (Windows). Returns an open text file object, or ``None`` when the leaf is a
    symlink, is absent, or cannot be opened, so callers fail open to "no log".
    """
    if log_path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(log_path, flags)
    except OSError:
        return None
    try:
        return os.fdopen(fd, "r", encoding="utf-8")
    except OSError:
        os.close(fd)  # fdopen never took ownership of the fd
        return None


def append_exec_log(
    repo_id: str,
    *,
    session_id: str,
    command: str,
    exit_code: int,
    duration_ms: int | None = None,
    test_command_seen: bool | None = None,
) -> None:
    """Append an HMAC-signed log entry. One entry per Bash invocation.

    Format: NDJSON, one JSON object per line. Each object:
      {
        "ts": <unix epoch float>,
        "session_id": str,
        "command_sha256": str (hex sha256 of the command; the body is never stored),
        "exit_code": int,
        "duration_ms": int | null,
        "test_command_seen": bool,
        "hmac": "<hex sha256-hmac>"
      }

    ``test_command_seen`` is the privacy-preserving test-runner classification of
    the command, computed by the caller from the raw command before it is
    discarded. When omitted it is derived here so any call site benefits; passing
    it explicitly lets the recorder classify once and reuse the result.
    """
    try:
        key = _ensure_hmac_key()
    except HMACKeyError:
        # No signing key (foreign-owned key file, /dev/urandom unavailable):
        # an unsigned entry could be forged, so skip logging rather than emit
        # one. HMACKeyError is not an OSError, so it must be caught here to keep
        # the fail-open contract self-contained instead of leaning on the caller.
        return
    from chameleon_mcp.optouts import _safe_session_marker

    try:
        log_dir = _exec_log_dir(repo_id)
    except OSError:
        # Symlinked, foreign-owned, or unwritable log dir: skip logging, never
        # crash the hook.
        return
    log_path = log_dir / f"{_safe_session_marker(session_id)}.jsonl"
    new_session = not log_path.exists()

    # Store only the SHA-256 of the command, never the body: the consumer just
    # needs to know a session was seen, and command lines routinely carry secrets.
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()

    if test_command_seen is None:
        test_command_seen = classify_test_command(command)

    payload = {
        "ts": time.time(),
        "session_id": session_id,
        "command_sha256": command_sha256,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "test_command_seen": bool(test_command_seen),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    payload["hmac"] = sig

    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if _append_line_nofollow(log_path, line) and new_session:
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


def append_check_event(
    repo_id: str,
    *,
    session_id: str,
    check: str,
    status: str,
    reason: str | None = None,
    file_rel: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one HMAC-signed check event to the session's checks sidecar.

    One line per turn-end check outcome. The field names are a cross-module
    contract (the judge paths write degraded/in-flight events into the same
    file the attestation reads): ``ts``, ``session_id``, ``check``, ``status``,
    ``reason``, ``file_rel``, ``detail``, ``hmac``. Signing mirrors
    ``append_exec_log``; when the HMAC key is unavailable the record is written
    with ``"hmac": null`` rather than dropped, so the reader can flag it as
    unverified instead of losing the event. Swallows every exception: this is
    called from hook paths, where a logging failure must never change the hook
    outcome.
    """
    try:
        payload: dict = {
            "ts": time.time(),
            "session_id": session_id,
            "check": check,
            "status": status,
            "reason": reason,
            "file_rel": file_rel,
            "detail": detail if isinstance(detail, dict) else None,
        }
        try:
            key = _ensure_hmac_key()
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            payload["hmac"] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        except HMACKeyError:
            payload["hmac"] = None

        from chameleon_mcp.optouts import _safe_session_marker

        log_path = _exec_log_dir(repo_id) / f"{_safe_session_marker(session_id)}.checks.jsonl"
        new_file = not log_path.exists()
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        # Symlink-safe append, matching append_exec_log: refuse a planted
        # checks.jsonl symlink rather than follow it.
        if _append_line_nofollow(log_path, line) and new_file:
            _gc_old_logs(log_path.parent)
    except Exception:
        return


def read_check_events(repo_id: str, session_id: str, *, limit: int) -> dict:
    """Read the newest ``limit`` check events for a session, HMAC-verified.

    Returns ``{"events": [<verified records>], "unverified": <int>}``. A line
    that fails verification (tampered, or written with a null hmac because no
    key was available) is excluded from ``events`` and counted in
    ``unverified``; corrupt (non-JSON) lines are skipped non-fatally. Fail-open
    to the empty shape on any error: a missing sidecar reads as "no checks
    observed", which the attestation's consumers treat as scrutiny-raising,
    never as clean.
    """
    empty = {"events": [], "unverified": 0}
    try:
        from chameleon_mcp.optouts import _safe_session_marker

        log_path = _exec_log_dir(repo_id) / f"{_safe_session_marker(session_id)}.checks.jsonl"
        f = _open_for_read_nofollow(log_path)
        if f is None:
            return empty
        with f:
            raw_lines = [ln.strip() for ln in f if ln.strip()]
        events: list[dict] = []
        unverified = 0
        for line in raw_lines[-max(0, int(limit)) :]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if verify_exec_log_line(line):
                events.append(record)
            else:
                unverified += 1
        return {"events": events, "unverified": unverified}
    except Exception:
        return {"events": [], "unverified": 0}


def session_test_run_seen(repo_id: str, session_id: str) -> bool:
    """True if a passing test run was observed in this session's exec log.

    Reads the session's HMAC-signed NDJSON log and returns True iff at least one
    entry both classified as a test runner and exited 0. Only HMAC-verified lines
    count, so a tampered or corrupt line cannot fake a "tests passed" signal.

    This is a turn-end-only read (the Stop gate), deliberately off the per-Bash
    hot path. Fails open to False on any error: an unreadable log degrades to
    "no test seen", which only strengthens an advisory nudge, never blocks.
    """
    try:
        from chameleon_mcp.optouts import _safe_session_marker

        log_path = _exec_log_dir(repo_id) / f"{_safe_session_marker(session_id)}.jsonl"
        f = _open_for_read_nofollow(log_path)
        if f is None:
            return False
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not record.get("test_command_seen"):
                    continue
                if record.get("exit_code") != 0:
                    continue
                if verify_exec_log_line(line):
                    return True
        return False
    except Exception:
        return False
