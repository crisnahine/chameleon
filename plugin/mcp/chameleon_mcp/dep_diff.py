"""No-network manifest/lockfile supply-chain diff helper.

Promotes the four pr-review "Step 2.5" dependency checks from skill prose to a
deterministic engine helper that parses a unified git diff of a package manifest
or lockfile and returns structured findings. The point is groundability: a
finding cites the exact added line it parsed, so the pr-review round-3 refuter
can verify it against a tool result instead of trusting model prose.

PURE PARSE. No network, no subprocess, no install. Only added (``+``) diff lines
carry a signal; removed (``-``) and context lines give the "previously present"
baseline. This is the no-network half of supply-chain review; the network CVE
audit lives in :mod:`chameleon_mcp.dep_audit`, gated and opt-in.

The four checks (severity in parentheses):
  2.5a new direct dependency (NIT listing) -- a dependency name absent before
  2.5b lockfile resolved host is not the registry (FIX)
  2.5c new install/build hook the manifest names: an npm or composer lifecycle
       script, a Cargo build script, a PEP 517 build backend, a remote Gradle
       script (FIX)
  2.5d non-registry dependency source: a VCS or direct URL, a local path, an
       alternate index or repository, a go replace, a Cargo patch (FIX)

Fails open by construction: an unparseable or empty diff yields no findings,
never a crash and never a fabricated finding.

Parsed ecosystems: npm (``package.json``, ``package-lock.json``,
``npm-shrinkwrap.json``), yarn (``yarn.lock`` classic and berry ``resolution:``
lines), pnpm (``pnpm-lock.yaml``), Bundler (``Gemfile``, ``Gemfile.lock`` and
``*.gemspec`` -- a gem declares its own dependencies via ``add_dependency`` in
the gemspec, not a Gemfile), Python (``requirements*.txt``, ``pyproject.toml``,
``Pipfile``, ``setup.cfg``), Go (``go.mod``), Rust (``Cargo.toml``), PHP
(``composer.json``) and Java (``pom.xml``, ``build.gradle``,
``build.gradle.kts``). Each ecosystem gets the checks its packaging model
actually has: every one gets 2.5a and 2.5d; 2.5c exists only where a manifest
can name code that runs at install/build time (npm/composer lifecycle scripts,
a Cargo ``build`` script, a PEP 517 ``build-backend``, a Gradle
``apply from: <url>``). Go and Maven declare no such hook, so nothing is
reported for them rather than something invented.

The dependency NAMES are read by ``knowledge.detect``'s per-manifest readers
(``_declared_deps`` and friends), not by a second parser written here: two
readers of the same file that disagree is a defect this repo has been bitten by
twice, and those readers already handle ``<exclusions>``, go ``// indirect``,
``exclude``/``retract`` blocks, Gradle comments-vs-path-globs, and PEP 503
normalization. A diff is a FRAGMENT of the file, so the reader is run over each
side of the diff (context + one marker) and the new dependencies are the
difference between the two name sets -- with the extra requirement that the name
appear on an ADDED line, so a side that fails to parse can never turn every
context-line dependency into a "new dependency" claim.

Two blind spots follow from reading a fragment, both silent by design (fewer
findings, never invented ones): a poetry-style ``[tool.poetry.dependencies]``
hunk whose table header sits above the diff's context lines, and a
``composer.json`` hunk whose ``"require": {`` opener is likewise out of context.
Both are the SAME cause -- three lines of context is not always enough to reach
the construct that gives the changed lines their meaning -- and neither is
fixable here: naming the enclosing table would mean guessing it, which is how a
`conflict` entry becomes a "new dependency". The fix, if these ever matter
enough, belongs at the FETCH site (a wider ``git diff -U``), not in the parser.
Python's ``dependencies = [`` case needs no widening because git's own ``@@``
trailer already names it; JSON has no such trailer, since composer.json has no
line starting in column 1 for git's funcname pattern to latch onto.

Still NOT parsed, and surfaced as ``uncovered_manifests`` instead:
``setup.py`` (arbitrary Python -- its dependency list is only knowable by
executing it, which this module never does) and the non-npm/Bundler LOCKFILES
(``poetry.lock``, ``Pipfile.lock``, ``go.sum``, ``Cargo.lock``,
``composer.lock``), whose formats do not carry the ``resolved``/``remote``
keyword the 2.5b host check keys on. Rather than let a change to one read as
"reviewed clean" (empty findings), ``is_uncovered_manifest`` names them and
``scan_dependency_changes`` returns them as ``uncovered_manifests`` so the
consumer hand-reviews them: an explicit "not covered", not a silent clean. The
diff-fetch caller surfaces a truncation signal when a manifest diff exceeds its
byte cap so a partial parse is never mistaken for full coverage either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# The manifest/lockfile basenames this helper knows how to parse. A consumer
# filters a changed-file list to these before fetching diffs, so a non-manifest
# edit never triggers a git fetch.
MANIFEST_LOCKFILE_BASENAMES = frozenset(
    {
        # npm / yarn / pnpm
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        # Bundler
        "Gemfile",
        "Gemfile.lock",
        # Python. A `requirements*.txt` is ALSO parsed but its name varies, so it
        # is matched by `_is_requirements_manifest` rather than listed here --
        # the same shape `*.gemspec` already uses.
        "requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.cfg",
        # Go / Rust / PHP
        "go.mod",
        "Cargo.toml",
        "composer.json",
        # Java
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
)

# Dependency files this module does NOT parse. A change to one is not reviewed by
# the supply-chain checks above, so scan_dependency_changes surfaces it as
# `uncovered_manifests` -- an honest "not covered" the consumer can hand-review,
# never a silent "reviewed clean".
#
# `setup.py` is arbitrary Python: its dependency list is only knowable by running
# it, which this module never does. The rest are LOCKFILES of the ecosystems
# whose manifests ARE parsed -- their formats carry no `resolved`/`remote`
# keyword, so the 2.5b resolved-host check has nothing to key on and would
# report a clean scan it never performed.
UNCOVERED_MANIFEST_BASENAMES = frozenset(
    {
        # Python
        "setup.py",
        "poetry.lock",
        "Pipfile.lock",
        # Go
        "go.sum",
        # Rust
        "Cargo.lock",
        # PHP
        "composer.lock",
    }
)


def _is_requirements_manifest(path: str) -> bool:
    """True for a pip requirements file, whose name varies.

    ``requirements.txt``, ``requirements-dev.txt``, and a ``base.txt`` inside a
    ``requirements/`` package are all the same manifest format, so they are
    matched by pattern the way ``*.gemspec`` is.
    """
    base = path.rsplit("/", 1)[-1]
    if not base.endswith(".txt"):
        return False
    return base.startswith("requirements") or "/requirements/" in f"/{path}"


def is_uncovered_manifest(path: str) -> bool:
    """True iff ``path`` is a dependency file this module does not parse.

    That is ``setup.py`` and the non-npm/Bundler lockfiles. A manifest this
    module DOES parse is never uncovered, so a caller checking both sets sees no
    overlap.
    """
    base = path.rsplit("/", 1)[-1]
    if base in MANIFEST_LOCKFILE_BASENAMES or _is_requirements_manifest(path):
        return False
    return base in UNCOVERED_MANIFEST_BASENAMES


# Lifecycle scripts that run automatically on `npm install` with no further
# prompt -- the classic vector for code that executes the moment a dependency
# tree is materialized.
_INSTALL_SCRIPT_KEYS = ("preinstall", "install", "postinstall")

# An added JSON object key line: `+   "<key>": <value>`. The leading `+` (not
# `+++`) marks a diff addition; the key is the first JSON string on the line.
_ADDED_JSON_KEY_RE = re.compile(r'^\+\s*"([^"]+)"\s*:\s*(.*)$')

# Version-specifier prefixes that pull code from somewhere OTHER than the
# package manager's registry publish path. Each is unambiguous as a dependency
# source -- a registry semver range (`^1.2.3`, `~1.0`, `1.2.3`, `*`, `>=1`,
# `npm:alias@1`, `workspace:*`) never starts with one of these.
_NPM_SOURCE_PREFIXES = (
    "git+ssh:",
    "git+https:",
    "git+http:",
    "git:",
    "github:",
    "gitlab:",
    "bitbucket:",
    "file:",
    "link:",
)
_TARBALL_SUFFIXES = (".tgz", ".tar.gz", ".tar")

# A bare GitHub-style git shorthand WITH a ref fragment (``user/repo#ref``). The
# ``#`` makes it unambiguous; a bare ``user/repo`` is deliberately NOT matched
# because it collides with relative path values like ``"main": "lib/index.js"``.
_BARE_GIT_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[^/\s]+$")

# A dependency VALUE looks like a registry version range; a script value is a
# command and a metadata value is prose/URL. This discriminates a dependency
# entry from other added JSON keys without tracking section context.
_VERSION_VALUE_RE = re.compile(r"^(?:[\^~><=*]|v?\d|x\b|npm:|jsr:|workspace:|catalog:)")
_DIST_TAGS = frozenset({"latest", "next", "canary", "beta", "alpha", "rc", "stable"})
# A value that is really a shell COMMAND (a lifecycle script), not a dependency
# version, betrays itself by a space, a shell metacharacter, or a
# digit-immediately-followed-by-a-letter (`7z`, `2to3`, `v8flags`) -- none of which
# appear in a version spec. Used to keep the install-script discriminator from
# downgrading a command that merely starts like a version to a dependency NIT.
_SCRIPT_COMMAND_HINT_RE = re.compile(r"[\s;&|`$()]|\d[A-Za-z]")

# package.json keys that legitimately hold a git/URL value and are NOT
# dependency entries, so a source-looking value under them is not 2.5d. (http/
# https is already restricted to tarball URLs, so homepage/bugs rarely collide;
# `repository` holding `git+https://...` is the real overlap this guards.)
_METADATA_URL_KEYS = frozenset({"repository", "homepage", "bugs", "funding", "author", "license"})

# Registry hosts that are the EXPECTED default for each ecosystem; a resolved
# URL on one of these is never flagged. registry.yarnpkg.com is yarn classic's
# default mirror of the npm registry.
_DEFAULT_NPM_HOSTS = frozenset({"registry.npmjs.org", "registry.yarnpkg.com"})
_DEFAULT_GEM_HOSTS = frozenset({"rubygems.org"})

# A lockfile line that records where a package was fetched from: npm/yarn
# `resolved`, pnpm `tarball`, Bundler `remote:`. Only lines carrying one of
# these keywords are host-checked, so an unrelated URL in the lockfile is not.
# yarn berry (v2/v3) records the fetch source on a `resolution:` line (e.g.
# `resolution: "pkg@https://host/..."`); a registry entry is `pkg@npm:ver` with
# no URL, so the keyword is safe to include.
_RESOLUTION_KEYWORD_RE = re.compile(r"\b(?:resolved|tarball|remote|resolution)\b")
_URL_HOST_RE = re.compile(r"https?://([^/\s\"',]+)")

# A dependency line naming a gem. Two syntaxes, both matched:
#   Gemfile:  `gem "name"`
#   .gemspec: `spec.add_dependency "name"` / `s.add_runtime_dependency "name"` /
#             `spec.add_development_dependency "name"`
# A gem declares its dependencies in its .gemspec, not a Gemfile, so a
# gemspec-only project (the common case for a library/gem) was invisible to the
# supply-chain scan until this line covered the add_dependency family. All three
# forms are matched, mirroring the Gemfile scanner, which flags every `gem` line
# regardless of its `group` (a dev/test dependency still executes in CI, a real
# supply-chain vector).
_GEM_LINE_RE = re.compile(
    r'^\s*(?:gem\s+|[A-Za-z_][\w]*\.add(?:_runtime|_development)?_dependency\s+)["\']([^"\']+)["\']'
)
_GEM_SOURCE_RE = re.compile(
    r"(?:\b(?:git|github|gitlab|bitbucket|path)\s*:)|(?::(?:git|github|gitlab|bitbucket|path)\s*=>)"
)


@dataclass(frozen=True)
class DepFinding:
    """One supply-chain signal parsed from a manifest/lockfile diff.

    ``evidence`` is the exact added line (or manifest key) the check parsed, so a
    reviewer or the refuter can ground the finding without re-parsing. ``detail``
    carries the structured specifics (dependency name, resolved host, source
    string) a consumer may render.
    """

    check: str
    severity: str
    path: str
    evidence: str
    message: str
    detail: dict = field(default_factory=dict)


def _added_lines(diff_text: str) -> list[str]:
    """Added (``+``) content lines of a unified diff, without the ``+`` marker.

    The ``+++ b/path`` file header is not a content addition, so it is excluded.
    """
    out: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            out.append(raw[1:])
    return out


def _scan_install_scripts(path: str, diff_text: str) -> list[DepFinding]:
    """2.5c: a newly added preinstall/install/postinstall script key (FIX)."""
    out: list[DepFinding] = []
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        m = _ADDED_JSON_KEY_RE.match(raw)
        if m is None:
            continue
        key = m.group(1)
        if key in _INSTALL_SCRIPT_KEYS:
            # "install"/"preinstall"/"postinstall" are also real npm package
            # NAMES. A version-range value means this is a dependency entry, not
            # a lifecycle-script command, so it is not 2.5c (it is 2.5a instead).
            #
            # But _looks_like_dep_value only checks the value's PREFIX, so a shell
            # command that merely STARTS like a version -- `7z x payload`, `0;curl`,
            # `2to3 -w`, `v8flags` -- would be misread as a dependency and the
            # install-script FIX silently downgraded to a dependency NIT (an
            # attacker dodge). A real command reveals itself by a space, a shell
            # metacharacter, or a digit-immediately-followed-by-a-letter, none of
            # which occur in a version spec; treat any such value as a script
            # regardless of the version-prefix match. Over-flagging a truly exotic
            # version as an install-script is a harmless advisory; missing a real
            # install script is a supply-chain false negative.
            value = _json_string_value(m.group(2))
            if (
                value is not None
                and _looks_like_dep_value(value)
                and not _SCRIPT_COMMAND_HINT_RE.search(value)
            ):
                continue
            out.append(
                DepFinding(
                    check="install-script",
                    severity="FIX",
                    path=path,
                    evidence=raw[1:].strip(),
                    message=(
                        f"New install-lifecycle script {key!r} runs automatically on "
                        "install; verify the command is intended."
                    ),
                    detail={"script": key, "command": m.group(2).strip().rstrip(",")},
                )
            )
    return out


def _json_string_value(value_text: str) -> str | None:
    """The inner text of the first JSON string in a key's value, or None.

    ``"git+https://x.git",`` -> ``git+https://x.git``. Returns None when the
    value is not a plain string (an object/array value carries no source here).
    """
    m = re.match(r'\s*"((?:[^"\\]|\\.)*)"', value_text)
    return m.group(1) if m else None


def _is_nonregistry_source(source: str) -> bool:
    """True when a dependency version specifier pulls from outside the registry."""
    s = source.strip()
    if any(s.startswith(p) for p in _NPM_SOURCE_PREFIXES):
        return True
    if s.startswith(("http://", "https://")) and s.endswith(_TARBALL_SUFFIXES):
        return True
    if _BARE_GIT_SHORTHAND_RE.match(s):
        return True
    return False


def _scan_nonregistry_source_npm(path: str, diff_text: str) -> list[DepFinding]:
    """2.5d (package.json): an added dependency whose source is not the registry."""
    out: list[DepFinding] = []
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        m = _ADDED_JSON_KEY_RE.match(raw)
        if m is None:
            continue
        key = m.group(1)
        if key in _METADATA_URL_KEYS or key in _INSTALL_SCRIPT_KEYS:
            continue
        source = _json_string_value(m.group(2))
        if source is None or not _is_nonregistry_source(source):
            continue
        out.append(
            DepFinding(
                check="non-registry-source",
                severity="FIX",
                path=path,
                evidence=raw[1:].strip(),
                message=(
                    f"Dependency {key!r} is pulled from a non-registry source; the "
                    "resolved code is not the registry artifact."
                ),
                detail={"name": key, "source": source},
            )
        )
    return out


def _scan_nonregistry_source_gem(path: str, diff_text: str) -> list[DepFinding]:
    """2.5d (Gemfile): an added `gem` line with a git/github/path source option."""
    out: list[DepFinding] = []
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        gm = _GEM_LINE_RE.match(line)
        if gm is None or _GEM_SOURCE_RE.search(line) is None:
            continue
        out.append(
            DepFinding(
                check="non-registry-source",
                severity="FIX",
                path=path,
                evidence=line.strip(),
                message=(
                    f"Gem {gm.group(1)!r} is pulled from a non-registry source "
                    "(git/github/path); not covered by the registry's integrity guarantees."
                ),
                detail={"name": gm.group(1)},
            )
        )
    return out


def _host_of(line: str) -> str | None:
    """The host of the first URL on a resolution line, port stripped, or None."""
    if _RESOLUTION_KEYWORD_RE.search(line) is None:
        return None
    m = _URL_HOST_RE.search(line)
    if m is None:
        return None
    return m.group(1).split(":", 1)[0].lower()


def _scan_lockfile_hosts(path: str, diff_text: str, default_hosts) -> list[DepFinding]:
    """2.5b: an added resolution URL whose host is not the registry (FIX).

    Baseline: hosts on pre-existing (context/removed) resolution lines are this
    repo's normal registries, so an added entry on one of them is not flagged --
    the private-registry repo is not penalized for using its own mirror.
    """
    baseline: set[str] = set()
    for raw in diff_text.splitlines():
        if raw.startswith("+"):
            continue  # added lines are the candidates, not the baseline
        if raw.startswith("-") and not raw.startswith("---"):
            host = _host_of(raw[1:])
        elif raw.startswith(" "):
            host = _host_of(raw[1:])
        else:
            host = None
        if host is not None:
            baseline.add(host)

    out: list[DepFinding] = []
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        host = _host_of(raw[1:])
        if host is None or host in default_hosts or host in baseline:
            continue
        out.append(
            DepFinding(
                check="non-registry-host",
                severity="FIX",
                path=path,
                evidence=raw[1:].strip(),
                message=(
                    f"Lockfile entry resolves from {host!r}, not the expected registry; "
                    "this is how a tampered or planted package enters."
                ),
                detail={"host": host},
            )
        )
    return out


def _looks_like_dep_value(source: str) -> bool:
    """True when a JSON string value is a dependency version range or source."""
    s = source.strip()
    if not s:
        return False
    if s in _DIST_TAGS or _VERSION_VALUE_RE.match(s) is not None:
        return True
    return _is_nonregistry_source(s)


def _install_script_value_is_command(value: str) -> bool:
    """True when a value on an ``install``/``preinstall``/``postinstall`` KEY is a
    lifecycle command (2.5c), not a dependency version (2.5a).

    Those key names are also real npm package names, so only the value
    discriminates: a version range that is NOT a shell command is a dependency
    entry; anything else -- including a command that merely STARTS like a version
    (``7z x payload``, ``0;curl``, ``2to3 -w``) -- is a script. Mirrors the guard
    in ``_scan_install_scripts`` so the install-script, new-dependency, and
    removed-dependency scanners classify the same line identically and never both
    fire on it.
    """
    return not (_looks_like_dep_value(value) and not _SCRIPT_COMMAND_HINT_RE.search(value))


# Every ``"key": "value"`` string pair on one line. A compact/inline dependency
# object keeps several deps on a SINGLE line (``"dependencies": { "next": "^1",
# "react": "^2" }``); a per-line-single-key match would read only the first key
# (whose value is the ``{`` object, not a version) and miss the inline deps
# entirely, so a compact-to-expanded reformat would flag every pre-existing dep
# as new. Harvesting all inline pairs makes the removed-baseline see them.
_INLINE_JSON_PAIR_RE = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')


def _removed_npm_dep_names(diff_text: str) -> set[str]:
    """Dependency names on removed (`-`) lines -- the "previously present" baseline.

    A bump shows the name on both a `-` and a `+` line, so the name appearing
    here means an added line with the same name is a bump, not a new dependency.
    This baseline only ever SUPPRESSES a new-dependency finding, so widening it to
    inline pairs can never invent a finding: a genuinely-new dependency name does
    not appear on any removed line, so it is never harvested here.
    """
    names: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("-") or raw.startswith("---"):
            continue
        # Scan every inline string pair on the line, not just the first key, so a
        # compact ``{ "a": "1", "b": "2" }`` dependency object on one removed line
        # contributes all of a, b (not only the leading `dependencies` key, whose
        # value is the object literal and is skipped by the dep-value gate below).
        for key, value in _INLINE_JSON_PAIR_RE.findall(raw):
            # Only the URL-bearing metadata keys are excluded; an install-script
            # key name ("install") IS a real package, so it is discriminated by
            # VALUE (a version range means a dependency, a command means a script).
            if key in _METADATA_URL_KEYS:
                continue
            if not _looks_like_dep_value(value):
                continue
            if key in _INSTALL_SCRIPT_KEYS and _install_script_value_is_command(value):
                continue
            names.add(key)
    return names


def _scan_new_dependencies_npm(path: str, diff_text: str) -> list[DepFinding]:
    """2.5a (package.json): a dependency name absent before this change (NIT)."""
    removed = _removed_npm_dep_names(diff_text)
    out: list[DepFinding] = []
    seen: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        m = _ADDED_JSON_KEY_RE.match(raw)
        if m is None:
            continue
        key, val = m.group(1), m.group(2)
        # Install-script key names are discriminated by VALUE (a version range
        # is a dependency, a command is a script), not excluded by name.
        if key in _METADATA_URL_KEYS or key in removed or key in seen:
            continue
        value = _json_string_value(val)
        if value is None or not _looks_like_dep_value(value):
            continue
        # An install-script key name is also a real package name; only a version
        # value that is NOT a shell command is a dependency (2.5a). A command value
        # is a lifecycle script (2.5c) handled by _scan_install_scripts and must
        # not also surface here, or a single added line double-reports (install
        # command that merely starts like a version -> phantom new-dependency NIT).
        if key in _INSTALL_SCRIPT_KEYS and _install_script_value_is_command(value):
            continue
        seen.add(key)
        out.append(
            DepFinding(
                check="new-dependency",
                severity="NIT",
                path=path,
                evidence=raw[1:].strip(),
                message=f"New direct dependency {key!r} ({value}); verify it is the intended package.",
                detail={"name": key, "version": value},
            )
        )
    return out


def _scan_new_dependencies_gem(path: str, diff_text: str) -> list[DepFinding]:
    """2.5a (Gemfile): a gem name absent before this change (NIT)."""
    removed: set[str] = set()
    for raw in diff_text.splitlines():
        if raw.startswith("-") and not raw.startswith("---"):
            gm = _GEM_LINE_RE.match(raw[1:])
            if gm is not None:
                removed.add(gm.group(1))
    out: list[DepFinding] = []
    seen: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        gm = _GEM_LINE_RE.match(raw[1:])
        if gm is None:
            continue
        name = gm.group(1)
        if name in removed or name in seen:
            continue
        seen.add(name)
        out.append(
            DepFinding(
                check="new-dependency",
                severity="NIT",
                path=path,
                evidence=raw[1:].strip(),
                message=f"New gem {name!r}; verify it is the intended package.",
                detail={"name": name},
            )
        )
    return out


# Manifest keys whose presence in a single added JSON OBJECT means a whole
# package.json was (re)written on ONE line. The per-key scanners parse
# ``+  "key": value`` lines, so a minified manifest defeats every check silently
# -- a postinstall script or git-source dep hidden in a one-line object yields
# zero findings, reading as a clean change. This surfaces it as a degraded review.
_MANIFEST_KEYS = frozenset(
    {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "scripts"}
)

# A dependency/scripts container OPENED inline on one added line with at least one
# ``"name": "value"`` pair after the brace -- e.g.
# ``"dependencies": { "evil": "git+ssh://...", "left-pad": "^1.0.0" }``. The per-key
# scanners are line-oriented (one ``"key": value`` per line), so pairs PACKED onto
# such a container line are extracted for none of them: a git-source dep or an
# install-shaped entry hidden there yields zero findings and reads as a clean add,
# WITHOUT the object ever being a full single ``{...}`` line (so the strict
# single-line check below misses it). ``overrides`` (npm) and ``resolutions`` (yarn)
# are in scope too: they pin a TRANSITIVE dependency to an arbitrary version OR
# source, so a packed ``"overrides": {"lib": "git+ssh://evil/x.git"}`` is the same
# non-registry-source evasion (the unpacked form is already caught line-by-line).
# Scoped to these dependency/pin/scripts containers only so a legitimately-inline
# ``"repository": {"type": "git", ...}`` or ``"engines": {...}`` never trips it.
_PACKED_MANIFEST_CONTAINER_RE = re.compile(
    r'"(?:dependencies|devDependencies|optionalDependencies|peerDependencies'
    r'|overrides|resolutions|scripts)"'
    r'\s*:\s*\{[^}]*"[^"]+"\s*:\s*"'
)


def _scan_minified_manifest(path: str, diff_text: str) -> list[DepFinding]:
    """Flag a package.json change that packs a dependency/scripts object onto one
    added line (fully minified, or a single container opened inline with pairs).

    The per-key scanners cannot decompose a one-line object, so they would
    silently return nothing. Emit one FIX so the reviewer knows the structural
    checks were skipped and the raw diff needs a manual read. A normal
    pretty-printed diff never has a full ``{...}`` object -- nor a
    dependency/scripts container with inline pairs -- on one added line, so this
    does not fire on the common one-key-per-line case.
    """
    for raw in _added_lines(diff_text):
        stripped = raw.strip().rstrip(",")
        # Case 1: the whole manifest (or a top-level object) collapsed to one line.
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict) and _MANIFEST_KEYS & obj.keys():
                return [
                    DepFinding(
                        check="minified-manifest",
                        severity="FIX",
                        path=path,
                        evidence=(stripped[:120] + "…") if len(stripped) > 120 else stripped,
                        message=(
                            "package.json change is minified to a single line; the per-key "
                            "supply-chain checks (install scripts, non-registry sources, new "
                            "dependencies) could not run. Review the raw diff manually."
                        ),
                        detail={"reason": "single-line-manifest"},
                    )
                ]
        # Case 2: a dependency/scripts container opened inline with packed pairs.
        if _PACKED_MANIFEST_CONTAINER_RE.search(stripped):
            return [
                DepFinding(
                    check="minified-manifest",
                    severity="FIX",
                    path=path,
                    evidence=(stripped[:120] + "…") if len(stripped) > 120 else stripped,
                    message=(
                        "package.json change packs a dependency/scripts object onto one "
                        "line; the per-key supply-chain checks (install scripts, "
                        "non-registry sources, new dependencies) cannot decompose it and "
                        "were silently skipped for that line. Expand it and review the raw "
                        "diff manually."
                    ),
                    detail={"reason": "packed-container-line"},
                )
            ]
    return []


# ---------------------------------------------------------------------------
# Ecosystems beyond npm and Bundler
#
# The npm and Bundler arms above are line-oriented because both manifests put one
# dependency on one line. Python, Go, Rust, PHP and Java do not share a shape, so
# instead of five more hand-rolled scanners the NAMES come from
# knowledge.detect's per-manifest readers -- the same ones framework detection
# uses, already hardened against prose, comments, <exclusions>, go
# `// indirect`, exclude/retract blocks, Gradle path-globs-vs-comments and PEP
# 503 spelling. Those read a FILE; a diff is a fragment of one, so each side of
# the diff is rebuilt into text and handed to them, and the change is the
# difference between the two name sets.
# ---------------------------------------------------------------------------

# Display cap on a source/hook string echoed into a finding message, mirroring
# the evidence cap `_scan_minified_manifest` already uses. A bound on rendering,
# not a tuning knob: the diff text itself is capped upstream by DEP_DIFF_MAX_BYTES.
_SOURCE_TEXT_MAX = 120

# TOML manifests need the fragment repair below; every other format's reader is
# line- or regex-oriented and reads a hunk fragment as-is.
_TOML_ARM_MANIFESTS = frozenset({"pyproject.toml", "Pipfile", "Cargo.toml"})
_GRADLE_ARM_MANIFESTS = frozenset({"build.gradle", "build.gradle.kts"})

# `@@ -a,b +c,d @@ <trailer>`. The trailer is git's funcname context: the last
# line above the hunk that opened the construct the hunk sits in.
_HUNK_HEADER_RE = re.compile(r"^@@[^@]*@@(.*)$")

# A TOML line that OPENS an array or inline table (`dependencies = [`). Only this
# shape is re-attached to a fragment: it is the construct whose absence makes the
# fragment unparseable, and re-attaching it cannot invent a table that is not
# already named in the file.
_TOML_OPENER_RE = re.compile(r"^[\"'A-Za-z0-9_.-]+\s*=\s*[\[{]\s*$")

# Per-manifest trailing-comment markers, used to keep a source/hook rule from
# firing on a commented-out line. Keyed by format because the marker differs and
# a `#` in one format is content in another; Gradle and Maven are absent because
# their comment syntax needs the structural strippers instead.
_LINE_COMMENT_MARKERS: dict[str, tuple[str, ...]] = {
    "requirements.txt": ("#",),
    "pyproject.toml": ("#",),
    "Pipfile": ("#",),
    "Cargo.toml": ("#",),
    "setup.cfg": ("#", ";"),
    "go.mod": ("//",),
}

# Quoted text and name-shaped tokens on one line. Used ONLY to locate the added
# line that declares a name the hardened reader already returned, never to claim
# a dependency, so a loose tokenizer here cannot fabricate a finding.
_QUOTED_TEXT_RE = re.compile(r"""["']([^"']*)["']""")
_NAME_TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9._/+@-]+")
_PEP503_RUN_RE = re.compile(r"[-_.]+")


def _side_text(diff_text: str, marker: str) -> str:
    """One side of a unified diff rebuilt as text: context plus ``marker`` lines.

    ``+`` gives the hunks' post-change content, ``-`` their pre-change content,
    so a whole-file reader sees the SAME artifact twice and the difference
    between the two name sets is what the change did. The ``+++``/``---`` file
    headers are not content.
    """
    out: list[str] = []
    for raw in diff_text.splitlines():
        if not raw:
            out.append("")
        elif raw[0] == " ":
            out.append(raw[1:])
        elif raw[0] == marker and not raw.startswith(marker * 3):
            out.append(raw[1:])
    return "\n".join(out)


def _balance_brackets(text: str, comment: str | None = None) -> str:
    """``text`` with closers appended for every bracket a fragment left open.

    A hunk cuts a document mid-structure, so its reconstruction ends inside an
    array or table and the format's parser rejects it. Closing what is open makes
    it parseable WITHOUT adding a name: only ``]`` and ``}`` are appended, never
    content. Quote-aware, so a bracket inside a string value is not counted.
    """
    stack: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif comment is not None and text.startswith(comment, i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        elif ch == "[":
            stack.append("]")
        elif ch == "{":
            stack.append("}")
        elif ch in "]}" and stack and stack[-1] == ch:
            stack.pop()
        i += 1
    return text + ("\n" + "".join(reversed(stack)) if stack else "")


def _toml_hunk_candidates(diff_text: str, marker: str) -> list[str]:
    """One repaired TOML document per hunk of ``diff_text``.

    An ordinary hunk of a `pyproject.toml` sits INSIDE `dependencies = [` with
    neither the opener nor the closer in its context lines, so tomllib rejects
    the raw reconstruction and the structured reader sees nothing. Re-attaching
    the opener the hunk's own `@@` trailer names, then closing what is left open,
    puts the fragment back into a document the reader can read.

    Each hunk is a SEPARATE document on purpose: two hunks of the same array
    would each carry the same top-level key and collide. Only an opener git
    itself reported for that hunk is re-attached, and only when it is an
    assignment -- carrying a table header across hunks would file one hunk's
    lines under another hunk's table, which is exactly the false dependency claim
    the structured readers exist to prevent.
    """
    out: list[str] = []
    lines: list[str] = []
    opener = ""

    def _flush() -> None:
        if not lines:
            return
        head = [opener] if opener and opener not in {ln.strip() for ln in lines} else []
        out.append(_balance_brackets("\n".join(head + lines), comment="#"))

    for raw in diff_text.splitlines():
        m = _HUNK_HEADER_RE.match(raw)
        if m is not None:
            _flush()
            lines = []
            trailer = m.group(1).strip()
            opener = trailer if _TOML_OPENER_RE.match(trailer) else ""
            continue
        if not raw:
            continue
        if raw[0] == " ":
            lines.append(raw[1:])
        elif raw[0] == marker and not raw.startswith(marker * 3):
            lines.append(raw[1:])
    _flush()
    return out


# setup.cfg is the one Python manifest knowledge.detect does not read, so its
# declaration surface is described here -- by the same contract: only the keys
# that hold requirements, and every entry through detect's own `_dep_name`.
_SETUPCFG_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")
_SETUPCFG_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*[=:]\s*(.*)$")
_SETUPCFG_DEP_KEYS = frozenset({"install_requires", "setup_requires", "tests_require"})
# In this section every KEY is an extra whose value is a requirement list.
_SETUPCFG_EXTRAS_SECTION = "options.extras_require"


def _setupcfg_dep_names(text: str) -> set[str]:
    """Requirement names a ``setup.cfg`` declares.

    A requirement key's value is usually an indented block under it, so the read
    is a two-state walk: a key line decides whether the block that follows holds
    requirements, and each continuation line is one requirement.
    """
    from chameleon_mcp.knowledge.detect import _dep_name

    out: set[str] = set()
    section = ""
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        sm = _SETUPCFG_SECTION_RE.match(stripped)
        if sm is not None:
            section = sm.group(1).strip()
            in_deps = False
            continue
        if not line[0].isspace():
            km = _SETUPCFG_KEY_RE.match(line)
            if km is None:
                in_deps = False
                continue
            in_deps = section == _SETUPCFG_EXTRAS_SECTION or km.group(1) in _SETUPCFG_DEP_KEYS
            if in_deps and (n := _dep_name(km.group(2).strip())):
                out.add(n)
            continue
        if in_deps and (n := _dep_name(stripped)):
            out.add(n)
    return out


# A composer.json section opened on its own line. `conflict`, `replace`,
# `provide` and `suggest` hold the same `vendor/package` keys without being
# dependencies, which is why the section a key sits under decides, never the key.
_JSON_SECTION_OPEN_RE = re.compile(r'^\s*"([^"]+)"\s*:\s*[\[{]\s*$')
_COMPOSER_DEP_SECTIONS = frozenset({"require", "require-dev"})

# A composer `require` entry that constrains the RUNTIME rather than naming a
# package: the interpreter, an extension, a bundled library, composer itself. No
# such entry is ever installed from a registry, so calling one a new dependency
# is the same false claim `_toml_dep_names` excludes poetry's `python` to avoid.
# Applied to the whole composer name set, so the whole-document reader and the
# section reader cannot disagree about the same file.
_COMPOSER_PLATFORM_RE = re.compile(r"^(?:php(?:-64bit)?|hhvm|composer(?:-\w+-api)?|(?:ext|lib)-)")


def _composer_side_names(diff_text: str, marker: str) -> set[str]:
    """Composer require/require-dev names one side of the diff shows.

    A composer.json hunk is rarely the whole document, and a JSON fragment does
    not parse, so the structured reader alone would see nothing on an ordinary
    change. This rebuilds ONLY the require/require-dev runs the diff itself
    exposes into a synthetic document and hands THAT to the same
    ``_json_dep_names`` reader, so there is still one reader deciding what counts
    as a dependency.
    """
    from chameleon_mcp.knowledge.detect import _json_dep_names

    doc: dict[str, dict[str, str]] = {"require": {}, "require-dev": {}}
    section = ""
    for line in _side_text(diff_text, marker).splitlines():
        sm = _JSON_SECTION_OPEN_RE.match(line)
        if sm is not None:
            section = sm.group(1)
            continue
        if line.strip().startswith(("}", "]")):
            section = ""
            continue
        if section not in _COMPOSER_DEP_SECTIONS:
            continue
        for key, value in _INLINE_JSON_PAIR_RE.findall(line):
            doc[section][key] = value
    return _json_dep_names(json.dumps(doc), ("require", "require-dev"))


def _declared_names(manifest: str, text: str) -> set[str]:
    """Dependency names a manifest text DECLARES, via detect's own readers.

    Imported lazily: ``autopass`` imports this module for the basename sets
    alone, and ``knowledge.detect`` pulls the taxonomy loader in behind it.
    """
    from chameleon_mcp.knowledge.detect import _declared_deps, _with_pep503

    if manifest == "setup.cfg":
        return _with_pep503(_setupcfg_dep_names(text))
    return _declared_deps(manifest, text)


def _declared_side_names(manifest: str, diff_text: str, marker: str) -> set[str]:
    """Every dependency name one side of a manifest diff declares.

    The union over the raw reconstruction and (for TOML) the per-hunk repairs:
    a name is a name whichever reconstruction exposed it, and both sides are
    read the same way, so a repair that recovers a context-line dependency
    recovers it on both sides and cancels out of the difference.
    """
    from chameleon_mcp._thresholds import threshold_int

    cap = threshold_int("DEP_DIFF_MAX_BYTES")
    candidates = [_side_text(diff_text, marker)]
    if manifest in _TOML_ARM_MANIFESTS:
        candidates.extend(_toml_hunk_candidates(diff_text, marker))
    names: set[str] = set()
    for text in candidates:
        if not text.strip() or len(text) > cap:
            continue
        try:
            names |= _declared_names(manifest, text)
        except Exception:
            # Per-candidate best effort: a fragment the reader rejects
            # contributes nothing rather than losing the candidates beside it.
            continue
    if manifest == "composer.json":
        try:
            names |= _composer_side_names(diff_text, marker)
        except Exception:
            pass
        names = {n for n in names if not _COMPOSER_PLATFORM_RE.match(n)}
    return names


def _evidence_names(line: str) -> set[str]:
    """Name-shaped tokens on one diff line, in written and PEP 503 spellings.

    EVIDENCE ONLY. A finding's name always comes from the hardened reader; this
    just answers "which added line declares it", so an over-eager token here
    cannot become a dependency claim -- a token absent from the reader's set is
    simply never matched. Both a whole-line read and a per-token read are tried
    because a name ends differently per format (`flask>=3.0`, `"pkg@1"`,
    `com.foo:bar:1.0`, `github.com/a/b v1`).
    """
    from chameleon_mcp.knowledge.detect import _dep_name

    pieces = [line.strip()]
    pieces.extend(_QUOTED_TEXT_RE.findall(line))
    pieces.extend(p for p in _NAME_TOKEN_SPLIT_RE.split(line) if p)
    out: set[str] = set()
    for piece in pieces:
        name = _dep_name(piece)
        if name:
            out.add(name)
            out.add(_PEP503_RUN_RE.sub("-", name))
    return out


def _line_body(manifest: str, line: str) -> str:
    """The non-comment part of a line, in the manifest's own comment syntax.

    A commented-out `implementation "g:a:v"` or `# -e git+...` is the record of
    something REMOVED; matching a source rule on it would report the opposite of
    what the file says.
    """
    if manifest in _GRADLE_ARM_MANIFESTS:
        from chameleon_mcp.knowledge.detect import _strip_gradle_comments

        body, _ = _strip_gradle_comments(line)
        return body
    if manifest == "pom.xml":
        # A one-line `<!-- ... -->` is the shape a removed coordinate lingers in.
        # A multi-line block cannot be recognised from one line, so a rule inside
        # one is reported -- over-reporting a comment, never missing a live tag.
        return "" if line.lstrip().startswith("<!--") else line
    body = line
    for marker in _LINE_COMMENT_MARKERS.get(manifest, ()):
        cut = body.find(marker)
        if cut != -1:
            body = body[:cut]
    return body


# 2.5d source forms. Each pattern's ONE capturing group (or the whole match when
# it has none) is the source string echoed in the finding.
_PY_VCS_URL_RE = re.compile(r"\b((?:git|hg|bzr|svn)\+(?:https?|ssh|git|file)://\S+)")
_PY_DIRECT_REF_RE = re.compile(r"@\s*((?:https?|file|ssh)://\S+)")
_PY_BARE_URL_RE = re.compile(r"^\s*((?:https?|file)://\S+)\s*$")
_PY_EDITABLE_RE = re.compile(r"^\s*(?:-e|--editable)[\s=]+(\S+)")
_PY_INDEX_OPT_RE = re.compile(r"^\s*--?(?:index-url|extra-index-url|find-links|i|f)[\s=]+(\S+)")
_PY_SOURCE_TABLE_RE = re.compile(r"^\s*\[\[\s*(?:tool\.poetry\.source|source)\s*\]\]")
# `git = "..."` / `path = "..."` inside a dependency entry, both the inline-table
# form (`foo = { git = "..." }`) and the sub-table form (`[deps.foo]\ngit = ...`).
# A Cargo `path` pointing at a `.rs` file is a `[[bin]]`/`[lib]` target, not a
# dependency, so that spelling is excluded rather than reported as a local dep.
_TOML_GIT_SOURCE_RE = re.compile(r"""\bgit\s*=\s*["']([^"']+)["']""")
_TOML_PATH_SOURCE_RE = re.compile(r"""\bpath\s*=\s*["'](?![^"']*\.rs["'])([^"']+)["']""")
_CARGO_REGISTRY_RE = re.compile(r"""\bregistry\s*=\s*["']([^"']+)["']""")
_CARGO_PATCH_RE = re.compile(r"^\s*(\[patch[.\]][^\n]*)")
# A go.mod `replace` swaps a module for a fork or a working tree. The arrow is
# checked first because what it points AT is the source the build actually uses;
# the keyword form catches a one-line replace whose target sits on a later line,
# and it excludes a bare `replace (` block opener, which names no module.
_GO_REPLACE_ARROW_RE = re.compile(r"=>\s*(\S+)")
_GO_REPLACE_KEYWORD_RE = re.compile(r"^\s*replace\s+([^\s(]\S*)")
_COMPOSER_VCS_REPO_RE = re.compile(
    r'"type"\s*:\s*"(vcs|git|svn|hg|fossil|gitlab|github|bitbucket|composer)"'
)
_COMPOSER_PATH_REPO_RE = re.compile(r'"type"\s*:\s*"(path)"')
_COMPOSER_ARTIFACT_REPO_RE = re.compile(r'"type"\s*:\s*"(artifact|package)"')
_POM_REPOSITORY_RE = re.compile(r"^\s*<(repository|pluginRepository)>")
_POM_SYSTEM_PATH_RE = re.compile(r"<systemPath>\s*([^<]+)")
_GRADLE_REPO_URL_RE = re.compile(
    r"""\burl\s*[=(]?\s*(?:uri\s*\(\s*)?["']((?:https?|file)://[^"']+)["']"""
)
_GRADLE_FLATDIR_RE = re.compile(r"\b(flatDir)\b")
_GRADLE_FILE_DEP_RE = re.compile(
    r"\b(?:implementation|api|compile|compileOnly|compileClasspath|runtimeOnly"
    r"|testImplementation|testCompile|testRuntimeOnly|annotationProcessor|kapt|classpath)"
    r"""\s*[\s(]\s*(?:files|fileTree)\s*\(\s*["']([^"']+)["']"""
)

_SOURCE_KIND_MESSAGE: dict[str, str] = {
    "vcs-url": (
        "Dependency source {source!r} is a version-control URL, not the registry; the "
        "resolved code is not a published registry artifact."
    ),
    "direct-url": (
        "Dependency source {source!r} is a direct URL, not the registry; the resolved "
        "artifact is not registry-published."
    ),
    "local-path": (
        "Dependency source {source!r} is a local path, not the registry; the resolved "
        "code is not a published registry artifact."
    ),
    "editable-install": (
        "Editable install {source!r} resolves from a working tree or VCS checkout rather "
        "than the registry."
    ),
    "alternate-index": (
        "A non-default package index is configured ({source!r}); dependencies can resolve "
        "from a host other than the public registry."
    ),
    "alternate-repository": (
        "A non-default package repository is configured ({source!r}); dependencies can "
        "resolve from a host other than the public registry."
    ),
    "module-replacement": (
        "A replace directive redirects a module to {source!r}; the built code is not the "
        "module the version names."
    ),
    "patched-source": (
        "A patch table ({source!r}) overrides a registry dependency; the built code is not "
        "the registry artifact."
    ),
    "local-file-dependency": (
        "Dependency {source!r} is a local file or directory, not a registry artifact."
    ),
    "declared-repository": (
        "A {source!r} element declares a package repository other than the build's default; "
        "dependencies can resolve from a host outside the public registry."
    ),
}

# 2.5c hooks: manifest entries naming code that runs on install or build.
_PY_BUILD_BACKEND_RE = re.compile(r"""^\s*build-backend\s*=\s*["']([^"']+)["']""")
_CARGO_BUILD_SCRIPT_RE = re.compile(r"""^\s*build\s*=\s*["']([^"']+)["']""")
_COMPOSER_SCRIPT_KEYS = (
    "pre-install-cmd",
    "post-install-cmd",
    "pre-update-cmd",
    "post-update-cmd",
    "pre-autoload-dump",
    "post-autoload-dump",
    "post-root-package-install",
    "post-create-project-cmd",
    "pre-package-install",
    "post-package-install",
    "pre-package-update",
    "post-package-update",
)
_COMPOSER_HOOK_RE = re.compile(r'"(' + "|".join(_COMPOSER_SCRIPT_KEYS) + r')"\s*:')
_GRADLE_APPLY_FROM_RE = re.compile(r"""\bapply\s+from\s*[:=]\s*["'](https?://[^"']+)["']""")

_HOOK_KIND_MESSAGE: dict[str, str] = {
    "install-hook": (
        "New install-lifecycle script {hook!r} runs automatically on install; verify the "
        "command is intended."
    ),
    "build-script": (
        "New build script {hook!r} runs automatically on build; verify the script is intended."
    ),
    "build-backend": (
        "Build backend {hook!r} runs at build and install time; verify the backend is intended."
    ),
    "remote-build-script": (
        "Remote build script {hook!r} is fetched and executed at build time; verify the "
        "source is intended."
    ),
}


@dataclass(frozen=True)
class _Arm:
    """How one ecosystem's manifest is read for the three per-manifest checks.

    ``manifest`` is the key knowledge.detect reads the format under, ``noun``
    names one dependency in a message, and the two rule tuples are the 2.5d and
    2.5c line patterns. An empty rule tuple is a deliberate statement that the
    ecosystem has no such concept (Go and Maven declare no install hook), not an
    omission.

    ``coordinate_line`` marks a format that writes a whole dependency as ONE
    ``group:artifact:version`` token. detect's reader records both halves (a
    profile may name either), so without this a single added Gradle line would
    report two "new dependency" findings citing the same evidence; with it the
    halves are rejoined into the coordinate they came from.
    """

    manifest: str
    noun: str
    source_rules: tuple[tuple[re.Pattern[str], str], ...] = ()
    hook_rules: tuple[tuple[re.Pattern[str], str], ...] = ()
    coordinate_line: bool = False


_PY_COMMON_SOURCE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PY_VCS_URL_RE, "vcs-url"),
    (_PY_DIRECT_REF_RE, "direct-url"),
)
_TOML_DEP_SOURCE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TOML_GIT_SOURCE_RE, "vcs-url"),
    (_TOML_PATH_SOURCE_RE, "local-path"),
)

