"""Convention schema, serialization, and extraction for Smart Injection."""

from __future__ import annotations

import heapq
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from chameleon_mcp._thresholds import threshold

if TYPE_CHECKING:
    from chameleon_mcp.extractors._base import ParsedFile

CONVENTIONS_SCHEMA_VERSION = 1


def _int_env(name: str, default: int) -> int:
    """Read a positive-int env override; else the default.

    For the "defaults ON for quality, opt out only for a reason" caps: the
    default surfaces all real signal, and the env var lets a repo bound it.
    """
    try:
        v = int(os.environ.get(name) or "")
    except ValueError:
        return default
    return v if v > 0 else default


# Floor below which an archetype's derivations (class_contract, inheritance,
# error_handling, required_guards, method_calls) are too thin a sample to trust.
# Env-overridable like the other sample-size floors (CALLABLE_SIGNATURE_MIN_FILES,
# IMPORT_ORDERING_MIN_SAMPLE, ...) so a repo smaller than the default can still
# derive these sections instead of getting {} on every archetype it has.
#
# The default was 10, which sat ABOVE the natural cohort size of a framework repo
# and so emptied these sections on ordinary codebases rather than small ones.
# Measured across five realistic repos: 13/13 archetypes below the floor on
# NestJS (largest cohort 8), 12/12 on DRF (largest 7), 15/16 on Django, 12/13 on
# Next.js, 9/13 on Rails. A 6-8 member role cohort IS the unit of those
# codebases; treating it as an untrustworthy sample means the product's core
# output is near-empty for most users.
#
# 5 is chosen from measurement, not preference. Re-deriving one NestJS repo at
# each floor gave 7 populated archetype-sections at 10, 14 at 5 (recovering
# class_contract and key_exports), and 16 at 4 -- the last adding only two more
# key_exports while dropping into genuinely coincidental sample sizes. It also
# matches MIN_SAMPLE_SIZE_NAMING below: both answer the same question -- how many
# siblings make a convention trustworthy -- so they should not disagree.
MIN_SAMPLE_SIZE = _int_env("CHAMELEON_MIN_SAMPLE_SIZE", 5)
MIN_SAMPLE_SIZE_NAMING = 5

# Conventions sections keyed at the repo level rather than by archetype name.
# Every other section in the conventions block (see empty_conventions) is keyed
# by archetype, so an archetype rename must remap those keys and leave these
# untouched. Source of truth for apply_archetype_renames' rekey loop.
REPO_LEVEL_CONVENTION_SECTIONS = frozenset({"layering", "repo_imports"})


def empty_conventions(*, generation: int) -> dict:
    return {
        "schema_version": CONVENTIONS_SCHEMA_VERSION,
        "generation": generation,
        "min_sample_size": MIN_SAMPLE_SIZE,
        "conventions": {
            "imports": {},
            # Repo-level (not per-archetype): modules a strong majority of the
            # whole corpus imports. Advisory context only -- never a source for
            # the import-preference enforcement rule, which stays archetype-
            # scoped. Fills the gap where every cluster sits under the sample
            # floor so no per-archetype imports entry can derive.
            "repo_imports": {},
            "import_ordering": {},
            "naming": {},
            "inheritance": {},
            "method_calls": {},
            "key_exports": {},
            "body_shape": {},
            "required_guards": {},
            "error_handling": {},
            "doc_coverage": {},
            "test_pairing": {},
            "callable_signatures": {},
            # Repo-level (not per-archetype): forbidden-upward cluster edges and a
            # static import-cycle report. Advisory context for status/PR-review.
            "layering": {},
            # Per-archetype class-body contract: the DSL macros, class decorators,
            # required methods, and base that the archetype's classes share. Surfaces
            # what a base class implies beyond its name (e.g. ActiveInteraction's
            # typed filters + #execute), which inheritance/method_calls miss.
            "class_contract": {},
        },
    }


def serialize_conventions(conventions: dict) -> str:
    return json.dumps(conventions, indent=2, sort_keys=False, ensure_ascii=False)


def merge_taught_competing(prior: dict, new: dict) -> None:
    """Carry user-taught banned imports across a re-derive, mutating ``new``.

    ``extract_all_conventions`` only derives the ``preferred`` import lists; the
    ``competing`` entries under ``conventions.imports.<archetype>`` are added by
    /chameleon-teach and have no derived source, so a refresh would otherwise drop
    them and silently disable banned-import enforcement. Both args use the on-disk
    shape ``{"conventions": {"imports": {<arch>: {"preferred", "competing"}}}}``.
    Competing entries already present in ``new`` are kept; missing ones are
    appended; duplicates (same over/preferred pair) are not re-added.
    """
    prior_imports = (prior or {}).get("conventions", {}).get("imports", {})
    if not isinstance(prior_imports, dict) or not prior_imports:
        return
    new_imports = new.setdefault("conventions", {}).setdefault("imports", {})
    for archetype, entry in prior_imports.items():
        competing = (entry or {}).get("competing") or []
        if not isinstance(competing, list) or not competing:
            continue
        dst = new_imports.setdefault(archetype, {"preferred": [], "competing": []})
        if not isinstance(dst.get("competing"), list):
            dst["competing"] = []
        seen = {
            (c.get("over"), c.get("preferred")) for c in dst["competing"] if isinstance(c, dict)
        }
        for c in competing:
            if isinstance(c, dict) and (c.get("over"), c.get("preferred")) not in seen:
                dst["competing"].append(c)
                seen.add((c.get("over"), c.get("preferred")))


_FRAMEWORK_THRESHOLD = 0.80
_MIN_PREFERRED_COUNT = 10
_MIN_COMPETING_COUNT = 5

_FRAMEWORK_MODULES = frozenset(
    {
        "react",
        "react-dom",
        "vue",
        "svelte",
        "next",
        "nuxt",
        "@angular/core",
        "@angular/common",
        "solid-js",
        "preact",
    }
)


def extract_import_conventions(
    files: list[ParsedFile],
    *,
    competing_pairs: list[tuple[str, str]] | None = None,
) -> dict:
    """Extract import conventions from a cluster of ParsedFile objects.

    Returns {"preferred": [...], "competing": [...]}.
    - preferred: modules imported frequently but not ubiquitously (framework noise).
    - competing: pairs where a wrapper dominates and the raw import is rare/absent.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {"preferred": [], "competing": []}

    total = len(files)
    module_counts: Counter[str] = Counter()
    for f in files:
        seen_in_file: set[str] = set()
        for module, _kind in f.import_specifiers:
            if module not in seen_in_file:
                module_counts[module] += 1
                seen_in_file.add(module)

    competing: list[dict] = []
    if competing_pairs:
        for preferred_mod, over_mod in competing_pairs:
            p_count = module_counts.get(preferred_mod, 0)
            o_count = module_counts.get(over_mod, 0)
            if p_count >= _MIN_COMPETING_COUNT and o_count <= 2:
                competing.append(
                    {
                        "preferred": preferred_mod,
                        "over": over_mod,
                        "preferred_count": p_count,
                        "over_count": o_count,
                    }
                )

    preferred: list[dict] = []
    for module, count in module_counts.most_common():
        if count / total > _FRAMEWORK_THRESHOLD and module in _FRAMEWORK_MODULES:
            continue
        if count < _MIN_PREFERRED_COUNT:
            continue
        preferred.append({"module": module, "source": module, "frequency": count, "total": total})

    return {"preferred": preferred, "competing": competing}


# Share of import-bearing files that must import a module before it counts as a
# repo-wide preference. A strong-majority bar, deliberately higher than any
# per-cluster frequency band: a repo-wide line speaks for every archetype at
# once, so only near-ubiquitous modules qualify.
_REPO_WIDE_PREFERRED_SHARE = 0.60


def extract_repo_wide_import_conventions(files: list[ParsedFile]) -> dict:
    """Repo-wide preferred imports over the WHOLE parsed corpus.

    The per-archetype pass (:func:`extract_import_conventions`) is cluster
    scoped, so a repo whose every cluster sits under ``MIN_SAMPLE_SIZE`` derives
    no imports entry at all -- even when nearly every file imports the same
    module (``__future__`` in a from-scratch Python repo). This pass runs over
    all parsed files and keeps only modules imported by a strong majority
    (``_REPO_WIDE_PREFERRED_SHARE``) of the files that import anything, with the
    same framework-noise skip the cluster pass applies. Returns
    ``{"preferred": [...]}``; ADVISORY ONLY -- the result renders into the
    conventions block marked repo-wide and never feeds the archetype-scoped
    import-preference enforcement rule.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {"preferred": []}

    module_counts: Counter[str] = Counter()
    files_with_imports = 0
    for f in files:
        modules = {module for module, _kind in getattr(f, "import_specifiers", ())}
        if not modules:
            continue
        files_with_imports += 1
        for module in modules:
            module_counts[module] += 1
    if files_with_imports < MIN_SAMPLE_SIZE:
        return {"preferred": []}

    preferred: list[dict] = []
    for module, count in module_counts.most_common():
        share = count / files_with_imports
        if share > _FRAMEWORK_THRESHOLD and module in _FRAMEWORK_MODULES:
            continue
        if share < _REPO_WIDE_PREFERRED_SHARE:
            continue
        preferred.append(
            {"module": module, "source": module, "frequency": count, "total": files_with_imports}
        )
    return {"preferred": preferred}


def _import_group(module: str) -> str:
    """Classify an import specifier as ``external`` or ``relative``.

    Relative covers on-disk paths (``./x``, ``../x``) and the common alias roots
    teams point at their own ``src`` (``@/``, ``~/``). Everything else — bare
    package names and scoped packages (``react``, ``@scope/pkg``) — is external.
    Ruby ``require_relative`` targets and ``require './...'`` paths land in
    relative the same way.
    """
    if module.startswith((".", "@/", "~/", "#/")):
        return "relative"
    return "external"


def _import_group_signature(import_specifiers: tuple[tuple[str, str], ...]) -> str | None:
    """Return this file's external-vs-relative grouping order, or None.

    Walks the imports in on-disk order, collapsing consecutive same-group runs
    so the result is the partition shape (``external-then-relative``,
    ``relative-then-external``, or a single group). Returns None when the file
    has no imports — those carry no ordering signal and must not count toward or
    against the partition vote. An interleaved file (external, relative,
    external) yields its full run sequence so it reads as divergent from a clean
    two-group layout rather than collapsing to a tidy one.
    """
    groups: list[str] = []
    for module, _kind in import_specifiers:
        group = _import_group(module)
        if not groups or groups[-1] != group:
            groups.append(group)
    if not groups:
        return None
    return "-then-".join(groups)


def extract_import_ordering_conventions(files: list[ParsedFile]) -> dict:
    """Derive the archetype's dominant import grouping order.

    Counts each file's external-vs-relative partition signature and records the
    dominant one when a clear majority of the import-bearing files share it.
    Advisory-only: import ordering is high-variance and competes with the
    deterministic formatters teams already run in CI, so this never blocks — it
    grounds a pr-review NIT with the actual sibling count.

    Returns ``{}`` below the sample floor or when no partition clears the
    frequency floor; otherwise ``{"pattern", "frequency", "matching", "total"}``.
    """
    min_sample = int(threshold("IMPORT_ORDERING_MIN_SAMPLE"))
    signatures: list[str] = []
    for f in files:
        sig = _import_group_signature(getattr(f, "import_specifiers", ()))
        if sig is not None:
            signatures.append(sig)
    total = len(signatures)
    if total < min_sample:
        return {}
    counts = Counter(signatures)
    pattern, matching = counts.most_common(1)[0]
    frequency = matching / total
    if frequency < threshold("IMPORT_ORDERING_FREQUENCY"):
        return {}
    return {
        "pattern": pattern,
        "frequency": round(frequency, 3),
        "matching": matching,
        "total": total,
    }


_TS_INTERFACE_NAME_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Z]\w*)", re.MULTILINE)
_TS_TYPE_NAME_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Z]\w*)\s*[=<]", re.MULTILINE)
_TS_ENUM_NAME_RE = re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Z]\w*)", re.MULTILINE)


# Ruby in-source declaration names, for casing-convention derivation. The
# method capture accepts any identifier start so a PascalCase `def FetchData`
# is still measurable; operator defs (`def ==`) carry no casing signal and are
# excluded by the identifier-start requirement. The class capture requires the
# uppercase start Ruby itself enforces; the permissive lint-side capture that
# must SEE a lowercase class name lives in lint_engine.
_RUBY_DECL_METHOD_RE = re.compile(
    r"^[ \t]*def\s+(?:self\s*\.\s*)?([a-zA-Z_]\w*[!?=]?)", re.MULTILINE
)
_RUBY_DECL_CLASS_RE = re.compile(r"^[ \t]*(?:class|module)\s+([A-Z]\w*(?:::\w+)*)", re.MULTILINE)
# Constant assignment; `[^=~]` keeps `==` / `=~` comparisons out.
_RUBY_DECL_CONSTANT_RE = re.compile(r"^[ \t]*([A-Z]\w*)\s*=[^=~]", re.MULTILINE)
# Python in-source declarations for casing derivation: any def/class name at any
# indent (the classifier buckets the casing). Methods and free functions share
# the snake_case rule, so both feed the "method" category; classes feed "class".
_PY_DECL_FUNCTION_RE = re.compile(r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE)
_PY_DECL_CLASS_RE = re.compile(r"^[ \t]*class\s+([A-Za-z_]\w*)", re.MULTILINE)


def extract_declarations_from_content(content: str, *, language: str) -> dict[str, list[str]]:
    """Extract declaration names from file content for naming derivation.

    TypeScript returns {"interface": [...], "type": [...], "enum": [...]}
    (prefix conventions). Ruby returns {"method": [...], "class": [...],
    "constant": [...]} (casing conventions), scanned over a strings/comments/
    heredoc-stripped copy so generator templates and docs don't pollute the
    measurement. Other languages return an empty dict.
    """
    result: dict[str, list[str]] = {}
    if language == "typescript":
        interfaces = _TS_INTERFACE_NAME_RE.findall(content)
        if interfaces:
            result["interface"] = interfaces
        types = _TS_TYPE_NAME_RE.findall(content)
        if types:
            result["type"] = types
        enums = _TS_ENUM_NAME_RE.findall(content)
        if enums:
            result["enum"] = enums
        return result
    if language == "ruby":
        # Local import: lint_engine imports from this module at load time.
        from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments

        scan = _strip_ruby_strings_and_comments(content)
        methods = _RUBY_DECL_METHOD_RE.findall(scan)
        if methods:
            result["method"] = methods
        classes = _RUBY_DECL_CLASS_RE.findall(scan)
        if classes:
            result["class"] = classes
        constants = _RUBY_DECL_CONSTANT_RE.findall(scan)
        if constants:
            result["constant"] = constants
        return result
    if language == "python":
        from chameleon_mcp.lint_engine import _strip_python_strings_and_comments

        scan = _strip_python_strings_and_comments(content)
        # def names feed "method" (snake_case, shared with Ruby); class names feed
        # "class" (PascalCase). Module-level constants are intentionally NOT
        # derived: a lowercase module var (logger = ...) is valid PEP 8, so a
        # constant-casing rule would false-flag it.
        functions = _PY_DECL_FUNCTION_RE.findall(scan)
        if functions:
            result["method"] = functions
        classes = _PY_DECL_CLASS_RE.findall(scan)
        if classes:
            result["class"] = classes
        return result
    return result


# Public declarations whose leading doc comment we measure for doc_coverage.
# TS: top-level exported function/class/const/interface/type/enum (re-export
# `export {x}` / `export * from` lines carry no declaration body and are
# excluded). The capture group is only used to keep the alternation anchored.
_TS_PUBLIC_DECL_RE = re.compile(
    r"^export\s+"
    r"(?:default\s+)?"
    r"(?:abstract\s+|async\s+)*"
    r"(function\b|class\b|const\s+\w|let\s+\w|var\s+\w|interface\b|type\s+\w|enum\b)"
)
# Ruby: a method definition (`def foo`) at any indent. Visibility (public vs a
# private/protected section) is tracked separately while walking the lines.
_RUBY_DEF_RE = re.compile(r"^\s*def\s+[\w.]+")
# A bare `private` / `protected` / `public` line flips the current visibility for
# the rest of the enclosing body. `private :sym` / `private def` forms name a
# single target and do NOT open a section, so they must not flip the flag.
_RUBY_VISIBILITY_RE = re.compile(r"^\s*(private|protected|public)\s*(#.*)?$")
_RUBY_SCOPE_OPEN_RE = re.compile(r"^\s*(class|module)\b")
# Python: a public top-level/nested def or class (name not underscore-prefixed).
# The docstring is the FIRST statement of the body (the line AFTER the header),
# unlike TS/Ruby where the doc comment sits ABOVE the declaration.
_PY_PUBLIC_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)")
_PY_DOCSTRING_START_RE = re.compile(r"""^[rRbBuUfF]{0,2}('''|\"\"\"|'|")""")
# Backstop on the header-end scan. Generated DRF/Django signatures can run to
# dozens of parameter lines, so this is wide; the next-def/class guard, not this
# cap, is what actually stops a one-liner from borrowing a sibling's docstring.
_PY_HEADER_SCAN_MAX_LINES = 200


