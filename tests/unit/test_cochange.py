"""Unit tests for the stale-test co-change computation.

These exercise the pure logic in ``cochange.py``: given a profile's test_pairing
and key_exports plus the set of files edited this turn, decide which edited source
files left an existing paired test untouched and which exports moved.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.cochange import (
    _COCHANGE_RULES,
    changed_exports_in_content,
    cochange_rule_disabled,
    stale_test_items,
)


def _touch(root: Path, rel: str, body: str = "// x\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class _Profile:
    """Resolver/reader callables for a single-archetype TS tree under ``root``."""

    def __init__(self, root: Path, archetype_of: dict[str, str | None]) -> None:
        self.root = root
        self._arch = archetype_of

    def archetype_of(self, abs_path: str) -> str | None:
        try:
            rel = Path(abs_path).relative_to(self.root).as_posix()
        except ValueError:
            return None
        return self._arch.get(rel)

    def language_of(self, abs_path: str) -> str | None:
        from chameleon_mcp.lint_engine import detect_language

        return detect_language(abs_path)

    def read_content(self, abs_path: str) -> str | None:
        try:
            return Path(abs_path).read_text(encoding="utf-8")
        except OSError:
            return None


class TestChangedExports:
    def test_ts_named_exports(self):
        body = "export function getUser() {}\nexport const fetchAll = () => {}\n"
        names = changed_exports_in_content(body, language="typescript")
        assert "getUser" in names
        assert "fetchAll" in names

    def test_ruby_class_and_module(self):
        body = "module Api\n  class UsersController\n  end\nend\n"
        names = changed_exports_in_content(body, language="ruby")
        # Namespaced names are reduced to the leaf, matching bootstrap derivation.
        assert "UsersController" in names
        assert "Api" in names

    def test_skip_names_dropped(self):
        body = "export default function () {}\nexport class Base {}\n"
        names = changed_exports_in_content(body, language="typescript")
        assert "default" not in names
        assert "Base" not in names

    def test_dedup_preserves_order(self):
        body = "export const a = 1\nexport const ab = 2\nexport const a = 3\n"
        names = changed_exports_in_content(body, language="typescript")
        assert names == ["ab"] or names[:1] == ["ab"]  # single-char 'a' dropped


class TestStaleTestItems:
    def _setup(self, tmp_path: Path):
        src = _touch(
            tmp_path,
            "src/user.ts",
            "export function getUser() {}\nexport function listUsers() {}\n",
        )
        test = _touch(tmp_path, "src/user.test.ts", "import {} from './user'\n")
        prof = _Profile(tmp_path, {"src/user.ts": "service", "src/user.test.ts": "test"})
        return src, test, prof

    def test_existing_test_unchanged_flags(self, tmp_path):
        src, test, prof = self._setup(tmp_path)
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9, "paired": 9, "total": 10}},
            key_exports={"service": ["getUser", "listUsers"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert len(items) == 1
        it = items[0]
        assert it.source_rel == "src/user.ts"
        assert it.test_rel == "src/user.test.ts"
        assert it.exports == ["getUser", "listUsers"]

    def test_test_edited_same_turn_not_stale(self, tmp_path):
        src, test, prof = self._setup(tmp_path)
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["getUser"]},
            edited_abs={str(src), str(test)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_no_test_on_disk_not_flagged(self, tmp_path):
        # Missing-test-for-change is intentionally out of scope here.
        src = _touch(tmp_path, "src/order.ts", "export function makeOrder() {}\n")
        prof = _Profile(tmp_path, {"src/order.ts": "service"})
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["makeOrder"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_low_pairing_archetype_absent_no_flag(self, tmp_path):
        # An archetype with no test_pairing entry (below the bootstrap floor) is
        # never high-pairing, so its edits are not surfaced even with a test on disk.
        src, test, prof = self._setup(tmp_path)
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={},  # bootstrap dropped this archetype
            key_exports={"service": ["getUser"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_unresolved_archetype_skipped(self, tmp_path):
        src, test, prof = self._setup(tmp_path)
        prof._arch["src/user.ts"] = None
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["getUser"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_editing_the_test_itself_skipped(self, tmp_path):
        # A test file is not a source file; editing only the test is not "stale".
        src, test, prof = self._setup(tmp_path)
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}, "test": {"frequency": 0.9}},
            key_exports={"service": ["getUser"]},
            edited_abs={str(test)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_unsupported_language_skipped(self, tmp_path):
        src = _touch(tmp_path, "src/thing.py", "def f():\n    pass\n")
        prof = _Profile(tmp_path, {"src/thing.py": "service"})
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["f"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []

    def test_exports_intersected_with_archetype(self, tmp_path):
        # Only exports the team treats as the contract (key_exports) are cited,
        # even if the file declares others.
        src = _touch(
            tmp_path,
            "src/user.ts",
            "export function getUser() {}\nexport function privateHelper() {}\n",
        )
        _touch(tmp_path, "src/user.test.ts", "x\n")
        prof = _Profile(tmp_path, {"src/user.ts": "service"})
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["getUser"]},  # privateHelper not a key export
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items[0].exports == ["getUser"]

    def test_ruby_mirrored_spec_flags(self, tmp_path):
        src = _touch(tmp_path, "app/models/user.rb", "class User\nend\n")
        _touch(tmp_path, "spec/models/user_spec.rb", "x\n")
        prof = _Profile(tmp_path, {"app/models/user.rb": "model"})
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"model": {"frequency": 0.8}},
            key_exports={"model": ["User"]},
            edited_abs={str(src)},
            archetype_of=prof.archetype_of,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert len(items) == 1
        assert items[0].test_rel == "spec/models/user_spec.rb"
        assert items[0].exports == ["User"]

    def test_empty_inputs_return_empty(self, tmp_path):
        prof = _Profile(tmp_path, {})
        assert (
            stale_test_items(
                repo_root=tmp_path,
                test_pairing={},
                key_exports={},
                edited_abs=set(),
                archetype_of=prof.archetype_of,
                language_of=prof.language_of,
                read_content=prof.read_content,
            )
            == []
        )

    def test_bad_resolver_fails_open_per_file(self, tmp_path):
        src, test, prof = self._setup(tmp_path)

        def _boom(_abs):
            raise RuntimeError("resolver exploded")

        # A resolver that raises must not crash the whole pass.
        items = stale_test_items(
            repo_root=tmp_path,
            test_pairing={"service": {"frequency": 0.9}},
            key_exports={"service": ["getUser"]},
            edited_abs={str(src)},
            archetype_of=_boom,
            language_of=prof.language_of,
            read_content=prof.read_content,
        )
        assert items == []


def _rule(rule_id: str):
    for r in _COCHANGE_RULES:
        if r.rule_id == rule_id:
            return r
    raise KeyError(rule_id)


class TestCochangeRuleDisabled:
    """The repo-applicability gate must reach the relevant source dirs even on a
    large monolith. Static-asset and test trees are skipped so the bounded walk
    is not exhausted inside `public/` or `spec/` before it visits `app/`/`db/`.
    """

    def _make_rails_repo(self, root: Path, *, n_models: int, n_assets: int) -> None:
        for i in range(n_models):
            _touch(root, f"app/models/m{i}.rb", "class M\nend\n")
        # One migration companion proves the convention is kept.
        _touch(root, "db/migrate/20240101_create.rb", "class C\nend\n")
        # A large static-asset tree whose name sorts AFTER `app`/`db`, so a
        # reverse-alphabetical DFS would visit it first and burn the budget.
        for i in range(n_assets):
            _touch(root, f"public/assets/a{i}.js", "x\n")
        for i in range(n_assets):
            _touch(root, f"spec/models/m{i}_spec.rb", "x\n")

    def test_model_migration_enabled_when_asset_tree_skipped(self, tmp_path, monkeypatch):
        # Cap below the asset+spec count: if those dirs were walked, the budget
        # would run out before any model is seen and the rule would disable.
        monkeypatch.setenv("CHAMELEON_COCHANGE_MAX_FILES_SCANNED", "120")
        self._make_rails_repo(tmp_path, n_models=10, n_assets=200)
        assert cochange_rule_disabled(_rule("cochange-model-migration"), tmp_path) is False

    def test_model_migration_disabled_when_no_migration_companion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_COCHANGE_MAX_FILES_SCANNED", "120")
        for i in range(10):
            _touch(tmp_path, f"app/models/m{i}.rb", "class M\nend\n")
        for i in range(200):
            _touch(tmp_path, f"public/assets/a{i}.js", "x\n")
        # No db/migrate companion -> the repo does not keep the pairing here.
        assert cochange_rule_disabled(_rule("cochange-model-migration"), tmp_path) is True

    def test_too_few_triggers_stays_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_COCHANGE_MAX_FILES_SCANNED", "4000")
        # Below the trigger floor: not enough committed models to trust the signal.
        _touch(tmp_path, "app/models/only.rb", "class M\nend\n")
        _touch(tmp_path, "db/migrate/20240101_create.rb", "class C\nend\n")
        assert cochange_rule_disabled(_rule("cochange-model-migration"), tmp_path) is True