_ARMS: dict[str, _Arm] = {
    "requirements.txt": _Arm(
        manifest="requirements.txt",
        noun="Python dependency",
        source_rules=(
            *_PY_COMMON_SOURCE_RULES,
            (_PY_EDITABLE_RE, "editable-install"),
            (_PY_INDEX_OPT_RE, "alternate-index"),
            (_PY_BARE_URL_RE, "direct-url"),
        ),
    ),
    "pyproject.toml": _Arm(
        manifest="pyproject.toml",
        noun="Python dependency",
        source_rules=(
            *_PY_COMMON_SOURCE_RULES,
            *_TOML_DEP_SOURCE_RULES,
            (_PY_SOURCE_TABLE_RE, "alternate-index"),
        ),
        hook_rules=((_PY_BUILD_BACKEND_RE, "build-backend"),),
    ),
    "Pipfile": _Arm(
        manifest="Pipfile",
        noun="Python dependency",
        source_rules=(
            *_PY_COMMON_SOURCE_RULES,
            *_TOML_DEP_SOURCE_RULES,
            (_PY_SOURCE_TABLE_RE, "alternate-index"),
        ),
    ),
    "setup.cfg": _Arm(
        manifest="setup.cfg",
        noun="Python dependency",
        source_rules=(*_PY_COMMON_SOURCE_RULES, (_PY_BARE_URL_RE, "direct-url")),
    ),
    "go.mod": _Arm(
        manifest="go.mod",
        noun="Go module dependency",
        source_rules=(
            (_GO_REPLACE_ARROW_RE, "module-replacement"),
            (_GO_REPLACE_KEYWORD_RE, "module-replacement"),
        ),
    ),
    "Cargo.toml": _Arm(
        manifest="Cargo.toml",
        noun="crate dependency",
        source_rules=(
            *_TOML_DEP_SOURCE_RULES,
            (_CARGO_REGISTRY_RE, "alternate-index"),
            (_CARGO_PATCH_RE, "patched-source"),
        ),
        hook_rules=((_CARGO_BUILD_SCRIPT_RE, "build-script"),),
    ),
    "composer.json": _Arm(
        manifest="composer.json",
        noun="Composer package",
        source_rules=(
            (_COMPOSER_VCS_REPO_RE, "alternate-repository"),
            (_COMPOSER_PATH_REPO_RE, "local-path"),
            (_COMPOSER_ARTIFACT_REPO_RE, "local-file-dependency"),
        ),
        hook_rules=((_COMPOSER_HOOK_RE, "install-hook"),),
    ),
    "pom.xml": _Arm(
        manifest="pom.xml",
        noun="Maven coordinate",
        source_rules=(
            (_POM_REPOSITORY_RE, "declared-repository"),
            (_POM_SYSTEM_PATH_RE, "local-path"),
        ),
    ),
    "build.gradle": _Arm(
        manifest="build.gradle",
        noun="Gradle coordinate",
        source_rules=(
            (_GRADLE_FILE_DEP_RE, "local-file-dependency"),
            (_GRADLE_FLATDIR_RE, "local-file-dependency"),
            (_GRADLE_REPO_URL_RE, "alternate-repository"),
        ),
        hook_rules=((_GRADLE_APPLY_FROM_RE, "remote-build-script"),),
        coordinate_line=True,
    ),
}
_ARMS["build.gradle.kts"] = _Arm(
    manifest="build.gradle.kts",
    noun="Gradle coordinate",
    source_rules=_ARMS["build.gradle"].source_rules,
    hook_rules=_ARMS["build.gradle"].hook_rules,
    coordinate_line=True,
)


