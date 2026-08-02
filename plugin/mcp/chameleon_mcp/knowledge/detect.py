"""Framework detection scored from the taxonomy, not from hand-written arms.

Chameleon's own classifier recognizes six frameworks, each as a hardcoded branch
that returns the first match. That shape has two costs: a seventh framework is a
code change in six places, and "first match wins" cannot say that a repo is
Next.js AND React AND Prisma, which is the ordinary case.

This module replaces the branches with a scored sweep over the 64 framework
profiles the taxonomy ships. Signals are graded by how much they prove:

    definitive        1.00  a dependency manifest names the package
    strong_config     0.80  the framework's own config file is present
    strong_convention 0.60  the framework's conventional dirs/files are present
    weak_idiom        0.20  a code idiom appears  (NOT IMPLEMENTED -- see below)

    score(F) = 1 - PROD(1 - w) over matched signals
    report F iff score(F) >= 0.60 AND at least one non-weak signal matched

The "at least one non-weak" clause is what stops a stray idiom from claiming a
framework: a lone `render(` in a helper never makes a repo React.

Weak idiom signals are deliberately not implemented. They would require scanning
file CONTENT across the repo, and by the rule above a weak signal can never
surface a framework on its own -- so their absence changes which frameworks are
reported not at all, only the score of ones already reported. That is a real
scope cut with a bounded consequence, stated rather than hidden.

Everything here reads file NAMES and dependency manifests. No repo code is
executed and no file body is parsed, so the cost is a bounded directory walk.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.knowledge.loader import concepts, enabled

# Clause endings that mark the tokens before them as dependency names. A hint
# reads "react+react-dom in package.json" or "@nestjs/core+@nestjs/common deps",
# so the manifest word is the marker and the package names sit to its left.
_DEP_MARKERS: Final[tuple[str, ...]] = (
    "in package.json",
    "in reqs",
    "in requirements",
    "in requirements.txt",
    "in pyproject",
    "in pyproject.toml",
    "in gemfile",
    "in go.mod",
    "in cargo.toml",
    "in composer.json",
    "in pom.xml",
    "in build.gradle",
    "deps",
    "dep",
    # Ruby profiles spell the manifest word as the noun: "sinatra gem",
    # "rspec-rails gem". It must stay AFTER "in gemfile" so that marker keeps
    # priority, and it needs a trailing space or clause end to match, so
    # "Gemfile with rails" is not mistaken for a dependency clause.
    "gem",
)

# Manifest files, and how to pull declared dependency names out of each.
_MANIFESTS: Final[tuple[str, ...]] = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "Gemfile",
    "go.mod",
    "Cargo.toml",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)

# A package name as written in a manifest: optional scope, then dotted/dashed
# segments. Deliberately strict -- a loose pattern turns prose words into
# dependency claims.
_PKG_RE: Final[re.Pattern[str]] = re.compile(r"@?[a-z][a-z0-9]*(?:[/._-][a-z0-9*]+)*")

# `next.config.(js|ts|mjs)` -- one filename standing for several.
_ALT_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<stem>[^()]*)\((?P<alts>[^()]+)\)(?P<tail>.*)$")

# A token that looks like a file rather than a word: has a dot and an extension
# of 1-5 letters, or is one of the extensionless config names build tools use.
_FILEISH_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.*/-]+\.[A-Za-z0-9]{1,5}$")
_BARE_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {"Makefile", "Dockerfile", "Gemfile", "Procfile", "Rakefile", "Brewfile"}
)

# Never a repo's own source, and each is large enough that walking it dominates
# the scan. `.`-prefixed directories are pruned separately.
_PRUNE_DIRS: Final[frozenset[str]] = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "out",
        "coverage",
        "__pycache__",
        "venv",
        ".venv",
        "tmp",
        "log",
        "logs",
        "public",
        "site-packages",
        "bower_components",
        "Pods",
        "DerivedData",
    }
)


def _expand_alternation(token: str) -> tuple[str, ...]:
    """`next.config.(js|ts|mjs)` -> the three real filenames."""
    m = _ALT_RE.match(token)
    if not m:
        return (token,)
    stem, tail = m.group("stem"), m.group("tail")
    return tuple(f"{stem}{alt.strip()}{tail}" for alt in m.group("alts").split("|") if alt.strip())


def _clauses(hint: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in hint.split(";") if part.strip())


def parse_hint(hint: str) -> dict[str, tuple[str, ...]]:
    """Split one `detection_hint` into dependency and config-file signals.

    Returns `{"deps": (...), "files": (...)}`. Anything that is neither -- a
    code idiom like `app.listen(port)` -- is dropped, per the module docstring.
    """
    deps: list[str] = []
    files: list[str] = []
    if not isinstance(hint, str):
        return {"deps": (), "files": ()}

    for clause in _clauses(hint):
        low = clause.casefold()
        marker = next((m for m in _DEP_MARKERS if low.endswith(m) or f"{m} " in low), None)
        if marker is not None:
            head = low.split(marker)[0].strip()
            # `react+react-dom` -- a clause may name several packages at once.
            for piece in head.replace(" and ", "+").split("+"):
                piece = piece.strip().strip(",")
                if _PKG_RE.fullmatch(piece):
                    deps.append(piece)
        # ALWAYS scan for a file too, dep clause or not: `next dep +
        # next.config.(js|ts|mjs)` carries both, and treating the clause as
        # dep-only silently lost every such config signal.
        for raw in clause.replace(",", " ").split():
            # Quotes come off first; the PARENS must survive, because they are
            # the alternation the next step expands.
            for token in _expand_alternation(raw.strip().strip("`'\"")):
                token = token.strip("()")
                if token in _BARE_CONFIG_NAMES or _FILEISH_RE.match(token):
                    # A bare `*.vue` is a convention glob, not a config file.
                    if not token.startswith("*") and not token.startswith("."):
                        files.append(token)
    return {"deps": tuple(dict.fromkeys(deps)), "files": tuple(dict.fromkeys(files))}


# What may legally follow a dependency name: a version specifier, extras, an
# environment marker, a URL, a comment, or end-of-string. Anything else means the
# name did not actually end there.
_DEP_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*@?([A-Za-z0-9][A-Za-z0-9._/-]*)\s*(?:[<>=!~;,\[(@#\"']|$)"
)

# No real package name approaches this; a longer token is malformed input rather
# than a dependency, and keeping it would let one crafted line hold a megabyte.
_DEP_NAME_MAX = 214


def _dep_name(raw: object) -> str:
    """The leading project name of a requirement string, casefolded.

    Strips the version specifier, extras, environment marker, URL, and trailing
    comment a manifest line may carry, so `flask-login>=0.6 ; python_version <
    "3.12"` yields `flask-login`.

    The name must END where a name legally can. A plain prefix match would let a
    token that merely STARTS with a framework's name be read as that framework --
    `flaské-login` truncating to `flask` at the first character it cannot
    consume -- which is the same false dependency claim `_declared_deps` exists
    to prevent, just arriving one layer down.
    """
    m = _DEP_NAME_RE.match(str(raw))
    if not m:
        return ""
    name = m.group(1)
    return name.casefold() if len(name) <= _DEP_NAME_MAX else ""


def _json_dep_names(text: str, sections: tuple[str, ...]) -> set[str]:
    """Dependency keys from the named object sections of a JSON manifest."""
    try:
        doc = json.loads(text)
    except ValueError:
        return set()
    if not isinstance(doc, dict):
        return set()
    out: set[str] = set()
    for section in sections:
        block = doc.get(section)
        if isinstance(block, dict):
            out.update(str(k).casefold() for k in block)
    return out


# Table names that hold dependencies, wherever they appear in the document. The
# set is the contract rather than the paths: enumerating vendor paths misses the
# next tool, and every one of these names means dependencies in every ecosystem
# that uses it.
_DEP_TABLE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "dependencies",  # PEP 621, poetry, Cargo, [target.*.], [workspace.]
        "dev-dependencies",  # poetry, Cargo, pdm
        "build-dependencies",  # Cargo
        "optional-dependencies",  # PEP 621
        "dependency-groups",  # PEP 735
    }
)

# `packages` means dependencies ONLY as a Pipfile's own top-level table. It is
# the most overloaded key in pyproject.toml -- under `[tool.setuptools]` and
# `[tool.mypy]` it names the project's OWN modules -- so matching it at any
# depth would read a vendored `flask/` package directory as a Flask dependency,
# which is the same false claim the per-manifest parsers exist to prevent.
_ROOT_ONLY_DEP_TABLE_KEYS: Final[frozenset[str]] = frozenset({"packages", "dev-packages"})

# Deep enough for `[tool.hatch.envs.default.dependencies]`, bounded so a
# pathological document cannot drive unbounded recursion.
_TOML_MAX_DEPTH: Final[int] = 8


def _toml_dep_names(text: str) -> set[str]:
    """Dependency names from the dependency TABLES of a TOML manifest.

    Walks for the table NAMES rather than for known paths, so PEP 621, PEP 735,
    poetry groups, pdm, hatch environments, Pipfile, and Cargo's plain,
    `[workspace.]` and `[target.'cfg(...)'.]` tables are all covered by one
    rule -- and a tool that adopts the same conventional names later is covered
    without an edit.
    """
    import tomllib

    try:
        doc = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()
    if not isinstance(doc, dict):
        return set()

    out: set[str] = set()

    def _absorb(block: object, depth: int) -> None:
        # A dependency table is a sequence of requirement strings (PEP 621,
        # Cargo's array form), a mapping of name -> constraint (poetry, Pipfile,
        # Cargo's usual form), or a mapping of GROUP -> sequence (PEP 621
        # optional-dependencies, PEP 735 dependency-groups). The three are told
        # apart by shape: values that are all lists mean the keys are group
        # labels, never package names.
        if depth > _TOML_MAX_DEPTH:
            return
        if isinstance(block, list):
            for item in block:
                if isinstance(item, str) and (n := _dep_name(item)):
                    out.add(n)
        elif isinstance(block, dict):
            if block and all(isinstance(v, list) for v in block.values()):
                for group in block.values():
                    _absorb(group, depth + 1)
                return
            for key in block:
                # `python` is poetry's interpreter constraint, not a package.
                # `orchestrator._python_dep_names` excludes it for the same
                # reason, and these two readers must not disagree about the same
                # file.
                if (n := _dep_name(key)) and n != "python":
                    out.add(n)

    def _walk(node: object, depth: int) -> None:
        if depth > _TOML_MAX_DEPTH or not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _DEP_TABLE_KEYS or (depth == 0 and key in _ROOT_ONLY_DEP_TABLE_KEYS):
                _absorb(value, depth)
            elif isinstance(value, dict):
                _walk(value, depth + 1)

    _walk(doc, 0)
    return out


_GEMFILE_RE: Final[re.Pattern[str]] = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]""", re.MULTILINE)
_GO_REQUIRE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:require\s+)?([a-z0-9][a-z0-9._/-]*\.[a-z]{2,}/[^\s]+)\s+v"
)
_POM_ARTIFACT_RE: Final[re.Pattern[str]] = re.compile(
    r"<(?:artifactId|groupId)>\s*([^<\s]+)\s*</(?:artifactId|groupId)>"
)
_GRADLE_COORD_RE: Final[re.Pattern[str]] = re.compile(r"""['"]([a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+)""")

