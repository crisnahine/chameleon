"""Unit tests for per-archetype source-to-test pairing convention derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.conventions import (
    _candidate_test_paths,
    _is_test_path,
    extract_test_pairing_conventions,
    format_conventions_for_session,
)


class _FakeFile:
    """Minimal stand-in for ParsedFile: only ``path`` is read by the derivation."""

    def __init__(self, path: Path) -> None:
        self.path = path


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("// x\n", encoding="utf-8")
    return p


class TestIsTestPath:
    def test_colocated_ts_test_is_test(self):
        assert _is_test_path("src/foo.test.ts", language="typescript")

    def test_colocated_ts_spec_is_test(self):
        assert _is_test_path("src/foo.spec.ts", language="typescript")

    def test_plain_ts_source_is_not_test(self):
        assert not _is_test_path("src/foo.ts", language="typescript")

    def test_ruby_spec_basename_is_test(self):
        assert _is_test_path("spec/models/user_spec.rb", language="ruby")

    def test_ruby_test_basename_is_test(self):
        assert _is_test_path("test/models/user_test.rb", language="ruby")

    def test_plain_ruby_source_is_not_test(self):
        assert not _is_test_path("app/models/user.rb", language="ruby")

    def test_django_bare_tests_py_is_test_path(self):
        # Django startapp's default app/tests.py is a test module, not source.
        assert _is_test_path("shop/tests.py", language="python")
        assert _is_test_path("pkg/test.py", language="python")

    def test_plain_python_source_is_not_test_path(self):
        assert not _is_test_path("shop/views.py", language="python")

    def test_test_dir_component_marks_test(self):
        # A plain-named file living under a test root still reads as a test.
        assert _is_test_path("__tests__/helpers.ts", language="typescript")
        assert _is_test_path("tests/support/factory.rb", language="ruby")


class TestCandidateTestPaths:
    def test_ts_colocated_candidates_present(self):
        cands = dict(
            (label, path)
            for label, path in (
                (lbl, p) for lbl, p in _candidate_test_paths("src/foo.ts", language="typescript")
            )
        )
        paths = set(cands.values())
        assert "src/foo.test.ts" in paths
        assert "src/foo.spec.ts" in paths
        assert "src/__tests__/foo.test.ts" in paths

    def test_ts_mirrored_swaps_source_root(self):
        paths = {p for _l, p in _candidate_test_paths("src/a/foo.ts", language="typescript")}
        # src/ swapped for a mirrored test root, tree preserved.
        assert "test/a/foo.test.ts" in paths
        assert "tests/a/foo.test.ts" in paths

    def test_ts_no_source_root_prefixes_test_root(self):
        paths = {p for _l, p in _candidate_test_paths("widgets/foo.ts", language="typescript")}
        # No leading src/app/lib -> the test root is prefixed.
        assert "test/widgets/foo.test.ts" in paths

    def test_ruby_colocated_candidates_present(self):
        paths = {p for _l, p in _candidate_test_paths("app/models/user.rb", language="ruby")}
        assert "app/models/user_spec.rb" in paths
        assert "app/models/user_test.rb" in paths

    def test_ruby_mirrored_swaps_app_for_spec(self):
        paths = {p for _l, p in _candidate_test_paths("app/models/user.rb", language="ruby")}
        assert "spec/models/user_spec.rb" in paths
        assert "test/models/user_test.rb" in paths

    def test_python_colocated_candidates_present(self):
        paths = {p for _l, p in _candidate_test_paths("shop/views.py", language="python")}
        assert "shop/test_views.py" in paths
        assert "shop/views_test.py" in paths

    def test_python_nested_app_tests_candidate(self):
        # Django/pytest dominant: a tests/ package sibling to the source's own
        # directory (myapp/views.py -> myapp/tests/test_views.py).
        paths = {p for _l, p in _candidate_test_paths("shop/views.py", language="python")}
        assert "shop/tests/test_views.py" in paths

    def test_python_root_mirrored_candidate_present(self):
        paths = {p for _l, p in _candidate_test_paths("src/shop/views.py", language="python")}
        # Leading source root swapped for the test root.
        assert "tests/shop/test_views.py" in paths

    def test_extensionless_basename_yields_no_candidates(self):
        assert _candidate_test_paths("Makefile", language="typescript") == []
        assert _candidate_test_paths(".gitignore", language="typescript") == []


class TestExtractTestPairingTypeScript:
    def test_colocated_pairing_above_floor_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            _touch(tmp_path, f"src/svc{i}.test.ts")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["paired"] == 10
        assert conv["total"] == 10
        assert conv["mapping"] == "co-located .test"

    def test_partial_pairing_at_floor_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            if i < 6:  # 6/10 = 0.60, exactly the floor
                _touch(tmp_path, f"src/svc{i}.test.ts")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv["frequency"] == 0.6
        assert conv["paired"] == 6

    def test_below_floor_returns_empty(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            if i < 5:  # 5/10 = 0.50, below the floor
                _touch(tmp_path, f"src/svc{i}.test.ts")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv == {}

    def test_below_sample_floor_returns_empty(self, tmp_path):
        # Fully paired but too few source files to trust the figure.
        files = []
        for i in range(9):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            _touch(tmp_path, f"src/svc{i}.test.ts")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv == {}

    def test_test_files_excluded_from_source_pool(self, tmp_path):
        # 10 sources all paired, plus 10 test files in the same archetype list.
        # The tests must not be counted as their own un-paired sources.
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.test.ts")))
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv["total"] == 10
        assert conv["frequency"] == 1.0

    def test_mirrored_tree_mapping_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/a/svc{i}.ts")))
            _touch(tmp_path, f"test/a/svc{i}.test.ts")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["mapping"] == "mirrored test/.../.test"

    def test_no_repo_root_returns_empty(self, tmp_path):
        files = [_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")) for i in range(10)]
        assert extract_test_pairing_conventions(files, language="typescript", repo_root=None) == {}


class TestExtractTestPairingRuby:
    def test_mirrored_spec_tree_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"app/models/m{i}.rb")))
            _touch(tmp_path, f"spec/models/m{i}_spec.rb")
        conv = extract_test_pairing_conventions(files, language="ruby", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["mapping"] == "mirrored spec/.../_spec.rb"

    def test_colocated_spec_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"lib/widget{i}.rb")))
            _touch(tmp_path, f"lib/widget{i}_spec.rb")
        conv = extract_test_pairing_conventions(files, language="ruby", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["mapping"] == "co-located _spec.rb"

    def test_ruby_below_floor_empty(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"app/models/m{i}.rb")))
            if i < 4:
                _touch(tmp_path, f"spec/models/m{i}_spec.rb")
        conv = extract_test_pairing_conventions(files, language="ruby", repo_root=tmp_path)
        assert conv == {}


class TestExtractTestPairingPython:
    def test_nested_app_tests_pairing_recorded(self, tmp_path):
        # The dominant Django/pytest per-app layout: app/tests/test_<stem>.py.
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"shop{i}/views.py")))
            _touch(tmp_path, f"shop{i}/tests/test_views.py")
        conv = extract_test_pairing_conventions(files, language="python", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["paired"] == 10
        assert "tests/" in conv["mapping"]

    def test_colocated_pytest_pairing_recorded(self, tmp_path):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"pkg/svc{i}.py")))
            _touch(tmp_path, f"pkg/test_svc{i}.py")
        conv = extract_test_pairing_conventions(files, language="python", repo_root=tmp_path)
        assert conv["frequency"] == 1.0
        assert conv["mapping"] == "co-located test_"


class TestThresholdOverride:
    def test_env_lowers_frequency_floor(self, tmp_path, monkeypatch):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            if i < 3:  # 0.30, normally below the 0.60 floor
                _touch(tmp_path, f"src/svc{i}.test.ts")
        monkeypatch.setenv("CHAMELEON_TEST_PAIRING_FREQUENCY", "0.25")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv["frequency"] == 0.3

    def test_env_raises_sample_floor(self, tmp_path, monkeypatch):
        files = []
        for i in range(10):
            files.append(_FakeFile(_touch(tmp_path, f"src/svc{i}.ts")))
            _touch(tmp_path, f"src/svc{i}.test.ts")
        monkeypatch.setenv("CHAMELEON_TEST_PAIRING_MIN_SAMPLE", "20")
        conv = extract_test_pairing_conventions(files, language="typescript", repo_root=tmp_path)
        assert conv == {}


class TestSessionFormatting:
    def test_test_pairing_section_rendered(self):
        conventions = {
            "conventions": {
                "test_pairing": {
                    "service": {
                        "frequency": 0.9,
                        "paired": 9,
                        "total": 10,
                        "mapping": "co-located .test",
                    }
                }
            }
        }
        out = format_conventions_for_session(conventions)
        assert "TEST PAIRING (advisory):" in out
        assert "90% of files ship a paired test" in out
        assert "mapped co-located .test" in out

    def test_malformed_entry_skipped(self):
        conventions = {
            "conventions": {
                "test_pairing": {
                    "bad": "not a dict",
                    "no_freq": {"paired": 3},
                }
            }
        }
        out = format_conventions_for_session(conventions)
        # Nothing valid to render -> no section, empty block overall.
        assert "TEST PAIRING" not in out

    def test_missing_mapping_omits_tail(self):
        conventions = {
            "conventions": {"test_pairing": {"svc": {"frequency": 0.7, "paired": 7, "total": 10}}}
        }
        out = format_conventions_for_session(conventions)
        assert "70% of files ship a paired test;" in out
        assert "mapped" not in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