def _arm_for(path: str) -> _Arm | None:
    """The ecosystem arm that reads ``path``, or None when none does."""
    arm = _ARMS.get(path.rsplit("/", 1)[-1])
    if arm is not None:
        return arm
    return _ARMS["requirements.txt"] if _is_requirements_manifest(path) else None


def _scan_new_dependencies_generic(path: str, arm: _Arm, diff_text: str) -> list[DepFinding]:
    """2.5a: a dependency name this change introduced (NIT).

    New = declared after, minus declared before, minus anything still named on a
    removed line (a version bump shows the name on both sides). The finding is
    only emitted when the name is also visible on an ADDED line: that is what
    makes the evidence real, and it is also the guard that keeps a side the
    reader could not parse from turning every context-line dependency into a
    "new dependency" claim.
    """
    after = _declared_side_names(arm.manifest, diff_text, "+")
    if not after:
        return []
    new = after - _declared_side_names(arm.manifest, diff_text, "-")
    for raw in diff_text.splitlines():
        if not new:
            return []
        if raw.startswith("-") and not raw.startswith("---"):
            new -= _evidence_names(raw[1:])
    if not new:
        return []

    out: list[DepFinding] = []
    seen: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        # Line order, not sorted order: a coordinate reads `group:artifact`, and
        # alphabetical would rejoin those two halves backwards.
        matched = [n for n in _evidence_names(line) & new if n not in seen]
        matched.sort(key=lambda n: (line.find(n) if n in line else len(line), n))
        if not matched:
            continue
        groups = [":".join(matched)] if arm.coordinate_line and len(matched) > 1 else matched
        seen.update(matched)
        for name in groups:
            out.append(
                DepFinding(
                    check="new-dependency",
                    severity="NIT",
                    path=path,
                    evidence=line.strip(),
                    message=f"New {arm.noun} {name!r}; verify it is the intended package.",
                    detail={"name": name},
                )
            )
    return out