def _py_decl_has_docstring(lines: list[str], decl_index: int) -> bool:
    """True if the Python def/class at ``decl_index`` opens with a docstring.

    Finds the end of the (possibly multi-line) header — the first line ending in
    ``:`` — then checks whether the first non-blank body line is a string
    literal. The real terminator is the next def/class (so a one-liner without a
    docstring can't borrow a sibling's); the line cap is only a backstop, kept
    wide enough that a long multi-line signature still reaches its colon.
    """
    end = min(len(lines), decl_index + _PY_HEADER_SCAN_MAX_LINES)
    i = decl_index
    while i < end:
        stripped = lines[i].split("#", 1)[0].rstrip()
        if stripped.endswith(":"):
            break
        if i > decl_index and _PY_PUBLIC_DEF_RE.match(lines[i]):
            return False
        i += 1
    else:
        return False
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return False
    return bool(_PY_DOCSTRING_START_RE.match(lines[j].strip()))


def _ts_decl_has_leading_doc(lines: list[str], decl_index: int) -> bool:
    """True if the TS declaration at ``decl_index`` has a leading doc comment.

    Scans upward past blank lines, then accepts either a JSDoc block ending in
    ``*/`` or a run of ``//`` line comments. A decorator line (``@Foo``) sitting
    between the comment and the declaration is transparent — the comment still
    documents the declaration below it.
    """
    i = decl_index - 1
    # Skip blank lines and decorators directly above the declaration.
    while i >= 0 and (not lines[i].strip() or lines[i].lstrip().startswith("@")):
        i -= 1
    if i < 0:
        return False
    above = lines[i].strip()
    if above.endswith("*/"):
        return True
    return above.startswith("//")


def _ruby_def_has_leading_doc(lines: list[str], def_index: int) -> bool:
    """True if the Ruby def at ``def_index`` has a leading ``#`` comment run.

    Skips blank lines upward, then requires the nearest non-blank line to be a
    ``#`` comment. A magic/encoding comment is still a leading comment for this
    purpose; the precision cost is negligible against the dominance gate.
    """
    i = def_index - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i < 0:
        return False
    return lines[i].lstrip().startswith("#")


def compute_doc_coverage_from_content(content: str, *, language: str) -> tuple[int, int]:
    """Count (documented, public) declarations in one file's content.

    Public declarations are the surface a sibling reviewer expects documented:
    TS top-level exports and Ruby public method definitions. ``documented`` is
    the subset carrying an immediately-preceding doc comment. Returns (0, 0) for
    an unsupported language or a file with no public surface so the caller can
    skip it without special-casing.
    """
    lines = content.splitlines()
    documented = 0
    public = 0
    if language == "typescript":
        for idx, line in enumerate(lines):
            if _TS_PUBLIC_DECL_RE.match(line):
                public += 1
                if _ts_decl_has_leading_doc(lines, idx):
                    documented += 1
    elif language == "ruby":
        # Local import: lint_engine imports from this module at load time.
        from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments

        # Detect declarations on a strings/comments/heredoc-stripped copy so a
        # `def`/`private` inside a heredoc template isn't counted — the same
        # exclusion the naming derivation applies. The strip is length-preserving
        # (bodies blanked to spaces, newlines kept), so indices align with the
        # original; the leading-comment check then reads the ORIGINAL lines,
        # since the stripper blanks the very `#` comment that documents the def.
        scan_lines = _strip_ruby_strings_and_comments(content).splitlines()
        # Visibility is per class/module body. A new class/module body and the
        # file top both reset to public; a bare `private`/`protected` flips it
        # until the next reset. This is the same private-section tracking the
        # AST would do, approximated line-wise.
        visibility = "public"
        for idx, line in enumerate(scan_lines):
            if _RUBY_SCOPE_OPEN_RE.match(line):
                visibility = "public"
                continue
            vis_match = _RUBY_VISIBILITY_RE.match(line)
            if vis_match:
                visibility = vis_match.group(1)
                continue
            if _RUBY_DEF_RE.match(line):
                if visibility != "public":
                    continue
                public += 1
                if _ruby_def_has_leading_doc(lines, idx):
                    documented += 1
    elif language == "python":
        from chameleon_mcp.lint_engine import _strip_python_strings_and_comments

        # Detect declarations on a strings/comments-stripped copy so a
        # `def`/`class` inside a triple-quoted code-generation template isn't
        # counted as public surface — consistent with the naming derivation. The
        # strip is length-preserving, so indices align with the original; the
        # docstring check reads the ORIGINAL lines, since the stripper blanks the
        # very docstring it would look for.
        scan_lines = _strip_python_strings_and_comments(content).splitlines()
        # Public surface = def/class whose name is not underscore-prefixed;
        # documented = opens with a docstring (the first body statement).
        for idx, line in enumerate(scan_lines):
            m = _PY_PUBLIC_DEF_RE.match(line)
            if not m or m.group(2).startswith("_"):
                continue
            public += 1
            if _py_decl_has_docstring(lines, idx):
                documented += 1
    return documented, public


def extract_doc_coverage_conventions(
    coverage_by_file: list[tuple[int, int]],
) -> dict:
    """Aggregate per-file (documented, public) counts into an archetype norm.

    ``coverage_by_file`` is one ``(documented, public)`` pair per member file.
    Records the pooled fraction only when the archetype carries enough public
    declarations to trust the figure AND the fraction clears the dominance
    floor; otherwise returns ``{}`` so the archetype emits nothing. Advisory-
    only: a missing doc comment is a NIT, never a block.
    """
    total_public = sum(public for _doc, public in coverage_by_file)
    total_doc = sum(doc for doc, _public in coverage_by_file)
    if total_public < int(threshold("DOC_COVERAGE_MIN_DECLS")):
        return {}
    fraction = total_doc / total_public
    if fraction < threshold("DOC_COVERAGE_FREQUENCY"):
        return {}
    return {
        "fraction": round(fraction, 3),
        "documented": total_doc,
        "public": total_public,
    }


# Test-pairing derivation. We measure, per archetype, what fraction of the
# archetype's non-test source files ship with a test at a derived path, and
# record the dominant source->test path-mapping convention that fraction was
# computed under. This grounds a "where is the test" advisory in the archetype's
# own observed pairing rate instead of free-text prose.
#
# A source file is paired when a test exists at one of the candidate paths the
# repo's conventions imply. Two families per language:
#   - co-located: foo.ts -> foo.test.ts / foo.spec.ts; app/x.rb -> app/x_spec.rb
#     / app/x_test.rb (the test sits next to the source).
#   - mirrored-tree: src/a/foo.ts -> test|tests|__tests__/a/foo.test.ts;
#     app/models/x.rb -> spec/models/x_spec.rb / test/models/x_test.rb (the test
#     tree mirrors the source tree under a top-level test root).
# Each candidate is labelled so the derivation can report which mapping the
# archetype actually follows, not just that some test exists.

# A file that is itself a test must not be counted as source needing a test.
# Mirrors the canonical-pool test exclusions (kept local so conventions.py stays
# decoupled from discovery): a test/spec/stories leaf name, or any test-root path
# component.
_TEST_BASENAME_RE = re.compile(r"\.(test|spec|stories|fixture)\.[A-Za-z0-9]+$")
_RUBY_TEST_BASENAME_RE = re.compile(r"_(spec|test)\.rb$")
# pytest/unittest: test_<x>.py (dominant), <x>_test.py, conftest.py, and Django
# startapp's default bare tests.py / test.py.
_PY_TEST_BASENAME_RE = re.compile(r"^(test_.+|.+_test|conftest|tests?)\.pyi?$")
_TEST_DIR_COMPONENTS = frozenset({"__tests__", "test", "tests", "spec", "specs", "cypress", "e2e"})
# Top-level roots a mirrored test tree commonly lives under, paired with the
# basename transform that turns a source stem into its test stem there.
_TS_MIRROR_ROOTS = ("test", "tests", "__tests__", "spec")
_RUBY_MIRROR_ROOTS = ("spec", "test")
# The roots that hold the source tree a mirrored test root mirrors. A source path
# starting with one of these has that segment swapped for a test root; a path that
# starts elsewhere is mirrored by prefixing the test root.
_TS_SOURCE_ROOTS = ("src", "app", "lib")
_RUBY_SOURCE_ROOTS = ("app", "lib")


def _is_test_path(rel_path: str, *, language: str) -> bool:
    """True if ``rel_path`` is itself a test/spec/story file, not source.

    Checks the leaf basename against the language's test-naming pattern and any
    path component against the test-root denylist, so both a co-located
    ``foo.test.ts`` and a mirrored ``spec/models/x_spec.rb`` read as tests and
    are dropped from the source pool.
    """
    p = rel_path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if language == "ruby":
        if _RUBY_TEST_BASENAME_RE.search(name):
            return True
    elif language == "python":
        if _PY_TEST_BASENAME_RE.match(name):
            return True
    elif _TEST_BASENAME_RE.search(name):
        return True
    parts = p.split("/")
    return any(part in _TEST_DIR_COMPONENTS for part in parts)


def _candidate_test_paths(rel_path: str, *, language: str) -> list[tuple[str, str]]:
    """Derive (mapping_label, candidate_test_relpath) pairs for a source file.

    ``rel_path`` is the source file's repo-relative POSIX path. The returned
    candidates cover the co-located and mirrored-tree conventions for the
    language; existence of any one means the source file is paired, and the label
    of the matched candidate is the mapping convention that pairing followed.
    """
    p = rel_path.replace("\\", "/")
    parts = p.split("/")
    name = parts[-1]
    dir_parts = parts[:-1]
    dot = name.rfind(".")
    if dot <= 0:
        return []
    stem, ext = name[:dot], name[dot:]
    candidates: list[tuple[str, str]] = []

    def _join(segments: list[str]) -> str:
        return "/".join(s for s in segments if s)

    if language == "python":
        # Co-located: x.py -> test_x.py (pytest dominant) / x_test.py.
        candidates.append(("co-located test_", _join(dir_parts + [f"test_{stem}{ext}"])))
        candidates.append(("co-located _test", _join(dir_parts + [f"{stem}_test{ext}"])))
        # Nested per-app tests/ package sibling to the source's own directory:
        # myapp/views.py -> myapp/tests/test_views.py. This is the dominant
        # Django/pytest layout (the analogue of the TS __tests__ sibling).
        candidates.append(
            ("nested tests/ test_", _join(dir_parts + ["tests", f"test_{stem}{ext}"]))
        )
        candidates.append(
            ("nested tests/ _test", _join(dir_parts + ["tests", f"{stem}_test{ext}"]))
        )
        # Mirrored tests/ tree: swap a leading source root for the test root, else
        # prefix it. The test stem keeps the pytest test_ prefix.
        for root in ("tests", "test"):
            if dir_parts and dir_parts[0] in ("src", "app", "lib"):
                mirror = [root] + dir_parts[1:]
            else:
                mirror = [root] + dir_parts
            candidates.append((f"mirrored {root}/.../test_", _join(mirror + [f"test_{stem}{ext}"])))
        # Mid-path source-root swap: the source root (`app`/`src`/`lib`) can sit
        # UNDER a project/monorepo prefix rather than lead the path -- the tiangolo
        # full-stack-fastapi layout roots code at `backend/app/` and its tests at
        # `backend/tests/` (backend/app/api/routes/items.py ->
        # backend/tests/api/routes/test_items.py). Swap the FIRST app/src/lib
        # component (at any depth) for the test root, keeping the prefix before it
        # and the package path after it. Only when it is not already the leading
        # component (that case is the leading-swap mirror above).
        for i, seg in enumerate(dir_parts):
            if i > 0 and seg in ("src", "app", "lib"):
                for root in ("tests", "test"):
                    swapped = dir_parts[:i] + [root] + dir_parts[i + 1 :]
                    candidates.append(
                        (f"mid-path {seg}->{root}", _join(swapped + [f"test_{stem}{ext}"]))
                    )
                break
        # Django per-app single test module: a whole app's tests live in one
        # tests.py beside the source (myapp/models.py -> myapp/tests.py). The
        # classic (pre-pytest) Django layout the mirror above never reaches.
        if stem != "tests":
            candidates.append(("django app tests.py", _join(dir_parts + ["tests.py"])))
        # Top-level-package layout: a project whose code lives under a package dir
        # named after the project (flaskbb/utils/helpers.py) roots its tests at
        # tests/ mirroring the package's INTERNAL structure, dropping the package
        # dir itself and often grouping by test type. Strip the first component and
        # try the direct mirror plus the dominant pytest group intermediates, so
        # this layout is not measured as testless (the `src|app|lib` swap above
        # only fires when the root is one of those literal names). Existence-gated
        # like every candidate, so a non-matching guess costs nothing.
        #
        # Require a subpackage (dir_parts >= 2): stripping the first component of a
        # bare top-level module (appA/models.py, appB/models.py) collapses BOTH to
        # `tests/test_models.py`, so a shared test would false-pair to both apps.
        # A subpackage keeps the discriminating inner path, and the flaskbb case
        # this exists for (flaskbb/utils/helpers.py) always has one.
        if len(dir_parts) >= 2:
            inner = dir_parts[1:]
            for root in ("tests", "test"):
                candidates.append(
                    (f"pkg-stripped {root}/", _join([root] + inner + [f"test_{stem}{ext}"]))
                )
                for group in ("unit", "functional", "integration"):
                    candidates.append(
                        (
                            f"pkg-stripped {root}/{group}/",
                            _join([root, group] + inner + [f"test_{stem}{ext}"]),
                        )
                    )
        # Flat test root: tests/test_db.py for src/coldchain/db.py, with no
        # mirrored subtree. Same rationale (and same over-pairing trade) as the
        # TypeScript flat candidate below.
        for root in ("tests", "test"):
            candidates.append((f"flat {root}/test_", _join([root, f"test_{stem}{ext}"])))
            candidates.append((f"flat {root}/_test", _join([root, f"{stem}_test{ext}"])))
        return candidates

    if language == "ruby":
        # Co-located: x.rb -> x_spec.rb / x_test.rb next to the source.
        for suffix, label in (("_spec", "co-located _spec.rb"), ("_test", "co-located _test.rb")):
            candidates.append((label, _join(dir_parts + [f"{stem}{suffix}{ext}"])))
        # Mirrored: app/models/x.rb -> spec/models/x_spec.rb (swap the source
        # root for the test root) or, when no source root leads, prefix it.
        for root in _RUBY_MIRROR_ROOTS:
            suffix = "_spec" if root == "spec" else "_test"
            label = f"mirrored {root}/.../{suffix}.rb"
            if dir_parts and dir_parts[0] in _RUBY_SOURCE_ROOTS:
                mirror = [root] + dir_parts[1:]
            else:
                mirror = [root] + dir_parts
            candidates.append((label, _join(mirror + [f"{stem}{suffix}{ext}"])))
        # Flat test root: spec/router_spec.rb for lib/freightline/router.rb, with
        # no mirrored subtree. Same rationale (and same over-pairing trade) as the
        # TypeScript flat candidate below.
        for root in _RUBY_MIRROR_ROOTS:
            suffix = "_spec" if root == "spec" else "_test"
            candidates.append((f"flat {root}/{suffix}", _join([root, f"{stem}{suffix}{ext}"])))
        return candidates

    # TypeScript / JavaScript.
    # Co-located: foo.ts -> foo.test.ts / foo.spec.ts next to the source.
    for marker, label in ((".test", "co-located .test"), (".spec", "co-located .spec")):
        candidates.append((label, _join(dir_parts + [f"{stem}{marker}{ext}"])))
    # Co-located __tests__ sibling dir: src/foo.ts -> src/__tests__/foo.test.ts.
    candidates.append(("__tests__ sibling", _join(dir_parts + ["__tests__", f"{stem}.test{ext}"])))
    # Mirrored: src/a/foo.ts -> test/a/foo.test.ts (swap the source root) or,
    # when no source root leads, prefix the test root.
    for root in _TS_MIRROR_ROOTS:
        label = f"mirrored {root}/.../.test"
        if dir_parts and dir_parts[0] in _TS_SOURCE_ROOTS:
            mirror = [root] + dir_parts[1:]
        else:
            mirror = [root] + dir_parts
        candidates.append((label, _join(mirror + [f"{stem}.test{ext}"])))
    # Flat test root: every test directly under tests/ with no mirrored subtree
    # (src/services/invoicing-service.ts -> tests/invoicing-service.test.ts).
    # This is the dominant vitest/jest layout, and generating only the mirrored
    # form measured a fully-paired repo at 0%, which emptied the convention and
    # left the stale-test advisory permanently inert. Two source files sharing a
    # stem in different dirs both map here, so a flat candidate can over-pair;
    # that direction only ever UNDER-nags (the advisory stays quiet or cites a
    # sibling's test) and is strictly better than the layout measuring as
    # testless. Existence-gated like every candidate, so a wrong guess is free.
    for root in _TS_MIRROR_ROOTS:
        for marker in (".test", ".spec"):
            candidates.append((f"flat {root}/{marker}", _join([root, f"{stem}{marker}{ext}"])))
    return candidates


