"""Production-branch detection and resolution.

Chameleon derives its profile from the repo's canonical line of
development (the branch deploys cut from), not from whatever feature
branch happens to be checked out. This module answers two questions,
offline-only (every git call is a bounded local subprocess; never the
network):

  * ``detect_production_branch(repo_root)`` — which branch is that?
  * ``resolve_production_ref(repo_root, branch)`` — what ref + commit
    does that branch point to right now?

Detection chain, strongest signal first:

  1. ``refs/remotes/origin/HEAD`` — the remote's declared default
     branch, written by git at clone time. An explicit team-level
     declaration, so it wins outright.
  2. A branch literally named ``production`` or ``prod``: repos that
     keep a dedicated deploy branch without making it the remote
     default.
  3. The conventional default names ``main``/``master``/``trunk``.

``conflict=True`` flags the cases where silently locking would guess
between live alternatives (symref answer coexists with a distinct
production-named branch, or two names in the same priority group both
exist). Callers ask the user only then; the clean cases lock
zero-touch.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 2

# Branch names that mark a dedicated deploy line, then the conventional
# trunk names. Order within each group is the tie-break preference.
_PRODUCTION_NAMES: tuple[str, ...] = ("production", "prod")
_DEFAULT_NAMES: tuple[str, ...] = ("main", "master", "trunk")


@dataclass(frozen=True)
class ProductionBranchDetection:
    branch: str | None
    source: str  # "origin_head" | "named_production" | "default_name" | "none"
    candidates: tuple[str, ...] = ()
    conflict: bool = False
    # True when the chosen branch is backed by the origin remote (symref or an
    # origin/<name> ref). Auto-locking at init/refresh requires this: a
    # local-only repo's branch names are too weak a signal to silently change
    # what tree the profile derives from.
    from_origin: bool = False


@dataclass(frozen=True)
class ResolvedRef:
    ref: str
    sha: str


def _run_git(
    repo_root: Path, *args: str, timeout_seconds: float = _GIT_TIMEOUT_SECONDS
) -> str | None:
    """Run git, returning stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def git_toplevel(repo_root: Path) -> Path | None:
    """The working-tree toplevel containing ``repo_root``, resolved, or None.

    A bootstrap root may be a subdirectory of its git repo (the JS-sidecar
    flow: ``bootstrap_repo(<repo>/app/javascript)``); production-ref
    derivation needs the containing toplevel to re-base the materialized
    tree onto the same subdirectory.
    """
    out = _run_git(repo_root, "rev-parse", "--show-toplevel")
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return None