_XML_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
# An <exclusions> block names what the build must NOT pull in, so its
# coordinates mean the opposite of a declaration. The self-closing form is
# removed FIRST: left to the paired pattern, `<exclusions/>` would open a match
# that only ends at the NEXT block's closing tag, deleting every declaration in
# between -- turning a strip-the-negation fix into a drop-the-declaration bug.
_POM_EMPTY_EXCLUSIONS_RE: Final[re.Pattern[str]] = re.compile(
    r"<exclusions\b[^>]*/>", re.IGNORECASE
)
_POM_EXCLUSIONS_RE: Final[re.Pattern[str]] = re.compile(
    r"<exclusions\b[^>]*>.*?</exclusions>", re.DOTALL | re.IGNORECASE
)
# Whatever the two patterns above left behind is an opener with no close.
_POM_EXCLUSIONS_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"<exclusions\b", re.IGNORECASE)
_GRADLE_BLOCK_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
# `(?<!:)` keeps a URL scheme's own `//` from reading as a comment: a
# `maven { url "https://..." }` sharing a line with a coordinate would otherwise
# take the coordinate with it when the rest of the line is stripped.
_GRADLE_LINE_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"(?<!:)//[^\n]*")

# PEP 503 treats `_`, `-` and `.` runs as equivalent. Applied to Python
# manifests only: a Ruby gem (`activerecord_import`) and a Go module path carry
# meaningful underscores, so normalizing everywhere would break those instead.
_PY_MANIFESTS: Final[frozenset[str]] = frozenset({"requirements.txt", "pyproject.toml", "Pipfile"})

