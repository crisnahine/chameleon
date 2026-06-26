"""A Python toolchain-missing bootstrap must report failed_python_unavailable.

The extractor-unavailable branch mapped NodeUnavailableError to
failed_node_unavailable and EVERYTHING ELSE to failed_ruby_unavailable, so a
Python repo whose libcst is unavailable reported failed_ruby_unavailable -- the
status contradicted the (libcst) error body. Each toolchain must name itself.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap import orchestrator as o


def _python_repo(tmp_path: Path, n: int) -> Path:
    repo = tmp_path / "pyrepo"
    (repo / "app").mkdir(parents=True)
    (repo / "manage.py").write_text("import django\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["django"]\n', encoding="utf-8"
    )
    for i in range(n):
        (repo / "app" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    return repo


def test_python_toolchain_missing_reports_python_not_ruby(tmp_path: Path, monkeypatch):
    repo = _python_repo(tmp_path, 4)

    from chameleon_mcp.extractors.python import PythonExtractor, PythonUnavailableError

    def _raise(self, repo_root, glob="**/*.py", limit=None, paths=None):
        raise PythonUnavailableError("libcst not available")

    monkeypatch.setattr(PythonExtractor, "parse_repo", _raise)
    report = o.bootstrap_repo(repo)
    assert report.status == "failed_python_unavailable"
    assert not (repo / ".chameleon").exists()