def extract_test_pairing_conventions(
    files: list[ParsedFile],
    *,
    language: str,
    repo_root: Path | None,
) -> dict:
    """Derive the archetype's source-to-test pairing rate and path mapping.

    For each non-test source file in the archetype, derives the candidate test
    paths the repo's conventions imply and checks the filesystem for any of them.
    The pairing rate is the fraction of source files with a paired test; the
    dominant mapping label is the convention most of those pairings followed.

    Returns ``{}`` below the sample floor (too few non-test source files to trust
    the figure), when ``repo_root`` is unknown (candidate paths cannot be
    resolved), or when the pairing rate is below the dominance floor. Otherwise
    ``{"frequency", "paired", "total", "mapping"}``.

    FS-stat based and run at bootstrap, off the hot path, like the byte-reading
    derivations above. Advisory data only: at the 60% floor up to 40% of an
    archetype's files legitimately lack a test, so a missing test is a hint to
    confirm coverage, never a hard failure.
    """
    if repo_root is None:
        return {}
    min_sample = int(threshold("TEST_PAIRING_MIN_SAMPLE"))

    total = 0
    paired = 0
    mapping_counts: Counter[str] = Counter()
    for f in files:
        path = getattr(f, "path", None)
        if path is None:
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            rel = path.name
        if _is_test_path(rel, language=language):
            continue
        total += 1
        for label, candidate in _candidate_test_paths(rel, language=language):
            try:
                if (repo_root / candidate).is_file():
                    paired += 1
                    mapping_counts[label] += 1
                    break
            except OSError:
                continue

    if total < min_sample:
        return {}
    frequency = paired / total if total else 0.0
    if frequency < threshold("TEST_PAIRING_FREQUENCY"):
        return {}
    result: dict = {
        "frequency": round(frequency, 3),
        "paired": paired,
        "total": total,
    }
    if mapping_counts:
        result["mapping"] = mapping_counts.most_common(1)[0][0]
    return result


_PREFIX_RE = re.compile(r"^([A-Z])[A-Z]")
_ENFORCE_THRESHOLD = 0.95
_STRONG_THRESHOLD = 0.60


# Unicode-aware (PEP 3131 for Python; Ruby's own source encoding is UTF-8 by
# default too), so a name is classified by its actual letter case rather than
# an ASCII character range: a caseless script (CJK, etc.) carries no case at
# all and never trips an uppercase/lowercase check, while a mixed-script name
# (``calc_café``) is judged the same way an all-ASCII one is. These back both
# the Ruby in-source casing derivation and the shared Python naming lint
# (`_python_naming_violations` in lint_engine.py reuses them), so an ASCII-only
# check here would misclassify a genuinely-conforming unicode identifier in
# either language as a violation.
def _is_snake_case_word(word: str) -> bool:
    """Legal identifier, no uppercase letter anywhere (any script)."""
    return bool(word) and word.isidentifier() and not any(ch.isupper() for ch in word)


def _is_camel_case_word(word: str) -> bool:
    """Legal identifier, lowercase-led, no separator character."""
    return bool(word) and word.isidentifier() and "_" not in word and word[0].islower()


def _is_pascal_case_word(word: str) -> bool:
    """Legal identifier, uppercase-led, no separator character."""
    return bool(word) and word.isidentifier() and "_" not in word and word[0].isupper()


def _is_screaming_snake_word(word: str) -> bool:
    """Legal identifier, uppercase-led, no lowercase letter anywhere."""
    return (
        bool(word)
        and word.isidentifier()
        and word[0].isupper()
        and not any(ch.islower() for ch in word)
    )


def _classify_ruby_method_casing(name: str) -> str:
    base = name.rstrip("!?=")
    if _is_snake_case_word(base):
        return "snake_case"
    if _is_camel_case_word(base):
        return "camelCase"
    return "other"


def _classify_ruby_class_casing(name: str) -> str:
    segments = name.split("::")
    if all(_is_pascal_case_word(s) for s in segments):
        return "PascalCase"
    return "other"


def _classify_ruby_constant_casing(name: str) -> str:
    if _is_screaming_snake_word(name):
        return "SCREAMING_SNAKE_CASE"
    if _is_pascal_case_word(name):
        # `Result = Struct.new(...)` — a class alias, not a value constant.
        return "PascalCase"
    return "other"


# (result key, classifier, asserted pattern, conforming buckets). Only the
# canonical Ruby casing is ever derived: a sample whose conforming share is
# below threshold (a camelCase-heavy corner, say) yields NO convention rather
# than a non-canonical one, keeping the block-eligible naming rule precise.
# PascalCase constants conform alongside SCREAMING_SNAKE because a
# `Result = Struct.new` class alias is legitimate in any Ruby repo.
_RUBY_CASING_KEYS = {
    "method": ("method_casing", _classify_ruby_method_casing, "snake_case", ("snake_case",)),
    "class": ("class_casing", _classify_ruby_class_casing, "PascalCase", ("PascalCase",)),
    "constant": (
        "constant_casing",
        _classify_ruby_constant_casing,
        "SCREAMING_SNAKE_CASE",
        ("SCREAMING_SNAKE_CASE", "PascalCase"),
    ),
}


def extract_naming_conventions(*, declarations: dict[str, list[str]]) -> dict:
    """Detect naming conventions from declaration names.

    TS declaration types ("interface", "type", "enum") yield ``<type>_prefix``
    entries when a dominant single-letter prefix clears ``_STRONG_THRESHOLD``.
    Ruby declaration types ("method", "class", "constant") yield
    ``<type>_casing`` entries when a dominant casing bucket clears the same
    threshold, giving the lint an in-source signal (``def fetchData`` against a
    snake_case repo) rather than only file-level naming.
    """
    result: dict = {}
    type_to_key = {"interface": "interface_prefix", "type": "type_prefix", "enum": "enum_prefix"}
    for decl_type, names in declarations.items():
        if len(names) < MIN_SAMPLE_SIZE_NAMING:
            continue
        casing = _RUBY_CASING_KEYS.get(decl_type)
        if casing is not None:
            key, classify, canonical, conforming_buckets = casing
            buckets = Counter(classify(name) for name in names)
            conforming = sum(buckets.get(b, 0) for b in conforming_buckets)
            consistency = conforming / len(names)
            if consistency >= _STRONG_THRESHOLD:
                result[key] = {
                    "pattern": canonical,
                    "consistency": round(consistency, 3),
                    "sample_size": len(names),
                }
            continue
        key = type_to_key.get(decl_type)
        if not key:
            continue
        prefix_counts: Counter[str] = Counter()
        for name in names:
            m = _PREFIX_RE.match(name)
            if m:
                prefix_counts[m.group(1)] += 1
        if not prefix_counts:
            continue
        most_common_prefix, count = prefix_counts.most_common(1)[0]
        consistency = count / len(names)
        if consistency >= _STRONG_THRESHOLD:
            result[key] = {
                "pattern": most_common_prefix,
                "consistency": round(consistency, 3),
                "sample_size": len(names),
            }
    return result


# File-naming derivation. We classify the casing of each member's basename stem
# (the basename with its extension and any compound suffix token removed) into
# one of four mutually exclusive buckets, then record the dominant bucket and,
# separately, the dominant compound-suffix token (``.service.ts``, ``_job.rb``)
# when one exists. Both ride the same 60/95 consistency gates as the other
# naming conventions.
#
# A compound suffix is the run of dotted/underscored tokens before the final
# extension: ``user.service.ts`` -> stem ``user``, suffix ``.service.ts``;
# ``billing_job.rb`` -> stem ``billing``, suffix ``_job.rb``. A plain
# ``user.ts`` / ``user.rb`` has no compound suffix.
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]+$")
_TS_COMPOUND_SUFFIX_RE = re.compile(r"((?:\.[a-z][a-z0-9]*)+)(\.(?:ts|tsx|js|jsx|mjs|cjs))$")
_RB_COMPOUND_SUFFIX_RE = re.compile(r"(_[a-z][a-z0-9]*)(\.rb)$")

# A stem reads as kebab/snake only when it carries the separator, and as
# camel/Pascal only when its casing actually distinguishes it. A bare single
# all-lowercase word (``index``, ``user``, ``route``) conforms to kebab, snake,
# and camel at once, so it carries no signal and must NOT be tallied — otherwise
# entry files (``index.ts``) and short module names false-positive against a
# kebab/snake convention. camelCase therefore requires an internal uppercase.
_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_CAMEL_RE = re.compile(r"^[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*$")
_PASCAL_RE = re.compile(r"^[A-Z][a-zA-Z0-9]*$")

# A stem that mixes an underscore separator with an embedded uppercase letter
# (``test_BadCasing``, ``user_APIClient``) matches none of the four buckets
# above — snake requires all-lowercase, Pascal/camel forbid the separator — but
# unlike a bare lowercase word it is NOT ambiguous: it is unambiguously not
# kebab, snake, camel, or Pascal. Falling through to None would silently drop
# it from both the consistency tally and the lint rule (which only flags a
# non-None, non-matching casing), letting exactly the kind of filename that
# most obviously breaks a repo's casing convention escape detection entirely.
_MIXED_UNDERSCORE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")


def _split_compound_suffix(basename: str) -> tuple[str, str | None]:
    """Split ``basename`` into (stem, compound_suffix).

    The compound suffix is the dotted run before a TS/JS extension
    (``.service.ts``) or the single underscore token before ``.rb``
    (``_job.rb``); the returned stem is the basename with both the compound
    suffix and the bare extension stripped. ``None`` suffix means the file
    is named with only a plain extension (``user.ts``, ``user.rb``).
    """
    m = _TS_COMPOUND_SUFFIX_RE.search(basename)
    if m:
        return basename[: m.start()], m.group(1) + m.group(2)
    m = _RB_COMPOUND_SUFFIX_RE.search(basename)
    if m:
        return basename[: m.start()], m.group(1) + m.group(2)
    return _FILE_EXT_RE.sub("", basename), None


def _classify_casing(stem: str) -> str | None:
    """Bucket a basename stem into kebab/snake/camel/Pascal/mixed, or None if unclear.

    Order matters: a separator-bearing stem is kebab or snake first; an
    upper-initial word is Pascal; a lower-initial word with an internal capital
    is camel. A stem that carries an underscore separator together with an
    embedded uppercase letter (``test_BadCasing``) conforms to none of those
    four and is classified as ``mixed_case`` — a distinct, unambiguously
    non-conforming shape, counted against consistency and flagged by the lint
    rule rather than silently excluded. A bare single lowercase word,
    index/entry files, and dot-prefixed config stems carry no distinguishing
    casing signal at all, so they return None and are excluded from the
    consistency tally.
    """
    if not stem or not stem[0].isalnum():
        return None
    if _KEBAB_RE.match(stem):
        return "kebab-case"
    if _SNAKE_RE.match(stem):
        return "snake_case"
    if _PASCAL_RE.match(stem):
        return "PascalCase"
    if _CAMEL_RE.match(stem):
        return "camelCase"
    if _MIXED_UNDERSCORE_RE.match(stem):
        return "mixed_case"
    return None


def extract_file_naming_convention(*, basenames: list[str]) -> dict:
    """Derive the dominant basename casing and suffix token from member paths.

    ``basenames`` is the list of member filenames for one archetype (e.g.
    ``["user.service.ts", "order.service.ts", ...]``). Returns a
    ``{"file_naming": {...}}`` fragment when a casing bucket clears the 60%
    consistency floor, recording the dominant casing, its consistency, and
    sample size. When a compound suffix token (``.service.ts``, ``_job.rb``)
    also dominates above the floor it is recorded alongside. Returns ``{}``
    when the sample is too thin or no signal dominates.

    Pure path-pattern derivation: no file is read. The casing tally ignores
    basenames that carry no casing signal (``index.ts``, ``.eslintrc.js``)
    so a folder full of entry files doesn't suppress a real convention.
    """
    if len(basenames) < threshold("FILE_NAMING_MIN_SAMPLE"):
        return {}

    casing_counts: Counter[str] = Counter()
    suffix_counts: Counter[str] = Counter()
    suffix_total = 0
    for name in basenames:
        stem, suffix = _split_compound_suffix(name)
        casing = _classify_casing(stem)
        if casing:
            casing_counts[casing] += 1
        # Every basename votes on the suffix axis: a file with only a plain
        # extension votes for "no suffix", which keeps a lone ``*.service.ts``
        # in a folder of plain files from reading as a convention.
        suffix_total += 1
        if suffix:
            suffix_counts[suffix] += 1

    casing_sample = sum(casing_counts.values())
    if casing_sample < threshold("FILE_NAMING_MIN_SAMPLE"):
        return {}

    # mixed_case is a non-conforming shape recorded ONLY to count against
    # consistency (a real convention's denominator) -- it must never win the
    # dominance vote itself, or a plurality of mixed-case files would make
    # "mixed_case" the archetype's own declared convention and then flag its
    # genuinely-conforming siblings as violators.
    conforming_counts = Counter({k: v for k, v in casing_counts.items() if k != "mixed_case"})
    if not conforming_counts:
        return {}
    dominant_casing, casing_hits = conforming_counts.most_common(1)[0]
    casing_consistency = casing_hits / casing_sample
    if casing_consistency < _STRONG_THRESHOLD:
        return {}

    entry: dict = {
        "casing": dominant_casing,
        "casing_consistency": round(casing_consistency, 3),
        "sample_size": casing_sample,
    }

    if suffix_counts:
        dominant_suffix, suffix_hits = suffix_counts.most_common(1)[0]
        suffix_consistency = suffix_hits / suffix_total if suffix_total else 0.0
        if suffix_consistency >= _STRONG_THRESHOLD:
            entry["suffix"] = dominant_suffix
            entry["suffix_consistency"] = round(suffix_consistency, 3)

    return {"file_naming": entry}


_INHERITANCE_THRESHOLD = 0.60

# The class name may be namespaced (``class Api::V1::FooController < Base``).
# A bare ``\w+`` stops at the first ``::`` and the whole declaration fails to
# match, so namespaced classes were invisible to convention-building (and the
# linter then flagged their bases as novel). ``[\w:]+`` matches the full name.
_RUBY_CLASS_RE = re.compile(r"^\s*class\s+[\w:]+\s*<\s*([\w:]+)", re.MULTILINE)
_RUBY_INCLUDE_RE = re.compile(r"^\s*include\s+([\w:]+)", re.MULTILINE)
# Generic Rails/framework class-body macros. These already surface in the
# method_calls "Common DSL" line, so class_contract excludes them — its value is
# the NON-allowlisted, repo-specific DSL (ActiveInteraction's typed filters, etc.).
_RUBY_DSL_ALLOWLIST = frozenset(
    {
        "validates",
        "validate",
        "belongs_to",
        "has_many",
        "has_one",
        "has_and_belongs_to_many",
        "scope",
        "enum",
        "before_action",
        "after_action",
        "around_action",
        "before_validation",
        "after_commit",
        "after_save",
        "before_save",
        "after_create",
        "before_create",
        "before_destroy",
        "after_destroy",
        "delegate",
        "attr_accessor",
        "attr_reader",
        "sidekiq_options",
        "sidekiq_throttle",
        "render_data",
        "render_error",
        "has_paper_trail",
        "acts_as_taggable_on",
        "mount_uploader",
        "has_one_attached",
        "has_many_attached",
        "default_scope",
        "counter_culture",
    }
)
# Longest-first alternation so ``validates`` is tried before ``validate`` (``\b``
# already disambiguates, but ordering keeps the match unambiguous).
# Any leading indent, NOT a literal two spaces: the standard Rails API layout
# wraps a class in `module Api` / `module V1`, indenting the body to 4 or 6
# spaces, and a `^  ` anchor silently matched none of it -- so controllers, the
# most module-nested archetype in every Rails app, derived no DSL convention at
# all. Mirrors lint_engine's `_RUBY_DSL_RE`, which had the any-indent form all
# along (same intent, two regexes, one wrong).
_RUBY_DSL_CALL_RE = re.compile(
    r"^[ \t]+(" + "|".join(sorted(_RUBY_DSL_ALLOWLIST, key=lambda s: (-len(s), s))) + r")\b",
    re.MULTILINE,
)


def _unqualified_name(base: str) -> str:
    """Last ``::``-segment of a (possibly namespaced) constant name."""
    return base.rsplit("::", 1)[-1]


