"""Trust gate enforced at the data layer, not only the hook presentation layer.

Before this change the boundary "untrusted -> no canonical injection" lived
ONLY in preflight_and_advise. The model-callable MCP tools returned untrusted
content directly (get_canonical_excerpt had no trust check at all), and
SessionStart injected conventions.json + principles.md with no trust gate and
no sanitization. These tests pin:
  - untrusted -> content/idioms blanked,
  - stale -> still flows (matches the documented contract),
  - trusted -> full content,
  - SessionStart sanitizes attacker inputs WITHOUT mangling the legit
    <chameleon-conventions> wrapper, and skips the block entirely when untrusted.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import (
    _compute_repo_id,
    get_canonical_excerpt,
    get_pattern_context,
    get_rules,
    lint_file,
    pause_session,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin"  # the installed-plugin surface

ARCH = "service"
WITNESS = "service.ts"
WITNESS_CONTENT = "export function makeService() {\n  return 1;\n}\n"
IDIOMS = "Always wrap fetches in the apiClient helper.\n"


def _build_repo(
    tmp_path: Path, *, conventions: bool = False, principles: str | None = None
) -> Path:
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "service objects"}}})
    )
    (cham / "rules.json").write_text(
        json.dumps({"generation": 1, "rules": {"no-default-export": {"severity": "warn"}}})
    )
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "deadbeef"}}]},
            }
        )
    )
    (cham / "idioms.md").write_text(IDIOMS)
    if conventions:
        (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    if principles is not None:
        (cham / "principles.md").write_text(principles)
    (cham / "COMMITTED").touch()
    (repo / WITNESS).write_text(WITNESS_CONTENT)
    return repo


# --- get_canonical_excerpt (model-callable; previously had NO trust gate) -----


def test_canonical_excerpt_blocked_when_untrusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("status") == "untrusted"
    assert res.get("content") is None


def test_canonical_excerpt_returned_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("status") != "untrusted"
    assert "makeService" in (res.get("content") or "")


def test_canonical_excerpt_flows_when_stale(tmp_path, monkeypatch):
    """Stale (trusted-then-changed) still returns content, matching the contract."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    # Mutate a profile artifact so the trust hash no longer matches (stale).
    (repo / ".chameleon" / "rules.json").write_text(
        json.dumps({"generation": 1, "rules": [{"id": "x"}]})
    )
    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("status") != "untrusted"
    assert "makeService" in (res.get("content") or "")


# --- get_pattern_context (idioms prove the gate fires without archetype-match) -


def test_get_pattern_context_blanks_idioms_when_untrusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    data = get_pattern_context(str(repo / WITNESS))["data"]
    assert data["repo"]["trust_state"] == "untrusted"
    assert data["idioms"] == ""
    assert data["canonical_excerpt"]["content"] == ""
    assert data["rules"] == []  # rules.json withheld for untrusted


def test_get_pattern_context_keeps_idioms_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    data = get_pattern_context(str(repo / WITNESS))["data"]
    assert data["repo"]["trust_state"] == "trusted"
    assert "apiClient" in data["idioms"]
    assert data["rules"]  # rules.json flows for trusted


# --- get_pattern_context pause gate (pa01-17) ----------------------------------


def test_get_pattern_context_blanks_guidance_when_paused(tmp_path, monkeypatch):
    # A direct MCP call bypassed pause_session's suppression entirely -- only
    # the PreToolUse hook checked it before calling this tool, so a paused
    # session got identical guidance through a direct call.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")

    before = get_pattern_context(str(repo / WITNESS))["data"]
    assert before["repo"]["paused"] is False
    assert "apiClient" in before["idioms"]

    pause_res = pause_session(str(repo), 15)["data"]
    assert pause_res["status"] == "success"

    after = get_pattern_context(str(repo / WITNESS))["data"]
    assert after["repo"]["paused"] is True
    assert after["idioms"] == ""
    assert after["canonical_excerpt"]["content"] == ""
    assert after["canonical_excerpt"]["redacted_reason"] == "paused"
    assert after["rules"] == []
    # trust_state is unaffected by pause -- it is a separate axis.
    assert after["repo"]["trust_state"] == "trusted"


