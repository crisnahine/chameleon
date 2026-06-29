"""Repo identity derivation.

How a repository's stable ``repo_id`` is computed: git-remote URL canonicalization,
the no-remote ``repo_uuid`` fallback, the case-insensitive-filesystem probe, and
the path-hash final fallback, plus the short-lived per-process cache. This is the
self-contained, stdlib-only core of repo identity; ``tools`` re-exports every
name here for backward compatibility, so existing imports and test patches that
reference ``chameleon_mcp.tools._compute_repo_id`` keep working.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

# Short-lived per-process cache of resolved-path -> repo_id, so a burst of hook
# calls in one session does not re-shell git on every edit.
_REPO_ID_CACHE: dict[str, tuple[float, str]] = {}
_REPO_ID_CACHE_TTL = 300

_CASE_INSENSITIVE_HOSTS: frozenset[str] = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com"}
)

_SSH_URL_RE = re.compile(r"^(?:[\w-]+@)?([^:]+):(.+?)(?:\.git)?/?$")


def _strip_port(host: str) -> str:
    """Drop a trailing ``:<port>`` from a URL host, IPv6-safe.

    A bracketed IPv6 literal (``[::1]`` or ``[::1]:22``) keeps the bracketed
    address and drops only a port after ``]``. A plain host drops a trailing
    ``:<digits>`` only, so a hostname that happens to contain a colon is never
    truncated.
    """
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return re.sub(r":\d+$", "", host)


def _normalize_git_url(url: str) -> str:
    """Canonicalize a git remote URL for repo_id derivation.

    The goal is that two checkouts of the same repo — regardless of whether
    the remote was cloned over https or ssh, with or without a trailing
    .git, and with or without case-variation on the host — collapse to the
    same canonical string.

    Transforms applied (in order):
    1. Strip surrounding whitespace.
    2. Rewrite scp/ssh syntax `git@host:owner/repo` → `ssh://git@host/owner/repo`.
    3. Strip a trailing `.git` from the path.
    4. Strip a trailing slash from the path.
    5. Force scheme to `https://` for the well-known hosting providers — both
       `https://github.com/...` and `ssh://git@github.com/...` resolve to the
       same repository.
    6. Strip a `:<port>` from the host before matching (IPv6-safe), and drop it
       from the canonical for the well-known hosts (their port is always the
       default). Unknown self-hosted hosts are left as-is.
    7. Lowercase the host and the owner/repo path for the well-known
       case-insensitive hosts (GitHub, GitLab, Bitbucket, Azure DevOps), which
       resolve owner/repo case-insensitively.

    Returns the canonical URL string. Non-URL input is returned stripped so
    we never crash — the caller still hashes whatever we return, which keeps
    the function total.
    """
    s = (url or "").strip()
    if not s:
        return s

    m = _SSH_URL_RE.match(s)
    if m and "://" not in s:
        host, path = m.group(1), m.group(2)
        s = f"ssh://git@{host}/{path}"

    s = re.sub(r"\.git/?$", "", s)
    s = s.rstrip("/")

    proto_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*)://([^/]+)(/.*)?$", s)
    if not proto_match:
        return s
    scheme, host, path = proto_match.group(1), proto_match.group(2), proto_match.group(3) or ""

    if "@" in host:
        host = host.split("@", 1)[1]

    # The host capture may carry a port (``host:22``); strip it (IPv6-safe)
    # before matching the case-insensitive set, so a ported clone of a
    # well-known host still normalizes to the same id. (Finding 8.)
    host_match = _strip_port(host)
    host_l = host_match.lower()
    if host_l in _CASE_INSENSITIVE_HOSTS:
        # Well-known hosts: force https so ssh / https clones collapse, drop the
        # (always-default) port, and case-fold (they resolve owner/repo
        # case-insensitively).
        host = host_l
        scheme = "https"
        path = path.lower()
    # Unknown self-hosted hosts are left as-is: scheme, host (any explicit port),
    # and path case are all preserved.

    return f"{scheme}://{host}{path}"


def _git_remote_url(repo_root: Path) -> str | None:
    """Return the `origin` remote URL, or None if not a git repo / no remote.

    Bounded by a 2 second timeout. If git takes longer than that to answer a
    config lookup something is wrong with the workspace, and the path-based
    fallback is the safer choice than blocking bootstrap. A timeout is the one
    failure mode worth surfacing: it silently degrades repo identity to the
    working-tree path, so it gets a one-line stderr warning. A missing git or a
    repo with no remote is normal and stays quiet.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        try:
            import sys as _sys

            print(
                "chameleon: git remote URL lookup timed out after 2s "
                f"(repo={str(repo_root)!r}); using path-based repo identity",
                file=_sys.stderr,
            )
        except Exception:  # noqa: BLE001
            pass
        return None
    except OSError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _persisted_repo_uuid(repo_root: Path) -> str | None:
    """Read ``.chameleon/config.json``'s ``repo_uuid`` if present.

    A non-empty string repo_uuid pins a no-remote repo's identity to a value
    that travels with the committed profile, so moving or renaming the working
    tree on disk does not orphan the trust grant. Fail-open: any read/parse
    error returns None and the caller falls back to the path hash. Read with a
    raw json.loads (not the strict config loader) so a config that is malformed
    for some unrelated feature still yields a usable uuid here.
    """
    try:
        raw = (repo_root / ".chameleon" / "config.json").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A non-UTF8 config.json (binary/corrupt bytes at the path) must fail open
        # to the path-hash identity, not raise. UnicodeDecodeError is a ValueError
        # subclass, not an OSError, so a bare OSError guard let it escape and crash
        # detect_repo and the hot-path get_pattern_context on a no-remote repo.
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    uuid = data.get("repo_uuid")
    if isinstance(uuid, str) and uuid.strip():
        return uuid.strip()
    return None


