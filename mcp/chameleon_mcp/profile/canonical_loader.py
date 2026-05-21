"""v0.6.0 branch-pinning: materialize a canonical-ref profile to a cache dir.

When ``.chameleon/config.json`` sets ``canonical_ref`` (e.g.
``"origin/main"``), chameleon should serve profile reads from THAT ref
instead of the working tree — so a developer on a feature branch keeps
seeing the team's main-branch conventions regardless of what their
local checkout has.

The implementation: run ``git show <ref>:<file>`` for each hashed
artifact and write the bytes to a per-ref cache under
``~/.local/share/chameleon/<repo_id>/canonical/<ref-sha>/``. Subsequent
tool calls use that cache directory as the profile_dir. Cache key is
``<ref-sha>``, so when the ref advances (someone pushes a new
``.chameleon/`` snapshot) the cache key changes and the new content
materializes lazily on the next call.

Returns ``None`` (callers fall back to the working tree) when:
  - the repo isn't a git repo
  - the ref doesn't resolve
  - the ref doesn't contain a ``.chameleon/`` tree
  - any subprocess call errors / times out
  - the materialized profile fails validation

Materialization is wrapped by an exclusive flock to prevent two
concurrent sessions from racing on the same cache dir.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import time
from pathlib import Path

# Artifacts we materialize. These mirror what load_profile_dir reads
# (excluding optional files like profile.summary.md / idioms.md /
# .archetype_renames.json which are loaded best-effort by the existing
# loader). The COMMITTED sentinel is created by this loader itself
# after a successful materialization so the existing loader's
# is_committed() check passes.
_REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "profile.json",
    "archetypes.json",
    "rules.json",
    "canonicals.json",
)
_OPTIONAL_ARTIFACTS: tuple[str, ...] = (
    "idioms.md",
    "profile.summary.md",
    ".archetype_renames.json",
)

_GIT_TIMEOUT_SECONDS = 5
_LOCK_FILENAME = ".materialize.lock"
_COMMITTED_FILENAME = "COMMITTED"
_REF_METADATA_FILENAME = ".canonical_ref"


def _run_git(args: list[str], *, cwd: Path, timeout: int = _GIT_TIMEOUT_SECONDS):
    """Run ``git`` with a short timeout, returning the completed process.

    Returns ``None`` on any failure (timeout, OSError, git not on PATH).
    Callers MUST handle ``None`` as a hard skip.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def _resolve_ref(repo_root: Path, ref: str) -> str | None:
    """Return the commit SHA the ref points to, or None."""
    result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_root)
    if result is None or result.returncode != 0:
        return None
    sha = (result.stdout or "").strip()
    return sha if len(sha) == 40 else None


def _materialize_artifact(
    repo_root: Path, ref_sha: str, artifact: str, dest: Path
) -> bool:
    """Write ``git show <ref_sha>:.chameleon/<artifact>`` to ``dest``.

    Returns True on success, False if the artifact doesn't exist at the
    ref (or any error). Dest's parent is created on success.
    """
    result = _run_git(
        ["show", f"{ref_sha}:.chameleon/{artifact}"],
        cwd=repo_root,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_text(result.stdout or "", encoding="utf-8")
    except OSError:
        return False
    return True


def _canonical_cache_root(repo_id: str) -> Path:
    """Return the per-repo canonical cache root.

    Sibling of the legacy plugin_data_dir per-repo directory so trust /
    drift / exec_log layout isn't disturbed.
    """
    from chameleon_mcp.profile.trust import plugin_data_dir

    return plugin_data_dir() / repo_id / "canonical"


def _cache_dir_for_ref(repo_id: str, ref_sha: str) -> Path:
    """Cache dir for a specific resolved ref SHA."""
    return _canonical_cache_root(repo_id) / ref_sha


def _is_cache_valid(cache_dir: Path) -> bool:
    """True when ``cache_dir`` has a COMMITTED sentinel + required artifacts."""
    if not (cache_dir / _COMMITTED_FILENAME).is_file():
        return False
    return all((cache_dir / a).is_file() for a in _REQUIRED_ARTIFACTS)


def materialize_canonical(
    repo_root: Path, repo_id: str, canonical_ref: str
) -> Path | None:
    """Materialize the canonical profile and return its cache dir, or None.

    The returned path is suitable as the ``profile_dir`` argument to
    ``load_profile_dir`` — it has a COMMITTED sentinel and all the
    required artifacts laid out the way the working-tree
    ``.chameleon/`` does.

    Idempotent: a second call for the same ref reuses the existing
    cache after a quick validity check. Cache miss (new ref SHA) does
    a fresh materialization under an exclusive flock.
    """
    ref_sha = _resolve_ref(repo_root, canonical_ref)
    if ref_sha is None:
        return None

    cache_dir = _cache_dir_for_ref(repo_id, ref_sha)
    if _is_cache_valid(cache_dir):
        return cache_dir

    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / _LOCK_FILENAME
    # Open with O_RDWR so we can flock the file; create if missing.
    try:
        lock_fd = os.open(
            str(lock_path),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
    except OSError:
        return None

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under lock: another caller may have materialized
        # while we were waiting.
        if _is_cache_valid(cache_dir):
            return cache_dir

        # Materialize every required artifact. Any failure aborts and
        # leaves the cache dir invalid (no COMMITTED sentinel).
        for artifact in _REQUIRED_ARTIFACTS:
            if not _materialize_artifact(
                repo_root, ref_sha, artifact, cache_dir / artifact
            ):
                return None
        # Best-effort optional artifacts. Failure here is fine.
        for artifact in _OPTIONAL_ARTIFACTS:
            _materialize_artifact(
                repo_root, ref_sha, artifact, cache_dir / artifact
            )
        # Write metadata so we can tell from disk which ref a cache
        # was built from.
        try:
            (cache_dir / _REF_METADATA_FILENAME).write_text(
                f"{canonical_ref}\n{ref_sha}\n{int(time.time())}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        # Finally, drop the COMMITTED sentinel — this is what
        # load_profile_dir gates on.
        (cache_dir / _COMMITTED_FILENAME).write_text(
            ref_sha + "\n", encoding="utf-8"
        )
        return cache_dir
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def gc_stale_caches(repo_id: str, *, keep_n: int = 4) -> int:
    """Best-effort GC: keep the N most-recent canonical caches per repo.

    Caches accumulate as the canonical ref advances. We don't need to
    keep history; any cache older than the latest N can go. Returns
    the number of directories removed.
    """
    import shutil

    root = _canonical_cache_root(repo_id)
    if not root.is_dir():
        return 0
    try:
        entries = [
            p for p in root.iterdir() if p.is_dir() and len(p.name) == 40
        ]
    except OSError:
        return 0
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in entries[keep_n:]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _content_sha_of_artifact(
    repo_root: Path, ref: str, artifact: str
) -> str | None:
    """Return the content SHA256 of an artifact at a ref, or None."""
    result = _run_git(
        ["show", f"{ref}:.chameleon/{artifact}"], cwd=repo_root
    )
    if result is None or result.returncode != 0:
        return None
    return hashlib.sha256((result.stdout or "").encode("utf-8")).hexdigest()
