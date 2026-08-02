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
ships a single helper script.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParseResult
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
}

# Extensions whose presence, alongside a marker, confirms the repo actually
# holds source of that language. A `composer.json` in a JS repo is real but says
# nothing on its own.
_MARKER_GLOB_CAP = 4000


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
        # `_EXTENSION_GRAMMARS` is the one place that knows which extensions map
        # to which language, spec-driven and first-class alike.
        from chameleon_mcp.extractors.treesitter.grammars import _EXTENSION_GRAMMARS

        by_language: dict[str, list[str]] = {}
        for ext, entry in _EXTENSION_GRAMMARS.items():
            by_language.setdefault(entry[2], []).append(ext)

        found: list[str] = []
        for language, extensions in sorted(by_language.items()):
            if language == exclude or language not in _MARKERS:
                continue
            probe = SpecDrivenExtractor(language, tuple(sorted(extensions)))
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
            return _has_source(repo_root, self._extensions)
        except OSError:
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


def _has_source(repo_root: Path, extensions: tuple[str, ...]) -> bool:
    """Whether the repo holds at least one source file of these extensions.

    Bounded: a marker plus one matching file is all the evidence needed, so the
    walk stops at the first hit and gives up after a cap rather than crawling a
    giant tree to answer a yes/no question.
    """
    seen = 0
    try:
        for path in repo_root.rglob("*"):
            seen += 1
            if seen > _MARKER_GLOB_CAP:
                return False
            if path.suffix in extensions and path.is_file():
                return True
    except OSError:
        return False
    return False


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
