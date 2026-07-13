"""PreToolUse deny tests for preflight_and_advise().

Task 11 inserts a deny check into the PreToolUse hook: before any advisory is
emitted, the proposed content (Edit new_string / Write content) is scanned for a
banned import. If the repo's enforcement mode is "enforce", the
import-preference-violation rule is in the active block set, the archetype was
AST-confirmed (match_quality == "ast") at high confidence, and the proposed
content carries a banned import, the tool call is denied before it runs.

Shadow mode never denies; it falls through to the advisory. CHAMELEON_ENFORCE=0
forces advisory. A non-AST match or non-high confidence skips the deny.

Isolation follows the sibling enforcement tests (no conftest): each run pins
CHAMELEON_PLUGIN_DATA at tmp_path, mocks repo/suppression resolution, forces the
in-process pattern-context path (daemon call mocked to None), and feeds a crafted
get_pattern_context result carrying the trust_state / confidence_band /
match_quality the deny gate reads. The competing-import convention is written to
the repo's on-disk conventions.json, which the deny path reads directly.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.enforcement_calibration import write_block_rules

# Proposed Write content that imports the banned module. The deny path only
# inspects imports in the proposed content, never the file's structure.
LODASH_CONTENT = "import _ from 'lodash'\n"

# Proposed content with no banned import; the preferred module is used instead.
CLEAN_CONTENT = "import { map } from 'lodash-es'\n"

# Banned import carrying the chameleon-ignore directive the deny message
# advertises. Following the message verbatim must clear the deny, not loop.
IGNORED_CONTENT = "import _ from 'lodash'  // chameleon-ignore import-preference-violation\n"


def _build_repo(
    tmp_path: Path, *, mode: str, with_competing_import: bool = True
) -> tuple[Path, str]:
    """Create a synthetic repo with config + conventions on disk.

    ``mode`` is written into ``.chameleon/config.json`` (enforcement.mode).
    When ``with_competing_import`` is set, a lodash -> lodash-es competing-import
    rule is written into conventions.json under the ``component`` archetype, which
    the deny path reads to detect the banned import.
    """
    repo_id = "deny_repo_id"
    (tmp_path / repo_id).mkdir(exist_ok=True)

    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": mode}}), encoding="utf-8"
    )

    conventions: dict = {"conventions": {}}
    if with_competing_import:
        conventions = {
            "conventions": {
                "imports": {
                    "component": {"competing": [{"over": "lodash", "preferred": "lodash-es"}]}
                }
            }
        }
    (chameleon / "conventions.json").write_text(json.dumps(conventions), encoding="utf-8")
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
) -> dict:
    """Drive preflight_and_advise() through the in-process pattern-context path.

    daemon_client.call returns None so the hook falls back to the in-process
    get_pattern_context, which is mocked to return the archetype + trust shape the
    deny gate reads.
    """
    result = {
        "data": {
            "repo": {"id": repo_id, "trust_state": trust_state},
            "archetype": {
                "archetype": "component",
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

        preflight_and_advise()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def test_banned_import_denied(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-deny",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny"


def test_chameleon_ignore_directive_clears_deny(tmp_path: Path):
    # The deny message tells the user to add
    # `// chameleon-ignore import-preference-violation`. Following it verbatim
    # must clear the deny gate, not leave the user in a deny loop.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=IGNORED_CONTENT,
        session_id="s-ignore",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_inline_ignore_records_override_at_deny_gate(tmp_path: Path):
    # An inline-ignored banned import bypasses the deny (allow), and the bypass is
    # RECORDED in the override audit. lint_conventions suppresses an ignored rule
    # so the lint-derived `banned` list is empty; the gate re-scans the content
    # with the directive stripped so the override still records. Without that
    # re-scan the recording is dead code and the bypass is invisible to the audit.
    import sqlite3

    from chameleon_mcp.drift import observations as obs

    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=IGNORED_CONTENT,
        session_id="s-ovr",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
    # Drop any cached write-connection so the read sees the committed row.
    for conn in list(obs._DRIFT_CONN.values()):
        conn.close()
    obs._DRIFT_CONN.clear()
    db = tmp_path / repo_id / "drift.db"
    assert db.is_file(), "the override should have created drift.db"
    con = sqlite3.connect(str(db))
    try:
        rules = [r[0] for r in con.execute("SELECT rule FROM rule_overrides").fetchall()]
    finally:
        con.close()
    assert "import-preference-violation" in rules


def test_banned_import_deny_records_decision_log(tmp_path: Path):
    # ex01-15: a real PreToolUse deny never reaches PostToolUse (the write is
    # denied, so the tool never runs), so the deny gate itself must log the
    # block to decision_log -- the audit /chameleon-explain replays -- exactly
    # like the sibling secret-detected-in-content and eval-call denies do.
    import sqlite3

    from chameleon_mcp.drift import observations as obs

    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-deny-log",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    # Drop any cached write-connection so the read sees the committed row.
    for conn in list(obs._DRIFT_CONN.values()):
        conn.close()
    obs._DRIFT_CONN.clear()
    db = tmp_path / repo_id / "drift.db"
    assert db.is_file(), "the block should have created drift.db"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT rel_path, outcome, blockable_rules FROM decision_log").fetchall()
    finally:
        con.close()
    assert any(r[1] == "blocked" and "import-preference-violation" in (r[2] or "") for r in rows), (
        f"missing blocked decision_log row: {rows}"
    )


def test_inline_ignore_records_decision_log_too(tmp_path: Path):
    # The override (bypass) path must ALSO log to decision_log, not just the
    # rule_overrides counter tested above -- explain_edit's classification
    # (blocked/overridden vs advised/coverage-gap) reads decision_log only.
    import sqlite3

    from chameleon_mcp.drift import observations as obs

    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=IGNORED_CONTENT,
        session_id="s-ovr-log",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
    for conn in list(obs._DRIFT_CONN.values()):
        conn.close()
    obs._DRIFT_CONN.clear()
    db = tmp_path / repo_id / "drift.db"
    assert db.is_file(), "the override should have created drift.db"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT rel_path, outcome, blockable_rules FROM decision_log").fetchall()
    finally:
        con.close()
    assert any(
        r[1] == "overridden" and "import-preference-violation" in (r[2] or "") for r in rows
    ), f"missing overridden decision_log row: {rows}"


def test_clean_import_not_denied(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=CLEAN_CONTENT,
        session_id="s-clean",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_shadow_mode_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="shadow")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-shadow",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_enforce_off_env_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-off",
        env={"CHAMELEON_ENFORCE": "0"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_inactive_rule_does_not_deny(tmp_path: Path):
    # The rule is calibrated inactive for this repo, so the banned import is
    # advisory only even in enforce mode.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": False, "fp_rate": 0.4, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-inactive",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_exact_match_denies_new_file(tmp_path: Path):
    # A brand-new file (Write target, no content on disk) resolves to
    # match_quality="exact" via the path-based match, a STRONGER signal than the
    # structural "ast" match. The deny gate must fire on it; new-file creation is
    # the most common place a banned import is introduced.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/NewWidget.ts"),
        content=LODASH_CONTENT,
        session_id="s-exact",
        env={"CHAMELEON_ENFORCE": "1"},
        match_quality="exact",
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny"


def test_exact_match_low_band_does_not_deny(tmp_path: Path):
    # An "exact" match at low confidence is the weak fallback (no AST query to
    # score, or an ambiguous multi-archetype path match), so it stays advisory.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/NewWidget.ts"),
        content=LODASH_CONTENT,
        session_id="s-exact-low",
        env={"CHAMELEON_ENFORCE": "1"},
        match_quality="exact",
        confidence_band="low",
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_non_ast_match_does_not_deny(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-heur",
        env={"CHAMELEON_ENFORCE": "1"},
        match_quality="heuristic",
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_untrusted_does_not_deny(tmp_path: Path):
    # Untrusted repos return early before the deny gate; the banned import is not
    # blocked because chameleon does not act on an untrusted profile.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "src/Widget.ts"),
        content=LODASH_CONTENT,
        session_id="s-untrusted",
        env={"CHAMELEON_ENFORCE": "1"},
        trust_state="untrusted",
    )
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"