def _scan_nonregistry_source_generic(path: str, arm: _Arm, diff_text: str) -> list[DepFinding]:
    """2.5d: an added line declaring a dependency source outside the registry (FIX)."""
    out: list[DepFinding] = []
    seen: set[tuple[str, str]] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        body = _line_body(arm.manifest, line)
        if not body.strip():
            continue
        for pattern, kind in arm.source_rules:
            m = pattern.search(body)
            if m is None:
                continue
            source = (m.group(1) if m.lastindex else m.group(0)).strip()[:_SOURCE_TEXT_MAX]
            evidence = line.strip()
            if (kind, evidence) not in seen:
                seen.add((kind, evidence))
                out.append(
                    DepFinding(
                        check="non-registry-source",
                        severity="FIX",
                        path=path,
                        evidence=evidence,
                        message=_SOURCE_KIND_MESSAGE[kind].format(source=source),
                        detail={"kind": kind, "source": source},
                    )
                )
            # One finding per line: the rules are alternative descriptions of the
            # same source, not independent signals.
            break
    return out


def _scan_build_hooks_generic(path: str, arm: _Arm, diff_text: str) -> list[DepFinding]:
    """2.5c: an added manifest entry naming code that runs on install or build (FIX)."""
    out: list[DepFinding] = []
    seen: set[tuple[str, str]] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        body = _line_body(arm.manifest, line)
        if not body.strip():
            continue
        for pattern, kind in arm.hook_rules:
            m = pattern.search(body)
            if m is None:
                continue
            hook = (m.group(1) if m.lastindex else m.group(0)).strip()[:_SOURCE_TEXT_MAX]
            evidence = line.strip()
            if (kind, hook) not in seen:
                seen.add((kind, hook))
                out.append(
                    DepFinding(
                        check="install-script",
                        severity="FIX",
                        path=path,
                        evidence=evidence,
                        message=_HOOK_KIND_MESSAGE[kind].format(hook=hook),
                        detail={"kind": kind, "script": hook, "command": evidence},
                    )
                )
            break
    return out


