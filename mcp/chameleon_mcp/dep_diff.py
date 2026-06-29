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
(``Gemfile``, ``Gemfile.lock``). Other-ecosystem lockfiles (``poetry.lock``,
``go.sum``, ``Cargo.lock``, ``composer.lock``) are out of scope and are not
fetched or parsed -- a change to one produces no findings, which reads as "not
covered", not "reviewed clean". The diff-fetch caller surfaces a truncation
signal when a manifest diff exceeds its byte cap so a partial parse is never
mistaken for full coverage.
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

# A Gemfile `gem` line carrying a non-registry source option (`git:`, `github:`,
# `path:`, or their hash-rocket forms). `gem "name"` opens the line.
_GEM_LINE_RE = re.compile(r'^\s*gem\s+["\']([^"\']+)["\']')
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
            value = _json_string_value(m.group(2))
            if value is not None and _looks_like_dep_value(value):
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


_REMOVED_JSON_KEY_RE = re.compile(r'^-\s*"([^"]+)"\s*:\s*(.*)$')


def _removed_npm_dep_names(diff_text: str) -> set[str]:
    """Dependency names on removed (`-`) lines -- the "previously present" baseline.

    A bump shows the name on both a `-` and a `+` line, so the name appearing
    here means an added line with the same name is a bump, not a new dependency.
    """
    names: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("-") or raw.startswith("---"):
            continue
        m = _REMOVED_JSON_KEY_RE.match(raw)
        if m is None:
            continue
        key, val = m.group(1), m.group(2)
        # Only the URL-bearing metadata keys are excluded; an install-script key
        # name ("install") IS a real package, so it is discriminated by VALUE
        # (a version range means a dependency, a command means a script) below.
        if key in _METADATA_URL_KEYS:
            continue
        value = _json_string_value(val)
        if value is not None and _looks_like_dep_value(value):
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


def _scan_minified_manifest(path: str, diff_text: str) -> list[DepFinding]:
    """Flag a package.json change minified to a single JSON line.

    The per-key scanners cannot decompose a one-line object, so they would
    silently return nothing. Emit one FIX so the reviewer knows the structural
    checks were skipped and the raw diff needs a manual read. A normal
    pretty-printed diff never has a full ``{...}`` object on one added line, so
    this does not fire on the common case.
    """
    for raw in _added_lines(diff_text):
        stripped = raw.strip().rstrip(",")
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
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
            elif base == "Gemfile":
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
    whose basename is in :data:`MANIFEST_LOCKFILE_BASENAMES` are fetched, so a
    non-manifest edit never triggers a git call. ``diff_fetcher`` returns the
    unified diff text for one path (the caller supplies the no-network git read).
    Fails open: a fetcher that raises or returns None contributes nothing.
    """
    files: dict[str, str] = {}
    for path in changed_paths or ():
        if not isinstance(path, str):
            continue
        if path.rsplit("/", 1)[-1] not in MANIFEST_LOCKFILE_BASENAMES:
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
