"""Declared and mined code ownership: CODEOWNERS rules plus git authorship shares.

Two independent answers to "who owns this file", and one resolver that prefers
the declared one:

* ``parse_codeowners`` reads the repo's CODEOWNERS file from the three standard
  locations and returns its rules in file order. ``owners_for`` answers for a
  single path under the real semantics -- gitignore-style globs, the LAST
  matching pattern wins (not the first), and a pattern carrying no owners
  UNASSIGNS the paths it matches.
* ``mine_commit_ownership`` derives per-file authorship shares from ONE bounded
  ``git log --name-only`` walk, so a repo that declares nothing still has an
  answer.
* ``resolve_owners`` prefers the declared owner and falls back to the dominant
  commit-share author only when that author's share clears a floor.

PRIVACY is load-bearing here, so the mined half is deliberately lossy. An author
EMAIL is used only as the in-memory key that groups one person's commits (it
survives a display-name change, which is why it is the key); it is never stored
in a returned row, and no raw address reaches a caller. What a row carries is the
display name git itself records, with any address in it masked
(``alice@corp.com`` -> ``alice@***``) and the result passed through
``sanitize_for_chameleon_context`` at the point it enters the artifact -- the
same boundary a CODEOWNERS owner token crosses, and for the same reason: both are
repo-derived strings headed for a model surface. Where both signals answer, the
CODEOWNERS handle wins: ``@team`` names a role, a mined name names a person.

Reads git METADATA and one committed text file -- never repo code, never the
network. Both reads are bounded (commit cap, per-commit file cap, subprocess
timeout, size cap) and every seam fails open to "no owner known", so a hostile or
absent CODEOWNERS file, a bare repo, or a missing git binary degrades to silence
rather than an exception. Kill switch: ``CHAMELEON_OWNERSHIP=0``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chameleon_mcp._thresholds import DEFAULTS, threshold_float, threshold_int
from chameleon_mcp.production_ref import git_toplevel
from chameleon_mcp.safe_open import UnsafeFileError, safe_read_text
from chameleon_mcp.sanitization import sanitize_for_chameleon_context

# The three locations git forges look in, in the order they resolve them: a repo
# carrying more than one uses the .github copy, then the root copy, then docs.
CODEOWNERS_LOCATIONS: Final[tuple[str, ...]] = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)

_SCHEMA = 1
# ``%x1f`` tells git to emit the ASCII Unit Separator once per commit; the byte
# marks a commit-header line in the combined ``--name-only`` stream (a tracked
# path cannot begin with it). ``%x1e`` (Record Separator) splits the two header
# fields. A bare literal-byte format is rejected by git, so the FORMAT uses the
# placeholders and the PARSE matches the bytes.
_COMMIT_FORMAT = "%x1f%aN%x1e%aE"
_COMMIT_MARK = "\x1f"
_FIELD_MARK = "\x1e"

# Tuning numbers, registered into the central threshold table so each one reads
# through threshold_int()/threshold_float() and is operator-overridable as
# CHAMELEON_<NAME> like every other threshold. setdefault, not assignment: a name
# the table declares in its own literal keeps that declared value.
_TUNING_DEFAULTS: Final[dict[str, int | float]] = {
    # Bounds the one git-log walk. Ownership skews recent, so a deeper walk buys
    # little beyond diluting a file's current owner with its original author.
    "OWNERSHIP_MAX_COMMITS": 2_000,
    # A commit touching more than this is a sweep (mass reformat, license header,
    # dependency bump); whoever ran it did not thereby become the owner of every
    # file it touched, so it is skipped rather than counted.
    "OWNERSHIP_MAX_FILES_PER_COMMIT": 30,
    # Artifact bounds: files kept (most-committed first) and authors per file.
    "OWNERSHIP_MAX_FILES": 5_000,
    "OWNERSHIP_MAX_AUTHORS_PER_FILE": 5,
    # A mined author is only offered as an owner when they wrote at least this
    # share of the file's commits. Below it there is no dominant author, and
    # naming the top of a flat distribution would be a guess dressed as a fact.
    "OWNERSHIP_MIN_DOMINANT_SHARE": 0.5,
    "OWNERSHIP_GIT_TIMEOUT_SECONDS": 25,
    # Read cap on the CODEOWNERS file itself. Real ones are kilobytes; anything
    # past this is not a rules file.
    "OWNERSHIP_MAX_CODEOWNERS_BYTES": 200_000,
    "OWNERSHIP_MAX_RULES": 2_000,
    # Per-pattern length bound. The compiled form of a pattern nests `.*` groups
    # once per `**`, so an absurdly long pattern is refused rather than handed to
    # the regex engine.
    "OWNERSHIP_MAX_PATTERN_CHARS": 512,
    "OWNERSHIP_MAX_OWNER_CHARS": 120,
}
for _name, _default in _TUNING_DEFAULTS.items():
    DEFAULTS.setdefault(_name, _default)

# An address embedded in an owner token or a git display name. The local part
# must be non-empty, so a `@user` handle and an `@org/team` slug -- which begin
# with the `@` -- are left alone.
_EMAIL_RE = re.compile(r"([^\s@]+)@([^\s@]+\.[^\s@]+)")


def _enabled() -> bool:
    """The kill switch. Offline git-metadata mining ships ON; ``=0`` turns it off."""
    return os.environ.get("CHAMELEON_OWNERSHIP") != "0"


def _mask_email(value: str) -> str:
    """``alice@corp.com`` -> ``alice@***``. Leaves ``@user`` / ``@org/team`` intact."""
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)}@***", value)


def _safe_identity(value: str) -> str:
    """Mask, sanitize and cap a repo-derived identity on its way into a result.

    The single boundary every owner string crosses, whether it came from a
    CODEOWNERS line or from a git author field.
    """
    masked = _mask_email(str(value or "").strip())
    return sanitize_for_chameleon_context(masked)[: threshold_int("OWNERSHIP_MAX_OWNER_CHARS")]


def _normalize_rel(rel_path) -> str:
    """A repo-relative path in the form patterns are matched against."""
    rel = str(rel_path or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _glob_body(pattern: str) -> str:
    """Translate one gitignore-style glob into a regex body.

    ``*`` and ``?`` stop at a separator, ``**/`` spans zero or more directories,
    a trailing ``**`` spans the rest of the path, and a ``[...]`` class passes
    through with ``!`` negation rewritten to ``^``.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                j = i
                while j < n and pattern[j] == "*":
                    j += 1
                if j < n and pattern[j] == "/":
                    # Collapse a run of `**/` into ONE group. Emitting one
                    # `(?:.*/)?` per segment gives sequential optional `.*`
                    # groups, whose split points multiply -- `**/` repeated a
                    # couple of dozen times inside the 512-char cap makes
                    # `owners_for` hang on a non-matching path, and CODEOWNERS
                    # is a repo-controlled file. Consecutive `**/` are
                    # semantically identical to one anyway.
                    while pattern.startswith("**/", i):
                        i += 3
                        while i < n and pattern[i] == "/":
                            i += 1
                    if not out or out[-1] != "(?:.*/)?":
                        out.append("(?:.*/)?")
                    continue
                else:
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
                continue
            body = pattern[i + 1 : close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = close + 1
            continue
        out.append(re.escape(char))
        i += 1
    return "".join(out)


def _pattern_to_regex(pattern: str):
    """Compile a CODEOWNERS pattern, or None when it cannot be honored.

    Anchoring follows gitignore: a separator at the start or the middle pins the
    pattern to the repo root, and a pattern without one matches at any depth. A
    trailing ``/`` restricts the match to paths INSIDE that directory; every
    other pattern matches the named path and everything under it, which is how
    ``docs`` comes to own ``docs/api.md``.
    """
    text = (pattern or "").strip()
    if not text or len(text) > threshold_int("OWNERSHIP_MAX_PATTERN_CHARS"):
        return None
    dir_only = text.endswith("/")
    text = text.rstrip("/")
    anchored = "/" in text
    text = text.lstrip("/")
    if not text:
        return None
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if dir_only else "(?:/.*)?"
    try:
        return re.compile(f"^{prefix}{_glob_body(text)}{suffix}$")
    except re.error:
        return None


@dataclass(frozen=True)
class CodeownersRule:
    """One honored CODEOWNERS line.

    ``owners`` is already masked and sanitized and is what a caller renders;
    ``pattern`` is the raw repo text kept for matching, so a caller that renders
    it must sanitize it first. An empty ``owners`` is meaningful: the line
    unassigns the paths it matches.
    """

    pattern: str
    owners: tuple[str, ...]
    line: int
    matcher: re.Pattern


def _parse_rules(text: str) -> list[CodeownersRule]:
    """Rules in file order. Unparseable lines are dropped, never guessed at."""
    rules: list[CodeownersRule] = []
    max_rules = threshold_int("OWNERSHIP_MAX_RULES")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if len(rules) >= max_rules:
            break
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        cut = next((i for i, tok in enumerate(tokens) if tok.startswith("#")), None)
        if cut is not None:
            tokens = tokens[:cut]
        if not tokens:
            continue
        pattern = tokens[0]
        if pattern.startswith("!"):
            # gitignore's negation is not CODEOWNERS syntax; a forge reports the
            # line as an error rather than inverting it, so it is dropped here.
            continue
        if pattern.startswith("\\#"):
            pattern = pattern[1:]
        matcher = _pattern_to_regex(pattern)
        if matcher is None:
            continue
        # An owner is a `@user`, an `@org/team`, or an address -- every form
        # carries an `@`. A token without one is noise, not an owner.
        owners = tuple(_safe_identity(tok) for tok in tokens[1:] if "@" in tok)
        rules.append(CodeownersRule(pattern=pattern, owners=owners, line=line_no, matcher=matcher))
    return rules


def parse_codeowners(repo_root) -> list[CodeownersRule]:
    """The repo's CODEOWNERS rules in precedence order, or [] when it has none.

    The first readable location in ``CODEOWNERS_LOCATIONS`` wins; the rest are
    not merged, matching how a forge picks one file. A location that fails the
    repo-boundary, symlink or size checks is skipped rather than read, so the
    next location still gets its turn.
    """
    if not _enabled():
        return []
    root = Path(repo_root)
    cap = threshold_int("OWNERSHIP_MAX_CODEOWNERS_BYTES")
    for rel in CODEOWNERS_LOCATIONS:
        try:
            text = safe_read_text(root, rel, max_size_bytes=cap)
        except (UnsafeFileError, OSError, ValueError):
            continue
        try:
            return _parse_rules(text)
        except (ValueError, TypeError, re.error):
            return []
    return []


def owners_for(rules, rel_path) -> tuple[str, ...]:
    """The owners of ``rel_path`` under last-match-wins, or () when it has none.

    () covers both "no rule matched" and "the last matching rule unassigned it";
    neither claims an owner, so a caller treats them the same.
    """
    rel = _normalize_rel(rel_path)
    if not rel:
        return ()
    winner: tuple[str, ...] = ()
    for rule in rules or ():
        try:
            if rule.matcher.match(rel):
                winner = rule.owners
        except (AttributeError, TypeError, re.error):
            continue
    return winner


def _commit_groups(top: Path, max_commits: int) -> list[tuple[str, str, list[str]]] | None:
    """``(author_name, author_email, files)`` per commit from one bounded walk, or
    None when git cannot answer.

    Merges are excluded: ``--name-only`` prints no paths for one anyway, so
    including them would spend the commit budget on empty groups. ``%aN``/``%aE``
    are the mailmap-resolved fields, so a repo with a .mailmap gets one identity
    per person for free.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(top),
                "-c",
                "core.quotePath=false",
                "log",
                "--no-renames",
                "--no-merges",
                f"--format={_COMMIT_FORMAT}",
                "--name-only",
                "-n",
                str(max(1, max_commits)),
            ],
            capture_output=True,
            text=True,
            timeout=threshold_int("OWNERSHIP_GIT_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None

    groups: list[tuple[str, str, list[str]]] = []
    name = ""
    email = ""
    files: list[str] | None = None
    for raw in (proc.stdout or "").split("\n"):
        if raw.startswith(_COMMIT_MARK):
            if files is not None:
                groups.append((name, email, files))
            header = raw[1:]
            name, _, email = header.partition(_FIELD_MARK)
            files = []
            continue
        if files is None or not raw:
            continue
        files.append(raw)
    if files is not None:
        groups.append((name, email, files))
    return groups


def _exists_in_tree(top: Path, rel: str) -> bool:
    try:
        return (top / rel).is_file()
    except (OSError, ValueError):
        return False


def mine_commit_ownership(
    repo_root,
    *,
    max_commits: int | None = None,
    max_files_per_commit: int | None = None,
    max_files: int | None = None,
    max_authors_per_file: int | None = None,
) -> dict | None:
    """Mine ``{file: [{author, commits, share}, ...]}`` from history, or None when
    git is unavailable.

    Shares are per file: an author's commits touching that file over the file's
    total commits in the walked window, rounded, highest first. A file no longer
    in the tree is dropped (nobody owns a path that is gone), and a bulk commit
    over ``max_files_per_commit`` contributes nothing. Bounded on every axis;
    thresholds default from the table but stay overridable for testing.

    The rows carry a masked, sanitized display name and never an address -- see
    the module docstring on why the email is the grouping key and nothing else.
    """
    if not _enabled():
        return None
    if max_commits is None:
        max_commits = threshold_int("OWNERSHIP_MAX_COMMITS")
    if max_files_per_commit is None:
        max_files_per_commit = threshold_int("OWNERSHIP_MAX_FILES_PER_COMMIT")
    if max_files is None:
        max_files = threshold_int("OWNERSHIP_MAX_FILES")
    if max_authors_per_file is None:
        max_authors_per_file = threshold_int("OWNERSHIP_MAX_AUTHORS_PER_FILE")

    # Keys are top-relative, so mining from a monorepo SUBDIR still produces one
    # coordinate system the whole repo's workspaces share.
    top = git_toplevel(Path(repo_root))
    if top is None:
        return None
    groups = _commit_groups(top, max_commits)
    if groups is None:
        return None

    per_file: dict[str, Counter] = defaultdict(Counter)
    display_names: dict[str, Counter] = defaultdict(Counter)
    for name, email, paths in groups:
        if not paths or len(paths) > max_files_per_commit:
            continue
        key = (email or name or "").strip().lower()
        if not key:
            continue
        display_names[key][name or email] += 1
        for rel in set(paths):
            per_file[rel][key] += 1

    # Rank before pruning so the existence check costs at most ``max_files``
    # stats: a hot file deleted since is worth one wasted slot, an unbounded
    # stat sweep over every path in 2,000 commits is not.
    ranked = sorted(per_file.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:max_files]

    files: dict[str, list[dict]] = {}
    for rel, counts in ranked:
        if not _exists_in_tree(top, rel):
            continue
        total = sum(counts.values())
        if total <= 0:
            continue
        rows = []
        for key, count in counts.most_common(max_authors_per_file):
            shown = display_names[key].most_common(1)
            author = _safe_identity(shown[0][0] if shown else key)
            if not author:
                continue
            rows.append({"author": author, "commits": count, "share": round(count / total, 3)})
        if rows:
            files[rel] = rows

    # ``root`` is the work-tree top the keys are relative to, so a consumer can
    # relativize its own paths without spawning git again.
    return {"schema": _SCHEMA, "root": str(top), "files": files}


def _mined_rows(mined, rel_path) -> list:
    """The mined rows for one path. Fail-open [] on a None / malformed artifact."""
    if not isinstance(mined, dict):
        return []
    files = mined.get("files")
    if not isinstance(files, dict):
        return []
    rows = files.get(_normalize_rel(rel_path))
    return rows if isinstance(rows, list) else []


def resolve_owners(rel_path, *, rules=None, mined=None, min_share: float | None = None) -> dict:
    """The best available owner for one path: ``{"owners": [...], "source": ...}``.

    ``source`` is ``codeowners`` for a declared owner, or ``commits`` for the
    mined dominant author (whose ``share`` is included). {} means nobody is
    claimed -- no rule matched, the matching rule unassigned the path, or no
    author cleared ``min_share``.

    ``rules`` and ``mined`` are supplied by the caller so this stays a pure
    lookup: both are built once per bootstrap/refresh, never per query. Their
    paths must be relative to the same root (the mined artifact records which).
    """
    if not _enabled():
        return {}
    try:
        declared = owners_for(rules or (), rel_path)
        if declared:
            return {"owners": list(declared), "source": "codeowners"}
        if min_share is None:
            min_share = threshold_float("OWNERSHIP_MIN_DOMINANT_SHARE")
        rows = _mined_rows(mined, rel_path)
        if not rows:
            return {}
        top = rows[0]
        if not isinstance(top, dict):
            return {}
        author = top.get("author")
        share = top.get("share")
        if not author or not isinstance(share, (int, float)) or isinstance(share, bool):
            return {}
        if share < min_share:
            return {}
        return {"owners": [author], "source": "commits", "share": share}
    except (AttributeError, TypeError, ValueError):
        return {}
