"""chameleon_mcp — MCP server for chameleon plugin.

See docs/architecture.md for the full design.
"""

# Top-level declaration: column-0 so release.yml's `^__version__ = ` regex
# can grep it. The static value is the source of truth; the metadata block
# below only overwrites it when the installed metadata is valid.
__version__ = "0.9.2"

# Read version from installed package metadata when available. Only overwrite
# the static value if metadata returns a non-None, non-empty string. Stale
# dist-info directories (from prior `uv sync` runs without cleanup) can cause
# importlib.metadata to return None, which previously broke the engine version
# check in the profile loader.
try:
    from importlib.metadata import PackageNotFoundError as _PkgNotFound
    from importlib.metadata import version as _pkg_version

    try:
        _meta_version = _pkg_version("chameleon-mcp")
        if _meta_version and isinstance(_meta_version, str) and _meta_version.strip():
            __version__ = _meta_version
    except _PkgNotFound:  # pragma: no cover
        pass  # keep the static __version__
except Exception:  # pragma: no cover
    pass  # keep the static __version__
