"""Git linked-worktree resolution for profile and trust lookups.

A linked git worktree (``git worktree add``) has a ``.git`` *file* (not a
directory) containing ``gitdir: <main>/.git/worktrees/<name>``. ``.chameleon/``
is normally gitignored and lives only at the main worktree, so a linked
worktree has no profile of its own and every chameleon lookup that keys on the
worktree's own path silently misses: no archetype injection, no idiom
enforcement, no trust.

``resolve_profile_root`` maps a worktree root to its main worktree root by
following that pointer, so the worktree inherits the main checkout's committed
profile and trust. It is the PROFILE/TRUST root only; callers keep the worktree
itself as the identity/archetype root (``repo_id`` is git-remote-derived and
already identical across worktrees, and archetype paths are relative to the
worktree so files still match ``app/models/*`` etc.).

Pure filesystem, no git subprocess (this runs on the per-edit hot path). It is
strictly additive: it returns the input root unchanged whenever the root has
its own ``.chameleon/``, when ``.git`` is a real directory (a standalone repo)
or absent, or when the main worktree cannot be resolved or has no
``.chameleon/`` — so non-worktree behavior is byte-identical.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_profile_root", "main_worktree_root"]


def resolve_profile_root(repo_root: Path) -> Path:
    """The root whose ``.chameleon/`` holds the profile and trust for ``repo_root``.

    Returns ``repo_root`` unchanged in every existing case. Only when
    ``repo_root`` is a linked git worktree with no ``.chameleon/`` of its own,
    AND its main worktree does have one, does it return the main worktree root.
    """
    try:
        if (repo_root / ".chameleon").exists():
            return repo_root
        git_marker = repo_root / ".git"
        # A standalone repo has a ``.git`` directory; a linked worktree has a
        # ``.git`` FILE. Only the file case is a worktree pointer to follow.
        if not git_marker.is_file():
            return repo_root
        main_root = main_worktree_root(git_marker)
        if main_root is not None and (main_root / ".chameleon").exists():
            return main_root
    except OSError:
        pass
    return repo_root


def main_worktree_root(git_file: Path) -> Path | None:
    """Resolve the main worktree root from a linked worktree's ``.git`` file.

    The file holds ``gitdir: <main>/.git/worktrees/<name>``. From that gitdir we
    read ``commondir`` (the shared ``.git``) and return its parent — the main
    worktree. Returns ``None`` when the file is not a worktree pointer or the
    main worktree cannot be determined (e.g. a bare repository, whose common
    dir is not named ``.git``).
    """
    try:
        text = git_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir_raw = text[len("gitdir:") :].strip()
    if not gitdir_raw:
        return None

    gitdir = Path(gitdir_raw)
    if not gitdir.is_absolute():
        # A relative gitdir is resolved against the worktree root (the .git
        # file's directory), matching how git itself interprets it.
        gitdir = git_file.parent / gitdir
    try:
        gitdir = gitdir.resolve()
    except OSError:
        return None

    common_dir = _common_dir(gitdir)
    if common_dir is None:
        return None
    # The main worktree is the parent of the shared ``.git`` directory.
    if common_dir.name != ".git":
        return None
    return common_dir.parent


def _common_dir(gitdir: Path) -> Path | None:
    """The shared git common dir for a worktree's gitdir.

    Prefers the ``commondir`` file git writes (robust across layouts); falls
    back to the structural ``<main>/.git/worktrees/<name>`` -> ``<main>/.git``
    when that file is absent.
    """
    commondir_file = gitdir / "commondir"
    try:
        if commondir_file.is_file():
            rel = commondir_file.read_text(encoding="utf-8").strip()
            if rel:
                cd = Path(rel)
                if not cd.is_absolute():
                    cd = gitdir / cd
                return cd.resolve()
    except OSError:
        pass
    # Fallback: .../.git/worktrees/<name> -> .../.git
    if gitdir.parent.name == "worktrees":
        return gitdir.parent.parent
    return None