# --- SessionStart conventions/principles injection -----------------------------

PRINCIPLE_MARKER = "ZZPRINCIPLEMARKER"
BIDI = "‮"  # right-to-left override; the sanitizer strips it


def _session_start_context(repo: Path, monkeypatch, *, trusted: bool) -> str:
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(repo.parent / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setattr("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None)
    if trusted:
        grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    monkeypatch.chdir(repo)

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO('{"session_id": "s1"}')),
        patch("sys.stdout") as out,
    ):
        out.write = captured.append
        from chameleon_mcp.hook_helper import session_start

        session_start()
    obj = json.loads("".join(captured))
    return obj["hookSpecificOutput"]["additionalContext"]


def test_session_start_skips_conventions_when_untrusted(tmp_path, monkeypatch):
    principles = f"1. {PRINCIPLE_MARKER} keep functions small {BIDI}\n"
    repo = _build_repo(tmp_path, conventions=True, principles=principles)
    ctx = _session_start_context(repo, monkeypatch, trusted=False)
    assert PRINCIPLE_MARKER not in ctx
    assert "<chameleon-conventions>" not in ctx


def test_session_start_injects_sanitized_conventions_when_trusted(tmp_path, monkeypatch):
    principles = f"1. {PRINCIPLE_MARKER} keep functions small {BIDI}\n"
    repo = _build_repo(tmp_path, conventions=True, principles=principles)
    ctx = _session_start_context(repo, monkeypatch, trusted=True)
    assert PRINCIPLE_MARKER in ctx  # conventions injected when trusted
    assert "<chameleon-conventions>" in ctx  # legit wrapper intact, not mangled
    assert BIDI not in ctx  # principles input was sanitized


# --- get_rules (model-callable; returns committed rules.json) ------------------


def test_get_rules_blocked_when_untrusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    repo = _build_repo(tmp_path)
    res = get_rules(str(repo))["data"]
    assert res.get("status") == "untrusted"
    assert res.get("rules") == []


def test_get_rules_returned_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    res = get_rules(str(repo))["data"]
    assert res.get("status") != "untrusted"


# --- lint_file (model-callable; profile-derived violations must not leak untrusted)


def test_lint_file_blocked_when_untrusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    res = lint_file(str(repo), ARCH, "export const x = 1;\n", file_path="x.ts")["data"]
    assert res.get("status") == "untrusted"
    assert res.get("stub") is True  # convention/AST checks withheld


def test_lint_file_proceeds_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = _build_repo(tmp_path)
    grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    res = lint_file(str(repo), ARCH, "export const x = 1;\n", file_path="x.ts")["data"]
    assert res.get("status") != "untrusted"


# --- posttool_verify (PostToolUse: must not feed untrusted violations) ---------