def _origin_head_branch(repo_root: Path) -> str | None:
    """Short branch name origin/HEAD points at, or None when unset."""
    out = _run_git(repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not out:
        return None
    # "origin/production" -> "production"
    return out.split("/", 1)[1] if "/" in out else out


def _branch_names(repo_root: Path) -> tuple[list[str], set[str]]:
    """Short names of local heads + origin remote branches, deduplicated.

    Returns (names, origin_backed): remote names are stripped of their
    "origin/" prefix so both sides compare on the bare branch name, and
    ``origin_backed`` holds the names that exist under refs/remotes/origin.
    origin/HEAD itself is excluded.
    """
    out = _run_git(
        repo_root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
        "refs/remotes/origin",
    )
    if out is None:
        return [], set()
    names: list[str] = []
    origin_backed: set[str] = set()
    for line in out.splitlines():
        name = line.strip()
        if not name or name == "origin":
            continue
        if name.startswith("origin/"):
            name = name[len("origin/") :]
            origin_backed.add(name)
        if name == "HEAD":
            continue
        if name not in names:
            names.append(name)
    return names, origin_backed


def _group_hits(names: list[str], group: tuple[str, ...]) -> list[str]:
    """Names matching the group, in group preference order (case-insensitive)."""
    lowered = {n.lower(): n for n in names}
    return [lowered[g] for g in group if g in lowered]


def detect_production_branch(repo_root: Path) -> ProductionBranchDetection:
    """Pick the repo's production/canonical branch. Never raises."""
    try:
        names, origin_backed = _branch_names(repo_root)

        head = _origin_head_branch(repo_root)
        if head:
            prod_hits = [n for n in _group_hits(names, _PRODUCTION_NAMES) if n != head]
            return ProductionBranchDetection(
                branch=head,
                source="origin_head",
                candidates=tuple(prod_hits),
                conflict=bool(prod_hits),
                from_origin=True,
            )

        for source, group in (
            ("named_production", _PRODUCTION_NAMES),
            ("default_name", _DEFAULT_NAMES),
        ):
            hits = _group_hits(names, group)
            if hits:
                return ProductionBranchDetection(
                    branch=hits[0],
                    source=source,
                    candidates=tuple(hits[1:]),
                    conflict=len(hits) > 1,
                    from_origin=hits[0] in origin_backed,
                )

        return ProductionBranchDetection(branch=None, source="none")
    except Exception:  # noqa: BLE001 — detection is advisory; fail to "ask the user"
        return ProductionBranchDetection(branch=None, source="none")


def _resolve_commit(
    repo_root: Path, ref: str, *, timeout_seconds: float = _GIT_TIMEOUT_SECONDS
) -> str | None:
    """Commit SHA the ref points to, or None."""
    sha = _run_git(
        repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}", timeout_seconds=timeout_seconds
    )
    if sha and len(sha) in (40, 64):
        return sha
    return None


# Materializing a full tree scales with repo size (a ~9k-file repo takes
# ~2.5s); the generous bound only guards against a hung git, not normal cost.
_WORKTREE_TIMEOUT_SECONDS = 300


