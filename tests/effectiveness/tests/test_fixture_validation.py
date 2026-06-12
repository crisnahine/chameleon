"""Bootstrap each committed fixture and validate scorer ground truth.

Spec requirement: cross-file tasks reference functions that exist in the
fixture's BUILT calls_index with enough caller edges (3+). These tests run
the real bootstrap, so they need the dump toolchains; they skip cleanly where
node / the vendored typescript / ruby are absent (e.g. a minimal CI runner)
and always run on dev machines.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "effectiveness" / "fixtures"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None
    or not (REPO_ROOT / "mcp" / "node_modules" / "typescript").is_dir(),
    reason="node + vendored typescript required to bootstrap the TS fixture",
)
needs_ruby = pytest.mark.skipif(
    shutil.which("ruby") is None,
    reason="ruby required to bootstrap the Rails fixture",
)


def _bootstrap(tmp_path: Path, name: str) -> Path:
    from tests.journey.harness.fixtures import setup_fixture

    work_dir, _origin = setup_fixture(name, FIXTURES / name, tmp_path / "working")
    from chameleon_mcp.tools import bootstrap_repo

    resp = bootstrap_repo(str(work_dir))
    status = resp["data"].get("status")
    assert status in ("success", "already_bootstrapped"), f"bootstrap failed: {resp['data']}"
    return work_dir


@needs_node
def test_eff_ts_crossfile_targets_have_three_plus_callers(tmp_path):
    work_dir = _bootstrap(tmp_path, "eff_ts")
    from chameleon_mcp.calls_index import load_calls_index

    idx = load_calls_index(work_dir)
    assert idx is not None, "bootstrap produced no calls_index.json"
    from tests.effectiveness.tasks import tier1_ts

    for task_id, target in tier1_ts.CROSSFILE_TARGETS.items():
        entry = idx.callers_of(target["module"], target["function"])
        assert entry is not None, f"{task_id}: target missing from calls_index"
        assert entry["total"] >= 3, f"{task_id}: only {entry['total']} caller edges"


@needs_node
def test_eff_ts_duplication_bait_in_catalog_and_idioms_survive(tmp_path):
    work_dir = _bootstrap(tmp_path, "eff_ts")
    from chameleon_mcp.function_catalog import load_function_catalog

    catalog = load_function_catalog(work_dir)
    assert catalog is not None
    names = {(fn.file, fn.name) for fn in catalog.functions}
    assert ("src/utils/slugify.ts", "slugify") in names
    idioms = (work_dir / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "all-http-via-api-client" in idioms
    assert "money-is-integer-cents" in idioms


@needs_ruby
def test_eff_rails_crossfile_targets_have_three_plus_callers(tmp_path):
    work_dir = _bootstrap(tmp_path, "eff_rails")
    from chameleon_mcp.calls_index import load_calls_index

    idx = load_calls_index(work_dir)
    assert idx is not None, "bootstrap produced no calls_index.json"
    from tests.effectiveness.tasks import tier1_rails

    for task_id, target in tier1_rails.CROSSFILE_TARGETS.items():
        entry = idx.callers_of(target["module"], target["function"])
        assert entry is not None, f"{task_id}: target missing from calls_index"
        assert entry["total"] >= 3, f"{task_id}: only {entry['total']} caller edges"


@needs_ruby
def test_eff_rails_duplication_bait_in_catalog_and_idioms_survive(tmp_path):
    work_dir = _bootstrap(tmp_path, "eff_rails")
    from chameleon_mcp.function_catalog import load_function_catalog

    catalog = load_function_catalog(work_dir)
    assert catalog is not None
    names = {(fn.file, fn.name) for fn in catalog.functions}
    assert ("app/lib/email_normalizer.rb", "normalize") in names
    idioms = (work_dir / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "services-return-result-never-raise" in idioms


def test_setups_mutate_then_fixture_tests_fail(tmp_path):
    """Each planted bug must make the fixture's own test command fail —
    otherwise the verification prompt lies and the task measures nothing."""
    import subprocess

    from tests.effectiveness.tasks import tier1_rails, tier1_ts

    ts_copy = tmp_path / "eff_ts"
    shutil.copytree(FIXTURES / "eff_ts", ts_copy)
    tier1_ts.SETUPS["plant_clamp_bug"](ts_copy)
    if shutil.which("node") is not None:
        r = subprocess.run(["npm", "test", "--silent"], cwd=ts_copy, capture_output=True, text=True)
        assert r.returncode != 0, "planted clamp bug did not fail the fixture tests"

    rails_copy = tmp_path / "eff_rails"
    shutil.copytree(FIXTURES / "eff_rails", rails_copy)
    tier1_rails.SETUPS["plant_refund_bug"](rails_copy)
    if shutil.which("ruby") is not None:
        r = subprocess.run(
            ["ruby", "-Itest", "tests/run_tests.rb"],
            cwd=rails_copy,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, "planted refund bug did not fail the fixture tests"
