"""SessionStart memory-channel dedup (v3.1.0).

When the repo already imports `.chameleon/conventions.md` (CLAUDE.md /
CLAUDE.local.md / .claude/rules), the identical conventions content arrives
through the higher-authority memory channel at session load, so re-injecting
the full `<chameleon-conventions>` block doubles several KB of context for a
strictly weaker delivery. session_start() swaps the block for a one-line
pointer — but ONLY losslessly: the mirror must exist and must carry the
principles sections whenever principles.md has content (a pre-3.1.0 mirror
lacks them). Kill switch: CHAMELEON_MEMORY_CHANNEL_DEDUP=0.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import _compute_repo_id

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin"

PRINCIPLE_MARKER = "ZZPRINCIPLEMARKER"
POINTER = "load through your @.chameleon/conventions.md import"

_CONV = {
    "generation": 1,
    "conventions": {
        "imports": {
            "service": {
                "preferred": [],
                "competing": [{"preferred": "./httpClient", "over": "./http"}],
            }
        }
    },
}
_PRINCIPLES = f"1. {PRINCIPLE_MARKER} keep functions small\n"


def _current_mirror() -> str:
    """A 3.1.0 mirror: rendered by the real engine from the same inputs the
    SessionStart block uses, so its section headers match exactly."""
    from chameleon_mcp.conventions import render_conventions_md

    return render_conventions_md(_CONV, _PRINCIPLES)


def _pre_310_mirror() -> str:
    """A pre-3.1.0 mirror: conventions only, no principles sections."""
    from chameleon_mcp.conventions import render_conventions_md

    return render_conventions_md(_CONV)


def _build_repo(tmp_path: Path) -> Path:
    """Trusted, loadable profile whose conventions render non-empty (a taught
    competing import), with principles.md content — the fullest dedup case."""
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(json.dumps({"generation": 1, "archetypes": {}}))
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "idioms.md").write_text("# idioms\n\n## active\n\n### wrap\nWrap fetches.\n")
    (cham / "conventions.json").write_text(json.dumps(_CONV))
    (cham / "principles.md").write_text(_PRINCIPLES)
    (cham / "COMMITTED").touch()
    return repo


def _session_start_context(repo: Path, monkeypatch) -> str:
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(repo.parent / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    monkeypatch.setattr("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None)
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


def _wire(repo: Path) -> None:
    (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")


def test_wired_complete_mirror_dedups_to_pointer(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER in ctx
    assert "<chameleon-conventions>" not in ctx
    assert PRINCIPLE_MARKER not in ctx


def test_kill_switch_keeps_full_injection(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    monkeypatch.setenv("CHAMELEON_MEMORY_CHANNEL_DEDUP", "0")
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert "<chameleon-conventions>" in ctx
    assert PRINCIPLE_MARKER in ctx


def test_unwired_repo_keeps_full_injection(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert PRINCIPLE_MARKER in ctx


def test_pre_310_mirror_with_principles_on_disk_keeps_full_injection(tmp_path, monkeypatch):
    # Lossless gate: principles.md has content but the old-format mirror lacks
    # the principles sections — swapping would silently drop them.
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_pre_310_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert PRINCIPLE_MARKER in ctx


def test_missing_mirror_keeps_full_injection(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    _wire(repo)
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert PRINCIPLE_MARKER in ctx


def test_pre_310_mirror_without_principles_still_dedups(tmp_path, monkeypatch):
    # No principles.md content -> the old-format mirror is already complete.
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "principles.md").write_text("")
    (repo / ".chameleon" / "conventions.md").write_text(_pre_310_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER in ctx
    assert "<chameleon-conventions>" not in ctx


def test_content_stale_mirror_keeps_full_injection(tmp_path, monkeypatch):
    # Lossless means LINE-level: a mirror synced before conventions.json gained
    # a new rule still has every section header, but not the new rule line —
    # the swap must not drop that rule from the session.
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    stale_plus = json.loads(json.dumps(_CONV))
    stale_plus["conventions"]["imports"]["service"]["competing"].append(
        {"preferred": "./logger", "over": "winston"}
    )
    (repo / ".chameleon" / "conventions.json").write_text(json.dumps(stale_plus))
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert "./logger, not winston" in ctx  # the new rule still reaches the session


def test_fenced_import_mention_keeps_full_injection(tmp_path, monkeypatch):
    # Claude Code does not evaluate imports inside code fences, so a CLAUDE.md
    # that merely documents the wiring must not suppress the injection.
    repo = _build_repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        "Wiring example:\n\n```\n@.chameleon/conventions.md\n```\n", encoding="utf-8"
    )
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert PRINCIPLE_MARKER in ctx