def _run_posttool_verify(repo: Path, monkeypatch, *, trusted: bool):
    """Run posttool_verify against a file in `repo`; return (emitted, daemon_calls).

    Spying on daemon_client.call (the first thing past the trust gate) proves
    whether the gate fired: untrusted must short-circuit before it.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(repo.parent / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    if trusted:
        grant_trust(_compute_repo_id(repo), repo / ".chameleon")
    calls: list = []
    monkeypatch.setattr(
        "chameleon_mcp.daemon_client.call",
        lambda method, *a, **k: (calls.append(method), None)[1],
    )
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / WITNESS)},
            "tool_response": {"success": True},
            "session_id": "s1",
        }
    )
    captured: list[str] = []
    with patch("sys.stdin", io.StringIO(payload)), patch("sys.stdout") as out:
        out.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()
    text = "".join(captured).strip()
    return (json.loads(text) if text else {}), calls


def test_posttool_verify_skips_untrusted(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    emitted, calls = _run_posttool_verify(repo, monkeypatch, trusted=False)
    assert emitted == {}  # no violation feedback
    assert calls == []  # gated before the archetype lookup / lint


def test_posttool_verify_proceeds_when_trusted(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    _emitted, calls = _run_posttool_verify(repo, monkeypatch, trusted=True)
    assert calls != []  # reached the lint path (gate did not fire)


# --- monorepo: a root grant must NOT vouch for an ungranted nested workspace ---
# A workspace with its own .chameleon shares the root's git-based repo_id, so a
# root-only grant previously leaked the workspace's canonical / rules / lint /
# verify output (the gates checked `trust record exists for repo_id`, not
# `record grants THIS root`). These pin the closed gate on every model-callable
# surface + PostToolUse.

_MONO_ID = "a1" * 32  # 64-char hex; stands in for the shared git-based repo_id


def _build_monorepo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "mono"
    ws = root / "packages" / "svc"
    for base in (root, ws):
        cham = base / ".chameleon"
        cham.mkdir(parents=True)
        (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
        (cham / "archetypes.json").write_text(
            json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "service objects"}}})
        )
        (cham / "rules.json").write_text(
            json.dumps({"generation": 1, "rules": {"no-default-export": {"severity": "warn"}}})
        )
        (cham / "canonicals.json").write_text(
            json.dumps(
                {
                    "generation": 1,
                    "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "deadbeef"}}]},
                }
            )
        )
        (cham / "idioms.md").write_text(IDIOMS)
        (cham / "COMMITTED").touch()
        (base / WITNESS).write_text(WITNESS_CONTENT)
    return root, ws


def _grant_root_only(monkeypatch, root: Path) -> None:
    # Collapse root + workspace to one repo_id (git does this), grant ONLY root.
    monkeypatch.setattr("chameleon_mcp.tools._compute_repo_id", lambda p: _MONO_ID)
    grant_trust(_MONO_ID, root / ".chameleon")


def test_canonical_excerpt_untrusted_for_ungranted_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    root, ws = _build_monorepo(tmp_path)
    _grant_root_only(monkeypatch, root)
    assert get_canonical_excerpt(str(root), ARCH)["data"].get("status") != "untrusted"
    res = get_canonical_excerpt(str(ws), ARCH)["data"]
    assert res.get("status") == "untrusted"
    assert res.get("content") is None


def test_get_rules_untrusted_for_ungranted_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    root, ws = _build_monorepo(tmp_path)
    _grant_root_only(monkeypatch, root)
    assert get_rules(str(root))["data"].get("status") != "untrusted"
    res = get_rules(str(ws))["data"]
    assert res.get("status") == "untrusted"
    assert res.get("rules") == []


def test_lint_file_untrusted_for_ungranted_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    root, ws = _build_monorepo(tmp_path)
    _grant_root_only(monkeypatch, root)
    assert (
        lint_file(str(root), ARCH, "export const x = 1;\n", file_path="x.ts")["data"].get("status")
        != "untrusted"
    )
    res = lint_file(str(ws), ARCH, "export const x = 1;\n", file_path="x.ts")["data"]
    assert res.get("status") == "untrusted"
    assert res.get("stub") is True


def test_posttool_verify_skips_ungranted_workspace(tmp_path, monkeypatch):
    root, ws = _build_monorepo(tmp_path)
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    _grant_root_only(monkeypatch, root)
    calls: list = []
    monkeypatch.setattr(
        "chameleon_mcp.daemon_client.call", lambda method, *a, **k: (calls.append(method), None)[1]
    )
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ws / WITNESS)},
            "tool_response": {"success": True},
            "session_id": "s1",
        }
    )
    captured: list[str] = []
    with patch("sys.stdin", io.StringIO(payload)), patch("sys.stdout") as out:
        out.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()
    text = "".join(captured).strip()
    assert (json.loads(text) if text else {}) == {}
    assert calls == []
