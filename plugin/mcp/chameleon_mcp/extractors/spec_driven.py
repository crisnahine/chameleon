"""Repo detection for the spec-driven languages.

These extractors DETECT only. Parsing is the in-process tree-sitter extractor's
job for every one of them, because a spec-driven language has no dump script to
fall back to -- which is why `can_handle` requires a real build manifest rather
than the mere presence of a source file. A single vendored `.go` file inside a
Python repo must not turn that repo into a Go profile, and a manifest is the
cheapest signal that says "this repo is BUILT as Go" rather than "this repo
contains Go".

`select_extractor` in the registry places these AFTER TypeScript and Ruby (whose
own markers are equally strong) and BEFORE Python, whose detector claims any
repo holding one `.py` file and would otherwise swallow a Go or Rust repo that
ships a single helper script. That position is only right while Python's claim
IS that weak one, so the registry breaks the other case with
:func:`outranked_by`.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParseResult
from chameleon_mcp.extractors.treesitter.grammars import extensions_by_language
from chameleon_mcp.extractors.treesitter.lang.specs import ALL as _SPECS

# Build manifests that identify a repo as belonging to a language. Every entry
# is a file a build tool REQUIRES, so its presence is a statement about the repo
# rather than about one file in it.
_MARKERS: dict[str, tuple[str, ...]] = {
    "go": ("go.mod", "go.work"),
    "rust": ("Cargo.toml",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"),
    "csharp": ("global.json", "Directory.Build.props"),
    "php": ("composer.json",),
    # The two FIRST-CLASS languages carry markers here as well, and the reason is
    # asymmetric: as a PRIMARY they are selected by their own extractors and
    # never consult this table, but as a SECONDARY in someone else's repo they
    # need the same manifest-plus-source bar every other language clears. Without
    # an entry a polyglot repo's Python service was invisible to
    # `languages_present`, so its files were never parsed and never indexed.
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"),
    "ruby": ("Gemfile", "Rakefile", ".ruby-version"),
    # TypeScript/JS belongs here for the SAME reason python and ruby do, and
    # its absence was the sharper gap: it is the usual PRIMARY, so forgetting
    # it costs nothing on a TS repo and everything on the most common polyglot
    # shape there is -- a Rails app with `app/javascript/`, a Django app with a
    # `frontend/` React tree. Those derived zero TypeScript archetypes.
    # `package.json` alone is NOT enough: a root one for prettier or husky is
    # routine in Ruby and Python repos, and the only `.js` left after
    # `discover_files` excludes node_modules is then a handful of root config
    # files (jest.config.js, tailwind.config.js) -- which share a path bucket and
    # a `module.exports` shape, clear the sparse floor, and put a BUILD CONFIG
    # forward as the canonical witness to imitate. `tsconfig`/`jsconfig` state
    # that the repo compiles its own sources, which a tooling-only dependency
    # does not.
    "typescript": ("tsconfig.json", "jsconfig.json"),
}


class SpecDrivenExtractor:
    """Detects one spec-driven language; parsing belongs to tree-sitter.

    Satisfies the `Extractor` protocol so the registry can treat it like any
    other, but `parse_repo` deliberately refuses: reaching it means the
    tree-sitter backend was unavailable, and there is no second parser for these
    languages. Refusing is what makes the orchestrator degrade to a clean failed
    report instead of writing a profile derived from nothing.
    """

    def __init__(self, language: str, extensions: tuple[str, ...]) -> None:
        self.language = language
        self._extensions = extensions

    @staticmethod
    def languages_present(repo_root: Path, *, exclude: str | None = None) -> tuple[str, ...]:
        """Every spec-driven language this repo genuinely contains.

        Same bar as :meth:`can_handle` -- a build manifest AND real source --
        applied to each language rather than to one. A repo whose PRIMARY
        language is TypeScript but which also ships a real Go service answers
        ``("go",)``, so the index builders can cover both while archetype
        derivation stays on the primary. The manifest requirement is what keeps
        a single vendored `.go` file from making that claim.
        """
        # Driven by the grammar table, NOT by the spec table. The spec table
        # holds only the five extraction-tier languages, so keying on it made
        # Python and Ruby invisible as SECONDARY languages: a polyglot repo's
        # Python service was never parsed and never indexed, even though
        # tree-sitter has a Python grammar and the repo carried a pyproject.
        # The grammar table is the one place that knows which extensions map to
        # which language, spec-driven and first-class alike.
        found: list[str] = []
        for language, extensions in extensions_by_language().items():
            if language == exclude or language not in _MARKERS:
                continue
            probe = SpecDrivenExtractor(language, extensions)
            try:
                if probe.can_handle(repo_root):
                    found.append(language)
            except Exception:
                continue
        return tuple(found)

    def can_handle(self, repo_root: Path) -> bool:
        """True when a build manifest AND real source of this language exist."""
        try:
            markers = _MARKERS.get(self.language, ())
            if not any((repo_root / marker).is_file() for marker in markers):
                # A marker may sit one level down in a monorepo half.
                if not _marker_in_child(repo_root, markers):
                    return False
            count, exhausted = _scan_source(repo_root, self._extensions, first_only=True)
            # An exhausted budget answers nothing, and the manifest above already
            # says the repo is BUILT as this language -- so an undecided walk
            # keeps the claim rather than dropping the language's whole corpus.
            return count > 0 or exhausted
        except Exception:
            # Not just OSError: the walk imports discovery's exclusion set at
            # call time, and select_extractor's loop does not wrap can_handle,
            # so an ImportError here would abort the whole bootstrap instead of
            # costing this one language.
            return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*",
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        raise ExtractorUnavailableError(
            f"{self.language!r} is parsed only by the in-process tree-sitter backend, "
            "which is unavailable here (grammar missing, ABI mismatch, or "
            "CHAMELEON_TREE_SITTER=0); no dump-script fallback exists for it"
        )


def _marker_in_child(repo_root: Path, markers: tuple[str, ...]) -> bool:
    try:
        for child in repo_root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if any((child / marker).is_file() for marker in markers):
                return True
    except OSError:
        return False
    return False


def _scan_source(
    repo_root: Path, extensions: tuple[str, ...], *, first_only: bool
) -> tuple[int, bool]:
    """Count the repo's source files of these extensions; report an exhausted walk.

    Returns ``(count, exhausted)``. The walk gives up after
    ``SPEC_MARKER_GLOB_CAP`` entries rather than crawling a giant tree to answer
    a yes/no question, and ``exhausted`` is what keeps that budget from reading
    as an absence -- a language reported absent is one whose whole corpus is
    silently dropped. ``first_only`` stops at the first hit, which is all the
    marker-plus-source bar needs.

    Exclusions are tested on the REPO-RELATIVE path. `rglob` yields absolute
    paths, so testing those matches directories ABOVE the checkout too: a repo
    living under `build/`, `tmp/` or `vendor/` would have every one of its own
    files skipped and answer "no source" for every language. Within the repo the
    exclusion still earns its keep -- `rglob` descends node_modules / .venv /
    vendor, and a large one could spend the whole budget before reaching real
    source. Suffixes are lowercased to match grammar selection, which does the
    same, so a `.CS` or `.PHP` file counts for both or for neither.
    """
    wanted = {ext.lower() for ext in extensions}
    cap = threshold_int("SPEC_MARKER_GLOB_CAP")
    seen = 0
    count = 0
    try:
        from chameleon_mcp.bootstrap.discovery import EXCLUDE_FROM_CLUSTERING_DIRS

        for path in repo_root.rglob("*"):
            try:
                rel = path.relative_to(repo_root)
            except ValueError:
                continue
            if EXCLUDE_FROM_CLUSTERING_DIRS & set(rel.parts):
                continue
            seen += 1
            if seen > cap:
                return count, True
            if path.suffix.lower() in wanted and path.is_file():
                count += 1
                if first_only:
                    return count, False
    except OSError:
        return count, False
    return count, False


def outranked_by(repo_root: Path, language: str, candidates: tuple[str, ...]) -> str | None:
    """The candidate language whose claim on ``repo_root`` beats ``language``'s.

    Both halves of the test matter. A candidate must clear the SAME
    manifest-plus-source bar :meth:`SpecDrivenExtractor.can_handle` demands, so
    a Go repo's lone `.py` helper script still makes no claim on it. And it must
    then carry strictly more source, so a Go repo that pins a `requirements.txt`
    for its tooling does not hand itself to Python either. What is left is the
    genuine tie -- a PyO3 crate's `pyproject.toml` beside its `Cargo.toml`, a
    Django repo with a `services/x/go.mod` sidecar -- where the language with
    the larger corpus is the one the repo is actually written in.

    Counts come from the same bounded walk detection uses, so a tree past the
    budget is compared on its first `SPEC_MARKER_GLOB_CAP` entries. The manifest
    bar is applied FIRST and costs nothing when it fails, so the ordinary Go or
    Rust repo -- no competing manifest anywhere -- pays no counting walk at all.
    """
    by_language = extensions_by_language()
    qualified: list[tuple[str, tuple[str, ...]]] = []
    for candidate in candidates:
        extensions = by_language.get(candidate, ())
        if extensions and SpecDrivenExtractor(candidate, extensions).can_handle(repo_root):
            qualified.append((candidate, extensions))
    if not qualified:
        return None

    winner: str | None = None
    best, _ = _scan_source(repo_root, by_language.get(language, ()), first_only=False)
    for candidate, extensions in qualified:
        count, _ = _scan_source(repo_root, extensions, first_only=False)
        if count > best:
            winner, best = candidate, count
    return winner


def extractor_classes() -> list[type]:
    """One extractor class per spec, in deterministic spec order."""
    built: list[type] = []
    for spec in _SPECS:
        built.append(
            type(
                f"{spec.name.capitalize()}Extractor",
                (SpecDrivenExtractor,),
                {
                    "__doc__": f"Detects a {spec.name} repo by its build manifest.",
                    "__init__": _make_init(spec.name, spec.extensions),
                },
            )
        )
    return built


def _make_init(language: str, extensions: tuple[str, ...]):
    def __init__(self) -> None:  # noqa: N807 - the generated class's own __init__
        SpecDrivenExtractor.__init__(self, language, extensions)

    return __init__
