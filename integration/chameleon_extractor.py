"""A chameleon `Extractor` backed by the chromatophore binary.

Drop this into a chameleon install and register it, and chameleon parses every
language the engine supports instead of the three its own dumpers cover.

The whole shim is the spawn plus the record loop, because the engine already
speaks chameleon's protocol: absolute paths on stdin, one NDJSON record per
file on stdout. Nothing here reshapes the records.

    from chameleon_mcp.extractors.registry import EXTRACTORS
    from chromatophore_extractor import ChromatophoreExtractor

    EXTRACTORS.insert(0, ChromatophoreExtractor)

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

BINARY_ENV = "CHROMATOPHORE_BIN"
BATCH_TIMEOUT_SECONDS = 600


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

    def can_handle(self, repo_root: Path) -> bool:
        """True when the engine is installed and the repo holds a file it parses."""
        try:
            _binary()
        except ChromatophoreUnavailableError:
            return False
        return any(repo_root.rglob("*.py"))

    def parse_repo(
        self,
        repo_root: Path,
        paths: list[Path] | None = None,
        glob: str = "**/*.py",
        limit: int | None = None,
    ) -> ParseResult:
        candidates = list(paths) if paths is not None else sorted(repo_root.glob(glob))
        if limit is not None:
            candidates = candidates[:limit]
        if not candidates:
            return ParseResult(files=[], skipped=[])

        stdin_data = "".join(f"{p.resolve()}\n" for p in candidates)
        proc = subprocess.Popen(
            [_binary(), "dump"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A neutral cwd and a scrubbed environment: the engine never
            # executes repo code, and this keeps it that way if it ever grows a
            # plugin path.
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
        skipped: list[tuple[Path, str]] = []
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
                files.append(_parsed_file_from_record(path, record))
            except (ValueError, TypeError):
                skipped.append((path, "malformed_record"))

        # A dead child means every file that never reached stdout is unknown,
        # not clean. Marking them keeps a truncated sample visible instead of
        # letting it read as the whole corpus.
        rc = proc.returncode
        if timed_out or rc not in (0, None):
            seen = {str(f.path) for f in files} | {str(p) for p, _ in skipped}
            reason = "extractor_timeout" if timed_out else f"extractor_exit_{rc}"
            detail = (stderr_data or "").strip()[:160]
            if detail:
                reason = f"{reason}: {detail}"
            for p in candidates:
                if str(p.resolve()) not in seen:
                    skipped.append((p, reason))

        return ParseResult(files=files, skipped=skipped)


def _parsed_file_from_record(path: Path, record: dict) -> ParsedFile:
    """Map one wire record onto chameleon's normalized dataclass.

    The eight normalized slots are the stability contract; everything else rides
    in `extras`.
    """
    # The six core keys are ALWAYS present, empty or not, because that is what
    # chameleon's own in-process extractor does and absent-vs-empty is a real
    # distinction downstream. The optional keys follow the dump scripts and are
    # omitted when empty.
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
    extras.update({key: record[key] for key in when_present if record.get(key)})

    return ParsedFile(
        path=path,
        content_first_200_bytes=record.get("content_first_200_bytes", ""),
        top_level_node_kinds=tuple(record.get("top_level_node_kinds") or ()),
        default_export_kind=record.get("default_export_kind"),
        named_export_count=int(record.get("named_export_count") or 0),
        import_specifiers=tuple(tuple(pair) for pair in record.get("import_specifiers") or ()),
        has_jsx=bool(record.get("has_jsx")),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count") or 0),
        extras=extras,
    )
