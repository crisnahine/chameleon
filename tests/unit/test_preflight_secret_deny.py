"""PreToolUse hard-secret deny tests for preflight_and_advise().

A deterministic hard-kind credential in the PROPOSED content (Write `content`
/ Edit `new_string` / NotebookEdit `new_source`) is denied before it reaches
disk. Unlike the import deny, the secret deny is archetype-independent: it
fires before the no-archetype early-return and carries no match-quality or
confidence gate. The enforcement spine still applies: enforce denies, shadow
records would_block and falls through, CHAMELEON_ENFORCE=0 and mode=off
disable, untrusted/stale profiles never block, and the rule must be in the
calibrated active block set. Only a rule-NAMED chameleon-ignore clears the
deny; the bare blanket form does not cover credentials.

Isolation mirrors test_preflight_deny.py (no conftest): CHAMELEON_PLUGIN_DATA
pinned at tmp_path, repo/suppression resolution mocked, daemon call mocked to
None, get_pattern_context mocked to a crafted trust/archetype shape.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.enforcement_calibration import write_block_rules

# The canonical documented AWS example key — deterministic kind, never a real
# credential. The same fixture the sibling secret tests use.
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

SECRET_CONTENT = f'const k = "{AWS_KEY}";\n'

CLEAN_CONTENT = "export const greeting = 'hello world';\n"

# Following the deny message verbatim must clear the deny, not loop.
NAMED_IGNORED_CONTENT = f'const k = "{AWS_KEY}"; // chameleon-ignore secret-detected-in-content\n'

BARE_IGNORED_CONTENT = f'const k = "{AWS_KEY}"; // chameleon-ignore\n'

# A 40-char base64 run next to a credential keyword: entropy/advisory kind
# only, excluded from the deny by construction.
ENTROPY_ONLY_CONTENT = 'const awsSecretKey = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0";\n'

ACTIVE_SECRET_RULE = {"secret-detected-in-content": {"active": True, "fp_rate": 0.0, "sampled": 3}}


def _build_repo(tmp_path: Path, *, mode: str) -> tuple[Path, str]:
    """Create a synthetic repo with enforcement config on disk."""
    repo_id = "secret_deny_repo_id"
    (tmp_path / repo_id).mkdir(exist_ok=True)

    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": mode}}), encoding="utf-8"
    )
    (chameleon / "conventions.json").write_text(json.dumps({"conventions": {}}), encoding="utf-8")
    return repo, repo_id


def _run_preflight(
    *,
    repo: Path,
    repo_id: str,
    tmp_path: Path,
    file_path: str,
    content: str,
    session_id: str,
    env: dict | None = None,
    trust_state: str = "trusted",
    confidence_band: str = "high",
    match_quality: str = "ast",
    tool_name: str = "Write",
    archetype: str | None = "component",
) -> dict:
    """Drive preflight_and_advise() through the in-process pattern-context path."""
    result = {
        "data": {
            "repo": {"id": repo_id, "trust_state": trust_state},
            "archetype": {
                "archetype": archetype,
                "confidence_band": confidence_band,
                "match_quality": match_quality,
                "summary": "",
            },
            "canonical_excerpt": {},
            "rules": [],
            "idioms": "",
        }
    }

    if tool_name == "Write":
        tool_input = {"file_path": file_path, "content": content}
    elif tool_name == "NotebookEdit":
        tool_input = {"notebook_path": file_path, "new_source": content}
    else:
        tool_input = {"file_path": file_path, "new_string": content}

    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
    }

    run_env = {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}
    if env:
        run_env.update(env)

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, run_env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", return_value=None),
        patch("chameleon_mcp.tools.get_pattern_context", return_value=result),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import preflight_and_advise

        rc = preflight_and_advise()

    assert rc == 0
    # Single-emit discipline: exactly one JSON object per invocation.
    lines = [ln for ln in "".join(captured).splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one hook-output object, got {len(lines)}"
    return json.loads(lines[0])


def _decision(out: dict) -> str | None:
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def _would_block_rows(tmp_path: Path) -> list[dict]:
    metrics = tmp_path / "metrics.jsonl"
    if not metrics.is_file():
        return []
    rows = [json.loads(ln) for ln in metrics.read_text(encoding="utf-8").splitlines() if ln]
    return [
        r for r in rows if r.get("would_block") and r.get("rule") == "secret-detected-in-content"
    ]


def _blocked_decisions(tmp_path: Path, repo_id: str) -> list[dict]:
    """Rows the deny logged to the decision_log (the non-shadow audit channel)."""
    db = tmp_path / repo_id / "drift.db"
    if not db.is_file():
        return []
    con = sqlite3.connect(str(db))
    try:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT * FROM decision_log WHERE outcome = 'blocked'").fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        con.close()


def test_enforce_write_with_secret_denied(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-deny",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_enforce_edit_fragment_denied(tmp_path: Path):
    # A fragment scans without whole-file context.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-edit",
        env={"CHAMELEON_ENFORCE": "1"},
        tool_name="Edit",
    )
    assert _decision(out) == "deny"


def test_deny_reason_is_actionable_and_never_leaks_the_token(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-reason",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "aws_access_key" in reason
    assert "line 1" in reason
    assert "// chameleon-ignore secret-detected-in-content" in reason
    assert AWS_KEY not in reason


def test_deny_fires_without_an_archetype(tmp_path: Path):
    # A credential is archetype-independent: the deny sits before the
    # no-archetype early-return, so unarchetyped files are covered.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "scripts/deploy.ts"),
        content=SECRET_CONTENT,
        session_id="s-noarch",
        env={"CHAMELEON_ENFORCE": "1"},
        archetype=None,
    )
    assert _decision(out) == "deny"


def test_deny_fires_regardless_of_match_quality_and_band(tmp_path: Path):
    # Unlike the import deny, there is no archetype-confidence gate.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-heur",
        env={"CHAMELEON_ENFORCE": "1"},
        match_quality="heuristic",
        confidence_band="low",
    )
    assert _decision(out) == "deny"


def test_shadow_mode_records_would_block_and_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="shadow")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-shadow",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"
    rows = _would_block_rows(tmp_path)
    assert rows, "shadow mode must record a would_block row"
    row = rows[0]
    assert row["hook"] == "preflight-and-advise"
    assert row["file_rel"] == "src/config.ts"
    assert row["line"] == 1


def test_enforce_deny_records_block_decision_not_would_block(tmp_path: Path):
    # would_block is a SHADOW measurement: an enforce deny must NOT inflate it,
    # because the shadow -> enforce promotion tally reads that counter (finding
    # #7). The actual block stays auditable via the decision log -- the same
    # channel the PostToolUse block uses -- so /chameleon-explain can replay it
    # without polluting the promotion signal.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-parity",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"
    assert not _would_block_rows(tmp_path)
    assert _blocked_decisions(tmp_path, repo_id)


def test_enforce_env_zero_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-off-env",
        env={"CHAMELEON_ENFORCE": "0"},
    )
    assert _decision(out) != "deny"


def test_mode_off_does_not_deny_and_writes_no_row(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="off")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-mode-off",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"
    assert _would_block_rows(tmp_path) == []


def test_inactive_rule_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"secret-detected-in-content": {"active": False, "fp_rate": 0.2, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-inactive",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_untrusted_and_stale_do_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    for i, trust_state in enumerate(("untrusted", "stale")):
        out = _run_preflight(
            repo=repo,
            repo_id=repo_id,
            tmp_path=tmp_path,
            file_path=str(repo / "src/config.ts"),
            content=SECRET_CONTENT,
            session_id=f"s-trust-{i}",
            env={"CHAMELEON_ENFORCE": "1"},
            trust_state=trust_state,
        )
        assert _decision(out) != "deny", trust_state


def test_named_directive_clears_deny_and_records_override(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=NAMED_IGNORED_CONTENT,
        session_id="s-named",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"
    db = tmp_path / repo_id / "drift.db"
    assert db.is_file(), "the named bypass must be recorded as an auditable override"
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT rule, blanket FROM rule_overrides").fetchall()
    finally:
        conn.close()
    assert ("secret-detected-in-content", 0) in rows


def test_bare_blanket_directive_does_not_clear_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=BARE_IGNORED_CONTENT,
        session_id="s-bare",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_file_scope_directive_must_be_named(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    bare = f"// chameleon-ignore-file\n{SECRET_CONTENT}"
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=bare,
        session_id="s-file-bare",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"

    named = f"// chameleon-ignore-file secret-detected-in-content\n{SECRET_CONTENT}"
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=named,
        session_id="s-file-named",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_edit_fragment_honors_named_file_directive_on_disk(tmp_path: Path):
    # A fixture file annotated once with a NAMED file-scope directive must not
    # deny every later fragment edit; the disk read is lazy (deny-candidate
    # path only).
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    target = repo / "src" / "fixtures.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "// chameleon-ignore-file secret-detected-in-content\nexport const fixtures = [];\n",
        encoding="utf-8",
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(target),
        content=SECRET_CONTENT,
        session_id="s-disk-named",
        env={"CHAMELEON_ENFORCE": "1"},
        tool_name="Edit",
    )
    assert _decision(out) != "deny"

    # A BARE on-disk file directive does not cover credentials.
    target.write_text("// chameleon-ignore-file\nexport const fixtures = [];\n", encoding="utf-8")
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(target),
        content=SECRET_CONTENT,
        session_id="s-disk-bare",
        env={"CHAMELEON_ENFORCE": "1"},
        tool_name="Edit",
    )
    assert _decision(out) == "deny"


def test_write_does_not_consult_on_disk_directives(tmp_path: Path):
    # A Write replaces the whole file: if the proposed content drops the named
    # directive, whatever is on disk no longer applies.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    target = repo / "src" / "fixtures.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// chameleon-ignore-file secret-detected-in-content\n", encoding="utf-8")
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(target),
        content=SECRET_CONTENT,
        session_id="s-write-disk",
        env={"CHAMELEON_ENFORCE": "1"},
        tool_name="Write",
    )
    assert _decision(out) == "deny"


def test_truncation_boundary_on_huge_payloads(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    # Secret inside the first 100KB of a 5MB payload: denied, and completes.
    early = SECRET_CONTENT + "x" * 5_000_000
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=early,
        session_id="s-huge-early",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"

    # Secret past PREWRITE_SECRET_SCAN_MAX_CHARS: the documented blind spot —
    # left to the PostToolUse/Stop scans of the on-disk file.
    late = "x" * 200_000 + "\n" + SECRET_CONTENT
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=late,
        session_id="s-huge-late",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_clean_content_falls_through_to_advisory(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=CLEAN_CONTENT,
        session_id="s-clean",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_entropy_only_content_never_denies(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=ENTROPY_ONLY_CONTENT,
        session_id="s-entropy",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_malformed_payloads_fail_open(tmp_path: Path):
    # These all bail before repo resolution; the deny block must not break the
    # exit-0 + single-valid-JSON contract on garbage input.
    from chameleon_mcp.hook_helper import preflight_and_advise

    payloads = [
        "",
        "not json",
        json.dumps({"tool_input": "not-a-dict"}),
        json.dumps({"tool_input": {"file_path": 42, "content": SECRET_CONTENT}}),
    ]
    for text in payloads:
        captured: list[str] = []
        with (
            patch("sys.stdin", io.StringIO(text)),
            patch("sys.stdout") as mock_stdout,
            patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}, clear=False),
        ):
            mock_stdout.write = captured.append
            rc = preflight_and_advise()
        assert rc == 0, repr(text)
        out = "".join(captured).strip()
        assert json.loads(out) == {}, repr(text)


# --------------------------------------------------------------------------- #
# REAL-TEST-REPORT-2026-06-21 regressions: config isolation (#1) + eval (#3)
# --------------------------------------------------------------------------- #

ACTIVE_EVAL_RULE = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 3}}
EVAL_CONTENT = "const r = eval(userInput);\n"
NAMED_IGNORED_EVAL = "const r = eval(userInput); // chameleon-ignore eval-call\n"


def test_unrelated_config_section_typo_does_not_disable_secret_deny(tmp_path: Path):
    # finding #1: load_config validates the WHOLE config, so a typo in an
    # UNRELATED section used to raise and silently downgrade the credential deny
    # to advisory. The gate now reads the enforcement section in isolation, so an
    # auto_refresh typo can no longer disable credential blocking.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    (repo / ".chameleon" / "config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce"}, "auto_refresh": {"enabled": "yes"}}),
        encoding="utf-8",
    )
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/config.ts"),
        content=SECRET_CONTENT,
        session_id="s-iso",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_eval_call_denied_pre_write_in_enforce(tmp_path: Path):
    # finding #3: a real eval()/exec() is an RCE and now earns the same pre-write
    # deny a hardcoded credential gets.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_EVAL_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/run.ts"),
        content=EVAL_CONTENT,
        session_id="s-eval",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_named_ignore_clears_eval_deny(tmp_path: Path):
    # a NAMED directive clears the eval deny; the bare form does not (eval-call is
    # blanket-immune), mirroring the credential rule.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_EVAL_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/run.ts"),
        content=NAMED_IGNORED_EVAL,
        session_id="s-eval-ign",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_eval_shadow_mode_does_not_deny(tmp_path: Path):
    # shadow never blocks; it records the would-block measurement instead.
    repo, repo_id = _build_repo(tmp_path, mode="shadow")
    write_block_rules(repo / ".chameleon", ACTIVE_EVAL_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/run.ts"),
        content=EVAL_CONTENT,
        session_id="s-eval-shadow",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"