def _strip_type_params(base: str) -> str:
    """Drop a generic type-parameter suffix from a base class name.

    A typed cohort subclasses the SAME base parameterized per model:
    ``BaseRepository[User]``, ``BaseRepository[Order]``, ... (Python ``[...]``) or
    ``Repository<User>`` (TS ``<...>``). Counting the raw subscripted strings
    fragments the dominance signal so the shared base never clears the floor. Cut
    at the first ``[`` or ``<`` so every variant normalizes to ``BaseRepository``.
    A base with no subscript, or one starting with the bracket, is returned
    stripped-unchanged (class names never begin with a bracket).
    """
    cut = len(base)
    for sep in ("[", "<"):
        i = base.find(sep)
        if i > 0:
            cut = min(cut, i)
    return base[:cut].strip()


def _dominant_base_family(base_counts: Counter[str]) -> tuple[str, list[str], int] | None:
    """Group bases by unqualified name; return the largest multi-namespace family.

    A controller convention often spans namespaces -- ``Api::V1::BaseController``
    and ``Api::V1::Admin::BaseController`` are the same ``BaseController`` base
    reached through different module paths. Grouping by the unqualified name lets
    the caller treat them as one convention when no single fully-qualified base
    dominates. Only families with more than one distinct fully-qualified member
    count; a single-member "family" is just the single base the caller already
    handles, so returning it here would add nothing.

    Returns ``(unqualified_name, [members], combined_count)`` for the family with
    the highest combined count, or ``None`` when no multi-member family exists.
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for base in base_counts:
        by_name[_unqualified_name(base)].append(base)

    best: tuple[str, list[str], int] | None = None
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        combined = sum(base_counts[b] for b in members)
        if best is None or combined > best[2]:
            best = (name, members, combined)
    return best


def _python_inheritance_conventions(files: list[ParsedFile], total: int) -> dict:
    """Dominant base + known bases for Python, from the parsed ``class_shapes``.

    Python conflates the single superclass and mixins into one base tuple, so
    every base a class declares is counted (deduped per file): the dominant base
    is whatever the archetype's classes share most (``models.Model`` for a Django
    model cohort, ``APIView`` for a DRF view cohort), and any base recurring at
    least twice is an established choice, not a violation. No separate
    ``dominant_include`` -- a Python mixin is just another base.
    """
    base_counts: Counter[str] = Counter()
    for f in files:
        shapes = (getattr(f, "extras", None) or {}).get("class_shapes")
        if not isinstance(shapes, list):
            continue
        seen_bases: set[str] = set()
        for sh in shapes:
            if not isinstance(sh, dict):
                continue
            bases = sh.get("bases")
            if not isinstance(bases, list):
                continue
            for raw_base in bases:
                if not isinstance(raw_base, str) or not raw_base:
                    continue
                base = _strip_type_params(raw_base)
                if base and base != "object" and base not in seen_bases:
                    base_counts[base] += 1
                    seen_bases.add(base)

    result: dict = {}
    if base_counts:
        top_base, top_count = base_counts.most_common(1)[0]
        if top_count / total >= _INHERITANCE_THRESHOLD:
            result["dominant_base"] = top_base
            result["frequency"] = round(top_count / total, 3)
            result["sample_size"] = total
            result["known_bases"] = sorted(b for b, c in base_counts.items() if c >= 2)
    return result


def _archetype_class_count(files: list[ParsedFile], language: str) -> int:
    """Number of class definitions across an archetype's files.

    DRF/Django (and some Rails) pack many classes into few role files (one
    serializers.py / models.py per app), so a file-count sample gate starves a
    5-file / 70-class archetype of every derived class convention. Counting
    classes lets such an archetype clear the same floor. TS returns 0 here (no
    comparable many-classes-per-file layout), so it keeps the file-count gate."""
    if language == "python":
        return sum(len((getattr(f, "extras", None) or {}).get("class_shapes") or []) for f in files)
    if language == "ruby":
        n = 0
        for f in files:
            try:
                content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            n += len(_RUBY_CLASS_NAME_RE.findall(content))
        return n
    return 0


def _meets_class_sample_gate(files: list[ParsedFile], language: str) -> bool:
    """True when the archetype has enough FILES or enough CLASSES for a class-level
    convention. Relaxes the pure file-count floor for class-heavy, few-file
    archetypes (DRF serializers, Django models) without lowering it for thin ones."""
    if len(files) >= MIN_SAMPLE_SIZE:
        return True
    return _archetype_class_count(files, language) >= MIN_SAMPLE_SIZE


def extract_inheritance_conventions(files: list[ParsedFile], *, language: str = "ruby") -> dict:
    """Detect dominant base class and include mixins by reading file content."""
    if not _meets_class_sample_gate(files, language):
        return {}

    total = len(files)
    if language == "python":
        return _python_inheritance_conventions(files, total)
    base_counts: Counter[str] = Counter()
    include_counts: Counter[str] = Counter()

    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        seen_bases: set[str] = set()
        for m in _RUBY_CLASS_RE.finditer(content):
            base = m.group(1)
            if base not in seen_bases:
                base_counts[base] += 1
                seen_bases.add(base)
        seen_includes: set[str] = set()
        for m in _RUBY_INCLUDE_RE.finditer(content):
            inc = m.group(1)
            if inc not in seen_includes:
                include_counts[inc] += 1
                seen_includes.add(inc)

    result: dict = {}

    if base_counts:
        top_base, top_count = base_counts.most_common(1)[0]
        if top_count / total >= _INHERITANCE_THRESHOLD:
            result["dominant_base"] = top_base
            result["frequency"] = round(top_count / total, 3)
            result["sample_size"] = total
            # Any base the archetype uses at least twice is an established
            # choice, not a violation (e.g. an intermediate
            # ``Api::V1::BaseController`` alongside a dominant
            # ``ApplicationController``). Recording the set lets the linter
            # flag only a genuinely novel base instead of every non-dominant
            # one -- the latter drove an unsatisfiable PostToolUse STOP loop.
            result["known_bases"] = sorted(b for b, c in base_counts.items() if c >= 2)
        else:
            # No single base clears the threshold, but the bases may share an
            # unqualified name across namespaces (``Api::V1::BaseController`` and
            # ``Api::V1::Admin::BaseController`` are both ``BaseController``). When
            # one such family covers the threshold, record it as the convention so
            # a controller inheriting an unrelated base is still flagged. Without
            # this, a repo whose controllers all inherit some ``*BaseController``
            # but split across namespaces drops the whole inheritance convention.
            family = _dominant_base_family(base_counts)
            if family is not None:
                family_name, members, family_count = family
                if family_count / total >= _INHERITANCE_THRESHOLD:
                    # The most-frequent fully-qualified member labels the message;
                    # known_bases carries every namespace variant so the linter
                    # accepts them all and flags only a base outside the family.
                    result["dominant_base"] = max(members, key=lambda b: base_counts[b])
                    result["frequency"] = round(family_count / total, 3)
                    result["sample_size"] = total
                    result["base_family"] = family_name
                    result["known_bases"] = sorted(members)

    if include_counts:
        top_include, inc_count = include_counts.most_common(1)[0]
        if inc_count / total >= _INHERITANCE_THRESHOLD:
            result["dominant_include"] = top_include
            result["include_frequency"] = round(inc_count / total, 3)

    return result


# Class-body calls that are structure/visibility, not domain DSL — never a contract.
_CONTRACT_MACRO_STOPLIST = frozenset(
    {
        "private",
        "public",
        "protected",
        "private_class_method",
        "private_constant",
        "require",
        "require_relative",
        "include",
        "extend",
        "prepend",
        "load",
        "autoload",
        "freeze",
    }
)
# Constructors and the universal Object/operator/conversion methods recur across
# any archetype — defining them is "writing a class", not a contract. (TS already
# excludes constructors via kind; this gives Ruby the same exclusion.)
_CONTRACT_METHOD_STOPLIST = frozenset(
    {
        "initialize",
        "to_s",
        "to_str",
        "to_h",
        "to_hash",
        "to_a",
        "to_ary",
        "to_proc",
        "inspect",
        "hash",
        "eql?",
        "==",
        "<=>",
        "coerce",
        "method_missing",
        "respond_to_missing?",
    }
)
_CONTRACT_METHOD_KINDS = frozenset({"method", "singleton_method", "staticmethod", "classmethod"})
_CONTRACT_REQUIRED_METHODS_CAP = 3
# Python's data-model dunders (__init__, __str__, __repr__, __eq__, __hash__, ...)
# are the language's universal Object methods -- the same "writing a class, not a
# contract" exclusion the Ruby stoplist encodes, but there are too many to
# enumerate and new ones keep being added, so they are matched by shape. Django
# recommends __str__ on every model and dataclasses synthesize __init__/__eq__,
# so unfiltered these fill the required-methods cap and bury the real contract.
_PY_DUNDER_RE = re.compile(r"^__\w+__$")


def _contract_rec(by_name: dict[str, dict], cname: str) -> dict:
    """Get-or-create the per-class accumulator for ``cname`` within one file."""
    rec = by_name.get(cname)
    if rec is None:
        rec = {
            "base": None,
            "decorators": set(),
            "macros": set(),
            "methods": set(),
            "implements": set(),
        }
        by_name[cname] = rec
    return rec


def _collect_contract_classes(files: list[ParsedFile], *, language: str) -> list[dict]:
    """One record per class: ``{base, decorators:set, macros:set, methods:set, implements:set}``.

    Records are per class (not per file) so a co-located helper/error/DTO class in
    the same file is its own record and never dilutes the primary class's contract.
    """
    classes: list[dict] = []
    for _fidx, f in enumerate(files):
        extras = getattr(f, "extras", {}) or {}
        by_name: dict[str, dict] = {}

        for shape in extras.get("class_shapes", []) or []:
            cname = shape.get("name")
            if not cname:
                continue
            rec = _contract_rec(by_name, cname)
            for dec in shape.get("decorators", []) or []:
                if dec:
                    rec["decorators"].add(dec)
            # TS class_shapes carry the base under `extends` (a plain string,
            # single inheritance only). The libcst dump carries the full list
            # under `bases`, and (for display only) a possibly-decorated
            # `extends` summary -- e.g. "Alpha (+2 more)" for a multi-base
            # class. Prefer the plain `bases[0]` when present: `extends` here
            # is a dominance/grouping KEY (Counter'd across a cohort and
            # rendered as the archetype's single declared base), and the
            # "(+N more)" marker varies per-class even when every class
            # shares the same primary base, which would fragment the
            # dominance count and could flip the winning base entirely.
            # `extends` is the fallback for TS, which never populates `bases`.
            base = next(iter(shape.get("bases") or []), None) or shape.get("extends")
            if base:
                rec["base"] = _strip_type_params(base)
            # TS-only: the interfaces a class `implements`. This is the anchor a
            # decorator or base cannot give -- @Injectable is shared by every
            # NestJS provider, but `implements CanActivate`/`NestInterceptor`/
            # `PipeTransform` is what actually distinguishes a guard/interceptor/
            # pipe from a plain service. Absent for Ruby/Python dumps, which carry
            # no such key, so this is a no-op for those languages.
            for iface in shape.get("implements") or []:
                if iface:
                    rec["implements"].add(iface)

        for call in extras.get("class_body_calls", []) or []:
            name = call.get("name")
            cname = call.get("class")
            if not name or not cname or name in _CONTRACT_MACRO_STOPLIST:
                continue
            _contract_rec(by_name, cname)["macros"].add(name)

        for sig in extras.get("callable_signatures", []) or []:
            cname = sig.get("enclosing_class")
            if not cname or sig.get("kind") not in _CONTRACT_METHOD_KINDS:
                continue
            rec = _contract_rec(by_name, cname)
            mname = sig.get("name")
            if (
                mname
                and mname not in _CONTRACT_METHOD_STOPLIST
                and not (language == "python" and _PY_DUNDER_RE.match(mname))
            ):
                rec["methods"].add(mname)
            base = sig.get("base_class")
            if base and not rec["base"]:
                rec["base"] = _strip_type_params(base)

        for _rec in by_name.values():
            # Tag the source file so the anchor gate can count DISTINCT FILES (its
            # 0.6*len(files) bar), not classes: many classes packed into a few
            # files must not inflate an anchor past a minority of the archetype's
            # files (a mixin in 2 of 15 files carried by 11 classes was projected
            # as archetype-wide though inheritance -- per-file -- rightly declined).
            _rec["_file"] = _fidx
        classes.extend(by_name.values())
    return classes


def _contract_from_cohort(cohort: list[dict], *, language: str) -> dict:
    """Build the contract over a cohort of classes sharing one anchor."""
    total = len(cohort)
    if total == 0:
        return {}

    def _dominant(counter: Counter) -> list[tuple[str, float]]:
        ranked = [
            (name, round(c / total, 3))
            for name, c in counter.items()
            if c / total >= _INHERITANCE_THRESHOLD
        ]
        ranked.sort(key=lambda kv: (-kv[1], kv[0]))
        return ranked

    macros = (
        _dominant(Counter(m for c in cohort for m in c["macros"])) if language == "ruby" else []
    )
    # Drop generic Rails macros — the Common DSL line already covers them; keep only
    # the repo-specific DSL that makes this archetype's contract distinct.
    macros = [(n, fr) for n, fr in macros if n not in _RUBY_DSL_ALLOWLIST]
    decorators = (
        _dominant(Counter(d for c in cohort for d in c["decorators"])) if language != "ruby" else []
    )
    methods = _dominant(Counter(m for c in cohort for m in c["methods"]))[
        :_CONTRACT_REQUIRED_METHODS_CAP
    ]
    bases = _dominant(Counter(c["base"] for c in cohort if c["base"]))
    # `implements` is a TS-only heritage clause (Ruby/Python dumps never
    # populate it), so restrict the dominance pass to that language the same
    # way `macros` is restricted to Ruby.
    implements = (
        _dominant(Counter(i for c in cohort for i in c["implements"]))
        if language == "typescript"
        else []
    )

    result: dict = {}
    freqs: dict[str, float] = {}
    if macros:
        result["dsl_macros"] = sorted(n for n, _ in macros)
        freqs.update(dict(macros))
    if decorators:
        result["decorators"] = sorted(n for n, _ in decorators)
        freqs.update(dict(decorators))
    if methods:
        result["required_methods"] = [n for n, _ in methods]
        freqs.update(dict(methods))
    if bases:
        result["base"] = bases[0][0]
        freqs.setdefault(bases[0][0], bases[0][1])
    if implements:
        result["implements"] = sorted(n for n, _ in implements)
        freqs.update(dict(implements))

    if not (
        result.get("dsl_macros")
        or result.get("decorators")
        or result.get("required_methods")
        or result.get("implements")
    ):
        return {}

    result["sample_size"] = total
    result["frequencies"] = freqs
    return result


def extract_class_contract_conventions(files: list[ParsedFile], *, language: str) -> dict:
    """Derive an archetype's shared class-body contract from dump data.

    Captures the shape a base class/decorator/interface implies but that the
    inheritance and method_calls conventions miss: the repo-specific DSL macros
    (Ruby), class decorators (TS/Python), implemented interfaces (TS), and
    required methods. A contract requires a structural anchor — a dominant base
    class, class decorator, or implemented interface — and is measured ONLY over
    the cohort of classes carrying that anchor, so a co-located helper class
    never dilutes or pollutes it. The interface anchor matters most for TS
    archetypes a decorator alone cannot tell apart: a NestJS guard/interceptor/
    pipe all carry the same @Injectable() as every other provider, but only a
    guard implements CanActivate.
    """
    if not _meets_class_sample_gate(files, language):
        return {}

    classes = _collect_contract_classes(files, language=language)
    if not classes:
        return {}

    # The anchor must clear the dominance threshold against the member count, the
    # same reference inheritance uses, so a file with two classes can't halve it.
    anchor_min = _INHERITANCE_THRESHOLD * len(files)
    # Count DISTINCT FILES carrying each base/decorator/interface, not classes, so
    # the gate shares anchor_min's file basis (and matches the per-file
    # inheritance detector). Class-counting let a mixin concentrated in a few
    # files clear a file-scaled bar it should have failed.
    _base_files: dict[str, set] = {}
    _dec_files: dict[str, set] = {}
    _impl_files: dict[str, set] = {}
    for c in classes:
        if c["base"]:
            _base_files.setdefault(c["base"], set()).add(c["_file"])
        for d in c["decorators"]:
            _dec_files.setdefault(d, set()).add(c["_file"])
        for i in c["implements"]:
            _impl_files.setdefault(i, set()).add(c["_file"])
    base_counts = {b: len(fs) for b, fs in _base_files.items()}
    decorator_counts = {d: len(fs) for d, fs in _dec_files.items()}
    implements_counts = {i: len(fs) for i, fs in _impl_files.items()}

    candidates: list[tuple[str, str]] = [
        ("base", b) for b, cnt in base_counts.items() if cnt >= anchor_min
    ]
    if language != "ruby":
        candidates += [("decorator", d) for d, cnt in decorator_counts.items() if cnt >= anchor_min]
    if language == "typescript":
        candidates += [
            ("implements", i) for i, cnt in implements_counts.items() if cnt >= anchor_min
        ]
    if not candidates:
        return {}

    # When several anchors qualify (e.g. an error base co-occurs with the real one),
    # pick the anchor whose cohort yields the richest contract — but only among
    # anchors covering a comparable share of the largest candidate cohort. The
    # file-count gate above tolerates many classes per file (Flask view modules
    # pack dozens), so a niche anchor (a decorator carried by 4 of 76 classes)
    # can qualify and then out-richness the archetype's real contract; its
    # within-cohort frequencies would be projected onto the whole archetype as
    # fact. A minority cohort never beats the dominant one on richness.
    materialized: list[tuple[str, str, list[dict]]] = []
    for kind, value in candidates:
        if kind == "base":
            cohort = [c for c in classes if c["base"] == value]
        elif kind == "decorator":
            cohort = [c for c in classes if value in c["decorators"]]
        else:
            cohort = [c for c in classes if value in c["implements"]]
        materialized.append((kind, value, cohort))
    max_cohort = max(len(c) for _k, _v, c in materialized)
    best: tuple[tuple, dict] | None = None
    for kind, value, cohort in materialized:
        if len(cohort) < _INHERITANCE_THRESHOLD * max_cohort:
            continue
        result = _contract_from_cohort(cohort, language=language)
        if not result:
            continue
        richness = (
            len(result.get("dsl_macros", []))
            + len(result.get("decorators", []))
            + len(result.get("required_methods", []))
            + len(result.get("implements", []))
        )
        # Cohort size leads the rank so the dominant cohort always wins; richness
        # is only a tiebreak among equally-dominant anchors. Leading with richness
        # let a smaller cohort (a decorator carried by 6 of 14 classes) out-rank
        # the dominant base (carried by 9) and project its within-cohort
        # frequencies as an archetype-wide MUST -- the case the docstring forbids.
        # `implements` shares the decorator/base tier of this tiebreak: it is a
        # structural heritage clause exactly like a base class, just the second
        # (interface) heritage list instead of the first.
        rank = (len(cohort), richness, kind in ("decorator", "implements"), value)
        if best is None or rank > best[0]:
            best = (rank, result)

    return best[1] if best else {}


# A controller's authorization is expressed as a before_action callback whose
# first argument is the guard method symbol (``before_action :authorize!``).
# The coarse DSL fingerprint only records the call NAME (before_action), so a
# controller that calls some unrelated before_action but skips the authz one
# matches its archetype fine. Capturing the argument symbol lets us tell the
# archetype's expected guard from any other callback.
#
# Three forms are recognized:
#   before_action :authorize!                  -> guard "authorize!", unscoped
#   before_action :set_thing, only: %i[show]   -> guard "set_thing", scoped
#   skip_before_action :authorize!             -> a removal, not a guard
# Only the first symbol argument is the guard name; trailing options
# (``only:``/``except:``/``if:``/``unless:``) scope it. A scoped callback runs on
# a subset of actions, so it is not the blanket guard the archetype enforces and
# must not count toward the required set.
_RUBY_BEFORE_ACTION_RE = re.compile(
    r"^[ \t]+(skip_before_action|before_action)\s+:([A-Za-z_]\w*[!?]?)(.*)$",
    re.MULTILINE,
)
_RUBY_GUARD_SCOPE_RE = re.compile(r"\b(only|except|if|unless)\s*:")

# A guard appearing as a blanket before_action in this fraction of the
# archetype's controllers reads as the convention every controller follows.
# Shares the 60% floor the inheritance derivation uses for the same reason: a
# choice the clear majority makes is the established norm, not noise.
_REQUIRED_GUARD_THRESHOLD = _INHERITANCE_THRESHOLD


def _scan_guard_calls(content: str) -> tuple[set[str], set[str], set[str]]:
    """Pull guard symbols out of one controller's before_action callbacks.

    Returns three sets for a single file:
    - blanket: guards installed unscoped (run on every action) -- these are the
      ones an archetype can be said to "require".
    - scoped: guards installed with only:/except:/if:/unless:, so they run on a
      subset and carry no archetype-wide requirement.
    - skipped: guards removed via skip_before_action; a controller that skips a
      guard legitimately lacks it, so it must not be counted as a witness for
      that guard being present.
    """
    blanket: set[str] = set()
    scoped: set[str] = set()
    skipped: set[str] = set()
    for m in _RUBY_BEFORE_ACTION_RE.finditer(content):
        call, symbol, rest = m.group(1), m.group(2), m.group(3)
        if call == "skip_before_action":
            skipped.add(symbol)
            continue
        if _RUBY_GUARD_SCOPE_RE.search(rest):
            scoped.add(symbol)
        else:
            blanket.add(symbol)
    return blanket, scoped, skipped


def extract_required_guards_conventions(files: list[ParsedFile]) -> dict:
    """Derive the authorization guards a controller archetype expects.

    A guard is the first symbol argument of a blanket ``before_action`` (one with
    no only:/except:/if:/unless: scope). A guard present in at least 60% of the
    archetype's controllers is recorded as ``required_guards``; every guard the
    archetype uses at least twice is recorded in ``known_guards`` so a legitimate
    variant of the same authz line does not read as missing.

    A controller that removes a guard via ``skip_before_action`` is not counted
    as a witness for that guard, so a folder where most controllers skip authz
    does not derive a requirement they don't actually keep.

    Advisory data only: Rails authz is routinely inherited from a base
    controller, so a clean controller can legitimately lack the line. The
    consuming check must treat a miss as a hint to confirm inheritance, never a
    hard failure, and must walk the archetype's known bases before deciding a
    file is genuinely unguarded.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {}

    total = len(files)
    blanket_counts: Counter[str] = Counter()
    any_use_counts: Counter[str] = Counter()
    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        blanket, scoped, skipped = _scan_guard_calls(content)
        # A file skipping a guard is neutral evidence for that guard: it neither
        # installs nor proves the archetype enforces it, so drop it from this
        # file's blanket votes before tallying.
        for symbol in blanket - skipped:
            blanket_counts[symbol] += 1
        for symbol in (blanket | scoped) - skipped:
            any_use_counts[symbol] += 1

    if not blanket_counts:
        return {}

    required = sorted(
        symbol
        for symbol, count in blanket_counts.items()
        if count / total >= _REQUIRED_GUARD_THRESHOLD
    )
    if not required:
        return {}

    known = sorted(symbol for symbol, count in any_use_counts.items() if count >= 2)
    return {
        "required_guards": required,
        "known_guards": known,
        "sample_size": total,
    }


