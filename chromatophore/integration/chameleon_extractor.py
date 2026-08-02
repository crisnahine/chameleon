"""A chameleon `Extractor` backed by the chromatophore binary.

Drop this into a chameleon install and register it, and chameleon parses the
language it was constructed for through the engine instead of that language's
own dump script. Python is the only language whose records are byte-identical to
chameleon's own (see `verify.py`); Ruby and TypeScript are close but not
drop-in, so registering those is a deliberate trade, not a free swap.

The whole shim is the spawn plus the record loop, because the engine already
speaks chameleon's protocol: absolute paths on stdin, one NDJSON record per
file on stdout. Nothing here reshapes the records.

    from chameleon_mcp.extractors.registry import EXTRACTORS, PythonExtractor
    from chameleon_extractor import ChromatophoreExtractor

    EXTRACTORS.insert(EXTRACTORS.index(PythonExtractor), ChromatophoreExtractor)

Three details the seam does not make obvious, all verified against
`extractors/registry.py`.

`register()` APPENDS, and the shipped `PythonExtractor` already claims any repo
holding one `.py` file, so an appended entry is never reached.

The position is ahead of `PythonExtractor` and BEHIND the TypeScript and Ruby
detectors, because `select_extractor` instantiates with no arguments
(`ext = ext_cls()`), so this class always builds with `language="python"`. At
index 0 it would claim a TS monorepo that happens to hold one `scripts/gen.py`,
and chameleon branches archetype naming, framework layers and lint gates on
`.language`. Anchored to `PythonExtractor` rather than to a length, because
`register()` appends: any other extractor registered first moves a
`len(EXTRACTORS) - 1` insert BEHIND Python, which is the silent no-op above.

And `select_extractor` swaps in chameleon's in-process `TreeSitterExtractor` for
python, ruby and typescript whatever the registry says, so the engine runs only
with `CHAMELEON_TREE_SITTER=0` set.

`verify.py` in this directory checks that what the engine emits survives
chameleon's own `_parsed_file_from_record` unchanged, which is the only claim
that matters for a drop-in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# Reuse chameleon's own types so the shim cannot drift from the contract it
# claims to satisfy.
from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParsedFile, ParseResult

try:
    import xxhash
except ImportError:  # pragma: no cover - chameleon pins it; a host may not
    xxhash = None

BINARY_ENV = "CHROMATOPHORE_BIN"
BATCH_TIMEOUT_SECONDS = 600
# The engine's own per-file ceiling (`Limits::max_file_bytes`), which is also
# where chameleon's tree-sitter backend stops. Neither side emits a record for a
# file past it, so a digest computed past it would describe bytes nothing parsed.
MAX_SHA_BYTES = 1_000_000

# What each chameleon language identifier means on disk. Deriving the glob and
# the detection scan from the language is what stops a `ruby` instance from
# claiming a repo because it holds a `.py` file, and from then handing the engine
# a list of Python paths.
_EXTENSIONS = {
    "python": ("py", "pyi"),
    "ruby": ("rb", "rake", "gemspec"),
    "typescript": ("ts", "mts", "cts", "tsx", "js", "jsx", "mjs", "cjs"),
    "javascript": ("js", "jsx", "mjs", "cjs"),
    "go": ("go",),
    "rust": ("rs",),
    "java": ("java",),
    "csharp": ("cs",),
    "php": ("php",),
    "swift": ("swift",),
    "kotlin": ("kt", "kts"),
    "scala": ("scala", "sc"),
    "elixir": ("ex", "exs"),
    "haskell": ("hs",),
    "lua": ("lua",),
    "bash": ("sh", "bash"),
    "c": ("c", "h"),
    "cpp": ("cc", "cpp", "cxx", "hpp", "hh"),
}


class ChromatophoreUnavailableError(ExtractorUnavailableError):
    """The engine binary is not installed or not executable."""


def _binary() -> str:
    """Resolve the engine, preferring an explicit path over PATH."""
    explicit = os.environ.get(BINARY_ENV)
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise ChromatophoreUnavailableError(f"{BINARY_ENV}={explicit} is not executable")
        return explicit
    found = shutil.which("chromatophore")
    if not found:
        raise ChromatophoreUnavailableError(
            "chromatophore not on PATH; set CHROMATOPHORE_BIN to the binary"
        )
    return found


class ChromatophoreExtractor:
    """Parses via the chromatophore binary, emitting chameleon's ParsedFile."""

    def __init__(self, language: str = "python") -> None:
        # The language string stays chameleon's, because everything downstream
        # keys on it. Only the parsing backend changes.
        self.language = language

    def _extensions(self) -> tuple[str, ...]:
        return _EXTENSIONS.get(self.language, ("py",))

    def can_handle(self, repo_root: Path) -> bool:
        """True when the engine is installed and the repo holds a file it parses."""
        try:
            _binary()
        except ChromatophoreUnavailableError:
            return False
        for ext in self._extensions():
            for _ in repo_root.rglob(f"*.{ext}"):
                return True
        return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str | None = None,
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Parse files under ``repo_root``. Returns ParseResult.

        Argument order is the protocol's, not a convenience: every in-tree
        caller passes ``paths`` by keyword today, but a positional glob against
        a swapped signature binds the string to ``paths``, and `list()` of a
        string explodes it into characters.
        """
        if paths is not None:
            candidates = list(paths)
        elif glob is not None:
            candidates = sorted(repo_root.glob(glob))
        else:
            candidates = sorted(
                p for ext in self._extensions() for p in repo_root.glob(f"**/*.{ext}")
            )
        if limit is not None:
            candidates = candidates[:limit]
        if not candidates:
            return ParseResult(files=[], skipped=[])

        # abspath, NOT resolve(): resolve() follows symlinks, which hands the
        # engine an already-resolved target and defeats its own symlink refusal.
        # A repo holding `notes.py -> ~/.aws/credentials` would otherwise be read
        # and its first 200 bytes carried into the record. The containment check
        # covers the other direction, a symlinked parent directory, which a
        # final-component stat cannot see.
        root = os.path.realpath(repo_root)
        inside, escaped = [], []
        for p in candidates:
            absolute = os.path.abspath(p)
            real = os.path.realpath(absolute)
            if real == root or real.startswith(root + os.sep):
                inside.append((p, absolute))
            else:
                escaped.append((p, "path_outside_repo"))
        if not inside:
            return ParseResult(files=[], skipped=escaped)
        candidates = [p for p, _ in inside]
        # The set the symlink and containment checks above actually cleared.
        # Anything the engine echoes back is matched against it before this
        # process opens a file, so the echoed string can never widen the input
        # side's decision about what is readable.
        validated = {absolute for _, absolute in inside}

        stdin_data = "".join(f"{absolute}\n" for _, absolute in inside)
        proc = subprocess.Popen(
            [_binary(), "dump"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Explicit, because the ambient locale is C in cron, CI and hook
            # subprocesses, and serde_json emits non-ASCII UTF-8 raw rather than
            # escaping it. Strict decoding of one CJK comment would raise
            # UnicodeDecodeError out of communicate() -- not an
            # ExtractorUnavailableError, so not caught by the degrade path, and
            # the whole extraction run dies on one file.
            encoding="utf-8",
            errors="replace",
            # A scrubbed environment: the engine never executes repo code, and
            # this keeps it that way if it ever grows a plugin path. The cwd is
            # the repo because every path arrives absolute on stdin, so nothing
            # is resolved against it.
            cwd=str(repo_root),
            env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")},
        )

        timed_out = False
        try:
            stdout_data, stderr_data = proc.communicate(input=stdin_data, timeout=BATCH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, stderr_data = proc.communicate()
            timed_out = True

        files: list[ParsedFile] = []
        skipped: list[tuple[Path, str]] = list(escaped)
        for line in stdout_data.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = Path(record.get("path", ""))
            if "error" in record and "top_level_node_kinds" not in record:
                skipped.append((path, record["error"]))
                continue
            try:
                files.append(_parsed_file_from_record(path, record, validated))
            except (ValueError, TypeError):
                skipped.append((path, "malformed_record"))

        # Every file that never reached stdout is unknown, not clean, whatever
        # the exit status. A record line that failed to parse was dropped above
        # with no path to file it under, so on a CLEAN exit its file would
        # appear in neither list and read as one that was never in the corpus.
        rc = proc.returncode
        if timed_out or rc not in (0, None):
            reason = "extractor_timeout" if timed_out else f"extractor_exit_{rc}"
            # Only a dead child's stderr explains the gap. On a clean exit it
            # carries the engine's own progress lines, which would read as a
            # cause when the cause was one unusable record.
            detail = (stderr_data or "").strip()[:160]
            if detail:
                reason = f"{reason}: {detail}"
        else:
            reason = "extractor_no_record"
        seen = {str(f.path) for f in files} | {str(p) for p, _ in skipped}
        for p in candidates:
            if os.path.abspath(p) not in seen:
                skipped.append((p, reason))

        return ParseResult(files=files, skipped=skipped)


def _sha_hint(path: str) -> str | None:
    """The host-side digest chameleon's own extractors carry.

    Not a wire field: `sha_hint` feeds drift-cache invalidation and every shipped
    extractor computes it from the file's bytes on the host. Leaving it None made
    the shim diverge from the backend on every single file, in a slot the
    normalized contract declares.

    Takes a path this run already validated, never one read off the wire, and
    reads no further than the engine itself would.
    """
    if xxhash is None:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SHA_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_SHA_BYTES:
        # Past the ceiling both parsers stop at, so there is no record this
        # digest could belong to. None is the honest answer; a digest over a
        # truncated read would be a wrong one.
        return None
    return xxhash.xxh64(data).hexdigest()


def _parsed_file_from_record(path: Path, record: dict, validated: set[str]) -> ParsedFile:
    """Map one wire record onto chameleon's normalized dataclass.

    The normalized slots are the stability contract; everything else rides in
    `extras`.
    """
    # Every key is ALWAYS present, empty or not, because that is what chameleon's
    # own in-process extractor does and absent-vs-empty is a real distinction
    # downstream. The engine serializes all ten unconditionally, so `record.get`
    # never introduces a third state.
    always = (
        "function_scopes",
        "callable_signatures",
        "class_shapes",
        "call_sites",
        "call_sites_total",
        "call_sites_truncated",
    )
    when_present = (
        "import_symbols",
        "namespace_imports",
        "named_export_names",
        "export_set_open",
    )
    extras = {key: record.get(key) for key in always}
    extras.update({key: record.get(key) for key in when_present})

    # The digest is taken from the path this run cleared, not from the one the
    # record echoes: the symlink and containment checks ran on the input side,
    # and opening an echoed string would skip both. A path outside that set gets
    # no digest rather than an unchecked read.
    absolute = os.path.abspath(path)
    sha_hint = _sha_hint(absolute) if absolute in validated else None

    return ParsedFile(
        path=path,
        content_first_200_bytes=record.get("content_first_200_bytes", ""),
        top_level_node_kinds=tuple(record.get("top_level_node_kinds") or ()),
        default_export_kind=record.get("default_export_kind"),
        named_export_count=int(record.get("named_export_count") or 0),
        import_specifiers=tuple(tuple(pair) for pair in record.get("import_specifiers") or ()),
        has_jsx=bool(record.get("has_jsx")),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count") or 0),
        sha_hint=sha_hint,
        extras=extras,
    )