def scan_dependency_diff(files: dict[str, str]) -> list[DepFinding]:
    """Scan a mapping of ``rel_path -> unified diff text`` for supply-chain signals.

    Each file is routed to the checks that apply to it by basename. Fails open:
    any per-file parse error contributes nothing rather than raising.
    """
    out: list[DepFinding] = []
    for path, diff_text in (files or {}).items():
        if not isinstance(path, str) or not isinstance(diff_text, str):
            continue
        try:
            base = path.rsplit("/", 1)[-1]
            if base == "package.json":
                out.extend(_scan_install_scripts(path, diff_text))
                out.extend(_scan_nonregistry_source_npm(path, diff_text))
                out.extend(_scan_new_dependencies_npm(path, diff_text))
                out.extend(_scan_minified_manifest(path, diff_text))
            elif base == "Gemfile" or base.endswith(".gemspec"):
                out.extend(_scan_nonregistry_source_gem(path, diff_text))
                out.extend(_scan_new_dependencies_gem(path, diff_text))
            elif base in (
                "package-lock.json",
                "npm-shrinkwrap.json",
                "yarn.lock",
                "pnpm-lock.yaml",
            ):
                out.extend(_scan_lockfile_hosts(path, diff_text, _DEFAULT_NPM_HOSTS))
            elif base == "Gemfile.lock":
                out.extend(_scan_lockfile_hosts(path, diff_text, _DEFAULT_GEM_HOSTS))
            else:
                arm = _arm_for(path)
                if arm is not None:
                    out.extend(_scan_new_dependencies_generic(path, arm, diff_text))
                    out.extend(_scan_nonregistry_source_generic(path, arm, diff_text))
                    out.extend(_scan_build_hooks_generic(path, arm, diff_text))
        except Exception:
            continue
    return out


