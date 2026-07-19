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
  2.5c new install lifecycle script: preinstall/install/postinstall (FIX)
  2.5d non-registry dependency source: git:/file:/http:/github:/path: (FIX)

Fails open by construction: an unparseable or empty diff yields no findings,
never a crash and never a fabricated finding.

Coverage boundary (deliberate): the parsed ecosystems are npm (``package.json``,
``package-lock.json``, ``npm-shrinkwrap.json``), yarn (``yarn.lock`` classic and
berry ``resolution:`` lines), pnpm (``pnpm-lock.yaml``), and Bundler
(``Gemfile``, ``Gemfile.lock``, and ``*.gemspec`` -- a gem declares its own
dependencies via ``add_dependency`` in the gemspec, not a Gemfile).
Dependency manifests of other ecosystems --
Python (``requirements*.txt``, ``pyproject.toml``, ``Pipfile``, ``setup.py``),
Go, Rust, PHP -- are NOT parsed. Rather than let a change to one read as
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
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
    }
)

# Dependency manifests of ecosystems this module does NOT parse. A change to one
# is not reviewed by the supply-chain checks above, so scan_dependency_changes
# surfaces it as `uncovered_manifests` -- an honest "not covered" the consumer
# can hand-review, never a silent "reviewed clean". Python is a first-class
# chameleon language whose manifests land here; the rest match the deliberate
# out-of-scope boundary named in the module docstring.
UNCOVERED_MANIFEST_BASENAMES = frozenset(
    {
        # Python
        "pyproject.toml",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
        # Go
        "go.mod",
        "go.sum",
        # Rust
        "Cargo.toml",
        "Cargo.lock",
        # PHP
        "composer.json",
        "composer.lock",
    }
)


def is_uncovered_manifest(path: str) -> bool:
    """True iff ``path`` is a dependency manifest of an ecosystem this module
    does not parse (Python / Go / Rust / PHP).

    Covered npm/Bundler manifests are never uncovered (they are parsed), so a
    caller checking both sets sees no overlap. A pip ``requirements*.txt`` is
    matched by pattern because the name varies (``requirements.txt``,
    ``requirements-dev.txt``, a ``requirements/base.txt`` under a requirements
    package).
    """
    base = path.rsplit("/", 1)[-1]
    if base in MANIFEST_LOCKFILE_BASENAMES:
        return False
    if base in UNCOVERED_MANIFEST_BASENAMES:
        return True
    if base.endswith(".txt") and (
        base.startswith("requirements") or "/requirements/" in f"/{path}"
    ):
        return True
    return False


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


_REMOVED_JSON_KEY_RE = re.compile(r'^-\s*"([^"]+)"\s*:\s*(.*)$')
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
        except Exception:
            continue
    return out


def collect_dependency_findings(
    changed_paths, diff_fetcher: Callable[[str], str | None]
) -> list[DepFinding]:
    """Route changed paths to the parser, fetching each manifest/lockfile's diff.

    ``changed_paths`` is the repo-relative file list of a change set; only those
    whose basename is in :data:`MANIFEST_LOCKFILE_BASENAMES` (or a ``*.gemspec``,
    whose name varies) are fetched, so a non-manifest edit never triggers a git
    call. ``diff_fetcher`` returns the unified diff text for one path (the caller
    supplies the no-network git read). Fails open: a fetcher that raises or
    returns None contributes nothing.
    """
    files: dict[str, str] = {}
    for path in changed_paths or ():
        if not isinstance(path, str):
            continue
        base = path.rsplit("/", 1)[-1]
        if base not in MANIFEST_LOCKFILE_BASENAMES and not base.endswith(".gemspec"):
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
