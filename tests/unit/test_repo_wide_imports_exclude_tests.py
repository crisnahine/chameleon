"""A repo-wide import preference must describe production code, not the tests.

``extract_repo_wide_import_conventions`` runs over the WHOLE parsed corpus and
keeps modules a strong majority of import-bearing files use. On a repo with more
test files than source files, the majority IS the test suite, so the pass
promotes test-only modules to repo-wide preferences.

Measured on chameleon's own profile: 507 test files (test 371, test-effectiveness
75, test-journey 43, tests-py 18) against 120 production files, 81% of the
corpus. The rendered mirror accordingly advertised ``Prefer pytest``,
``Prefer unittest.mock`` and ``Prefer tests.journey.harness.checkpoints`` -- and a
repo-wide line speaks for EVERY archetype, so a model writing a production file
was told to prefer the test harness.

This is scope leakage rather than a judgment call about which modules are worth
naming: the per-archetype pass already covers test archetypes on their own
terms, and a module only the tests import is by construction not a repo-wide
convention.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.conventions import (
    extract_repo_wide_import_conventions,
    format_conventions_for_session,
)


class _Parsed:
    """Minimal ParsedFile stand-in: the pass reads path + import_specifiers."""

    def __init__(self, path: str, modules: list[str]) -> None:
        self.path = Path(path)
        self.import_specifiers = [(m, "named") for m in modules]


def _modules(files, repo_root=None):
    out = extract_repo_wide_import_conventions(files, repo_root=repo_root)
    return {p["module"] for p in out["preferred"]}


ROOT = "/repo"


def test_test_only_modules_do_not_become_repo_wide_preferences():
    files = [
        _Parsed(f"{ROOT}/tests/unit/test_{i}.py", ["pytest", "chameleon_mcp.tools"])
        for i in range(20)
    ]
    files += [_Parsed(f"{ROOT}/src/mod_{i}.py", ["chameleon_mcp.tools"]) for i in range(5)]
    mods = _modules(files, repo_root=Path(ROOT))
    assert "chameleon_mcp.tools" in mods
    assert "pytest" not in mods


def test_a_module_the_production_corpus_uses_still_qualifies():
    files = [_Parsed(f"{ROOT}/tests/unit/test_{i}.py", ["pytest"]) for i in range(20)]
    files += [_Parsed(f"{ROOT}/src/mod_{i}.py", ["app.http"]) for i in range(8)]
    assert "app.http" in _modules(files, repo_root=Path(ROOT))


def test_a_test_heavy_repo_no_longer_lets_tests_set_the_majority():
    # The real shape: tests outnumber source 4:1. Before the fix the harness
    # module cleared the repo-wide share on test files alone.
    files = [
        _Parsed(f"{ROOT}/tests/journey/act_{i}.py", ["tests.journey.harness.checkpoints"])
        for i in range(40)
    ]
    files += [_Parsed(f"{ROOT}/src/mod_{i}.py", ["app.http"]) for i in range(10)]
    mods = _modules(files, repo_root=Path(ROOT))
    assert "tests.journey.harness.checkpoints" not in mods
    assert "app.http" in mods


def test_spec_and_test_suffixes_are_recognized_across_languages():
    files = [_Parsed(f"{ROOT}/spec/models/user_{i}_spec.rb", ["rspec"]) for i in range(20)]
    files += [_Parsed(f"{ROOT}/app/models/user_{i}.rb", ["app/base"]) for i in range(6)]
    mods = _modules(files, repo_root=Path(ROOT))
    assert "rspec" not in mods
    assert "app/base" in mods


def test_a_repo_that_is_all_tests_still_derives_from_them():
    # A test-only repo (a harness, a fixture pack) has no production corpus to
    # prefer; falling back to the whole corpus beats deriving nothing at all.
    files = [_Parsed(f"{ROOT}/tests/unit/test_{i}.py", ["pytest"]) for i in range(20)]
    assert "pytest" in _modules(files, repo_root=Path(ROOT))


def test_repo_root_is_optional_and_omitting_it_keeps_old_behavior():
    # Callers that cannot supply a root must not silently lose the pass; without
    # a root there is no reliable way to tell a test path from a source one.
    files = [_Parsed(f"{ROOT}/tests/unit/test_{i}.py", ["pytest"]) for i in range(20)]
    assert "pytest" in _modules(files)


def _render(imports: dict) -> str:
    return format_conventions_for_session({"conventions": {"imports": imports}})


class TestRenderScope:
    """The second half of the same leak: an archetype-scoped preference must not
    RENDER as if it were repo-wide.

    The taught competing lines in this section already carry their archetype
    scope (``- Use X, not Y (service files)``). The derived preferred lines were
    flattened into one unscoped list, so ``pytest``, derived from the test
    archetype alone, read as advice for every file in the repo.
    """

    def test_an_archetype_scoped_preference_names_its_archetype(self):
        out = _render(
            {"test": {"preferred": [{"module": "pytest", "frequency": 40}], "competing": []}}
        )
        assert "- Prefer pytest (test files)" in out

    def test_a_module_shared_by_several_archetypes_names_them_all(self):
        out = _render(
            {
                "test": {"preferred": [{"module": "pytest", "frequency": 40}], "competing": []},
                "test-journey": {
                    "preferred": [{"module": "pytest", "frequency": 20}],
                    "competing": [],
                },
            }
        )
        assert "- Prefer pytest (test, test-journey files)" in out

    def test_a_repo_wide_entry_stays_marked_repo_wide(self):
        out = format_conventions_for_session(
            {
                "conventions": {
                    "imports": {},
                    "repo_imports": {"preferred": [{"module": "pathlib", "frequency": 90}]},
                }
            }
        )
        assert "- Prefer pathlib (repo-wide)" in out


def test_a_path_outside_the_root_keeps_its_vote_rather_than_being_guessed_at():
    # relative_to raises for a path outside the root, so the file cannot be
    # classified. It stays in the corpus rather than being dropped: silently
    # discarding unclassifiable files would shrink the denominator and let a
    # minority module clear the majority share.
    outside = [_Parsed("/elsewhere/tests/test_a.py", ["pytest"]) for _ in range(20)]
    inside = [_Parsed(f"{ROOT}/src/mod_{i}.py", ["app.http"]) for i in range(8)]
    out = extract_repo_wide_import_conventions(outside + inside, repo_root=Path(ROOT))
    assert isinstance(out["preferred"], list)
    # 8 of 28 files is below the repo-wide share, so nothing is promoted -- the
    # honest answer when most of the corpus could not be classified.
    assert "app.http" not in {p["module"] for p in out["preferred"]}
