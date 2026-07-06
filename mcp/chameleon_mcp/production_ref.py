"""Production-branch detection and resolution.

Chameleon derives its profile from the repo's canonical line of
development (the branch deploys cut from), not from whatever feature
branch happens to be checked out. This module answers two questions,
offline-only — EXCEPT ``fetch_production_ref``, the single explicit
network entry point (every other git call is a bounded local
subprocess; never the network):

  * ``detect_production_branch(repo_root)`` — which branch is that?
  * ``resolve_production_ref(repo_root, branch)`` — what ref + commit
    does that branch point to right now?
  * ``fetch_production_ref(repo_root, branch)`` — refresh the remote
    tracking ref first, so resolution sees the latest production tip
    instead of the user's last fetch. Non-interactive and hang-proof;
    fails open to the existing tracking ref on any error.

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

import contextlib
import os
import re
import signal
import subprocess
import time
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


def branch_is_origin_backed(repo_root: Path, branch: str) -> bool:
    """True when ``refs/remotes/origin/<branch>`` exists — i.e. the SPECIFIC
    branch is origin-backed, not merely that some branch is.

    The fetch gate must check the LOCKED branch itself: a repo with an
    origin-backed default plus a local-only ``production`` branch would
    otherwise pass a generic ``from_origin`` check and fire a doomed network
    fetch for the local-only branch. Never raises.
    """
    try:
        branch = (branch or "").strip()
        if not branch:
            return False
        _names, origin_backed = _branch_names(repo_root)
        return branch in origin_backed
    except Exception:  # noqa: BLE001
        return False


def detect_production_branch(repo_root: Path) -> ProductionBranchDetection:
    """Pick the repo's production/canonical branch. Never raises."""
    try:
        names, origin_backed = _branch_names(repo_root)

        head = _origin_head_branch(repo_root)
        # Trust origin/HEAD only when its target is a real tracking ref. `git
        # remote remove` (and a pruned remote default) leaves the origin/HEAD
        # symref file behind, dangling: symbolic-ref still resolves the NAME
        # though refs/remotes/origin/<head> no longer exists and no remote is
        # configured. Claiming from_origin off that stale name would auto-lock a
        # production_ref on an effectively local-only repo and pin derivation to
        # a branch that can never be fetched. When the symref is dangling, fall
        # through to local-branch detection instead.
        if head and head in origin_backed:
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
        # Unknowable: a name we can't parse a pid from could belong to an
        # in-flight tree, so treat it as alive rather than sweep it.
        return True
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


def invalidate_resolve_memo(repo_root: Path, branch: str) -> None:
    """Drop the cached resolution for (repo_root, branch).

    A fresh fetch moved origin/<branch>; a later same-process resolve with
    ``use_memo=True`` must not return the pre-fetch SHA. No-op in a fresh
    process (empty memo) — the detached auto-refresh child — and belt-and-
    suspenders in the long-lived MCP server.
    """
    _RESOLVE_MEMO.pop((str(repo_root), branch), None)


# ---------------------------------------------------------------------------
# fetch_production_ref — the one network entry point.
# ---------------------------------------------------------------------------

# A non-interactive fetch can still FAIL; classify so the caller can tell the
# user precisely why a refresh fell back to the stale local ref. Patterns are
# matched against stderr captured under LC_ALL=C (stable English).
_FETCH_CLASSIFY: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "no_network",
        re.compile(
            r"Could not resolve host|unable to access|Connection (timed out|refused)"
            r"|[Nn]etwork is unreachable",
            re.I,
        ),
    ),
    (
        "auth",
        re.compile(
            r"Permission denied|publickey|Authentication failed|terminal prompts disabled"
            r"|could not read Username|Host key verification failed",
            re.I,
        ),
    ),
    (
        "no_remote_ref",
        re.compile(
            r"couldn't find remote ref|no such remote|does not appear to be a git repository",
            re.I,
        ),
    ),
    (
        "concurrent",
        re.compile(r"cannot lock ref|unable to (lock|update) ref|unable to update local ref", re.I),
    ),
)