# Python authz-guard signals (the DRF/Django analog of a Rails blanket
# before_action). PRESENCE is the signal: a class that assigns one of these
# attributes (any value, including AllowAny) has made an authz decision; so has
# one extending an authz mixin or carrying an authz decorator. Tails only
# (the last dotted segment) so `auth.login_required` matches `login_required`.
_PY_AUTHZ_ATTRS = frozenset({"permission_classes", "authentication_classes"})
_PY_AUTHZ_MIXINS = frozenset(
    {"LoginRequiredMixin", "PermissionRequiredMixin", "UserPassesTestMixin"}
)
_PY_AUTHZ_DECORATORS = frozenset(
    {"login_required", "permission_required", "user_passes_test", "staff_member_required"}
)


def _tails(names) -> set[str]:
    """Last dotted segment of each name (``rest_framework.IsAuthenticated`` ->
    ``IsAuthenticated``), for matching against the bare authz vocabulary."""
    out: set[str] = set()
    for n in names or ():
        if isinstance(n, str) and n:
            out.add(n.rsplit(".", 1)[-1])
    return out


def _python_file_has_authz_signal(parsed_file) -> bool:
    """True when a file declares at least one view that made an authz decision:
    a class assigning permission_classes/authentication_classes, extending an
    authz mixin, or carrying an authz decorator (class- or function-level)."""
    extras = getattr(parsed_file, "extras", None) or {}
    for shape in extras.get("class_shapes", []) or ():
        if set(shape.get("class_attrs") or ()) & _PY_AUTHZ_ATTRS:
            return True
        if _tails(shape.get("bases")) & _PY_AUTHZ_MIXINS:
            return True
        if _tails(shape.get("decorators")) & _PY_AUTHZ_DECORATORS:
            return True
    for sig in extras.get("callable_signatures", []) or ():
        if _tails(sig.get("decorators")) & _PY_AUTHZ_DECORATORS:
            return True
    return False


def extract_python_authz_guard_conventions(files: list[ParsedFile]) -> dict:
    """Derive whether a Python view archetype conventionally restricts access.

    The Python analog of ``extract_required_guards_conventions``. Per cohort, a
    file is "guarded" if it declares any view that made an authz decision (see
    ``_python_file_has_authz_signal``). When at least 60% of a cohort's files are
    guarded (and the sample is large enough), the cohort records
    ``authz_required`` so the edit-time lint can flag an unguarded view as an
    advisory outlier. Self-gating: a non-view cohort (models, serializers) has no
    authz signal and derives nothing.

    Advisory data only -- authz is routinely inherited from a project base view,
    so the consuming check treats a miss as a hint to confirm intent (and walks
    the cohort's known bases first), never a hard failure.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {}
    total = len(files)
    guarded = sum(1 for f in files if _python_file_has_authz_signal(f))
    if total == 0 or guarded / total < _REQUIRED_GUARD_THRESHOLD:
        return {}
    return {"authz_required": True, "frequency": round(guarded / total, 3), "sample_size": total}


# Error-handling contract derivation. We measure, per archetype, how uniformly
# the archetype's files express error handling, and record the dominant shape so
# a data-backed principle can say "actions here rescue into the project error
# format" instead of free-text "check the witness" prose.
#
# The unit differs by language, because the idiomatic locus of error handling
# differs:
#   - TypeScript: a try/catch inside a function/method body is the per-unit
#     contract, so we count files whose bodies contain at least one try block.
#   - Ruby/Rails: error handling is normally centralized at the controller base
#     via `rescue_from`, NOT repeated per action. Counting per-action inline
#     rescue would mismeasure idiomatic Rails and risk a principle that fights
#     the dominant idiom, so we count files that declare `rescue_from` (the
#     base-level pattern) and, separately, the dominant render target a rescue
#     hands the error to (render json:/render_error/an ErrorSerializer call).
_TS_TRY_RE = re.compile(r"(?:^|[^.\w])try\s*\{", re.MULTILINE)
_PY_TRY_RE = re.compile(r"^[ \t]*try\s*:", re.MULTILINE)
_RUBY_RESCUE_FROM_RE = re.compile(r"^\s*rescue_from\b", re.MULTILINE)
# Ruby's built-in exception classes. `raise StandardError.new("...")` is raising a
# stdlib exception, not handing the error to the project's render shape, so these
# must not be mistaken for a custom *Error/*Serializer render target below.
_RUBY_BUILTIN_ERROR_CLASSES: frozenset[str] = frozenset(
    {
        "StandardError",
        "RuntimeError",
        "ArgumentError",
        "TypeError",
        "NameError",
        "NoMethodError",
        "RangeError",
        "IndexError",
        "KeyError",
        "IOError",
        "NotImplementedError",
        "StopIteration",
        "ScriptError",
        "SystemError",
        "SystemExit",
        "SecurityError",
        "ZeroDivisionError",
        "FrozenError",
        "Exception",
    }
)
# The render target a rescue hands the error to. These are the project-error-shape
# signals: a JSON error render, a project render_error/render_data helper, or a
# named *Serializer/*Error call. The dominant one names the shape the principle
# points the model at; absence just means we record the rescue rate without a shape.
# The *Serializer/*Error matcher captures the class name so a built-in exception
# class (a raised stdlib error, not a render target) can be excluded by the caller.
_RUBY_ERROR_RENDER_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("render json: { error", re.compile(r"render\s+json:\s*\{\s*errors?\b")),
    ("render_error", re.compile(r"\brender_error\b")),
    ("ErrorSerializer", re.compile(r"\b(\w*Error(?:Serializer)?)\.new\b")),
)


def extract_error_handling_conventions(files: list[ParsedFile], *, language: str) -> dict:
    """Measure the archetype's dominant error-handling shape by reading bytes.

    Mirrors ``extract_inheritance_conventions``: re-reads each file's bytes (off
    the hot path, at bootstrap) and tallies a regex signal, then gates on
    ``MIN_SAMPLE_SIZE`` and the 60% frequency floor.

    For TypeScript the signal is "this file's body contains a try block". For
    Ruby the signal is "this file declares rescue_from" -- the controller-base
    pattern, not per-action inline rescue, which is the wrong unit for Rails.
    When a Ruby archetype clears the floor we also record the dominant error
    render target so a principle can name the project error shape.

    Returns ``{}`` when the sample is too thin or no shape clears the floor.
    Result shape: ``{"rescues"|"try_catch": <freq>, "sample_size": N,
    optional "error_shape": <target>}``.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {}

    total = len(files)
    handled = 0
    shape_counts: Counter[str] = Counter()

    for f in files:
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        if language == "ruby":
            if _RUBY_RESCUE_FROM_RE.search(content):
                handled += 1
            for label, pat in _RUBY_ERROR_RENDER_RES:
                # The *Error/*Serializer matcher captures the class name; a bare
                # `StandardError.new` (and the other stdlib exceptions) is a raised
                # built-in, not the project render shape. Scan every match so a real
                # custom serializer later in the file still counts even when a
                # built-in `raise` precedes it.
                if label == "ErrorSerializer":
                    if not any(
                        mm.group(1) not in _RUBY_BUILTIN_ERROR_CLASSES
                        for mm in pat.finditer(content)
                    ):
                        continue
                elif not pat.search(content):
                    continue
                shape_counts[label] += 1
                break
        elif language == "python":
            if _PY_TRY_RE.search(content):
                handled += 1
        else:
            if _TS_TRY_RE.search(content):
                handled += 1

    frequency = handled / total if total else 0.0
    if frequency < threshold("ERROR_HANDLING_FREQUENCY"):
        return {}

    key = "rescues" if language == "ruby" else "try_catch"
    result: dict = {key: round(frequency, 3), "sample_size": total}
    if shape_counts:
        dominant_shape, shape_count = shape_counts.most_common(1)[0]
        if shape_count / total >= threshold("ERROR_HANDLING_FREQUENCY"):
            result["error_shape"] = dominant_shape
    return result


