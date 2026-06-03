"""Cross-platform path-portability unit tests (T5).

These reproduce defects that are latent on macOS/Linux but break on Windows:
- witness paths stored with the host separator (backslash on Windows) break the
  forward-slash comparisons throughout the codebase;
- a backslash witness path loaded from a cross-platform-shared profile is parsed
  wrong by the calibration sibling sampler on POSIX;
- POSIX-only ownership calls (os.geteuid / st_uid) crash on Windows.

To exercise the Windows behavior on a POSIX host we either inject a
PureWindowsPath (which stringifies with backslashes regardless of host) or hide
os.geteuid to mimic the Windows runtime.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

# --- Finding: Windows path separator in canonicals.json (storage SOURCE) -----


def test_witness_relpath_stored_forward_slash_for_windows_path():
    """The orchestrator must store witness paths with forward slashes even when
    the selected witness is a native Windows path (backslash separators)."""
    from chameleon_mcp.bootstrap import orchestrator as o

    repo_root = PureWindowsPath(r"C:\repo")
    witness = PureWindowsPath(r"C:\repo\src\components\Button.tsx")

    stored = o._witness_relpath(witness, repo_root)
    assert stored == "src/components/Button.tsx"
    assert "\\" not in stored


def test_witness_relpath_falls_back_to_forward_slash_outside_root():
    """A witness outside the repo root falls back to the absolute path, which
    must still be normalized to forward slashes."""
    from chameleon_mcp.bootstrap import orchestrator as o

    repo_root = PureWindowsPath(r"C:\repo")
    witness = PureWindowsPath(r"D:\elsewhere\file.ts")

    stored = o._witness_relpath(witness, repo_root)
    assert "\\" not in stored
    assert stored.endswith("elsewhere/file.ts")


# --- Finding: backslash witness path breaks calibration sibling sampling -----


def _loaded_with_witness(witness_path: str):
    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [
                    {
                        "witness": {"path": witness_path},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    return _Loaded()


def test_sample_files_handles_backslash_witness_path(tmp_path: Path):
    """A witness path with backslashes (cross-platform-shared profile) must still
    resolve to the right directory so siblings are sampled correctly."""
    from chameleon_mcp.enforcement_calibration import _sample_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")

    sample = _sample_files(tmp_path, _loaded_with_witness("src\\a.ts"))
    rels = {rel for rel, _arch in sample}

    # The sibling b.ts (in the witness's real directory) must be discovered, and
    # every emitted path must use forward slashes.
    assert "src/b.ts" in rels
    assert all("\\" not in rel for rel in rels)


def test_sample_files_emits_forward_slash_sibling_paths(tmp_path: Path):
    """Sibling paths emitted by the sampler use forward slashes regardless of
    the host path flavour."""
    from chameleon_mcp.enforcement_calibration import _sample_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")
    (tmp_path / "src" / "c.ts").write_text("export const c = 3\n", encoding="utf-8")

    sample = _sample_files(tmp_path, _loaded_with_witness("src/a.ts"))
    rels = {rel for rel, _arch in sample}
    assert "src/b.ts" in rels
    assert "src/c.ts" in rels
    assert all("\\" not in rel for rel in rels)


# --- Finding: os.geteuid / st_uid not available on Windows --------------------


def test_ensure_hmac_key_no_geteuid(tmp_path: Path):
    """When os.geteuid is unavailable (Windows), the HMAC key ownership check is
    skipped rather than raising AttributeError."""
    from chameleon_mcp import exec_log

    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    # Hide os.geteuid entirely to mimic the Windows runtime, where the attribute
    # does not exist.
    had = hasattr(os, "geteuid")
    saved = os.geteuid if had else None
    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        try:
            if had:
                del os.geteuid  # type: ignore[attr-defined]
            key = exec_log._ensure_hmac_key()
        finally:
            if had:
                os.geteuid = saved  # type: ignore[attr-defined]

    assert key == b"k" * 32


def test_ensure_hmac_key_owner_mismatch_still_enforced_on_posix(tmp_path: Path):
    """On POSIX, a key owned by a different uid still raises (guard must not
    weaken the existing ownership check when geteuid is present)."""
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX-only ownership check")

    from chameleon_mcp import exec_log

    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    import stat as _stat

    real_uid = os.geteuid()

    class _FakeStat:
        st_uid = real_uid + 1
        st_mode = _stat.S_IFREG | 0o600

    # key_path.is_file() uses pathlib's own os reference, so spoofing only
    # exec_log.os.stat affects the explicit ownership check, not file discovery.
    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        with patch.object(exec_log.os, "stat", return_value=_FakeStat()):
            with pytest.raises(exec_log.HMACKeyError):
                exec_log._ensure_hmac_key()


# --- Finding: workspace resolve() failure must skip, not abort fan-out --------


def test_workspace_resolve_failure_skips_not_aborts(tmp_path: Path, monkeypatch):
    """A workspace package whose resolve() raises (broken/looping symlink) is
    skipped and recorded; the rest of the bootstrap still completes."""
    from chameleon_mcp.bootstrap import orchestrator as o
    from chameleon_mcp.bootstrap.workspace import WorkspaceInfo

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    class _ExplodingPath(type(repo)):
        def resolve(self, *a, **kw):  # noqa: D401 - mimic resolve() blowing up
            raise OSError("broken symlink in workspace path")

    bad_ws = _ExplodingPath(repo / "packages" / "broken")

    monkeypatch.setattr(
        o,
        "detect_workspace",
        lambda root: WorkspaceInfo(is_workspace=True, manager="pnpm", workspace_paths=[bad_ws]),
    )

    report = o.bootstrap_repo(repo)
    assert "packages/broken" in report.workspace_skipped_warnings
    assert "workspace_skipped_warnings" in report.to_dict()


# --- Finding: iterdir batched once per directory across shared witnesses ------


def test_sample_files_iterdir_called_once_per_directory(tmp_path: Path):
    """Two witnesses in the same directory must trigger a single iterdir() scan
    of that directory, not one scan per witness."""
    from chameleon_mcp import enforcement_calibration as ec

    (tmp_path / "src").mkdir()
    for name in ("a.ts", "b.ts", "c.ts", "d.ts"):
        (tmp_path / "src" / name).write_text("export const x = 1\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "alpha": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}],
                "beta": [{"witness": {"path": "src/b.ts"}, "normative_shape": {"ast_query": {}}}],
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    real_iterdir = Path.iterdir
    calls: list[str] = []

    def _counting_iterdir(self):
        calls.append(self.as_posix())
        return real_iterdir(self)

    with patch.object(Path, "iterdir", _counting_iterdir):
        ec._sample_files(tmp_path, _Loaded())

    src_scans = [c for c in calls if c.endswith("/src")]
    assert len(src_scans) == 1, f"expected one scan of src/, got {src_scans}"
