"""A chosen extractor must never be handed files it cannot parse, and a run
degraded by per-file rejections must not be reported as a dead subprocess.

`paths_glob` narrows discovery but has no say in extractor selection, so a glob
naming another language's extension used to route every matched file into the
wrong dumper. `ts_dump.mjs` parses an unknown extension as JavaScript, so a
whole Python tree came back as `too_many_parse_errors` and surfaced as
"the extractor subprocess likely died mid-run" — a toolchain outage that never
happened, pointing the reader at the interpreter instead of the language pick.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap import orchestrator as o


@pytest.fixture(autouse=True)
def _allow_tmp():
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    yield


def _ts_repo_with_python_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "tsrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"name":"t","version":"1.0.0","devDependencies":{"typescript":"^5"}}', encoding="utf-8"
    )
    for i in range(12):
        (repo / "src" / f"c{i}.ts").write_text(
            f"export function f{i}() {{ return {i}; }}\n", encoding="utf-8"
        )
    (repo / "svc").mkdir(parents=True)
    for i in range(30):
        (repo / "svc" / f"m{i}.py").write_text(
            f"class M{i}:\n    def run(self):\n        return {i}\n", encoding="utf-8"
        )
    return repo


def test_glob_for_a_foreign_language_is_refused_not_mangled(tmp_path: Path) -> None:
    repo = _ts_repo_with_python_tree(tmp_path)

    report = o.bootstrap_repo(repo, paths_glob="**/*.py")

    assert report.status == "failed_glob_language_mismatch", (
        f"a .py glob against a typescript repo must be refused, got {report.status!r}"
    )
    assert report.files_skipped_parse == 0, "no file may reach the wrong dumper"
    assert "typescript" in (report.error or ""), "the error must name the selected language"
    assert ".py" in (report.error or ""), "the error must name the dropped extension"


def test_foreign_extensions_are_dropped_when_the_glob_also_matches_owned_files(
    tmp_path: Path,
) -> None:
    repo = _ts_repo_with_python_tree(tmp_path)

    report = o.bootstrap_repo(repo, paths_glob="**/*.{ts,py}")

    assert report.status == "success", f"owned files must still bootstrap, got {report.status!r}"
    assert report.files_processed == 12, (
        f"only the 12 .ts files may be parsed, got {report.files_processed}"
    )
    assert report.files_skipped_parse == 0, "dropped .py files are not parse failures"


def test_degraded_parse_error_does_not_blame_the_subprocess_for_per_file_rejections() -> None:
    msg = o._degraded_parse_error(
        files_skipped=596,
        attempted=629,
        skipped=[
            (Path("a/_thresholds.py"), "too_many_parse_errors"),
            (Path("a/_excerpt_cache.py"), "too_many_parse_errors"),
        ],
        language="typescript",
    )

    lowered = msg.lower()
    assert "died" not in lowered, "per-file rejections are not a dead child"
    assert "toolchain is healthy" not in lowered, (
        "re-running cannot fix a language mismatch, so the message must not advise it"
    )
    assert "typescript" in msg, "the message must name the language that did the rejecting"
    assert "too_many_parse_errors" in msg, "the real per-file reason must survive"


def test_degraded_parse_error_still_blames_the_subprocess_when_the_child_died() -> None:
    msg = o._degraded_parse_error(
        files_skipped=596,
        attempted=629,
        skipped=[
            (Path("a/one.ts"), "extractor_exit_1"),
            (Path("a/two.ts"), "extractor_exit_1"),
        ],
        language="typescript",
    )

    assert "subprocess" in msg.lower(), "a real non-zero exit must still read as a dead subprocess"


def test_degraded_parse_error_treats_a_timeout_as_a_dead_subprocess() -> None:
    msg = o._degraded_parse_error(
        files_skipped=10,
        attempted=12,
        skipped=[(Path("a/one.ts"), "extractor_timeout")],
        language="typescript",
    )

    assert "subprocess" in msg.lower(), "a timeout is a dead child, not a per-file rejection"
