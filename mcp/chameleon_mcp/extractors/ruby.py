"""Ruby AST extractor.

Spawns `scripts/prism_dump.rb` as a long-lived Ruby subprocess, sends file
paths via stdin (one per line), reads NDJSON ParsedFile records via
subprocess.communicate() (avoids the pipe-deadlock bug from Phase 5).

Per docs/architecture.md "TypeScript-first extractor" → "expansion"
+ ADR-0003.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import xxhash

from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParsedFile, ParseResult
from chameleon_mcp.plugin_paths import plugin_root


class RubyUnavailableError(ExtractorUnavailableError):
    """Ruby (or the prism_dump.rb script) is unavailable.

    Raised by the Ruby extractor when its subprocess cannot be started. The
    bootstrap orchestrator catches it (via ``ExtractorUnavailableError``) and
    degrades to a ``failed_ruby_unavailable`` report instead of letting the
    exception escape to the MCP boundary.
    """


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
            raise RubyUnavailableError(
                f"prism_dump.rb not found at {self._prism_dump_script}; "
                "Ruby support requires this script."
            )

        if not shutil.which("ruby"):
            raise RubyUnavailableError(
                "chameleon: `ruby` not found on PATH. Install Ruby >= 3.3 "
                "(ships Prism) to use the Ruby extractor."
            )

        input_data = "".join(f"{fp.resolve()}\n" for fp in files)

        # Defense-in-depth, matching the TypeScript extractor: run from a neutral
        # cwd (never the untrusted repo root) and drop RUBYOPT/RUBYLIB so a
        # poisoned interpreter option can't make ruby auto-load repo code before
        # prism_dump.rb runs. prism_dump only parses (Prism) and requires stdlib,
        # so this is hardening, not a live hole — but it keeps both extractors
        # consistent.
        env = os.environ.copy()
        env.pop("RUBYOPT", None)
        env.pop("RUBYLIB", None)
        proc = subprocess.Popen(
            ["ruby", str(self._prism_dump_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(plugin_root() / "mcp"),
        )

        timed_out = False
        try:
            stdout_data, _stderr = proc.communicate(input=input_data, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, _stderr = proc.communicate()
            timed_out = True

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
            try:
                results.append(_parsed_file_from_record(path, record))
            except (ValueError, TypeError):
                # One malformed record must skip that file, not abort the whole
                # corpus (mirrors the per-line JSONDecodeError skip above).
                skipped.append((path, "malformed_record"))
                continue

        # A timeout or non-zero exit means files past the failure point never
        # reached stdout. Mark them skipped so a truncated sample is VISIBLE
        # (the bootstrap can warn) instead of being silently treated as the
        # whole corpus.
        rc = proc.returncode
        if timed_out or rc not in (0, None):
            seen = {str(pf.path) for pf in results} | {str(p) for p, _ in skipped}
            reason = "extractor_timeout" if timed_out else f"extractor_exit_{rc}"
            for fp in files:
                rp = str(fp.resolve())
                if rp not in seen:
                    skipped.append((Path(rp), reason))

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
        import_specifiers=tuple((str(m), str(k)) for m, k in record.get("import_specifiers", [])),
        has_jsx=bool(record.get("has_jsx", False)),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count", 0)),
        sha_hint=sha_hint,
        extras=_extras_from_record(record),
    )


def _extras_from_record(record: dict) -> dict:
    """Carry subprocess-only fields that don't map onto a normalized ParsedFile slot.

    ``function_scopes`` is the per-method body-shape measurement (line span,
    nesting depth, branch and parameter counts) feeding the per-archetype
    body_shape norms. ``callable_signatures`` is the per-method declaration
    header (name, param shape, enclosing class + base) feeding the per-archetype
    signature consensus. Both are kept OUT of the cluster signature so they can't
    perturb signature stability.
    """
    extras: dict = {}
    scopes = record.get("function_scopes")
    if isinstance(scopes, list) and scopes:
        extras["function_scopes"] = scopes
    signatures = record.get("callable_signatures")
    if isinstance(signatures, list) and signatures:
        extras["callable_signatures"] = signatures
    # Receiverless class-body calls (the DSL-macro vocabulary), tagged with class.
    class_body_calls = record.get("class_body_calls")
    if isinstance(class_body_calls, list) and class_body_calls:
        extras["class_body_calls"] = class_body_calls
    # Call sites feed the calls-index builder (caller -> callee edges).
    # Row-level validation lives in the builder, which skips anything
    # malformed, so the list is carried as-is.
    call_sites = record.get("call_sites")
    if isinstance(call_sites, list) and call_sites:
        extras["call_sites"] = call_sites
    call_sites_total = record.get("call_sites_total")
    if isinstance(call_sites_total, int):
        extras["call_sites_total"] = call_sites_total
    call_sites_truncated = record.get("call_sites_truncated")
    if call_sites_truncated:
        extras["call_sites_truncated"] = True
    return extras
