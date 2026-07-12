"""Status enforcement-section tests for get_status().

/chameleon-status must surface the active enforcement mode, the block rules
that calibration kept active for this repo, and any block rule calibration
demoted (kept advisory) along with the false-positive rate that demoted it.

Isolation mirrors the sibling enforcement tests (no shared conftest): the
make_trusted_repo factory builds a real repo + config + plugin-data dir under
tmp_path and patches repo resolution so get_status reads the on-disk config and
enforcement.json.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def make_trusted_repo(tmp_path):
    """Factory: a trusted repo with an enforcement config and an isolated data dir.

    Returns ``(repo, data_dir, session_id, file_path, profile_dir)``. The repo's
    resolution (find_repo_root / _compute_repo_id) and plugin-data dir are
    patched so get_status reads the config and enforcement.json under the repo.
    """
    stack = ExitStack()

    def _factory(*, mode: str = "shadow", stop_block_cap: int = 3):
        repo_id = "status_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "stop_block_cap": stop_block_cap}}),
            encoding="utf-8",
        )
        # A COMMITTED, LOADABLE profile: bootstrap always writes the core trio
        # (archetypes/canonicals/rules) atomically with a shared integer
        # generation, so a real profile that get_status renders always has them.
        # _profile_unrenderable_status reports corrupt on a missing/mismatched core
        # artifact (a real full profile with one deleted), so the fixture writes a
        # minimal-but-complete set at one generation.
        _gen = 1
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"schema_version": 1, "language": "typescript", "generation": _gen}),
            encoding="utf-8",
        )
        profile_dir.joinpath("archetypes.json").write_text(
            json.dumps({"generation": _gen, "archetypes": {}}), encoding="utf-8"
        )
        profile_dir.joinpath("canonicals.json").write_text(
            json.dumps({"generation": _gen, "canonicals": {}}), encoding="utf-8"
        )
        profile_dir.joinpath("rules.json").write_text(
            json.dumps({"generation": _gen, "rules": {}}), encoding="utf-8"
        )
        profile_dir.joinpath("COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-status"

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id))
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        return repo, data_dir, session_id, file_path, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def test_status_reports_enforcement(make_trusted_repo):
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    write_block_rules(
        profile_dir,
        {
            "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 9},
            "jsx-presence-mismatch": {"active": False, "fp_rate": 0.05, "sampled": 9},
        },
    )
    out = get_status(str(repo))
    text = json.dumps(out)
    assert "enforcement" in text.lower()
    assert "shadow" in text.lower()


def test_status_reports_degraded_block(make_trusted_repo, monkeypatch):
    """get_status surfaces a cumulative degraded-delivery block read from
    .hook_errors.log (no-interpreter/spawn-failed) and metrics.jsonl (fail_open)."""
    import time

    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    (data_dir / ".hook_errors.log").write_text(
        f"[{ts}] preflight-and-advise no-interpreter (no Python >=3.11, uv unavailable)\n"
        f"[{ts}] posttool-verify failed (python=/usr/bin/python3)\n",
        encoding="utf-8",
    )

    out = get_status(str(repo))
    degraded = out["data"]["degraded"]
    assert degraded["window_days"] == 7
    assert degraded["no_interpreter"] == 1
    assert degraded["spawn_failed"] == 1
    assert degraded["total"] == 2
    assert degraded["last_ts"] == ts


def test_status_degraded_block_fails_open_when_reader_raises(make_trusted_repo):
    """If the degraded reader raises, get_status omits the degraded key and still
    returns its normal enforcement envelope -- a status read never crashes."""
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    with patch(
        "chameleon_mcp.degraded_telemetry.read_degraded_summary",
        side_effect=RuntimeError("boom"),
    ):
        out = get_status(str(repo))

    assert "degraded" not in out["data"]
    assert out["data"]["enforcement"]["mode"] == "shadow"


def test_status_surfaces_proposed_demotions_section(make_trusted_repo):
    # A rule carrying a pending demotion proposal is surfaced in
    # enforcement.proposed_demotions while staying in the active (blocking) list;
    # the key is omitted entirely when no entry carries a proposal.
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    proposal = {
        "reason": "high-override-rate",
        "override_rate": 0.9,
        "events": 12,
        "distinct_sessions": 1,
        "security_rule": False,
    }
    write_block_rules(
        profile_dir,
        {
            "import-preference-violation": {
                "active": True,
                "fp_rate": 0.0,
                "sampled": 9,
                "demotion_proposed": proposal,
            },
            "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 9},
        },
    )

    out = get_status(str(repo))
    enforcement = out["data"]["enforcement"]
    assert enforcement["proposed_demotions"] == [
        {"rule": "import-preference-violation", **proposal}
    ]
    # Still blocking: a proposal never moves the rule out of the active set.
    assert "import-preference-violation" in enforcement["active"]

    write_block_rules(
        profile_dir,
        {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 9}},
    )
    out = get_status(str(repo))
    assert "proposed_demotions" not in out["data"]["enforcement"]


def test_status_unknown_repo_id_returns_no_repo():
    # A 64-hex repo_id that maps to no known repo must signal no_repo, not be
    # treated as a relative path (which walks up to the CWD's repo and reports
    # ITS enforcement state under the bogus id) (BUG-2).
    from chameleon_mcp.tools import get_status

    out = get_status("d" * 64)
    assert out["data"]["status"] == "no_repo"
    assert "mode" not in out["data"]


# --------------------------------------------------------------------------- #
# B5: headline calibration-precision summary
# --------------------------------------------------------------------------- #


def test_block_precision_summary_aggregates_active_rules():
    from chameleon_mcp.tools import _block_precision_summary

    block_rules = {
        "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 12},
        "naming-convention-violation": {"active": True, "fp_rate": 0.01, "sampled": 12},
        "jsx-presence-mismatch": {"active": False, "fp_rate": 0.4, "sampled": 12},
    }
    summary = _block_precision_summary(
        block_rules, ["phantom-import", "naming-convention-violation"]
    )
    assert summary["active_block_rules"] == 2
    assert summary["sampled_files"] == 12
    assert summary["max_fp_rate"] == 0.01  # the inactive 0.4 is excluded
    assert summary["mean_fp_rate"] == 0.005


def test_block_precision_summary_empty_active():
    from chameleon_mcp.tools import _block_precision_summary

    summary = _block_precision_summary({}, [])
    assert summary["active_block_rules"] == 0
    assert summary["max_fp_rate"] == 0.0


def test_status_surfaces_precision_summary(make_trusted_repo):
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    write_block_rules(
        profile_dir,
        {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 9}},
    )
    out = get_status(str(repo))
    precision = out["data"]["enforcement"].get("precision")
    assert precision is not None
    # phantom-import from the artifact plus the two read-time-exempt security
    # rules (always listed active; absent artifact entries contribute no sample).
    assert precision["active_block_rules"] == 3
    assert precision["sampled_files"] == 9
    assert precision["max_fp_rate"] == 0.0


def test_status_lists_security_rules_active_without_artifact(make_trusted_repo):
    # No enforcement.json at all: the gates still deny hard secrets/evals (the
    # read-time exemption), so status must list those two rules active rather
    # than reporting a firing gate as off — and never list them as demoted,
    # even when a legacy artifact carries a stale inactive entry for them.
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    out = get_status(str(repo))
    enf = out["data"]["enforcement"]
    assert "secret-detected-in-content" in enf["active"]
    assert "eval-call" in enf["active"]

    # Legacy zero-witness entry marked inactive: still active, never demoted.
    write_block_rules(
        profile_dir,
        {
            "secret-detected-in-content": {
                "active": False,
                "fp_rate": 0.0,
                "sampled": 0,
            },
            "eval-call": {"active": False, "fp_rate": 0.0, "sampled": 0},
        },
    )
    out = get_status(str(repo))
    enf = out["data"]["enforcement"]
    assert "secret-detected-in-content" in enf["active"]
    assert "eval-call" in enf["active"]
    assert all(d["rule"] not in ("secret-detected-in-content", "eval-call") for d in enf["demoted"])


def test_status_unprofiled_repo_lists_no_security_rules(make_trusted_repo):
    # No profile.json means no hook gate can ever fire (trust requires a
    # committed profile), so the security-rule union must not run: listing the
    # pair active on an unprofiled repo would be a false assurance. The
    # artifact-unreadable flag shares the same gate -- a repo that was never
    # profiled is not an enforcement-artifact problem.
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    # A genuinely UNPROFILED repo has no profile artifacts at all -- not a
    # profile.json-less dir that still carries core artifacts (that is a corrupt
    # partial, which _profile_unrenderable_status now reports as corrupt).
    for _art in (
        "profile.json",
        "COMMITTED",
        "archetypes.json",
        "canonicals.json",
        "rules.json",
    ):
        (profile_dir / _art).unlink(missing_ok=True)

    enf = get_status(str(repo))["data"]["enforcement"]
    assert "secret-detected-in-content" not in enf["active"]
    assert "eval-call" not in enf["active"]
    assert enf["enforcement_artifact_unreadable"] is False


# --------------------------------------------------------------------------- #
# REAL-TEST-REPORT-2026-06-21: status surfaces malformed config (#2) + the
# correctness_judge flag (#6).
# --------------------------------------------------------------------------- #


def test_status_reports_correctness_judge_flag(make_trusted_repo):
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    enf = get_status(str(repo))["data"]["enforcement"]
    # finding #6: correctness_judge was parsed but never surfaced in status.
    assert enf["correctness_judge"] is True
    assert enf["config_malformed"] is False


def test_status_surfaces_malformed_enforcement_config(make_trusted_repo):
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    # Break the enforcement section; enforcement.json (a separate artifact) still
    # lists the secret rule active.
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": 42}}), encoding="utf-8"
    )
    write_block_rules(
        profile_dir,
        {"secret-detected-in-content": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    enf = get_status(str(repo))["data"]["enforcement"]
    # finding #2: status used to show mode "off" beside an "active" secret rule
    # with no malformed signal. Now it flags the malformed config and does not
    # claim rules are armed while the mode is unreadable.
    assert enf["config_malformed"] is True
    assert enf["mode"] == "off"
    assert enf["active"] == []


def test_status_unrelated_section_typo_still_shows_enforce(make_trusted_repo):
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="enforce")
    # A typo in an UNRELATED section must not make status report enforcement off
    # (the gates still enforce via the isolated read), so config_malformed is
    # False and the mode is the real "enforce".
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce"}, "auto_refresh": {"enabled": "yes"}}),
        encoding="utf-8",
    )
    enf = get_status(str(repo))["data"]["enforcement"]
    assert enf["config_malformed"] is False
    assert enf["mode"] == "enforce"