def collect_dependency_findings(
    changed_paths, diff_fetcher: Callable[[str], str | None]
) -> list[DepFinding]:
    """Route changed paths to the parser, fetching each manifest/lockfile's diff.

    ``changed_paths`` is the repo-relative file list of a change set; only those
    whose basename is in :data:`MANIFEST_LOCKFILE_BASENAMES` (or a ``*.gemspec``
    or a ``requirements*.txt``, whose names vary) are fetched, so a non-manifest
    edit never triggers a git call. ``diff_fetcher`` returns the unified diff
    text for one path (the caller supplies the no-network git read). Fails open:
    a fetcher that raises or returns None contributes nothing.
    """
    files: dict[str, str] = {}
    for path in changed_paths or ():
        if not isinstance(path, str):
            continue
        base = path.rsplit("/", 1)[-1]
        if (
            base not in MANIFEST_LOCKFILE_BASENAMES
            and not base.endswith(".gemspec")
            and not _is_requirements_manifest(path)
        ):
            continue
        try:
            diff_text = diff_fetcher(path)
        except Exception:
            continue
        if isinstance(diff_text, str) and diff_text:
            files[path] = diff_text
    return scan_dependency_diff(files)


def render_findings(findings: list[DepFinding]) -> list[str]:
    """Sanitized advisory lines for the pr-review dependency section, FIX before NIT.

    Every line is passed through the chameleon-context sanitizer because the
    evidence is verbatim untrusted manifest/lockfile text. Returns [] for no
    findings.
    """
    if not findings:
        return []
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    order = {"FIX": 0, "NIT": 1}
    ordered = sorted(findings, key=lambda f: (order.get(f.severity, 2), f.path, f.check))
    lines: list[str] = []
    for f in ordered:
        lines.append(
            sanitize_for_chameleon_context(
                f"[{f.severity}] {f.check} ({f.path}): {f.message} | {f.evidence}"
            )
        )
    return lines
