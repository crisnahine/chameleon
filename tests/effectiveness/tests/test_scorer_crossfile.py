"""Crossfile scorer against a real tmp git repo with a canned calls_index."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.effectiveness.scorers import crossfile
from tests.effectiveness.tests.test_scorer_base import _ctx

CALLS_INDEX = {
    "schema_version": 1,
    "callees": {
        "src/utils/format_money.ts": {
            "formatMoney": {
                "callers": [
                    {"path": "src/a.ts", "caller": "renderA", "line": 3, "grade": "import"},
                    {"path": "src/b.ts", "caller": "renderB", "line": 3, "grade": "import"},
                    {"path": "src/c.ts", "caller": "renderC", "line": 3, "grade": "import"},
                ],
                "total": 3,
                "truncated": False,
            }
        }
    },
}

CROSSFILE_TWO_HIGH = {
    "api_version": "1",
    "data": {
        "found": True,
        "findings": [
            {
                "symbol": "x",
                "module": "src/m.ts",
                "count": 1,
                "high_confidence": True,
                "sites": [{"path": "src/a.ts", "line": 1}],
            },
            {
                "symbol": "y",
                "module": "src/m.ts",
                "count": 1,
                "high_confidence": True,
                "sites": [{"path": "src/b.ts", "line": 1}],
            },
            {
                "symbol": "z",
                "module": "src/m.ts",
                "count": 1,
                "high_confidence": False,
                "sites": [{"path": "src/c.ts", "line": 1}],
            },
        ],
    },
}

CROSSFILE_UNAVAILABLE = {
    "api_version": "1",
    "data": {"found": False, "findings": [], "reason": "no-reverse-index"},
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo_with_index(tmp_path: Path, caller_texts: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "calls_index.json").write_text(json.dumps(CALLS_INDEX))
    (repo / "src").mkdir()
    for rel, text in caller_texts.items():
        (repo / rel).write_text(text)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def _make_ctx(tmp_path, monkeypatch, caller_texts, crossfile_resp):
    repo, sha = _repo_with_index(tmp_path, caller_texts)
    monkeypatch.setattr(crossfile, "_crossfile_context", lambda repo_path: crossfile_resp)
    ctx = _ctx(tmp_path)
    ctx.worktree = repo
    ctx.baseline_sha = sha
    ctx.pack.crossfile_targets[ctx.task.task_id] = {
        "module": "src/utils/format_money.ts",
        "function": "formatMoney",
        "new_name": "formatCurrency",
    }
    return ctx


def test_counts_high_confidence_breaks_and_updated_callers(tmp_path, monkeypatch):
    ctx = _make_ctx(
        tmp_path,
        monkeypatch,
        {
            "src/a.ts": "import { formatCurrency } from './utils/format_money';\nformatCurrency(1);\n",
            "src/b.ts": "import { formatCurrency } from './utils/format_money';\nformatCurrency(2);\n",
            "src/c.ts": "import { formatMoney } from './utils/format_money';\nformatMoney(3);\n",
        },
        CROSSFILE_TWO_HIGH,
    )
    out = crossfile.score(ctx)
    assert out["broken_exports"] == 2  # low-confidence finding dropped
    assert out["callers_total"] == 3
    assert out["callers_updated"] == 2
    assert out["callers_stale"] == 1


def test_unavailable_breakage_recorded_with_reason(tmp_path, monkeypatch):
    ctx = _make_ctx(
        tmp_path,
        monkeypatch,
        {
            "src/a.ts": "formatCurrency(1);\n",
            "src/b.ts": "formatCurrency(2);\n",
            "src/c.ts": "formatCurrency(3);\n",
        },
        CROSSFILE_UNAVAILABLE,
    )
    out = crossfile.score(ctx)
    assert out["broken_exports_unscored"] == "no-reverse-index"
    assert out["callers_updated"] == 3


def test_missing_calls_index_is_unscored(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/a.ts": "x\n"}, CROSSFILE_UNAVAILABLE)
    # Re-point the target at a module the canned index does not record.
    ctx.pack.crossfile_targets[ctx.task.task_id]["module"] = "src/nope.ts"
    out = crossfile.score(ctx)
    assert set(out) == {"unscored"}
    assert "calls_index" in out["unscored"]


def test_no_target_declared_skips_caller_half(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/a.ts": "x\n"}, CROSSFILE_TWO_HIGH)
    del ctx.pack.crossfile_targets[ctx.task.task_id]
    out = crossfile.score(ctx)
    assert out["broken_exports"] == 2
    assert "callers_total" not in out