# The same 50k figure `orchestrator._read_marker_text` uses, though counted in
# CHARACTERS here (text mode, post-decode) where that one counts BYTES -- on
# multibyte content the two cut at different points, which is harmless because
# neither is a correctness boundary, only a bound. A manifest past this is not a
# manifest, and reading it whole is how a hostile or generated file turns a
# bounded scan into a MemoryError.
_MANIFEST_MAX_CHARS: Final[int] = 50_000


# Comment delimiters per manifest, for truncation repair only. Keyed by format
# because the trim must not fire on one that has no such comment syntax: a
# `<!--` inside a TOML description is a string, not an opener, and cutting from
# it would drop every real dependency after it.
_TRUNCATION_COMMENTS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "pom.xml": (("<!--", "-->"),),
    "build.gradle": (("/*", "*/"),),
    "build.gradle.kts": (("/*", "*/"),),
}


def _trim_truncated(manifest: str, text: str) -> str:
    """Drop the unreliable tail of a manifest the read cap cut short.

    Truncation makes a read produce WRONG names rather than merely fewer, in two
    ways, and both are the fabrication class the parsers exist to prevent:

    - a cut landing between `<!--` and its `-->` leaves the comment body live,
      so a coordinate the team REMOVED is resurrected as a declaration;
    - a cut landing mid-token leaves a partial requirement, and end-of-string is
      a legal name terminator, so `django-environ` cut short reads as `django`.

    Dropping the last (partial) line and anything after an unterminated comment
    opener puts truncation back on the safe side: fewer names, never invented
    ones. The comment half is scoped by format, because over-trimming loses real
    declarations -- the opposite error, and no less wrong for being quieter.
    """
    newline = text.rfind("\n")
    if newline != -1:
        text = text[: newline + 1]
    for opener, closer in _TRUNCATION_COMMENTS.get(manifest, ()):
        start = text.rfind(opener)
        if start != -1 and text.find(closer, start) == -1:
            text = text[:start]
    return text


