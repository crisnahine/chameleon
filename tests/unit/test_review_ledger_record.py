"""Unit tests for the persisted PR-review ledger in chameleon_mcp.review_ledger.

Covers the write/read round trip, the HMAC tamper-evidence (verified flag flips
when a line is edited), the findings normalization, the recency trim, the
merged-despite-BLOCK panel (with a real git repo so merge-base is exercised),
and fail-open on a missing/corrupt ledger.

Isolation: CHAMELEON_PLUGIN_DATA and CHAMELEON_HMAC_KEY_PATH both point under a
fresh tmp_path, so the per-repo ledger dir and the signing key are sandboxed and
never touch the developer's own state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp.review_ledger import (
    _ledger_path,
    build_review_ledger_panel,
    read_review_history,
    record_review,
)

REPO = "c" * 64


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    yield


def test_record_and_read_round_trip():
    rec = record_review(
        REPO,
        commit_sha="abc123",
        verdict="APPROVE",
        findings={"BLOCK": 0, "FIX": 2, "NIT": 5},
        profile_sha256="deadbeef",
        generation=7,
        schema_version=3,
        trust_state="trusted",
        engine_version="2.1.4",
        pr_id="42",
    )
    assert rec["hmac"]  # signed
    assert rec["verdict"] == "APPROVE"
    assert rec["findings"] == {"BLOCK": 0, "FIX": 2, "NIT": 5}

    history = read_review_history(REPO)
    assert history["total"] == 1
    assert history["unverified"] == 0
    record = history["records"][0]
    assert record["commit_sha"] == "abc123"
    assert record["profile_sha256"] == "deadbeef"
    assert record["generation"] == 7
    assert record["schema_version"] == 3
    assert record["trust_state"] == "trusted"
    assert record["engine_version"] == "2.1.4"
    assert record["pr_id"] == "42"
    assert record["reviewer"]
    assert record["verified"] is True


def test_history_newest_first_and_limit():
    for i in range(5):
        record_review(REPO, commit_sha=f"sha{i}", verdict="APPROVE")
    history = read_review_history(REPO, limit=2)
    assert history["total"] == 5
    shas = [r["commit_sha"] for r in history["records"]]
    assert shas == ["sha4", "sha3"]  # newest first, limited to 2


def test_tampered_record_fails_verification():
    record_review(REPO, commit_sha="x", verdict="BLOCK")
    path = _ledger_path(REPO)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["verdict"] = "APPROVE"  # flip the verdict, leave the old signature
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    history = read_review_history(REPO)
    assert history["records"][0]["verified"] is False
    assert history["unverified"] == 1


def test_unsigned_record_reads_unverified(monkeypatch):
    import chameleon_mcp.review_ledger as rl

    def _no_sign(_record):
        raise RuntimeError("no key")

    monkeypatch.setattr(rl, "_sign", _no_sign)
    rec = record_review(REPO, commit_sha="y", verdict="FIX")
    assert rec["hmac"] is None  # kept but unsigned

    history = read_review_history(REPO)
    assert history["records"][0]["verified"] is False
    assert history["unverified"] == 1


def test_findings_normalization_drops_garbage():
    rec = record_review(
        REPO,
        commit_sha="z",
        verdict="APPROVE",
        findings={"BLOCK": "not-an-int", 99: 1, "FIX": -3, "NIT": 4},
    )
    # non-int value, non-str key, and negative count all dropped; only NIT kept.
    assert rec["findings"] == {"NIT": 4}


def test_corrupt_line_skipped_not_fatal():
    record_review(REPO, commit_sha="ok", verdict="APPROVE")
    path = _ledger_path(REPO)
    with open(path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
    record_review(REPO, commit_sha="ok2", verdict="APPROVE")

    history = read_review_history(REPO)
    # Two valid records, the garbage line ignored.
    assert history["total"] == 2
    assert {r["commit_sha"] for r in history["records"]} == {"ok", "ok2"}


def test_trim_keeps_most_recent(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_LEDGER_MAX_RECORDS", "3")
    for i in range(6):
        record_review(REPO, commit_sha=f"c{i}", verdict="APPROVE")
    history = read_review_history(REPO, limit=100)
    assert history["total"] == 3
    assert [r["commit_sha"] for r in history["records"]] == ["c5", "c4", "c3"]


def test_read_failopen_missing_ledger():
    history = read_review_history(REPO)
    assert history == {"repo_id": REPO, "records": [], "total": 0, "unverified": 0}


def test_read_failopen_no_repo():
    history = read_review_history(None)
    assert history["records"] == []
    assert history["total"] == 0


def test_panel_none_when_empty():
    assert build_review_ledger_panel(REPO) is None
    assert build_review_ledger_panel(None) is None


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", name], repo)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_panel_flags_merged_despite_block(tmp_path, monkeypatch):
    git = subprocess.run(["git", "--version"], capture_output=True)
    if git.returncode != 0:
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    merged_sha = _commit(repo, "a.txt")  # ancestor of HEAD
    _commit(repo, "b.txt")  # advances HEAD past merged_sha

    # A trust record maps REPO -> repo_root so the panel can run git.
    from chameleon_mcp.profile.trust import repo_data_dir

    trust = repo_data_dir(REPO) / ".trust"
    trust.write_text(
        json.dumps(
            {
                "granted_at": "x",
                "granted_by_user": "t",
                "profile_sha256": "h",
                "repo_root": str(repo),
            }
        ),
        encoding="utf-8",
    )

    record_review(REPO, commit_sha=merged_sha, verdict="BLOCK")
    record_review(REPO, commit_sha="never0000", verdict="BLOCK")  # not in HEAD history
    record_review(REPO, commit_sha=merged_sha, verdict="APPROVE")  # approve, not a flag

    panel = build_review_ledger_panel(REPO)
    assert panel is not None
    assert panel["total"] == 3
    assert panel["last"]["verdict"] == "APPROVE"
    flagged = {e["commit_sha"] for e in panel["shipped_over_block"]}
    assert merged_sha in flagged
    assert "never0000" not in flagged  # unknown SHA degrades to not-merged
    assert panel["unverified"] == 0