# Persistent failures (an absent key, a deleted remote branch) recur every
# session; a transient one (timeout, a lock race) does not. Only the persistent
# kinds arm the backoff so chameleon doesn't pay a doomed fetch every session.
_FETCH_BACKOFF_OUTCOMES: frozenset[str] = frozenset({"auth", "no_remote_ref"})

# A production_ref value flows positionally into `git fetch origin <value>`. It
# MUST be a plain branch name: a ':' makes git treat it as a <src>:<dst> refspec
# that WRITES a local ref; '+' forces it; '*' is a wildcard refspec; a leading
# '-' is parsed as an option (--upload-pack=<cmd> is RCE); whitespace/glob/
# control chars never appear in a real branch. This allowlist is stricter than
# git check-ref-format but safe by construction.
_SAFE_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")


def is_safe_branch_name(branch: str) -> bool:
    """True if ``branch`` is a plain, fetch-safe branch name (no refspec/option
    metacharacters). Used to refuse a poisoned or malformed ``production_ref``
    before it reaches a git subprocess."""
    b = (branch or "").strip()
    if not b or b.endswith("/") or b.endswith(".lock") or "//" in b:
        return False
    return bool(_SAFE_BRANCH_RE.match(b))


@dataclass(frozen=True)
class FetchOutcome:
    """Result of a production-ref fetch attempt. ``status`` is one of
    ok / timeout / no_network / auth / no_remote_ref / concurrent / unknown /
    disabled. ``reason`` is user-facing text (empty for ok / silent statuses).
    ``attempted`` is False when the fetch was gated off (disabled / not locked).
    """

    status: str
    reason: str = ""
    attempted: bool = True

    def as_dict(self) -> dict:
        return {"attempted": self.attempted, "outcome": self.status, "reason": self.reason}


def _fetch_reason(status: str, branch: str) -> str:
    if status == "timeout":
        return "network unreachable or slow remote; used last fetched ref"
    if status == "no_network":
        return "network unreachable; used last fetched ref"
    if status == "auth":
        return (
            "SSH key or credentials not available to a non-interactive process; run "
            f"`git fetch origin {branch}` once manually, then re-run /chameleon-refresh"
        )
    if status == "no_remote_ref":
        return "branch no longer on origin; used last fetched ref"
    if status == "concurrent":
        # Matches both a benign lock race (ref IS current) and a real
        # failure-to-update (stale .lock, disk full, permissions) where the ref
        # stays stale. We can't tell them apart, so never go silent: surface it
        # so a recurring real failure is visible rather than deriving stale.
        return (
            f"could not update the remote tracking ref (lock race or stale lock); it may be "
            f"behind production — run `git fetch origin {branch}` if this recurs"
        )
    return ""  # ok / unknown-with-stderr handled by caller


