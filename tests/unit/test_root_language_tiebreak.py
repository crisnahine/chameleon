"""Root extractor selection must not let a few stray .ts files outvote a
dominant, marked Python/Ruby backend.

The TypeScript extractor's can_handle accepts ANY shallow .ts file when there is
no tsconfig / package.json TS dependency. So a Django repo (manage.py +
pyproject + 80 .py files) with 2 stray .ts files in static/ misclassified as
typescript and produced a 0-archetype profile. Root selection now applies a
language-magnitude tiebreak (like the inherited-signals branch already does):
when TS was picked on a WEAK signal and a marked backend dominates by file
count, prefer the backend language. Strong TS signals (tsconfig / package.json
TS dep) are never overridden.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap import orchestrator as o


@pytest.fixture(autouse=True)
def _allow_tmp():
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    yield


def _persisted_language(repo: Path) -> str | None:
    pj = repo / ".chameleon" / "profile.json"
    if not pj.is_file():
        return None
    try:
        return json.loads(pj.read_text()).get("language")
    except Exception:
        return None


def test_python_dominant_repo_with_stray_ts_bootstraps_as_python(tmp_path: Path) -> None:
    repo = tmp_path / "pyrepo"
    (repo / "app").mkdir(parents=True)
    (repo / "static").mkdir(parents=True)
    (repo / "manage.py").write_text("import django\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["django"]\n', encoding="utf-8"
    )
    for i in range(40):
        (repo / "app" / f"m{i}.py").write_text(
            f"class M{i}:\n    def run(self):\n        return {i}\n", encoding="utf-8"
        )
    (repo / "static" / "w1.ts").write_text("export const a = 1;\n", encoding="utf-8")

    report = o.bootstrap_repo(repo)
    assert _persisted_language(repo) == "python", "a Python-dominant repo must derive as python"
    assert report.archetypes_detected > 0, "the dominant Python tree must yield archetypes"


def test_strong_ts_signal_is_not_overridden_by_some_py_files(tmp_path: Path) -> None:
    # tsconfig.json is a STRONG TS signal: even with a few .py scripts present,
    # the repo must stay typescript (no false re-tie).
    repo = tmp_path / "tsrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"name":"t","version":"1.0.0","devDependencies":{"typescript":"^5"}}', encoding="utf-8"
    )
    for i in range(20):
        (repo / "src" / f"c{i}.ts").write_text(
            f"export function f{i}() {{ return {i}; }}\n", encoding="utf-8"
        )
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    (repo / "scripts.py").write_text("print('build')\n", encoding="utf-8")

    o.bootstrap_repo(repo)
    assert _persisted_language(repo) == "typescript", "strong TS signal must not be overridden"


def test_weak_ts_without_backend_marker_stays_typescript(tmp_path: Path) -> None:
    # Weak TS (no tsconfig) but NO python/ruby project marker -> no re-tie.
    repo = tmp_path / "weakts"
    (repo / "src").mkdir(parents=True)
    for i in range(15):
        (repo / "src" / f"c{i}.ts").write_text(
            f"export function f{i}() {{ return {i}; }}\n", encoding="utf-8"
        )
    # a couple of stray .py with no marker (no manage.py/pyproject/etc.)
    (repo / "helper.py").write_text("print(1)\n", encoding="utf-8")

    o.bootstrap_repo(repo)
    assert _persisted_language(repo) == "typescript", "no backend marker -> stay typescript"