_TS_EXPORT_NAME_RE = re.compile(
    r"^\s*export\s+(?:const|let|var|function|class|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)
# Capture the full namespaced name (Api::V1::Foo); a bare \w+ stops at the
# first '::' and records the outer namespace ("Api") for every namespaced
# class, polluting the key-export list and losing the real name. Callers take
# the last "::" segment as the meaningful export name.
_RUBY_CLASS_NAME_RE = re.compile(r"^\s*class\s+([\w:]+)", re.MULTILINE)
_RUBY_MODULE_NAME_RE = re.compile(r"^\s*module\s+([\w:]+)", re.MULTILINE)
# A valid Ruby constant name (class/module segment): starts uppercase, word-only.
# The class/module captures accept `[\w:]+`, which admits a single-colon
# non-constant (`javascript:alert`) and, after the `::` split, an empty remnant;
# both leaked into key_exports as junk "reuse these" names. A real Ruby constant
# always matches this, so it is a lossless filter.
_RUBY_CONST_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


# Stored-artifact cap only: every prompt-side consumer re-caps downstream (the
# SessionStart union to _MAX_CONVENTION_ITEMS, the stale-test advisory to its
# own export cap), so this bounds conventions.json size, not context. Wide
# archetypes on large Rails repos legitimately exceed 200 distinct exports, and
# truncation here blinds the name-collision and stale-test lookups to the tail.
_MAX_KEY_EXPORTS = _int_env("CHAMELEON_MAX_KEY_EXPORTS", 400)

# Generous ceiling on the ASSEMBLED SessionStart convention block. Per-archetype
# counts are small, but their UNION across a large monorepo (hundreds of
# archetypes) is not — and an 80K-token wall of comma-separated names dilutes
# the model's attention, which hurts quality. So cap the two repo-size-scaling
# sinks (preferred imports, key-export union) at a generous, env-overridable
# value with an explicit "+N more" tail (no silent drop). 60 >> the real signal
# in any normal repo; raise CHAMELEON_MAX_CONVENTION_ITEMS to lift it.
_MAX_CONVENTION_ITEMS = _int_env("CHAMELEON_MAX_CONVENTION_ITEMS", 60)


def extract_key_exports(files: list[ParsedFile], *, language: str) -> list[str]:
    """Extract the most common exported names across files in an archetype."""
    if not _meets_class_sample_gate(files, language):
        return []

    name_counts: Counter[str] = Counter()
    for f in files:
        seen: set[str] = set()
        if language == "python":
            # named_export_names is the IMPORTABLE-name set (it exists for the
            # phantom-symbol existence check), so by construction it folds in every
            # module-level import local. key_exports drives the "reuse this before
            # creating a new one" directive, so an import (json, os, models, User,
            # reverse, ...) must NOT appear -- it is not defined in the archetype
            # and cannot be reused instead of created. Subtract the import locals so
            # only names DEFINED here (classes, functions, module-level assigns)
            # remain, matching the TS export-only and Ruby class/module sets.
            import_locals: set[str] = set()
            for row in f.extras.get("import_symbols") or []:
                if isinstance(row, dict) and isinstance(row.get("local"), str):
                    import_locals.add(row["local"])
            for row in f.extras.get("namespace_imports") or []:
                if isinstance(row, dict) and isinstance(row.get("alias"), str):
                    import_locals.add(row["alias"])
            for name in f.extras.get("named_export_names") or []:
                if name.startswith("_") or name in seen or len(name) <= 1 or name in import_locals:
                    continue
                name_counts[name] += 1
                seen.add(name)
            continue
        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        if language == "typescript":
            for m in _TS_EXPORT_NAME_RE.finditer(content):
                name = m.group(1)
                if name not in seen and len(name) > 1:
                    name_counts[name] += 1
                    seen.add(name)
        elif language == "ruby":
            # Scan string/comment-stripped content: a `class`/`module` keyword inside
            # a heredoc or string literal is fixture text, not a definition — a Go
            # go.mod heredoc (`module javascript:alert()`, `module example.com/...`)
            # in a spec fixture was captured as an "export". The stripper blanks those
            # bodies length-preservingly. The constant-shape filter is a second gate:
            # it drops the single-colon `javascript:alert` and any empty split remnant
            # the `[\w:]+` capture would otherwise admit.
            from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments

            ruby_scan = _strip_ruby_strings_and_comments(content)
            for _rx in (_RUBY_CLASS_NAME_RE, _RUBY_MODULE_NAME_RE):
                for m in _rx.finditer(ruby_scan):
                    name = m.group(1).split("::")[-1]  # Api::V1::Foo -> Foo
                    if not _RUBY_CONST_RE.match(name):
                        continue
                    if name not in seen:
                        name_counts[name] += 1
                        seen.add(name)

    skip = {"default", "module", "class", "React", "Component", "ApplicationRecord", "Base"}
    result = []
    for name, _count in name_counts.most_common(_MAX_KEY_EXPORTS + len(skip)):
        if name in skip:
            continue
        # Ruby: a bare leaf defined in 2+ files is an AMBIGUOUS reuse hint. The
        # block-nested Rails form (`module PushNotifications; class Send`) loses its
        # namespace in the regex scan, so several distinct classes (Notifications::
        # Send, PushNotifications::Send, ...) collapse to one bare `Send`. `seen` is
        # per-file, so name_counts is the number of files defining the leaf: a
        # genuinely unique class is 1, a multi-namespace collision is >1. Offering
        # bare `Send`/`Add`/`Remove` as "reuse this before creating a new one" is
        # not actionable, so drop the ambiguous ones (TS/Python export names are
        # already namespace-unique, so this is scoped to Ruby).
        if language == "ruby" and _count >= 2:
            continue
        result.append(name)
        if len(result) >= _MAX_KEY_EXPORTS:
            break
    return result


def extract_method_call_conventions(files: list[ParsedFile]) -> dict:
    """Extract top DSL/method call patterns by reading file content."""
    if len(files) < MIN_SAMPLE_SIZE:
        return {}

    total = len(files)
    call_counts: Counter[str] = Counter()

    for f in files:
        seen: set[str] = set()
        # The parser already recorded every class-body macro call with its
        # enclosing class, at any module-nesting depth. Prefer it: it cannot be
        # fooled by indentation the way a line regex can. The raw-bytes regex
        # stays as the fallback for a degraded or legacy profile whose extras
        # carry no class_body_calls.
        body_calls = (getattr(f, "extras", None) or {}).get("class_body_calls")
        if isinstance(body_calls, list) and body_calls:
            for call in body_calls:
                name = call.get("name") if isinstance(call, dict) else None
                if name in _RUBY_DSL_ALLOWLIST and name not in seen:
                    call_counts[name] += 1
                    seen.add(name)
            continue

        try:
            content = f.path.read_bytes()[:50_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        for m in _RUBY_DSL_CALL_RE.finditer(content):
            call = m.group(1)
            if call not in seen:
                call_counts[call] += 1
                seen.add(call)

    if not call_counts:
        return {}

    # Store ALL matched DSL calls (the regex is already an allow-list, so this is
    # naturally bounded); the 'common_top5' key name is kept for back-compat.
    common_top5 = [name for name, _count in call_counts.most_common()]
    return {"common_top5": common_top5, "sample_size": total}


# Body-shape dimensions, ordered with the structural signals first. Branch count
# and nesting depth are the primary complexity signal; raw line span is
# secondary because long-but-flat code (literal tables, JSX trees, switch
# dispatch) inflates it without being hard to read.
_BODY_SHAPE_DIMENSIONS = ("branch_count", "max_depth", "line_span", "param_count")


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (0.0 <= pct <= 1.0)."""
    if not sorted_values:
        return 0.0
    if pct <= 0:
        return sorted_values[0]
    if pct >= 1:
        return sorted_values[-1]
    # Nearest-rank: ceil(pct * n) clamped into range, 1-indexed.
    rank = int(-(-pct * len(sorted_values) // 1))  # ceil without importing math
    rank = max(1, min(rank, len(sorted_values)))
    return sorted_values[rank - 1]


def _collect_function_scopes(files: list[ParsedFile]) -> list[dict]:
    """Gather every per-function body-shape record carried in ``extras``.

    Skips files whose extractor emitted no scopes (interfaces, type-only
    modules, config) so they don't dilute the function pool.
    """
    scopes: list[dict] = []
    for f in files:
        raw = (f.extras or {}).get("function_scopes")
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if isinstance(entry, dict):
                scopes.append(entry)
    return scopes


def extract_body_shape_conventions(files: list[ParsedFile]) -> dict:
    """Per-archetype body-shape norms: median/p90 of each function dimension.

    Derived from the per-function records the AST dumps emit. Requires a
    thicker function pool than the generic sample gate because a p90 from a
    handful of functions is too noisy to ground an outlier claim. Advisory
    only -- the result feeds context and a pr-review NIT, never a block rule.
    """
    min_functions = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
    scopes = _collect_function_scopes(files)
    if len(scopes) < min_functions:
        return {}

    dims: dict = {}
    for dim in _BODY_SHAPE_DIMENSIONS:
        values = sorted(
            float(s[dim])
            for s in scopes
            if isinstance(s.get(dim), (int, float)) and s[dim] is not None
        )
        if not values:
            continue
        dims[dim] = {
            "median": round(_percentile(values, 0.5), 1),
            "p90": round(_percentile(values, 0.9), 1),
        }

    if not dims:
        return {}

    return {
        "sample_size": len(files),
        "function_count": len(scopes),
        "dimensions": dims,
    }


def _collect_callable_signatures(files: list[ParsedFile]) -> list[tuple[Path, dict]]:
    """Gather (source_file, header) pairs from every file's ``extras``.

    Each header is one declaration's name, kind, param shape, and (for Ruby) its
    enclosing class + base. The source file is carried so per-name file counts
    can be computed without double-counting overloads within one file.
    """
    pairs: list[tuple[Path, dict]] = []
    for f in files:
        raw = (f.extras or {}).get("callable_signatures")
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                pairs.append((f.path, entry))
    return pairs


def _param_identity(params: list) -> tuple:
    """Literal (name, type) fingerprint of a param list, ignoring `optional`.

    `optional` is already folded into the caller's arity/mask consensus; the
    identity is only the part a mask match does NOT guarantee agrees: whether
    the params are actually the same name and type, not just the same shape
    (e.g. one required positional param) with a different DI type in every file.
    """
    return tuple((p.get("name"), p.get("type")) for p in params if isinstance(p, dict))


def _consensus_param_shape(headers: list[dict]) -> dict:
    """Pick the representative parameter shape for one callable name.

    The most common positional-arity wins, and within that arity the most common
    optional-mask (``agreement``). ``params`` is filled from the mask group's
    MOST COMMON literal (name, type) identity, not just whichever header the
    scan reaches first, and ``params_agreement`` counts how many of that mask
    group actually share that literal identity — so a consumer can tell a
    genuine consensus (``params_agreement == agreement``: every sibling truly
    has this same param) from a shape-only match (e.g. a controller
    constructor's DI param that is a different service type per file, so
    ``params`` is only one file's literal names/types representing an arity the
    others share, not a value they all repeat).
    """
    by_arity: Counter[int] = Counter()
    for h in headers:
        params = h.get("params") or []
        if isinstance(params, list):
            by_arity[len(params)] += 1
    if not by_arity:
        return {"params": [], "agreement": 0, "params_agreement": 0, "sample": len(headers)}
    dominant_arity, _ = by_arity.most_common(1)[0]

    masks: Counter[tuple[bool, ...]] = Counter()
    identities: dict[tuple[bool, ...], Counter[tuple]] = defaultdict(Counter)
    representative: dict[tuple[bool, ...], dict[tuple, list]] = defaultdict(dict)
    for h in headers:
        params = h.get("params") or []
        if not isinstance(params, list) or len(params) != dominant_arity:
            continue
        mask = tuple(bool(p.get("optional")) for p in params if isinstance(p, dict))
        masks[mask] += 1
        identity = _param_identity(params)
        identities[mask][identity] += 1
        representative[mask].setdefault(identity, params)
    if not masks:
        return {"params": [], "agreement": 0, "params_agreement": 0, "sample": len(headers)}
    dominant_mask, agreement = masks.most_common(1)[0]
    dominant_identity, params_agreement = identities[dominant_mask].most_common(1)[0]
    return {
        "params": representative[dominant_mask][dominant_identity],
        "agreement": agreement,
        "params_agreement": params_agreement,
        "sample": len(headers),
    }


def extract_callable_signatures(files: list[ParsedFile]) -> dict:
    """Per-archetype consensus on the callable shapes its members declare.

    Records, for each callable name that the archetype's files share, the
    representative parameter shape (positional arity + which slots are optional)
    and the in-repo base class the name is overridden from when one exists. This
    is structured context for a full-file review comparison; it is never a
    block-eligible rule and the parameter naming is advisory, not a hard schema.
    """
    pairs = _collect_callable_signatures(files)
    if not pairs:
        return {}

    min_files = int(threshold("CALLABLE_SIGNATURE_MIN_FILES"))
    max_names = int(threshold("CALLABLE_SIGNATURE_MAX_NAMES"))

    headers_by_name: dict[str, list[dict]] = {}
    files_by_name: dict[str, set[str]] = {}
    # An in-repo base class for a name: recorded only when the SAME name is
    # defined directly on a class whose own base is also captured in this corpus.
    # That keeps the override hint to intra-repo hierarchies; framework bases
    # (ApplicationController, Sidekiq) are invisible here and never asserted.
    defined_classes: set[str] = set()
    base_by_name: dict[str, Counter[str]] = {}

    for source, header in pairs:
        name = header["name"]
        headers_by_name.setdefault(name, []).append(header)
        files_by_name.setdefault(name, set()).add(str(source))
        cls = header.get("enclosing_class")
        if isinstance(cls, str) and cls:
            defined_classes.add(cls)
        base = header.get("base_class")
        if isinstance(base, str) and base:
            base_by_name.setdefault(name, Counter())[base] += 1

    ranked = sorted(
        headers_by_name.items(),
        key=lambda kv: (-len(files_by_name.get(kv[0], set())), kv[0]),
    )

    signatures: dict[str, dict] = {}
    for name, headers in ranked:
        if len(signatures) >= max_names:
            break
        file_count = len(files_by_name.get(name, set()))
        if file_count < min_files:
            continue
        shape = _consensus_param_shape(headers)
        kinds = Counter(h.get("kind") for h in headers if isinstance(h.get("kind"), str))
        entry: dict = {
            "kind": kinds.most_common(1)[0][0] if kinds else "function",
            "params": shape["params"],
            "agreement": shape["agreement"],
            # How many of the `agreement` files also share `params`' literal
            # name/type, not just its arity + optional-mask. Equal to
            # `agreement` means every sibling genuinely has this same param;
            # lower (e.g. a DI constructor where each file injects a different
            # service) means `params` is only one file's representative shape
            # for an arity/mask the rest merely happen to share, not a literal
            # value repeated across the archetype.
            "params_agreement": shape["params_agreement"],
            "file_count": file_count,
        }
        bases = base_by_name.get(name)
        if bases:
            base, _ = bases.most_common(1)[0]
            # Only assert the base when its class is itself defined in this
            # corpus, so the override hint stays within the repo's own hierarchy.
            if base in defined_classes:
                entry["overrides_base"] = base
        signatures[name] = entry

    if not signatures:
        return {}
    return {"sample_size": len(files), "signatures": signatures}


def extract_all_conventions(
    *,
    files_by_archetype: dict[str, list[ParsedFile]],
    declarations_by_archetype: dict[str, dict[str, list[str]]],
    generation: int,
    language: str = "typescript",
    doc_coverage_by_archetype: dict[str, list[tuple[int, int]]] | None = None,
    repo_root: Path | None = None,
    all_files: list[ParsedFile] | None = None,
) -> dict:
    """Extract import and naming conventions for each archetype.

    Called by the bootstrap orchestrator after clustering.  Returns a
    full conventions dict ready for ``serialize_conventions`` and
    writing to ``conventions.json``.

    ``language`` (the profile's extractor language) gates the Ruby/Rails-only
    extractors so a TypeScript repo doesn't get bogus inheritance / DSL
    conventions, and selects the key-export extraction mode.

    ``doc_coverage_by_archetype`` carries the per-file (documented, public)
    declaration counts the orchestrator gathers during its per-member re-read.
    Passed in rather than recomputed here so each member file is read once, not
    a second time, during convention extraction.

    ``repo_root`` enables the repo-level import-layering graph (resolving each
    file's relative/alias imports to a target path -> archetype). When omitted
    the layering section stays empty; every other convention is unaffected.

    ``all_files`` is the FULL parsed corpus (dense + sparse cluster members) the
    repo-wide preferred-imports pass samples. When omitted it falls back to the
    union of ``files_by_archetype`` -- the dense-cluster members only, which
    undercounts on a heavily sparse repo.
    """
    conventions = empty_conventions(generation=generation)
    for archetype, files in files_by_archetype.items():
        import_conv = extract_import_conventions(files)
        if import_conv["preferred"] or import_conv["competing"]:
            conventions["conventions"]["imports"][archetype] = import_conv
    corpus = all_files
    if corpus is None:
        seen_corpus_paths: set[str] = set()
        corpus = []
        for files in files_by_archetype.values():
            for f in files:
                key = str(getattr(f, "path", "")) or str(id(f))
                if key in seen_corpus_paths:
                    continue
                seen_corpus_paths.add(key)
                corpus.append(f)
    repo_wide = extract_repo_wide_import_conventions(corpus)
    if repo_wide["preferred"]:
        conventions["conventions"]["repo_imports"] = repo_wide
    for archetype, files in files_by_archetype.items():
        ordering_conv = extract_import_ordering_conventions(files)
        if ordering_conv:
            conventions["conventions"].setdefault("import_ordering", {})[archetype] = ordering_conv
    for archetype, declarations in declarations_by_archetype.items():
        naming_conv = extract_naming_conventions(declarations=declarations)
        if naming_conv:
            conventions["conventions"]["naming"][archetype] = naming_conv
    # File-naming is path-only and language-agnostic, so it runs for every
    # archetype off the member basenames rather than the content-derived
    # declarations. Merge into the same per-archetype naming slot so the prefix
    # and file-naming conventions coexist.
    for archetype, files in files_by_archetype.items():
        basenames = [f.path.name for f in files if getattr(f, "path", None) is not None]
        file_naming = extract_file_naming_convention(basenames=basenames)
        if file_naming:
            conventions["conventions"]["naming"].setdefault(archetype, {}).update(file_naming)
    if language in ("ruby", "python"):
        # Inheritance derivation applies to both class-based languages: a Django
        # model cohort sharing ``models.Model``, a DRF view cohort sharing
        # ``APIView``, a Rails controller cohort sharing ``ApplicationController``.
        # The DSL/method-call and required-guard derivations below stay Ruby-only.
        for archetype, files in files_by_archetype.items():
            inheritance_conv = extract_inheritance_conventions(files, language=language)
            if inheritance_conv:
                conventions["conventions"].setdefault("inheritance", {})[archetype] = (
                    inheritance_conv
                )
    if language == "ruby":
        for archetype, files in files_by_archetype.items():
            method_conv = extract_method_call_conventions(files)
            if method_conv:
                conventions["conventions"].setdefault("method_calls", {})[archetype] = method_conv
        for archetype, files in files_by_archetype.items():
            contract = extract_class_contract_conventions(files, language=language)
            if contract:
                conventions["conventions"].setdefault("class_contract", {})[archetype] = contract
        # Required-guard derivation reuses the inheritance result so the
        # consuming check can suppress a miss when the archetype's own base
        # controller (its dominant/known bases) is the one carrying the guard.
        inheritance_section = conventions["conventions"].get("inheritance", {})
        for archetype, files in files_by_archetype.items():
            guard_conv = extract_required_guards_conventions(files)
            if not guard_conv:
                continue
            inh = inheritance_section.get(archetype)
            if isinstance(inh, dict):
                bases = list(inh.get("known_bases") or ())
                dominant = inh.get("dominant_base")
                if dominant and dominant not in bases:
                    bases.append(dominant)
                if bases:
                    guard_conv["known_bases"] = sorted(bases)
            conventions["conventions"].setdefault("required_guards", {})[archetype] = guard_conv
    if language == "python":
        # The Python analog of the Rails required-guard derivation: a DRF/Django
        # view cohort that conventionally restricts access. Stored under the same
        # required_guards key so the edit-time wiring and lint dispatch are
        # shared; the lint branches on language. Reuses the inheritance result so
        # a view inheriting authz from a known project base is not flagged.
        inheritance_section = conventions["conventions"].get("inheritance", {})
        for archetype, files in files_by_archetype.items():
            guard_conv = extract_python_authz_guard_conventions(files)
            if not guard_conv:
                continue
            inh = inheritance_section.get(archetype)
            if isinstance(inh, dict):
                bases = list(inh.get("known_bases") or ())
                dominant = inh.get("dominant_base")
                if dominant and dominant not in bases:
                    bases.append(dominant)
                if bases:
                    guard_conv["known_bases"] = sorted(bases)
            conventions["conventions"].setdefault("required_guards", {})[archetype] = guard_conv
    for archetype, files in files_by_archetype.items():
        exports = extract_key_exports(files, language=language)
        if exports:
            conventions["conventions"].setdefault("key_exports", {})[archetype] = exports
    for archetype, files in files_by_archetype.items():
        body_shape = extract_body_shape_conventions(files)
        if body_shape:
            conventions["conventions"].setdefault("body_shape", {})[archetype] = body_shape
    # Error-handling shape runs for both languages off the file bytes: TS counts
    # function bodies with a try block, Ruby counts files declaring rescue_from
    # (the controller-base pattern, not per-action inline rescue).
    for archetype, files in files_by_archetype.items():
        error_handling = extract_error_handling_conventions(files, language=language)
        if error_handling:
            conventions["conventions"].setdefault("error_handling", {})[archetype] = error_handling
    if doc_coverage_by_archetype:
        for archetype, coverage_by_file in doc_coverage_by_archetype.items():
            doc_conv = extract_doc_coverage_conventions(coverage_by_file)
            if doc_conv:
                conventions["conventions"].setdefault("doc_coverage", {})[archetype] = doc_conv
    # Test pairing is FS-stat based off the member paths, so it runs only when the
    # repo root is known (candidate test paths can't be resolved without it).
    if repo_root is not None:
        for archetype, files in files_by_archetype.items():
            test_pairing = extract_test_pairing_conventions(
                files, language=language, repo_root=repo_root
            )
            if test_pairing:
                conventions["conventions"].setdefault("test_pairing", {})[archetype] = test_pairing
    for archetype, files in files_by_archetype.items():
        signatures = extract_callable_signatures(files)
        if signatures:
            conventions["conventions"].setdefault("callable_signatures", {})[archetype] = signatures
    # Class contract for TS runs here (the Ruby branch above already ran it). TS captures
    # the decorator + heritage + required-method shape that has no inheritance section.
    if language != "ruby":
        for archetype, files in files_by_archetype.items():
            contract = extract_class_contract_conventions(files, language=language)
            if contract:
                conventions["conventions"].setdefault("class_contract", {})[archetype] = contract
    if repo_root is not None:
        from chameleon_mcp.bootstrap.import_graph import build_layering

        layering = build_layering(
            files_by_archetype=files_by_archetype,
            repo_root=repo_root,
            language=language,
        )
        if layering:
            conventions["conventions"]["layering"] = layering
    return conventions


def _fmt_metric(value: float) -> str:
    """Drop a trailing ``.0`` so an integer norm reads as ``28`` not ``28.0``."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _format_body_shape_lines(body_shape: dict) -> list[str]:
    """Render per-archetype body-shape norms as advisory context lines.

    One line per archetype, leading with the structural signal (nesting +
    branching) and trailing with the secondary line-span figure, so the model
    reads "functions here stay shallow and short" as data, not a rule. Skips an
    archetype whose stored norm is malformed rather than emitting a broken line.
    """
    lines: list[str] = []
    for arch, data in body_shape.items():
        if not isinstance(data, dict):
            continue
        dims = data.get("dimensions")
        if not isinstance(dims, dict):
            continue
        parts: list[str] = []
        for dim, label in (
            ("max_depth", "nesting"),
            ("branch_count", "branches"),
            ("line_span", "lines"),
        ):
            entry = dims.get(dim)
            if not isinstance(entry, dict):
                continue
            median = entry.get("median")
            p90 = entry.get("p90")
            if not isinstance(median, (int, float)) or not isinstance(p90, (int, float)):
                continue
            parts.append(f"{label} median {_fmt_metric(median)}, p90 {_fmt_metric(p90)}")
        if parts:
            lines.append(f"- {arch}: {'; '.join(parts)}")
    return lines


def _format_error_handling_lines(error_handling: dict) -> list[str]:
    """Render the per-archetype error-handling contract as advisory context.

    One line per archetype that cleared the frequency floor at bootstrap, framed
    as data the model matches against ("X% of files here rescue_from / try"),
    not a rule. When a dominant error render target was recorded, the line names
    it so the model knows the project error shape to match. Skips a malformed
    entry rather than emitting a broken line.
    """
    lines: list[str] = []
    for arch, data in error_handling.items():
        if not isinstance(data, dict):
            continue
        freq = data.get("rescues")
        shape = data.get("error_shape")
        if isinstance(freq, (int, float)):
            tail = f", into {shape}" if isinstance(shape, str) and shape else ""
            lines.append(
                f"- {arch}: {freq:.0%} of files rescue_from at the base; match it"
                f" (render the project error shape{tail})"
            )
            continue
        freq = data.get("try_catch")
        if isinstance(freq, (int, float)):
            lines.append(
                f"- {arch}: {freq:.0%} of files wrap their work in try/catch;"
                f" handle errors the way siblings do"
            )
    return lines


_IMPORT_ORDERING_LABELS = {
    "external-then-relative": "group external imports before relative",
    "relative-then-external": "group relative imports before external",
    "external": "import only external modules",
    "relative": "import only relative modules",
}


def _format_import_ordering_lines(import_ordering: dict) -> list[str]:
    """Render the per-archetype import grouping order as advisory context.

    One line per archetype that cleared the dominance floor, framed as data the
    model matches ("N/M siblings group external before relative") rather than a
    rule. Interleaved patterns (more than two runs) read as "no settled order"
    and emit nothing. Skips a malformed entry rather than emitting a broken line.
    """
    lines: list[str] = []
    for arch, data in import_ordering.items():
        if not isinstance(data, dict):
            continue
        pattern = data.get("pattern")
        matching = data.get("matching")
        total = data.get("total")
        label = _IMPORT_ORDERING_LABELS.get(pattern)
        if not label or not isinstance(matching, int) or not isinstance(total, int) or total <= 0:
            continue
        lines.append(f"- {arch}: {label} ({matching}/{total} siblings)")
    return lines


def _format_doc_coverage_lines(doc_coverage: dict) -> list[str]:
    """Render the per-archetype doc-coverage norm as advisory context.

    One line per archetype where the documented share cleared the floor, framed
    as data ("X% of public declarations here carry a doc comment") so the model
    documents a new public declaration the way siblings do. Skips a malformed
    entry rather than emitting a broken line.
    """
    lines: list[str] = []
    for arch, data in doc_coverage.items():
        if not isinstance(data, dict):
            continue
        fraction = data.get("fraction")
        if not isinstance(fraction, (int, float)):
            continue
        lines.append(
            f"- {arch}: {fraction:.0%} of public declarations carry a doc comment;"
            f" document new public declarations the way siblings do"
        )
    return lines


def _format_test_pairing_lines(test_pairing: dict) -> list[str]:
    """Render the per-archetype source-to-test pairing rate as advisory context.

    One line per archetype where the pairing rate cleared the floor, framed as
    data ("X% of files here ship a test, mapped <convention>") so the model adds
    a test for a new file the way siblings do. Skips a malformed entry rather
    than emitting a broken line.
    """
    lines: list[str] = []
    for arch, data in test_pairing.items():
        if not isinstance(data, dict):
            continue
        freq = data.get("frequency")
        if not isinstance(freq, (int, float)):
            continue
        mapping = data.get("mapping")
        tail = f", mapped {mapping}" if isinstance(mapping, str) and mapping else ""
        lines.append(
            f"- {arch}: {freq:.0%} of files ship a paired test{tail};"
            f" add a test for a new file the way siblings do"
        )
    return lines


def format_conventions_for_session(conventions: dict, *, principles_text: str = "") -> str:
    """Format conventions for SessionStart injection.

    Imperative framing for >=95% consistency, context for 60-95%.
    Skip anything below 60%.
    """
    lines: list[str] = []
    conv = conventions.get("conventions", {})

    import_lines: list[str] = []
    # A taught competing import is stored and ENFORCED per archetype, so render
    # its archetype scope (like NAMING lines do) rather than presenting it as a
    # repo-wide rule -- otherwise the model avoids a legitimate stdlib import
    # everywhere, not just in the governed archetype.
    competing_scopes: dict[tuple[str, str], list[str]] = {}
    for _arch, data in conv.get("imports", {}).items():
        if not isinstance(data, dict):
            continue
        for c in data.get("competing", []):
            # Tolerate a malformed competing entry (hand-edited conventions.json
            # / buggy merge) — drop just that entry, not the whole block. The
            # lint path is guarded the same way.
            if not isinstance(c, dict):
                continue
            pref, over = c.get("preferred"), c.get("over")
            if not pref or not over:
                continue
            archs = competing_scopes.setdefault((pref, over), [])
            if isinstance(_arch, str) and _arch not in archs:
                archs.append(_arch)
    for (pref, over), archs in sorted(competing_scopes.items()):
        scope = ", ".join(sorted(a for a in archs if a))
        suffix = f" ({scope} files)" if scope else ""
        import_lines.append(f"- Use {pref}, not {over}{suffix}")

    seen_preferred: set[str] = set()
    all_preferred: list[tuple[int, str, bool]] = []
    for _arch, data in conv.get("imports", {}).items():
        if not isinstance(data, dict):
            continue
        for p in data.get("preferred", []):
            if not isinstance(p, dict) or not p.get("module"):
                continue
            mod = p["module"]
            if mod not in seen_preferred:
                seen_preferred.add(mod)
                all_preferred.append((p.get("frequency", 0), mod, False))
    # Repo-wide preferred imports (the whole-corpus majority pass) render into
    # the same IMPORTS section marked repo-wide; a module already carried by an
    # archetype-scoped entry keeps its unmarked line.
    _repo_wide_data = conv.get("repo_imports", {})
    if isinstance(_repo_wide_data, dict):
        for p in _repo_wide_data.get("preferred", []):
            if not isinstance(p, dict) or not p.get("module"):
                continue
            mod = p["module"]
            if mod not in seen_preferred:
                seen_preferred.add(mod)
                all_preferred.append((p.get("frequency", 0), mod, True))
    all_preferred.sort(reverse=True)
    _pref_shown = _pref_total = 0
    for _freq, mod, _repo_wide in all_preferred:
        basename = mod.rsplit("/", 1)[-1]
        if len(basename) > 2 and basename not in ("index", "types", "utils"):
            _pref_total += 1
            if _pref_shown < _MAX_CONVENTION_ITEMS:
                suffix = " (repo-wide)" if _repo_wide else ""
                import_lines.append(f"- Prefer {mod}{suffix}")
                _pref_shown += 1
    if _pref_total > _pref_shown:
        import_lines.append(f"- (+{_pref_total - _pref_shown} more preferred modules)")

    naming_lines: list[str] = []
    seen_naming: set[str] = set()
    seen_file_naming: set[str] = set()
    for arch, data in conv.get("naming", {}).items():
        if not isinstance(data, dict):
            continue
        for key in ("interface_prefix", "type_prefix", "enum_prefix"):
            entry = data.get(key)
            if not entry or key in seen_naming:
                continue
            consistency = entry.get("consistency", 0)
            if consistency < _STRONG_THRESHOLD:
                continue
            seen_naming.add(key)
            type_name = key.replace("_prefix", "").replace("_", " ")
            pattern = entry["pattern"]
            pct = f"{consistency:.0%}"
            if consistency >= _ENFORCE_THRESHOLD:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct}, enforced)")
            else:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct})")
        # method_casing/class_casing/constant_casing (Ruby's in-source casing
        # signal from extract_naming_conventions) share the prefix keys'
        # {pattern, consistency} shape but read as "Name Xs in Y", not
        # "Prefix Xs with Y" -- a separate loop keeps each key's own wording.
        for key, plural in (
            ("method_casing", "methods"),
            ("class_casing", "classes"),
            ("constant_casing", "constants"),
        ):
            entry = data.get(key)
            if not entry or key in seen_naming:
                continue
            consistency = entry.get("consistency", 0)
            if consistency < _STRONG_THRESHOLD:
                continue
            seen_naming.add(key)
            pattern = entry["pattern"]
            pct = f"{consistency:.0%}"
            if consistency >= _ENFORCE_THRESHOLD:
                naming_lines.append(f"- Name {plural} in {pattern} ({pct}, enforced)")
            else:
                naming_lines.append(f"- Name {plural} in {pattern} ({pct})")
        # File-naming is per-archetype (a service folder may be kebab while a
        # component folder is Pascal), so it stays keyed by archetype rather
        # than deduped on the convention key alone.
        fn = data.get("file_naming")
        if isinstance(fn, dict) and arch not in seen_file_naming:
            casing = fn.get("casing")
            casing_consistency = fn.get("casing_consistency", 0)
            if casing and casing_consistency >= _STRONG_THRESHOLD:
                seen_file_naming.add(arch)
                pct = f"{casing_consistency:.0%}"
                suffix = fn.get("suffix")
                suffix_part = f", suffix {suffix}" if suffix else ""
                enforced = ", enforced" if casing_consistency >= _ENFORCE_THRESHOLD else ""
                naming_lines.append(f"- {arch} files use {casing}{suffix_part} ({pct}{enforced})")

    inheritance_lines: list[str] = []
    seen_inheritance: set[str] = set()
    for _arch, data in conv.get("inheritance", {}).items():
        if not isinstance(data, dict):
            continue
        base = data.get("dominant_base")
        if base and base not in seen_inheritance:
            seen_inheritance.add(base)
            freq = data.get("frequency", 0)
            if freq >= _ENFORCE_THRESHOLD:
                inheritance_lines.append(f"- Inherit {base} ({freq:.0%}, enforced)")
            elif freq >= _STRONG_THRESHOLD:
                inheritance_lines.append(f"- Inherit {base} ({freq:.0%})")
        include = data.get("dominant_include")
        if include and include not in seen_inheritance:
            seen_inheritance.add(include)
            inc_freq = data.get("include_frequency", 0)
            if inc_freq >= _STRONG_THRESHOLD:
                inheritance_lines.append(f"- Include {include} ({inc_freq:.0%})")

    contract_lines: list[str] = []
    class_contract = conv.get("class_contract", {})
    if isinstance(class_contract, dict):
        for _arch in sorted(class_contract):
            summary = _contract_summary(class_contract[_arch])
            if summary:
                contract_lines.append(f"- {_arch}: {summary}")
        if len(contract_lines) > _MAX_CONVENTION_ITEMS:
            overflow = len(contract_lines) - _MAX_CONVENTION_ITEMS
            contract_lines = contract_lines[:_MAX_CONVENTION_ITEMS]
            contract_lines.append(f"- (+{overflow} more)")

    guard_lines: list[str] = []
    seen_guards: set[str] = set()
    for _arch, data in conv.get("required_guards", {}).items():
        if not isinstance(data, dict):
            continue
        for guard in data.get("required_guards", []):
            if isinstance(guard, str) and guard not in seen_guards:
                seen_guards.add(guard)
                guard_lines.append(
                    f"- Controllers usually call before_action :{guard}; confirm "
                    f"authz is present or inherited"
                )

    method_lines: list[str] = []
    seen_methods: set[str] = set()
    for _arch, data in conv.get("method_calls", {}).items():
        if not isinstance(data, dict):
            continue
        for call in data.get("common_top5", []):
            if call not in seen_methods:
                seen_methods.add(call)
    if seen_methods:
        _dsl = sorted(seen_methods)
        _dsl_shown = _dsl[:_MAX_CONVENTION_ITEMS]
        _dsl_tail = f" (+{len(_dsl) - len(_dsl_shown)} more)" if len(_dsl) > len(_dsl_shown) else ""
        method_lines.append(f"- Common DSL: {', '.join(_dsl_shown)}{_dsl_tail}")

    export_lines: list[str] = []
    all_exports: set[str] = set()
    for _arch, names in conv.get("key_exports", {}).items():
        # Migration classes (Rails AddXToY, Django Migration, Alembic revisions)
        # are never reused, yet an alphabetical union across all archetypes lets
        # them dominate the capped "check before creating" reuse list on any
        # mature repo, hiding the real reusable exports. Exclude that archetype.
        if isinstance(_arch, str) and (_arch == "migration" or _arch.startswith("migration")):
            continue
        if not isinstance(names, (list, tuple, set)):
            continue
        for n in names:
            all_exports.add(n)
    if all_exports:
        sorted_exports = sorted(all_exports)
        shown = sorted_exports[:_MAX_CONVENTION_ITEMS]
        overflow = len(sorted_exports) - len(shown)
        tail = f" (+{overflow} more)" if overflow > 0 else ""
        export_lines.append(f"- Check before creating: {', '.join(shown)}{tail}")

    shape_lines = _format_body_shape_lines(conv.get("body_shape", {}))

    error_handling_lines = _format_error_handling_lines(conv.get("error_handling", {}))

    import_ordering_lines = _format_import_ordering_lines(conv.get("import_ordering", {}))

    doc_coverage_lines = _format_doc_coverage_lines(conv.get("doc_coverage", {}))

    test_pairing_lines = _format_test_pairing_lines(conv.get("test_pairing", {}))

    principle_lines: list[str] = []
    if principles_text:
        try:
            principle_lines = [
                f"- {line.split('. ', 1)[1] if '. ' in line else line}"
                for line in principles_text.strip().splitlines()
                if line.strip() and line[0].isdigit()
            ]
        except Exception:
            pass

    protocol_lines: list[str] = []
    if principles_text:
        try:
            in_protocol = False
            for line in principles_text.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("## anti-hallucination protocol"):
                    in_protocol = True
                    continue
                if in_protocol:
                    if stripped.startswith("## "):
                        break  # next section
                    if stripped.startswith("- "):
                        protocol_lines.append(stripped)
        except Exception:
            protocol_lines = []

    if (
        not import_lines
        and not naming_lines
        and not inheritance_lines
        and not contract_lines
        and not guard_lines
        and not method_lines
        and not export_lines
        and not shape_lines
        and not error_handling_lines
        and not import_ordering_lines
        and not doc_coverage_lines
        and not test_pairing_lines
        and not principle_lines
        and not protocol_lines
    ):
        return ""

    lines.append("<chameleon-conventions>")
    lines.append("PROJECT CONVENTIONS — authoritative, follow exactly like CLAUDE.md instructions.")
    lines.append(
        "These are the team's CURRENT decisions for this repo. Follow them on every edit. "
        "When a canonical witness — or the MAJORITY of existing files — diverges from a "
        "convention below, the convention wins: existing files may be mid-migration, and "
        "the old pattern's prevalence is exactly why the rule was recorded. Never infer a "
        "convention from sibling-file majority against a rule here, and never dismiss a "
        "rule as 'inverted from actual usage'."
    )
    lines.append("")
    if import_lines:
        lines.append(
            "IMPORTS (enforce — team decision; files still using the discouraged form "
            "are mid-migration, do not imitate them):"
        )
        lines.extend(import_lines)
        lines.append("")
    if naming_lines:
        lines.append("NAMING:")
        lines.extend(naming_lines)
        lines.append("")
    if inheritance_lines:
        lines.append("INHERITANCE:")
        lines.extend(inheritance_lines)
        lines.append("")
    if contract_lines:
        lines.append("CONTRACT:")
        lines.extend(contract_lines)
        lines.append("")
    if guard_lines:
        lines.append("AUTHZ (advisory):")
        lines.extend(guard_lines)
        lines.append("")
    if method_lines:
        lines.append("PATTERNS:")
        lines.extend(method_lines)
        lines.append("")
    if export_lines:
        lines.append("REUSE:")
        lines.extend(export_lines)
        lines.append("")
    if shape_lines:
        lines.append("SHAPE (advisory):")
        lines.extend(shape_lines)
        lines.append("")
    if error_handling_lines:
        lines.append("ERROR HANDLING (advisory):")
        lines.extend(error_handling_lines)
        lines.append("")
    if import_ordering_lines:
        lines.append("IMPORT ORDERING (advisory):")
        lines.extend(import_ordering_lines)
        lines.append("")
    if doc_coverage_lines:
        lines.append("DOC COVERAGE (advisory):")
        lines.extend(doc_coverage_lines)
        lines.append("")
    if test_pairing_lines:
        lines.append("TEST PAIRING (advisory):")
        lines.extend(test_pairing_lines)
        lines.append("")
    if principle_lines:
        lines.append("PRINCIPLES:")
        lines.extend(principle_lines)
        lines.append("")
    if protocol_lines:
        lines.append("ANTI-HALLUCINATION PROTOCOL:")
        lines.extend(protocol_lines)
        lines.append("")
    lines.append("</chameleon-conventions>")
    return "\n".join(lines)