def _fs_is_case_insensitive(path: Path) -> bool:
    """True when ``path``'s filesystem matches names case-insensitively.

    Probes the real filesystem: the repo dir reached through a case-swapped
    variant of its own name must point at the same inode+device. Defaults to
    False (case-sensitive) on any ambiguity, so two genuinely distinct repos on
    a case-sensitive filesystem (e.g. ``/srv/Foo`` and ``/srv/foo`` on Linux)
    never collapse to one id. macOS/Windows default volumes probe True.
    """
    try:
        name = path.name
        swapped_name = name.swapcase()
        if swapped_name == name:
            return False  # no letters to swap, cannot tell; assume case-sensitive
        st = path.stat()
        sst = path.with_name(swapped_name).stat()
        return st.st_ino == sst.st_ino and st.st_dev == sst.st_dev
    except OSError:
        return False


def _compute_repo_id(repo_root: Path) -> str:
    """Canonical repo_id.

    Identity precedence (most stable first):
      1. git remote URL, stable across moved/renamed checkouts and machines.
      2. a persisted ``repo_uuid`` in ``.chameleon/config.json``, for repos
         without a remote (fresh `git init`, vendored snapshots, archive
         extracts), this travels with the committed profile so a moved working
         tree keeps its identity.
      3. the resolved absolute path, the final fallback.

    The path fallback lower-cases the resolved path ONLY when the repo lives on a
    case-insensitive filesystem (default macOS/Windows), so a repo reached through
    a path that differs only in letter case maps to one id there, while two
    distinct repos on a case-sensitive filesystem (Linux) keep separate ids. The
    pre-fix path id stays reachable via `_legacy_path_repo_id`, which
    `detect_repo` uses to surface a re-trust hint for grants made by earlier
    engines, so the change is migration-safe.

    A linked git worktree resolves to its MAIN worktree first: the git remote
    (branch 1) is already shared via git config, but a no-remote repo falls to
    the repo_uuid in config.json (branch 2) or the path hash (branch 3) -- both
    per-worktree -- so the worktree would otherwise get a DISTINCT repo_id, read
    "untrusted", and silently no-op enforcement. Resolving to the main root
    stabilizes all three branches; it is the identity for any non-worktree root
    and for a worktree whose main has no profile (nothing to share).
    """
    from chameleon_mcp.worktree import resolve_profile_root

    repo_root = resolve_profile_root(repo_root)
    key = str(repo_root.resolve())
    cached = _REPO_ID_CACHE.get(key)
    if cached is not None:
        cached_at, repo_id = cached
        if (time.monotonic() - cached_at) < _REPO_ID_CACHE_TTL:
            return repo_id

    url = _git_remote_url(repo_root)
    if url:
        canonical = _normalize_git_url(url)
        if canonical:
            repo_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            _REPO_ID_CACHE[key] = (time.monotonic(), repo_id)
            return repo_id

    uuid = _persisted_repo_uuid(repo_root)
    if uuid:
        repo_id = hashlib.sha256(f"chameleon-uuid:{uuid}".encode()).hexdigest()
        _REPO_ID_CACHE[key] = (time.monotonic(), repo_id)
        return repo_id

    path_key = key.lower() if _fs_is_case_insensitive(repo_root) else key
    repo_id = hashlib.sha256(path_key.encode("utf-8")).hexdigest()
    _REPO_ID_CACHE[key] = (time.monotonic(), repo_id)
    return repo_id


def _legacy_path_repo_id(repo_root: Path) -> str:
    """The pre-v6 path-derived repo_id (case-preserving).

    Used by `detect_repo` to look up trust grants made by early engines, which
    hashed the resolved path verbatim (no case folding, no uuid). A trust
    record found at the legacy id surfaces a `legacy_trust_hint` so the model
    can prompt the user to re-trust under the current id.
    """
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