def _fetch_backoff_marker(repo_data_dir: Path, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", branch) or "ref"
    return repo_data_dir / f".prod_fetch_backoff.{safe}"


def _classify_fetch_failure(stderr: str, branch: str) -> FetchOutcome:
    for status, pat in _FETCH_CLASSIFY:
        if pat.search(stderr):
            return FetchOutcome(status, _fetch_reason(status, branch))
    first = next((ln for ln in (stderr or "").strip().splitlines() if ln.strip()), "")
    return FetchOutcome("unknown", (first[:160] or "fetch failed; used last fetched ref"))


def _kill_process_group(p: subprocess.Popen) -> None:
    """Kill the fetch process AND its ssh grandchild. A bare ``p.kill()`` leaves
    the ssh child alive holding the pipe; kill the whole group/tree instead.

    A grandchild that ``setsid``s out of the group (e.g. a forced
    ``ControlMaster auto`` in the user's ssh config) could survive the killpg,
    but the fetch argv pins ``BatchMode=yes`` with no ControlMaster, so this is
    not reachable on the path we spawn.
    """
    try:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                return
        else:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(p.pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
            return
    except Exception:  # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        p.kill()


def fetch_production_ref(
    repo_root: Path,
    branch: str,
    *,
    repo_data_dir: Path | None = None,
    timeout_seconds: float = 10.0,
    backoff_hours: float = 6.0,
) -> FetchOutcome:
    """Fetch ``origin <branch>`` non-interactively. Never raises; never hangs.

    Updates only ``refs/remotes/origin/<branch>`` (plain refspec, never
    ``<branch>:<branch>`` which fails when production is checked out). The git
    env disables every interactive prompt (terminal, askpass, SSH BatchMode) so
    a missing credential is a clean failure, not a hang; a hard timeout plus a
    process-group kill backstops a stuck transfer. On a persistent failure
    (auth / no_remote_ref) a backoff marker under ``repo_data_dir`` suppresses
    re-fetching every session; an ``ok`` clears it, a transient failure does not
    arm it. Caller decides whether to fetch (lock + flag + origin-backed + CI);
    this function just executes and classifies.
    """
    try:
        branch = (branch or "").strip()
        if not branch:
            return FetchOutcome("disabled", attempted=False)
        # SECURITY: only a plain branch name may reach `git fetch origin <name>`.
        # A leading '-' is an option (--upload-pack=<cmd> -> RCE); a ':' or '+'
        # makes it a <src>:<dst> refspec that writes a LOCAL ref; '*' is a
        # wildcard refspec. Refuse anything that isn't a plain name (the argv
        # below ALSO uses --end-of-options as a second line of defense for the
        # option case).
        if not is_safe_branch_name(branch):
            return FetchOutcome(
                "disabled",
                "refused: production_ref is not a plain branch name",
                attempted=False,
            )

        marker = _fetch_backoff_marker(repo_data_dir, branch) if repo_data_dir else None
        if marker is not None:
            try:
                if (time.time() - marker.stat().st_mtime) < backoff_hours * 3600:
                    return FetchOutcome(
                        "disabled",
                        "skipped (a recent fetch failed; backing off)",
                        attempted=False,
                    )
            except OSError:
                pass

        ssh_cmd = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5"
        cmd = [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "credential.helper=",  # last-wins clears an inherited HTTPS keychain helper
            "-c",
            f"core.sshCommand={ssh_cmd}",  # repo-level can win over the env var; set both
            "fetch",
            "--no-tags",
            "--quiet",
            # Everything after this is positional: a dashed remote/refspec can
            # never be parsed as an option (defense-in-depth vs the leading-dash
            # refusal above). git >= 2.24; the env already assumes modern git.
            "--end-of-options",
            "origin",
            branch,
        ]
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": ssh_cmd,
            "SSH_ASKPASS_REQUIRE": "never",
            "LC_ALL": "C",
            "LANG": "C",
        }
        if os.name == "posix":
            # An empty askpass that exits non-zero turns a missing credential into
            # an immediate clean failure instead of a prompt. POSIX-only path.
            env["GIT_ASKPASS"] = "/bin/false"
            env["SSH_ASKPASS"] = "/bin/false"

        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": env,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True  # own process group for killpg
        else:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        # Context-managed so the pipe read-ends are closed on EVERY exit
        # (including the timeout path) -- a bare Popen leaks 2 fds per timed-out
        # fetch in the long-lived MCP server.
        try:
            with subprocess.Popen(cmd, **popen_kwargs) as p:  # noqa: S603
                try:
                    _out, err = p.communicate(timeout=timeout_seconds)
                    rc = p.returncode
                except subprocess.TimeoutExpired:
                    _kill_process_group(p)
                    with contextlib.suppress(Exception):
                        p.wait(timeout=2)
                    return FetchOutcome("timeout", _fetch_reason("timeout", branch))
        except OSError:
            return FetchOutcome("unknown", "git unavailable; used last fetched ref")

        outcome = FetchOutcome("ok") if rc == 0 else _classify_fetch_failure(err or "", branch)

        if marker is not None:
            try:
                if outcome.status in _FETCH_BACKOFF_OUTCOMES:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(outcome.status, encoding="utf-8")
                elif outcome.status == "ok":
                    marker.unlink(missing_ok=True)
            except OSError:
                pass
        return outcome
    except Exception:  # noqa: BLE001 — fetch is best-effort; never break refresh
        return FetchOutcome("unknown", "fetch failed unexpectedly; used last fetched ref")
