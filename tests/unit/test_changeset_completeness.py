"""Unit tests for the change-set-completeness co-change computation.

Two layers are covered:

- the pure logic in ``cochange.py`` (the rule table, the per-repo disable gate,
  and the new-file-vs-changeset item computation), driven with injected
  callables and on-disk fixtures;
- the Stop-time wiring in ``hook_helper._changeset_completeness_lines``, driven
  against a real git work tree so the new-file detection and per-rule cache run
  for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from chameleon_mcp.cochange import (
    _COCHANGE_RULES,
    changeset_completeness_items,
    cochange_rule_disabled,
)
from chameleon_mcp.lint_engine import detect_language


def _touch(root: Path, rel: str, body: str = "x\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.test")
    _git(root, "config", "user.name", "t")


def _commit_all(root: Path, message: str = "seed") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


class TestRuleTable:
    def test_rules_are_directional_and_unique(self):
        ids = [r.rule_id for r in _COCHANGE_RULES]
        assert len(ids) == len(set(ids))
        # The curated set stays small and framework-keyed (Rails + TS + Django).
        assert 3 <= len(_COCHANGE_RULES) <= 6
        assert {r.language for r in _COCHANGE_RULES} == {"ruby", "typescript", "python"}

    def test_model_trigger_excludes_concerns_and_base(self):
        rule = next(r for r in _COCHANGE_RULES if r.rule_id == "cochange-model-migration")
        assert rule.trigger("app/models/order.rb")
        assert not rule.trigger("app/models/concerns/auditable.rb")
        assert not rule.trigger("app/models/application_record.rb")
        assert not rule.trigger("app/services/order.rb")
        assert rule.companion("db/migrate/20240101_create_orders.rb")
        assert not rule.companion("app/models/order.rb")

    def test_controller_trigger_and_route_companion(self):
        rule = next(r for r in _COCHANGE_RULES if r.rule_id == "cochange-controller-route")
        assert rule.trigger("app/controllers/orders_controller.rb")
        assert not rule.trigger("app/controllers/application_controller.rb")
        assert rule.companion("config/routes.rb")
        assert rule.companion("config/routes/admin.rb")
        assert not rule.companion("app/controllers/orders_controller.rb")


class TestRuleDisable:
    def _model_rule(self):
        return next(r for r in _COCHANGE_RULES if r.rule_id == "cochange-model-migration")

    def test_enabled_when_repo_pairs_trigger_and_companion(self, tmp_path):
        for i in range(10):
            _touch(tmp_path, f"app/models/m{i}.rb")
        _touch(tmp_path, "db/migrate/20240101_x.rb")
        assert cochange_rule_disabled(self._model_rule(), tmp_path) is False

    def test_disabled_when_no_companion_anywhere(self, tmp_path):
        for i in range(10):
            _touch(tmp_path, f"app/models/m{i}.rb")
        # No db/migrate file at all -> the repo does not follow the pairing.
        assert cochange_rule_disabled(self._model_rule(), tmp_path) is True

    def test_disabled_when_trigger_sample_too_thin(self, tmp_path):
        _touch(tmp_path, "app/models/only.rb")
        _touch(tmp_path, "db/migrate/20240101_x.rb")
        # One model is below the trust floor: stay silent rather than guess.
        assert cochange_rule_disabled(self._model_rule(), tmp_path) is True

    def test_min_trigger_threshold_override(self, tmp_path, monkeypatch):
        _touch(tmp_path, "app/models/a.rb")
        _touch(tmp_path, "app/models/b.rb")
        _touch(tmp_path, "db/migrate/20240101_x.rb")
        monkeypatch.setenv("CHAMELEON_COCHANGE_MIN_TRIGGER_FILES", "2")
        assert cochange_rule_disabled(self._model_rule(), tmp_path) is False


class TestChangeSetItems:
    def test_new_model_without_migration_flags(self, tmp_path):
        model = _touch(tmp_path, "app/models/order.rb")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs={str(model)},
            edited_abs={str(model)},
            language_of=detect_language,
        )
        assert len(items) == 1
        assert items[0].rule_id == "cochange-model-migration"
        assert items[0].source_rel == "app/models/order.rb"

    def test_new_model_with_migration_in_changeset_is_quiet(self, tmp_path):
        model = _touch(tmp_path, "app/models/order.rb")
        migration = _touch(tmp_path, "db/migrate/20240101_create_orders.rb")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs={str(model)},
            edited_abs={str(model), str(migration)},
            language_of=detect_language,
        )
        assert items == []

    def test_companion_satisfied_by_existing_edited_file(self, tmp_path):
        # The route file is an EDIT to an existing file, not itself new; it still
        # satisfies the companion because the check is over the whole change-set.
        controller = _touch(tmp_path, "app/controllers/orders_controller.rb")
        routes = _touch(tmp_path, "config/routes.rb")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs={str(controller)},
            edited_abs={str(controller), str(routes)},
            language_of=detect_language,
        )
        assert items == []

    def test_only_new_files_trigger_not_edits(self, tmp_path):
        # The model is edited, not created (not in new_files_abs): no migration demand.
        model = _touch(tmp_path, "app/models/order.rb")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs=set(),
            edited_abs={str(model)},
            language_of=detect_language,
        )
        assert items == []

    def test_disabled_rule_does_not_fire(self, tmp_path):
        model = _touch(tmp_path, "app/models/order.rb")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs={str(model)},
            edited_abs={str(model)},
            language_of=detect_language,
            rule_enabled=lambda _rule: False,
        )
        assert items == []

    def test_unsupported_language_skipped(self, tmp_path):
        doc = _touch(tmp_path, "README.md")
        items = changeset_completeness_items(
            repo_root=tmp_path,
            new_files_abs={str(doc)},
            edited_abs={str(doc)},
            language_of=detect_language,
        )
        assert items == []

    def test_empty_new_files_returns_empty(self, tmp_path):
        assert (
            changeset_completeness_items(
                repo_root=tmp_path,
                new_files_abs=set(),
                edited_abs=set(),
                language_of=detect_language,
            )
            == []
        )


class TestStopGate:
    """Drive hook_helper._changeset_completeness_lines against a real git tree."""

    def _cfg(self, mode="shadow", changeset_completeness=True):
        return SimpleNamespace(mode=mode, changeset_completeness=changeset_completeness)

    def _state_for(self, paths):
        from chameleon_mcp.enforcement import EnforcementState, FileState

        st = EnforcementState()
        for p in paths:
            st.files[str(p)] = FileState()
        return st

    def _call(self, repo_root, state, cfg):
        from chameleon_mcp.hook_helper import _changeset_completeness_lines

        return _changeset_completeness_lines(
            repo_root=repo_root,
            state=state,
            cfg=cfg,
            daemon_state={"available": False},
        )

    def _seed_rails(self, root: Path) -> None:
        # Enough committed models + a migration so the rule is enabled for the repo.
        _init_repo(root)
        for i in range(10):
            _touch(root, f"app/models/m{i}.rb")
        _touch(root, "db/migrate/20240101_seed.rb")
        _touch(root, "config/routes.rb")
        _commit_all(root)

    def test_new_uncommitted_model_flagged(self, tmp_path):
        self._seed_rails(tmp_path)
        new_model = _touch(tmp_path, "app/models/order.rb")
        lines = self._call(tmp_path, self._state_for([new_model]), self._cfg())
        assert lines
        joined = "\n".join(lines)
        assert "app/models/order.rb" in joined
        assert "companion" in joined

    def test_existing_committed_model_edit_not_flagged(self, tmp_path):
        self._seed_rails(tmp_path)
        # m0.rb is committed -> editing it is not a creation, no migration demand.
        existing = tmp_path / "app/models/m0.rb"
        existing.write_text("# changed\n", encoding="utf-8")
        lines = self._call(tmp_path, self._state_for([existing]), self._cfg())
        assert lines == []

    def test_off_mode_quiet(self, tmp_path):
        self._seed_rails(tmp_path)
        new_model = _touch(tmp_path, "app/models/order.rb")
        lines = self._call(tmp_path, self._state_for([new_model]), self._cfg(mode="off"))
        assert lines == []

    def test_disabled_flag_quiet(self, tmp_path):
        self._seed_rails(tmp_path)
        new_model = _touch(tmp_path, "app/models/order.rb")
        lines = self._call(
            tmp_path,
            self._state_for([new_model]),
            self._cfg(changeset_completeness=False),
        )
        assert lines == []

    def test_inline_ignore_opts_out(self, tmp_path):
        self._seed_rails(tmp_path)
        new_model = _touch(tmp_path, "app/models/order.rb", "# chameleon-ignore cochange\n")
        lines = self._call(tmp_path, self._state_for([new_model]), self._cfg())
        assert lines == []

    def test_companion_in_changeset_quiet(self, tmp_path):
        self._seed_rails(tmp_path)
        new_model = _touch(tmp_path, "app/models/order.rb")
        new_migration = _touch(tmp_path, "db/migrate/20240202_create_orders.rb")
        lines = self._call(tmp_path, self._state_for([new_model, new_migration]), self._cfg())
        assert lines == []

    def test_no_git_fails_safe_quiet(self, tmp_path):
        # No git repo: new-file detection cannot confirm a creation -> stay silent.
        for i in range(10):
            _touch(tmp_path, f"app/models/m{i}.rb")
        _touch(tmp_path, "db/migrate/20240101_seed.rb")
        new_model = _touch(tmp_path, "app/models/order.rb")
        lines = self._call(tmp_path, self._state_for([new_model]), self._cfg())
        assert lines == []