def _with_pep503(names: set[str]) -> set[str]:
    """Both the written and the PEP 503-normalized spelling of each name.

    Kept as BOTH rather than replaced, so a taxonomy hint spelling a package
    either way still matches. `orchestrator._python_dep_names` normalizes for the
    same reason; the two answer the same question about the same file and must
    not disagree.
    """
    return names | {re.sub(r"[-_.]+", "-", n) for n in names}


def _declared_deps(manifest: str, text: str) -> set[str]:
    """Dependency names a manifest DECLARES, never words it merely contains.

    Each format is read through its own declaration surface rather than scraped
    for package-shaped tokens. The scrape this replaced turned any prose that
    happened to spell a framework into a dependency claim -- a `pyproject.toml`
    whose description read "a lightweight alternative to flask" detected as
    Flask -- and framework names are ordinary words (flask, express, next,
    solid, astro, hono), so the collision is the common case rather than the
    rare one. Fails open to an empty set per manifest.
    """
    if manifest in ("package.json", "composer.json"):
        sections = (
            ("dependencies", "devDependencies", "peerDependencies")
            if manifest == "package.json"
            else ("require", "require-dev")
        )
        return _json_dep_names(text, sections)

    if manifest in ("pyproject.toml", "Pipfile", "Cargo.toml"):
        found = _toml_dep_names(text)
        return _with_pep503(found) if manifest in _PY_MANIFESTS else found

    if manifest == "requirements.txt":
        out: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            # `-r base.txt` / `-e .` are directives, not requirements.
            if not line or line.startswith(("#", "-")):
                continue
            if n := _dep_name(line):
                out.add(n)
        return _with_pep503(out)

    if manifest == "Gemfile":
        return {n for m in _GEMFILE_RE.finditer(text) if (n := m.group(1).casefold())}

    if manifest == "go.mod":
        # Per line, because `// indirect` marks a TRANSITIVE dependency: a CLI
        # that merely pulls a web framework in through something else must not
        # be classified as using it, and real go.mod files are mostly such
        # lines.
        #
        # `exclude`/`retract`/`replace` must be tracked as BLOCKS, not matched
        # per line: their parenthesized bodies are indented, so the directive
        # keyword never appears on the line carrying the module path, and an
        # explicitly EXCLUDED module would otherwise score as a dependency.
        out = set()
        skipping_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if skipping_block:
                if stripped.startswith(")"):
                    skipping_block = False
                continue
            if stripped.startswith(("replace", "exclude", "retract")):
                # `exclude (` opens a block; `exclude one/module v1` is one line.
                # The trailing comment is stripped first because `exclude ( //
                # dropped: CVE-...` is legal and common -- reading the raw line
                # end would miss the paren and let the excluded module through.
                skipping_block = _GRADLE_LINE_COMMENT_RE.sub("", stripped).rstrip().endswith("(")
                continue
            if stripped.startswith("//") or "// indirect" in line:
                continue
            m = _GO_REQUIRE_RE.match(line)
            if m:
                out.add(m.group(1).casefold())
        return out

    if manifest == "pom.xml":
        # A commented-out coordinate is the record of a dependency REMOVED, and
        # an <exclusions> block names what must not be pulled in; both read as
        # declarations to a bare element match.
        body = _XML_COMMENT_RE.sub(" ", text)
        body = _POM_EXCLUSIONS_RE.sub(" ", _POM_EMPTY_EXCLUSIONS_RE.sub(" ", body))
        # An <exclusions> left unclosed (malformed, or cut by the read cap) would
        # otherwise spill its excluded coordinates into the result, so everything
        # from the opener on is dropped -- losing declarations rather than
        # claiming ones the file negates.
        unclosed = _POM_EXCLUSIONS_OPEN_RE.search(body)
        if unclosed is not None:
            body = body[: unclosed.start()]
        return {n for m in _POM_ARTIFACT_RE.finditer(body) if (n := m.group(1).casefold())}

    if manifest in ("build.gradle", "build.gradle.kts"):
        # Gradle coordinates are `group:artifact:version`; both halves of the
        # leading pair are worth recording, since profiles name either. Comments
        # go first: a commented-out `implementation "g:a:v"` is the commonest way
        # a removed dependency lingers in a build file.
        body = _GRADLE_LINE_COMMENT_RE.sub(" ", _GRADLE_BLOCK_COMMENT_RE.sub(" ", text))
        out = set()
        for m in _GRADLE_COORD_RE.finditer(body):
            group, _, artifact = m.group(1).partition(":")
            out.add(group.casefold())
            if artifact:
                out.add(artifact.casefold())
        return out

    return set()


