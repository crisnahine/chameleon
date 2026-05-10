"""Ruby AST extractor — Phase 8 (v1.5) implementation.

Spawns `scripts/prism_dump.rb` as a long-lived Ruby subprocess, sends file
paths via stdin (one per line), reads NDJSON ParsedFile records via
subprocess.communicate() (avoids the pipe-deadlock bug from Phase 5).

Per ARCHITECTURE.md "TypeScript-first extractor" → "v1.5 expansion"
+ ADR-0003.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import xxhash

from chameleon_mcp.extractors._base import Extractor, ParsedFile, ParseResult
from chameleon_mcp.plugin_paths import plugin_root


class RubyExtractor:
    """Ruby AST extractor backed by prism_dump.rb subprocess."""

    language = "ruby"

    _prism_dump_script: Path

    def __init__(self, prism_dump_script: Path | None = None) -> None:
        if prism_dump_script is None:
            self._prism_dump_script = plugin_root() / "scripts" / "prism_dump.rb"
        else:
            self._prism_dump_script = prism_dump_script

    def can_handle(self, repo_root: Path) -> bool:
        """Detect Ruby via Gemfile or *.gemspec."""
        if (repo_root / "Gemfile").exists():
            return True
        if any(repo_root.glob("*.gemspec")):
            return True
        return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*.rb",
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Parse Ruby files under `repo_root`. Returns ParseResult."""
        if paths is not None:
            files = list(paths)
        else:
            files = list(repo_root.glob(glob))

        if limit is not None:
            files = files[:limit]
        if not files:
            return ParseResult(files=[], skipped=[])

        if not self._prism_dump_script.exists():
            raise FileNotFoundError(
                f"prism_dump.rb not found at {self._prism_dump_script}; "
                "Phase 8 (v1.5) Ruby support requires the script."
            )

        # Build NDJSON-ish stdin: one path per line
        input_data = "".join(f"{fp.resolve()}\n" for fp in files)

        env = os.environ.copy()
        # Use system Ruby (3.3+ ships Prism as a default gem; rubies older than
        # that need `gem install prism`)
        proc = subprocess.Popen(
            ["ruby", str(self._prism_dump_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        # communicate() handles pipe-deadlock via background threads. 600s
        # timeout matches typescript.py for consistency.
        try:
            stdout_data, _stderr = proc.communicate(input=input_data, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, _stderr = proc.communicate()

        results: list[ParsedFile] = []
        skipped: list[tuple[Path, str]] = []
        for line in stdout_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = Path(record.get("path", ""))
            if "error" in record:
                skipped.append((path, record["error"]))
                continue
            results.append(_parsed_file_from_record(path, record))

        return ParseResult(files=results, skipped=skipped)


def _parsed_file_from_record(path: Path, record: dict) -> ParsedFile:
    try:
        sha_hint = xxhash.xxh64(path.read_bytes()).hexdigest()
    except OSError:
        sha_hint = None

    return ParsedFile(
        path=path,
        content_first_200_bytes=record.get("content_first_200_bytes", ""),
        top_level_node_kinds=tuple(record.get("top_level_node_kinds", [])),
        default_export_kind=record.get("default_export_kind"),
        named_export_count=int(record.get("named_export_count", 0)),
        import_specifiers=tuple(
            (str(m), str(k)) for m, k in record.get("import_specifiers", [])
        ),
        has_jsx=bool(record.get("has_jsx", False)),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count", 0)),
        sha_hint=sha_hint,
    )


# Verify protocol conformance at import time
_extractor: Extractor = RubyExtractor()