def materialize_production_tree(repo_root: Path, dest: Path, sha: str) -> Path | None:
    """Check out the commit's tree at ``dest`` via a detached git worktree.

    Local object store only — never the network. Returns ``dest`` on
    success, None on any failure (with best-effort cleanup of a partial
    checkout). The caller owns the returned tree and must release it
    with :func:`remove_production_tree` when done.

    ``core.hooksPath`` is force-pointed at a path that cannot exist as a
    hook dir: ``git worktree add`` runs the repo's post-checkout hook by
    default, which would execute repo-controlled code during a derivation
    that promises static analysis only.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "git",
                "-c",
                # os.devnull ("/dev/null" / "nul") can never hold hook files
                # on either platform, so hooks are disabled portably.
                f"core.hooksPath={os.devnull}",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "--detach",
                str(dest),
                sha,
            ],
            capture_output=True,
            text=True,
            timeout=_WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            remove_production_tree(repo_root, dest)
            return None
        return dest
    except (subprocess.TimeoutExpired, OSError):
        remove_production_tree(repo_root, dest)
        return None
    except Exception:  # noqa: BLE001 — materialization is best-effort
        remove_production_tree(repo_root, dest)
        return None


def remove_production_tree(repo_root: Path, dest: Path) -> None:
    """Release a materialized tree: worktree remove, rmtree fallback, prune.

    Tolerates every failure mode (tree already gone, git missing, dest
    never registered) — cleanup must not mask the result of the
    derivation that used the tree.
    """
    try:
        subprocess.run(
            # Double --force: a SIGKILL during `worktree add` leaves the
            # registration locked (reason "initializing"), and git refuses a
            # single-force remove for locked trees. The second --force is a
            # no-op for unlocked trees.
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", "--force", str(dest)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        if dest.is_file() or dest.is_symlink():
            # rmtree silently refuses plain files; a stray file in the
            # container must still be sweepable.
            dest.unlink(missing_ok=True)
        elif dest.exists():
            import shutil

            shutil.rmtree(dest, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    # `worktree prune` skips locked registrations even when their dir is
    # gone, so a crash-locked entry whose dir the rmtree fallback removed
    # would otherwise live in .git/worktrees forever. Unlock failures
    # (not locked, never registered) are expected and ignored.
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "unlock", str(dest)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    # Drop the .git/worktrees registration left behind when the dir was
    # removed without `git worktree remove` (crash, rmtree fallback).
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def prune_stale_production_trees(repo_root: Path, container: Path) -> None:
    """Sweep leftover materialized trees from crashed runs under ``container``.

    Tree dirs are named ``<sha12>-<pid>``; a dir whose creating pid is
    still alive is skipped — bootstrap/refresh serialize per repo_id via
    advisory locks, but a second checkout of the same remote shares the
    repo_id and its in-flight tree must not be swept from here.
    """
    try:
        if container.is_dir():
            for child in container.iterdir():
                if _creating_pid_alive(child.name):
                    continue
                remove_production_tree(repo_root, child)
        # Registrations can outlive their dirs: a crash during `worktree add`
        # leaves a locked .git/worktrees entry, and once the dir itself is
        # swept the container loop above never sees it again. Walk git's own
        # registration list for entries pointing under our container and
        # release the dead ones too.
        out = _run_git(repo_root, "worktree", "list", "--porcelain", timeout_seconds=10)
        for line in (out or "").splitlines():
            if not line.startswith("worktree "):
                continue
            wt = Path(line[len("worktree ") :].strip())
            try:
                wt.relative_to(container)
            except ValueError:
                continue
            if _creating_pid_alive(wt.name):
                continue
            remove_production_tree(repo_root, wt)
    except Exception:  # noqa: BLE001
        pass


def _creating_pid_alive(tree_name: str) -> bool:
    """True when the ``<sha12>-<pid>`` dir name's creating pid still runs.

    Unknowable (permission, malformed name) reads as alive so an in-flight
    tree from a second checkout sharing the repo_id is never swept.
    """
    pid_part = tree_name.rsplit("-", 1)[-1]
    if not pid_part.isdigit():
        return False
    try:
        os.kill(int(pid_part), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


# Process-local memo: SessionStart resolves the same lock twice (tip banner,
# then the auto-refresh trigger) within one short-lived hook process; the
# second lookup must not pay a second subprocess. Holds misses too. Never
# crosses processes, so no TTL is needed; long-lived processes (the MCP
# server) bypass it via use_memo=False.
_RESOLVE_MEMO: dict[tuple[str, str], ResolvedRef | None] = {}


def resolve_production_ref(
    repo_root: Path,
    branch: str,
    *,
    timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
    use_memo: bool = False,
) -> ResolvedRef | None:
    """Resolve a locked branch name to the ref + commit derivation should use.

    The origin tip is preferred over the local branch: the remote is the
    team's shared truth and a local branch may sit behind it (or carry
    unpushed work that should not shape the team profile). A value that
    already names a path (``origin/production``, ``refs/...``) is
    resolved as given. Returns None when nothing resolves; never raises.
    """
    try:
        branch = branch.strip()
        if not branch:
            return None
        memo_key = (str(repo_root), branch)
        if use_memo and memo_key in _RESOLVE_MEMO:
            return _RESOLVE_MEMO[memo_key]
        result: ResolvedRef | None = None
        if "/" in branch:
            sha = _resolve_commit(repo_root, branch, timeout_seconds=timeout_seconds)
            result = ResolvedRef(ref=branch, sha=sha) if sha else None
        else:
            for ref in (f"origin/{branch}", branch):
                sha = _resolve_commit(repo_root, ref, timeout_seconds=timeout_seconds)
                if sha:
                    result = ResolvedRef(ref=ref, sha=sha)
                    break
        if use_memo and len(_RESOLVE_MEMO) < 64:
            _RESOLVE_MEMO[memo_key] = result
        return result
    except Exception:  # noqa: BLE001 — resolution is best-effort; None means "unavailable"
        return None