def _manifest_dep_names(root: Path, max_dirs: int) -> frozenset[str]:
    """Every dependency name declared anywhere in the repo's manifests.

    Reads the root and its direct children, matching chameleon's own
    `_candidate_manifest_dirs`: a `frontend/` + `backend/` split is covered, and
    nothing deeper is walked.
    """
    names: set[str] = set()
    dirs: list[Path] = [root]
    try:
        for child in sorted(root.iterdir()):
            if len(dirs) >= max_dirs:
                break
            if child.is_dir() and not child.name.startswith("."):
                dirs.append(child)
    except OSError:
        pass

    for d in dirs:
        for manifest in _MANIFESTS:
            path = d / manifest
            try:
                if not path.is_file():
                    continue
                # Capped like `orchestrator._read_marker_text`, its sibling for
                # exactly this job: a manifest is a small declarative file, and
                # an unbounded read of a multi-gigabyte or device file raises
                # MemoryError, which is not an OSError and escapes every guard
                # between here and the caller.
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(_MANIFEST_MAX_CHARS + 1)
                if len(text) > _MANIFEST_MAX_CHARS:
                    text = _trim_truncated(manifest, text[:_MANIFEST_MAX_CHARS])
            except OSError:
                continue
            try:
                names |= _declared_deps(manifest, text)
            except Exception:
                # Parsing is best-effort per manifest. A deeply nested document
                # raises RecursionError from json/tomllib, which is neither the
                # ValueError the parsers catch nor an OSError, and this module's
                # documented contract is that any failure yields no signal
                # rather than propagating.
                continue
    return frozenset(names)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str] | None:
    """Translate a glob to a regex over a `/`-joined relative path.

    Written out rather than delegated because neither stdlib option works here.
    `PurePath.match` treats `**` as a single `*`, so `app/**/page.tsx` never
    matches `app/page.tsx` -- the Next.js ROOT page, the commonest shape there
    is -- and `PurePath.full_match`, which does handle it, landed in 3.13 while
    this package supports 3.11.

    Semantics are the ordinary glob ones: `**/` spans zero or more directories,
    `*` and `?` stop at a separator, and `{a,b}` alternates (pnpm's
    `{apps,packages}/*/package.json` needs it).
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "{":
            close = pattern.find("}", i)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            alts = pattern[i + 1 : close].split(",")
            out.append("(?:" + "|".join(re.escape(a.strip()) for a in alts if a.strip()) + ")")
            i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    try:
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def _glob_hit(root: Path, pattern: str, dir_budget: int, max_depth: int) -> bool:
    """Whether any path matches, over a walk bounded in BOTH depth and breadth.

    `Path.glob` is lazy, so a HIT is cheap -- but a MISS walks the whole tree,
    and a miss is the common case when scoring 64 profiles against one repo.
    This walks explicitly instead, stopping after `dir_budget` directories or
    `max_depth` levels, and never entering a directory that is not a repo's own
    source.
    """
    pattern = pattern.strip()
    if not pattern or pattern.startswith("!"):
        return False
    rx = _glob_regex(pattern)
    if rx is None:
        return False

    visited = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and visited < dir_budget:
        current, depth = stack.pop()
        visited += 1
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                rel = str(Path(entry.path).relative_to(root)).replace(os.sep, "/")
            except ValueError:
                continue
            if rx.match(rel):
                return True
            if (
                depth < max_depth
                and entry.name not in _PRUNE_DIRS
                and not entry.name.startswith(".")
                and entry.is_dir(follow_symlinks=False)
            ):
                stack.append((Path(entry.path), depth + 1))
    return False


def _dep_declared(dep: str, declared: frozenset[str]) -> bool:
    """Whether a hint's package name is among the repo's declared dependencies.

    A hint may name a FAMILY rather than one package -- `spring-boot-starter-*`,
    `@remix-run/*`, `@fastify/*` -- because that is how those ecosystems ship.
    An exact-membership test silently misses every one of them, which is how a
    Spring repo came back as Quarkus.
    """
    want = dep.casefold()
    if "*" not in want:
        return want in declared
    rx = _glob_regex(want)
    return rx is not None and any(rx.match(name) for name in declared)


def _is_language_manifest(path: str) -> bool:
    """Whether a path is the LANGUAGE's manifest rather than a framework's config.

    `package.json` proves a repo is JavaScript, never that it is any particular
    framework -- its CONTENTS are the definitive signal, already read as
    dependencies. Several profiles list it under `layout` and `file_archetypes`
    anyway, and honoring that made every JS repo match pnpm-workspaces.
    """
    return path.rsplit("/", 1)[-1] in _MANIFESTS


def _layout_hit(root: Path, entry: str) -> bool:
    """A `layout` entry is a directory (`src/pages/`) or a file (`nuxt.config.ts`)."""
    entry = entry.strip()
    if not entry or entry.startswith("*"):
        return False
    try:
        return (root / entry.rstrip("/")).exists()
    except OSError:
        return False


def score_frameworks(
    repo_root: Path,
    language: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Score every candidate framework against the repo.

    Returns the reported frameworks, highest score first, each as
    `{"framework", "score", "signals": {tier: [...]}, "language": [...]}`.
    An empty tuple means nothing cleared the bar -- which is the honest answer
    for a repo whose framework the taxonomy does not describe.

    Fails open: any error yields an empty tuple, matching the contract
    `_classify_framework` already keeps.
    """
    if not enabled():
        return ()
    try:
        root = Path(repo_root)
        if not root.is_dir():
            return ()
    except (OSError, TypeError):
        return ()

    w_def = threshold_float("TAXONOMY_TIER_DEFINITIVE")
    w_cfg = threshold_float("TAXONOMY_TIER_STRONG_CONFIG")
    w_conv = threshold_float("TAXONOMY_TIER_STRONG_CONVENTION")
    floor = threshold_float("TAXONOMY_REPORT_FLOOR")
    max_dirs = threshold_int("TAXONOMY_MANIFEST_DIRS")
    max_globs = threshold_int("TAXONOMY_GLOBS_PER_FRAMEWORK")
    dir_budget = threshold_int("TAXONOMY_SCAN_DIRS")
    max_depth = threshold_int("TAXONOMY_SCAN_DEPTH")

    try:
        declared = _manifest_dep_names(root, max_dirs)
    except OSError:
        declared = frozenset()

    want_lang = language.casefold() if isinstance(language, str) else None
    out: list[dict[str, Any]] = []

    for profile in concepts("framework-profile"):
        langs = [str(x).casefold() for x in profile.get("language") or () if isinstance(x, str)]
        # A language filter narrows the sweep; without one every profile is a
        # candidate, which is what a polyglot repo needs.
        if want_lang is not None and langs and want_lang not in langs:
            continue

        parsed = parse_hint(str(profile.get("detection_hint") or ""))
        signals: dict[str, list[str]] = {
            "definitive": [],
            "strong_config": [],
            "strong_convention": [],
        }
        # One PATH proves one thing. Several profiles name the same file in both
        # their hint and their layout, and counting it twice turned a single
        # `package.json` into a 0.92 for pnpm-workspaces on every JS repo.
        seen_paths: set[str] = set()

        for dep in parsed["deps"]:
            if _dep_declared(dep, declared):
                signals["definitive"].append(dep)

        for filename in parsed["files"]:
            key = filename.strip("/")
            if key in seen_paths or _is_language_manifest(key):
                continue
            if _layout_hit(root, filename):
                seen_paths.add(key)
                signals["strong_config"].append(filename)

        # Convention signals come from the STRUCTURED fields, so they need no
        # prose parsing: the globs and the layout are already machine-readable.
        checked = 0
        for archetype in profile.get("file_archetypes") or ():
            if checked >= max_globs:
                break
            pattern = archetype.get("pattern") if isinstance(archetype, dict) else None
            if not isinstance(pattern, str) or pattern.strip("/") in seen_paths:
                continue
            if _is_language_manifest(pattern.strip("/")):
                continue
            checked += 1
            if _glob_hit(root, pattern, dir_budget, max_depth):
                seen_paths.add(pattern.strip("/"))
                signals["strong_convention"].append(pattern)
        for entry in profile.get("layout") or ():
            if checked >= max_globs:
                break
            if not isinstance(entry, str) or not entry.endswith("/"):
                continue
            key = entry.strip("/")
            if key in seen_paths:
                continue
            checked += 1
            if _layout_hit(root, entry):
                seen_paths.add(key)
                signals["strong_convention"].append(entry)

        weights = (
            [w_def] * len(signals["definitive"])
            + [w_cfg] * len(signals["strong_config"])
            + [w_conv] * len(signals["strong_convention"])
        )
        if not weights:
            continue
        # One conventional directory is not a framework. `app/` alone scores
        # exactly at the spec's 0.60 floor, which would make every repo with an
        # `app/` directory a Remix repo -- so a framework carried by conventions
        # ALONE needs two independent ones. A definitive or config signal still
        # stands on its own, because those name the framework.
        if not signals["definitive"] and not signals["strong_config"]:
            if len(signals["strong_convention"]) < 2:
                continue
        miss = 1.0
        for w in weights:
            miss *= 1.0 - w
        score = 1.0 - miss
        if score < floor:
            continue
        out.append(
            {
                "framework": profile["term"],
                "score": round(score, 4),
                "signals": {k: v for k, v in signals.items() if v},
                "language": list(profile.get("language") or ()),
            }
        )

    # A framework carried by conventions ALONE is dropped when some other
    # framework for the same language named itself in a manifest. Shared layout
    # is not evidence: Spring Boot and Quarkus both live in `src/main/java/`,
    # and Quarkus's own profile globs `**/*.java`, so a Spring repo that never
    # mentions Quarkus was being reported as Quarkus. A named dependency
    # outranks a directory they have in common.
    named_langs = {
        lang for row in out if row["signals"].get("definitive") for lang in row["language"]
    }
    out = [
        row
        for row in out
        if row["signals"].get("definitive")
        or row["signals"].get("strong_config")
        or not (named_langs & set(row["language"]))
    ]

    # Most-specific first: score, then the number of matched convention
    # patterns, so Next.js leads React on a repo that is both.
    out.sort(
        key=lambda row: (
            -row["score"],
            -len(row["signals"].get("strong_convention", ())),
            row["framework"],
        )
    )
    return tuple(out)


def primary_framework(repo_root: Path, language: str | None = None) -> str | None:
    """The single best-scoring framework, or None.

    The narrow answer chameleon's `profile.json` and `detect_repo` already
    carry. `score_frameworks` is the full one.
    """
    ranked = score_frameworks(repo_root, language)
    return ranked[0]["framework"] if ranked else None
