"""Worktree lifecycle: isolation, setup commit, changed-file detection."""

from __future__ import annotations

from pathlib import Path

from tests.effectiveness.arms import parse_arms
from tests.effectiveness.worktrees import changed_files, prepare_cell
from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import setup_fixture


def _seed_repo(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / ".chameleon").mkdir(parents=True)
    (seed / ".chameleon" / "config.json").write_text("{}\n")
    (seed / "src").mkdir()
    (seed / "src" / "a.ts").write_text("export const a = 1;\n")
    work_dir, _ = setup_fixture("fix", seed, tmp_path / "working")
    return work_dir


def test_prepare_cell_creates_isolated_worktree(tmp_path):
    repo = _seed_repo(tmp_path)
    shadow = parse_arms("shadow", None)[0]
    granted = []
    wt = tmp_path / "wt1"
    baseline = prepare_cell(
        fixture_repo=repo,
        dest=wt,
        arm=shadow,
        setup_fn=None,
        trust_fn=lambda p: granted.append(p) or "rid",
    )
    assert (wt / "src" / "a.ts").is_file()
    assert (wt / ".chameleon" / "config.json").is_file()
    assert granted == [wt]
    # arm-setup commit means the worktree diff vs baseline is empty
    assert changed_files(wt, baseline) == []
    # editing the worktree never touches the fixture repo
    (wt / "src" / "a.ts").write_text("export const a = 2;\n")
    assert (repo / "src" / "a.ts").read_text() == "export const a = 1;\n"


def test_setup_mutation_is_committed_not_diffed(tmp_path):
    repo = _seed_repo(tmp_path)
    shadow = parse_arms("shadow", None)[0]

    def plant(worktree: Path) -> None:
        (worktree / "src" / "planted.ts").write_text("export const bug = true;\n")

    wt = tmp_path / "wt2"
    baseline = prepare_cell(
        fixture_repo=repo, dest=wt, arm=shadow, setup_fn=plant, trust_fn=lambda p: "rid"
    )
    assert (wt / "src" / "planted.ts").is_file()
    assert changed_files(wt, baseline) == []  # planted BEFORE baseline


def test_changed_files_tracks_modified_and_untracked_not_chameleon(tmp_path):
    repo = _seed_repo(tmp_path)
    shadow = parse_arms("shadow", None)[0]
    wt = tmp_path / "wt3"
    baseline = prepare_cell(
        fixture_repo=repo, dest=wt, arm=shadow, setup_fn=None, trust_fn=lambda p: "rid"
    )
    (wt / "src" / "a.ts").write_text("export const a = 2;\n")
    (wt / "src" / "new.ts").write_text("export const n = 1;\n")
    (wt / ".chameleon" / "scratch.json").write_text("{}\n")
    assert changed_files(wt, baseline) == ["src/a.ts", "src/new.ts"]


def test_two_arms_same_task_do_not_contaminate(tmp_path):
    repo = _seed_repo(tmp_path)
    off, shadow = parse_arms("off,shadow", None)
    wt_off, wt_shadow = tmp_path / "wt-off", tmp_path / "wt-shadow"
    prepare_cell(fixture_repo=repo, dest=wt_off, arm=off, setup_fn=None, trust_fn=lambda p: "rid")
    prepare_cell(
        fixture_repo=repo, dest=wt_shadow, arm=shadow, setup_fn=None, trust_fn=lambda p: "rid"
    )
    (wt_off / "src" / "a.ts").write_text("OFF EDIT\n")
    assert (wt_shadow / "src" / "a.ts").read_text() == "export const a = 1;\n"
    r = run_bash("git worktree list", cwd=repo)
    assert "wt-off" in r.stdout and "wt-shadow" in r.stdout
