"""Python AST extractor.

Spawns ``scripts/libcst_dump.py`` as a long-lived subprocess under the plugin's
own interpreter (``sys.executable``), sends file paths via stdin (one per line),
and reads NDJSON ParsedFile records via ``communicate()`` (same pipe-deadlock-safe
protocol as the Ruby extractor).

Running under ``sys.executable`` -- the interpreter already serving the MCP
server -- is the key choice: libcst is a hard dependency of ``chameleon-mcp``, so
that interpreter always has it, and a user's repo never needs libcst installed.
The Ruby extractor must hunt for ``ruby`` on PATH because Prism ships in Ruby's
stdlib; here the toolchain travels with the plugin.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import xxhash

from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParsedFile, ParseResult
from chameleon_mcp.plugin_paths import plugin_root

# Marker files that identify a Python project root. Presence of any one is a
# strong signal; absent all of them we fall back to "are there .py files at all".
_PYTHON_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "manage.py",
    "Pipfile",
    "tox.ini",
)


class PythonUnavailableError(ExtractorUnavailableError):
    """Python/libcst (or the libcst_dump.py script) is unavailable.

    Raised when the extractor's subprocess cannot be started: the dump script is
    missing, or libcst is not importable in the serving interpreter. The
    bootstrap orchestrator catches it (via ``ExtractorUnavailableError``) and
    degrades to a clean failed report instead of letting it escape to the MCP
    boundary.
    """


class PythonExtractor:
    """Python AST extractor backed by the libcst_dump.py subprocess."""

    language = "python"

    _libcst_dump_script: Path

    def __init__(self, libcst_dump_script: Path | None = None) -> None:
        if libcst_dump_script is None:
            self._libcst_dump_script = plugin_root() / "scripts" / "libcst_dump.py"
        else:
            self._libcst_dump_script = libcst_dump_script

    def can_handle(self, repo_root: Path) -> bool:
        """Detect Python via a project marker, or any ``*.py`` file in the tree."""
        for marker in _PYTHON_MARKERS:
            if (repo_root / marker).exists():
                return True
        # No marker: claim the repo only if it actually contains Python. A
        # bounded scan -- the first match short-circuits, so a huge non-Python
        # tree never walks far.
        for _ in repo_root.rglob("*.py"):
            return True
        return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*.py",
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Parse Python files under ``repo_root``. Returns ParseResult."""
        if paths is not None:
            files = list(paths)
        else:
            files = list(repo_root.glob(glob))

        if limit is not None:
            files = files[:limit]
        if not files:
            return ParseResult(files=[], skipped=[])

        if not self._libcst_dump_script.exists():
            raise PythonUnavailableError(
                f"libcst_dump.py not found at {self._libcst_dump_script}; "
                "Python support requires this script."
            )

        if importlib.util.find_spec("libcst") is None:
            raise PythonUnavailableError(
                "chameleon: libcst is not importable in the serving interpreter "
                f"({sys.executable}). Python support requires libcst (a chameleon-mcp "
                "dependency); reinstall the plugin's environment."
            )

        input_data = "".join(f"{fp.resolve()}\n" for fp in files)

        # Defense-in-depth, matching the TypeScript/Ruby extractors: run from a
        # neutral cwd (never the untrusted repo root) and drop PYTHONPATH /
        # PYTHONSTARTUP so a poisoned env can't make the interpreter import repo
        # code before libcst_dump.py runs. libcst only parses (no exec), so this
        # is hardening, not a live hole -- but it keeps all three extractors
        # consistent.
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONSTARTUP", None)
        proc = subprocess.Popen(
            [sys.executable, str(self._libcst_dump_script)],
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
                # One malformed record skips that file, not the whole corpus
                # (mirrors the per-line JSONDecodeError skip above).
                skipped.append((path, "malformed_record"))
                continue

        # A timeout or non-zero exit means files past the failure point never
        # reached stdout. Mark them skipped so a truncated sample is VISIBLE
        # instead of being silently treated as the whole corpus.
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

    ``function_scopes`` is the per-function body-shape measurement feeding the
    per-archetype body_shape norms. ``callable_signatures`` is the per-function
    declaration header (name, param shape, enclosing class + base, decorators)
    feeding the per-archetype signature consensus and the framework priors.
    ``class_shapes`` carries per-class bases + decorators (the heritage signal
    Django/DRF/FastAPI base classes and Flask/FastAPI route decorators are read
    from). All are kept OUT of the cluster signature so they can't perturb
    signature stability.
    """
    extras: dict = {}
    scopes = record.get("function_scopes")
    if isinstance(scopes, list) and scopes:
        extras["function_scopes"] = scopes
    signatures = record.get("callable_signatures")
    if isinstance(signatures, list) and signatures:
        extras["callable_signatures"] = signatures
    class_shapes = record.get("class_shapes")
    if isinstance(class_shapes, list) and class_shapes:
        extras["class_shapes"] = class_shapes
    call_sites = record.get("call_sites")
    if isinstance(call_sites, list) and call_sites:
        extras["call_sites"] = call_sites
    call_sites_total = record.get("call_sites_total")
    if isinstance(call_sites_total, int):
        extras["call_sites_total"] = call_sites_total
    call_sites_truncated = record.get("call_sites_truncated")
    if call_sites_truncated:
        extras["call_sites_truncated"] = True
    # The full importable-name set + open-set flag (phantom-symbol existence
    # check / exports index) and the named-import + namespace-import binding rows
    # (reverse index + calls-index import grade). Same extras keys + validation
    # the TypeScript extractor uses, so the cross-file consumers read Python from
    # the same place.
    names = record.get("named_export_names")
    if isinstance(names, list) and names:
        extras["named_export_names"] = [str(n) for n in names if isinstance(n, str)]
    if record.get("export_set_open"):
        extras["export_set_open"] = True
    import_symbols = record.get("import_symbols")
    if isinstance(import_symbols, list) and import_symbols:
        rows: list[dict] = []
        for sym in import_symbols:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name")
            module = sym.get("module")
            if not isinstance(name, str) or not isinstance(module, str):
                continue
            local = sym.get("local")
            line = sym.get("line")
            rows.append(
                {
                    "name": name,
                    "local": local if isinstance(local, str) and local else name,
                    "module": module,
                    "line": int(line) if isinstance(line, int) else None,
                }
            )
        if rows:
            extras["import_symbols"] = rows
    namespace_imports = record.get("namespace_imports")
    if isinstance(namespace_imports, list) and namespace_imports:
        extras["namespace_imports"] = namespace_imports
    return extras