def render_conventions_md(
    conventions: dict,
    principles_text: str | None = None,
    idioms_text: str | None = None,
) -> str:
    """Render `.chameleon/conventions.md` — the CLAUDE.md-channel mirror of the
    session conventions block.

    Content delivered through CLAUDE.md (directly or via an `@` import) carries
    materially more instruction authority than the same content injected by a
    hook: in the migration-scenario A/B (2026-07-11) the identical rule scored
    100% via this channel vs 40% as a leading hook advisory. This file is what a
    repo's CLAUDE.md `@.chameleon/conventions.md` line imports; bootstrap,
    refresh, and teach keep it in sync with conventions.json. Returns "" when
    there is nothing to render (caller then skips/removes the file) — including
    on malformed conventions data: a corrupt committed conventions.json must
    degrade the mirror, never crash a teach or bootstrap.

    ``idioms_text`` (active idioms.md content) renders as a TEAM IDIOMS gist
    section — name + first-sentence directive per taught idiom — so taught rules
    ride the high-authority channel ambiently instead of being re-pushed at the
    hooks; the Stop self-review keys its terse rendering off this section.

    Inputs are sanitized with the same boundary treatment as the SessionStart
    injection path (`_sanitize_profile_obj` on the dict, prose scrub on
    principles, the boundary sanitizer on idiom gists): the mirror enters the
    memory channel with FULL instruction authority, so a tag-boundary or
    injection token in a taught/committed value must be neutralized here exactly
    as it is at the hook boundary — parity, not a weaker standard, for the
    stronger channel.
    """
    idioms_section: list[str] = []
    try:
        if idioms_text and idioms_text.strip():
            from chameleon_mcp.tools import MIRROR_IDIOMS_HEADER, render_idiom_gists

            gists = render_idiom_gists(idioms_text)
            if gists:
                idioms_section = [
                    MIRROR_IDIOMS_HEADER,
                    *gists.splitlines(),
                    "",
                ]
    except Exception:
        idioms_section = []
    try:
        import copy

        from chameleon_mcp.hook_helper import _sanitize_profile_obj
        from chameleon_mcp.profile.loader import scrub_conventions_prose
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        conv = copy.deepcopy(conventions) if isinstance(conventions, dict) else conventions
        if isinstance(conv, dict):
            scrub_conventions_prose(conv)
        conv = _sanitize_profile_obj(conv)
        if principles_text:
            principles_text = sanitize_for_chameleon_context(principles_text)
        block = format_conventions_for_session(conv, principles_text=principles_text)
    except Exception:
        return ""
    if not block and not idioms_section:
        return ""
    if not block:
        # Idioms-only profile (taught rules, nothing derivable, no principles):
        # the gists still need the authority preamble the session block carries.
        block = "PROJECT CONVENTIONS — authoritative, follow exactly like CLAUDE.md instructions.\n"
    body = [
        ln
        for ln in block.splitlines()
        if ln.strip() not in ("<chameleon-conventions>", "</chameleon-conventions>")
    ]
    if idioms_section:
        # Ahead of PRINCIPLES, so taught rules sit with the enforced convention
        # sections rather than trailing the generic protocol text.
        _at = next(
            (i for i, ln in enumerate(body) if ln.strip() == "PRINCIPLES:"),
            None,
        )
        if _at is None:
            body.extend(["", *idioms_section])
        else:
            body[_at:_at] = idioms_section
    header = (
        "<!-- Maintained by chameleon (bootstrap/refresh/teach rewrite it); do not\n"
        "     hand-edit. Wire it into Claude's memory channel with ONE of:\n"
        "     - .claude/rules/chameleon-conventions.md containing\n"
        "       @../../.chameleon/conventions.md   (auto-loads for the whole team)\n"
        "     - CLAUDE.local.md containing @.chameleon/conventions.md   (personal)\n"
        "     - a CLAUDE.md line @.chameleon/conventions.md   (edits the team file) -->"
    )
    return header + "\n\n" + "\n".join(body).strip() + "\n"


