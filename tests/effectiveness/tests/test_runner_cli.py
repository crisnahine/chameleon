"""Runner CLI: --list, --dry-run, task selection, arg validation."""

from __future__ import annotations

import pytest
from tests.effectiveness import runner
from tests.effectiveness.tasks import EffTask


@pytest.fixture()
def stub_tasks(monkeypatch):
    tasks = [
        EffTask(
            task_id="t1-stub-conv",
            tier="ci",
            fixture="ts",
            prompt="p1",
            category="convention",
            scorers=("convention", "cost"),
        ),
        EffTask(
            task_id="t1-stub-dup",
            tier="ci",
            fixture="rails",
            prompt="p2",
            category="duplication",
            scorers=("duplication", "cost"),
            repeats=2,
        ),
    ]
    monkeypatch.setattr(runner, "_collect_tasks", lambda: tasks)
    return tasks


def test_list_exits_zero_and_prints_tasks(stub_tasks, capsys):
    rc = runner.main(["--list"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "t1-stub-conv" in err and "t1-stub-dup" in err


def test_dry_run_prints_cell_plan_without_spawning(stub_tasks, capsys):
    rc = runner.main(["--dry-run", "--arms", "off,shadow"])
    assert rc == 0
    err = capsys.readouterr().err
    # 2 tasks x 2 arms, repeats 1 + 2 -> 6 cells
    assert "cells: 6" in err
    assert "DRY RUN" in err


def test_unknown_task_id_is_an_error(stub_tasks, capsys):
    rc = runner.main(["--dry-run", "--tasks", "nope"])
    assert rc == 2
    assert "unknown task id" in capsys.readouterr().err


def test_tasks_filter_selects_subset(stub_tasks, capsys):
    rc = runner.main(["--dry-run", "--tasks", "t1-stub-conv", "--arms", "off,shadow"])
    assert rc == 0
    assert "cells: 2" in capsys.readouterr().err


def test_repeats_override(stub_tasks, capsys):
    rc = runner.main(["--dry-run", "--repeats", "3", "--arms", "off"])
    assert rc == 0
    # 2 tasks x 1 arm x 3 repeats (CLI overrides per-task repeats)
    assert "cells: 6" in capsys.readouterr().err


def test_budget_too_small_aborts(stub_tasks, capsys):
    rc = runner.main(["--dry-run", "--max-budget-usd", "0.01"])
    assert rc == 1
    assert "max-budget-usd" in capsys.readouterr().err
