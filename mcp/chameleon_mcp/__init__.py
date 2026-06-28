"""chameleon_mcp — MCP server for chameleon plugin.

See docs/architecture.md for the full design.
"""

__version__ = "2.38.0"

try:
    from importlib.metadata import PackageNotFoundError as _PkgNotFound
    from importlib.metadata import version as _pkg_version

    try:
        _meta_version = _pkg_version("chameleon-mcp")
        if _meta_version and isinstance(_meta_version, str) and _meta_version.strip():
            __version__ = _meta_version
    except _PkgNotFound:  # pragma: no cover
        pass
except Exception:  # pragma: no cover
    pass