_SOURCE_EXTENSIONS = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rb",
        ".py",
    }
)

# A source filename never legitimately contains a control byte. A hostile or
# corrupt sibling name that does -- a newline that would split the single-line
# "Nearby:" listing, a CR/tab, or any other C0/DEL byte -- is stripped for
# display so the listing stays one clean line and attacker-controlled bytes
# cannot ride a planted filename into the model context. The downstream
# `sanitize_for_chameleon_context` deliberately preserves LF/CR/tab (they are
# meaningful in the canonical code excerpt it also wraps), so a filename that
# carries them must be scrubbed here, at the point a NAME (never code) is
# rendered. The file is still listed, name minus the control bytes, so a real
# "check before creating" candidate is never hidden.
_FILENAME_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _safe_display_name(name: str) -> str:
    return _FILENAME_CONTROL_RE.sub("", name)


def format_directory_listing(
    file_path: str | None, *, max_files: int = _int_env("CHAMELEON_MAX_SIBLINGS", 60)
) -> str:
    """List sibling files in the same directory, framed as actionable context.

    Returns something like:
    "Nearby: useDebounce.ts, useToggle.ts, useConfig.ts -- check before creating a new file."

    Returns empty string if directory doesn't exist, has 0 siblings, or file_path is None.
    """
    if not file_path:
        return ""
    try:
        parent = Path(file_path).parent
        if not parent.is_dir():
            return ""
        target_name = Path(file_path).name
        names = [
            entry.name
            for entry in parent.iterdir()
            if entry.is_file() and entry.suffix in _SOURCE_EXTENSIONS and entry.name != target_name
        ]
    except OSError:
        return ""
    if not names:
        return ""
    # Take the alphabetically-first ``max_files`` directly rather than sorting the
    # whole directory then slicing. heapq.nsmallest is O(d log max_files) and
    # returns the same deterministic prefix a full sort would, which keeps the
    # per-edit cost bounded on a flat directory holding thousands of files.
    display = heapq.nsmallest(max_files, names)
    # Flag the overflow so the model does not read a capped list as the complete
    # set and wrongly conclude a "reuse before creating" check came up empty.
    more = len(names) - len(display)
    tail = f" (+{more} more)" if more > 0 else ""
    # Sort/dedup on the raw name (deterministic), scrub control bytes only for
    # display so a planted newline/CR cannot break out of the one-line listing.
    safe = [_safe_display_name(n) for n in display]
    return f"Nearby: {', '.join(safe)}{tail} -- check before creating a new file."


def _contract_summary(cc: dict) -> str:
    """One-line human summary of a class_contract entry, or '' if empty.

    Shared by the edit-time echo and the SessionStart block so both phrase the
    contract identically: decorators, base, implemented interfaces, DSL macros,
    then required methods. `implements` sits right after `base` because both are
    heritage-clause anchors (TS `extends`/`implements`); for an archetype like a
    NestJS guard, the interface (CanActivate) is the one anchor here that a
    shared decorator (@Injectable, common to every provider) cannot express.
    """
    if not isinstance(cc, dict):
        return ""
    bits: list[str] = []
    base = cc.get("base")
    decorators = cc.get("decorators") or []
    implements = cc.get("implements") or []
    macros = cc.get("dsl_macros") or []
    methods = cc.get("required_methods") or []
    if decorators:
        bits.append("@" + "/@".join(str(d) for d in decorators[:3]))
    if base:
        bits.append(f"extends {base}")
    if implements:
        bits.append("implements " + ", ".join(str(i) for i in implements[:3]))
    if macros:
        bits.append("macros " + "/".join(str(m) for m in macros[:3]))
    if methods:
        bits.append("define " + ", ".join(str(m) for m in methods[:2]))
    return ", ".join(bits)


def format_conventions_echo(conventions: dict, *, archetype: str, principles_text: str = "") -> str:
    """Compact one-line convention echo for Tier 1 PreToolUse pointer. ~30 tokens max.

    Every dimension is scoped STRICTLY to the edited archetype, matching the
    Tier-2 ``_archetype_facts_section``. An earlier build fell back to the first
    archetype in each dimension's dict (``next(iter(...values()))``) "so the echo
    is never empty" -- but that leaked a DIFFERENT archetype's data into the
    pointer: a model edit whose archetype had no ``class_contract`` entry printed
    the first contract in the file (e.g. ``extends LiquidTagBase, define render``
    from a liquid-tag archetype, or ``ActiveRecord::Migration`` as ``Base:`` from
    a migration), directly contradicting the model's own ``Base: ApplicationRecord``
    line. A base class and a required-methods contract are archetype-SPECIFIC
    facts; showing the wrong one is worse than showing none. The fixed
    anti-hallucination reminder appended below guarantees the echo is never empty
    regardless, so the fallback bought nothing.
    """
    parts: list[str] = []
    conv = conventions.get("conventions", {})

    arch_imports = conv.get("imports", {}).get(archetype, {})
    if not isinstance(arch_imports, dict):
        arch_imports = {}
    for c in arch_imports.get("competing", [])[:2]:
        if isinstance(c, dict) and c.get("preferred"):
            parts.append(f"Imports: {c['preferred']}")
    if not parts:
        top_preferred = arch_imports.get("preferred", [])[:2]
        for p in top_preferred:
            if not isinstance(p, dict) or not p.get("module"):
                continue
            basename = p["module"].rsplit("/", 1)[-1]
            if len(basename) > 2 and basename not in ("index", "types", "utils"):
                parts.append(f"Imports: {p['module']}")
                break

    arch_naming = conv.get("naming", {}).get(archetype, {})
    if not isinstance(arch_naming, dict):
        arch_naming = {}
    for key in ("interface_prefix", "type_prefix"):
        entry = arch_naming.get(key)
        if entry and entry.get("consistency", 0) >= _STRONG_THRESHOLD:
            parts.append(f"Naming: {entry['pattern']}-prefix")
            break
    else:
        # No TS prefix convention for this archetype -- try Ruby's in-source
        # casing signal (method_casing/class_casing/constant_casing) instead,
        # the same fallback format_conventions_for_session's NAMING section
        # renders for these keys.
        for key, label in (
            ("method_casing", "methods"),
            ("class_casing", "classes"),
            ("constant_casing", "constants"),
        ):
            entry = arch_naming.get(key)
            if entry and entry.get("consistency", 0) >= _STRONG_THRESHOLD:
                parts.append(f"Naming: {label} in {entry['pattern']}")
                break

    arch_inheritance = conv.get("inheritance", {}).get(archetype, {})
    if not isinstance(arch_inheritance, dict):
        arch_inheritance = {}
    base = arch_inheritance.get("dominant_base")
    if base and arch_inheritance.get("frequency", 0) >= _STRONG_THRESHOLD:
        parts.append(f"Base: {base}")

    class_contract = conv.get("class_contract", {})
    if not isinstance(class_contract, dict):
        class_contract = {}
    arch_contract = class_contract.get(archetype, {})
    summary = _contract_summary(arch_contract)
    if summary:
        parts.append(f"Contract: {summary}")

    if principles_text:
        p_lines = [
            line.split(". ", 1)[1] if ". " in line else line
            for line in principles_text.strip().splitlines()
            if line.strip() and line[0].isdigit()
        ]
        if p_lines:
            # zlib.crc32 (not builtin hash()) so the chosen principle is stable
            # across processes — hash() is salted per-process via PYTHONHASHSEED.
            import zlib

            idx = zlib.crc32(archetype.encode("utf-8")) % len(p_lines)
            principle = p_lines[idx].rstrip()
            if len(principle) > 80:
                # Cut at a word boundary — a mid-word chop ("…over man.")
                # reads as garble in the most frequent injection users see.
                principle = principle[:80].rsplit(" ", 1)[0].rstrip(" ,;:.") + "..."
            else:
                # The joiner below adds ". "; avoid doubling a final period.
                principle = principle.rstrip(".")
            parts.append(principle)

    # Fixed anti-hallucination reminder, always present (not derived from
    # principles_text) so it shows on every edit regardless of the rotating
    # principle picked above.
    parts.append("Verify symbols/imports/paths exist before using them; don't invent")

    return ". ".join(parts)
