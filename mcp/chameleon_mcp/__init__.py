"""chameleon_mcp — MCP server for chameleon plugin.

See docs/architecture.md for the full design.
"""

# Top-level declaration: column-0 so release.yml's `^__version__ = ` regex
# can grep it. Overwritten at import time by the importlib.metadata block
# below when the package is installed (the normal runtime case).
__version__ = "0.5.11"

# v0.5.6: read version from installed package metadata so a single bump
# of pyproject.toml propagates to every reader (profile.json's
# engine_min_version, the loader's ENGINE_VERSION check, etc.).
try:
    from importlib.metadata import PackageNotFoundError as _PkgNotFound
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("chameleon-mcp")
    except _PkgNotFound:  # pragma: no cover
        __version__ = "0.5.11"
except Exception:  # pragma: no cover
    __version__ = "0.5.11"
