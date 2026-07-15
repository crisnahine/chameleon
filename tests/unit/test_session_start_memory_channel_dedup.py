"""SessionStart memory-channel dedup (v3.1.0, per-item since Phase 4).

When the repo already imports `.chameleon/conventions.md` (CLAUDE.md /
CLAUDE.local.md / .claude/rules), the identical conventions content arrives
through the higher-authority memory channel at session load, so re-injecting
it in full doubles several KB of context for a strictly weaker delivery.
session_start() dedups PER ITEM: any line the mirror already carries is
dropped from the fresh injection (with its section header kept attached to
whatever bullet survives, never left as an orphan), a fully-covered block
collapses to nothing, and a fully-covered conventions_block collapses to a
one-line pointer — exactly as before. Losslessly gated at line granularity:
a line is dropped ONLY when that exact line already appears in the text the
import actually delivers, so a pre-3.1.0 mirror missing the principles
sections, or a content-stale mirror missing one newer rule line, still
injects exactly the missing part (not the whole block, and not nothing).
Kill switch: CHAMELEON_MEMORY_CHANNEL_DEDUP=0.
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
# Both the full-coverage and partial-coverage pointer sentences share this
# substring, so a bare membership check works for either shape.
POINTER = "load through your @.chameleon/conventions.md import"
HTTPCLIENT_LINE = "Use ./httpClient, not ./http (service files)"

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
    import chameleon_mcp.hook_helper as _hh

    _hh._WIRED_MIRROR_CACHE.clear()
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


def test_pre_310_mirror_injects_only_missing_principles_section(tmp_path, monkeypatch):
    # Lossless gate, per-item: principles.md has content but the old-format
    # mirror lacks the principles sections entirely, so those sections still
    # inject in full — but the IMPORTS section, which the mirror DOES carry,
    # must not be duplicated (per-item, not all-or-nothing).
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_pre_310_mirror())
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER in ctx
    assert PRINCIPLE_MARKER in ctx
    assert HTTPCLIENT_LINE not in ctx


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


def test_content_stale_mirror(tmp_path, monkeypatch):
    # Lossless means LINE-level, and dedup is now PER-ITEM: a mirror synced
    # before conventions.json gained a new rule still has every section
    # header and the original rule line, but not the new rule line — the
    # fresh injection must carry ONLY that missing line (plus the pointer
    # for what the mirror already delivers), not the whole block, and must
    # not re-inject the line the mirror already carries.
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())
    stale_plus = json.loads(json.dumps(_CONV))
    stale_plus["conventions"]["imports"]["service"]["competing"].append(
        {"preferred": "./logger", "over": "winston"}
    )
    (repo / ".chameleon" / "conventions.json").write_text(json.dumps(stale_plus))
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER in ctx  # partial coverage still points at the mirror
    assert "./logger, not winston" in ctx  # the new rule still reaches the session
    assert HTTPCLIENT_LINE not in ctx  # already delivered — not duplicated
    assert PRINCIPLE_MARKER not in ctx  # fully covered by the mirror — dropped


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


def test_zero_overlap_mirror_keeps_full_injection(tmp_path, monkeypatch):
    # A wired mirror that shares NOTHING with the block (e.g. it renders from
    # a totally different profile) buys nothing from dedup — per-item must
    # not emit a pointer for zero actual coverage; the full block goes
    # through untouched, same as an unwired repo.
    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text("Unrelated mirror content.\n")
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert "<chameleon-conventions>" in ctx
    assert PRINCIPLE_MARKER in ctx
    assert HTTPCLIENT_LINE in ctx


def test_dedup_exception_falls_back_to_full_injection(tmp_path, monkeypatch):
    # Fail-open: a bug in the dedup helper itself must never lose conventions
    # content — the assignment is skipped and the full block goes through.
    import chameleon_mcp.hook_helper as _hh

    repo = _build_repo(tmp_path)
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(_current_mirror())

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_hh, "_dedupe_conventions_block", _boom)
    ctx = _session_start_context(repo, monkeypatch)
    assert POINTER not in ctx
    assert "<chameleon-conventions>" in ctx
    assert PRINCIPLE_MARKER in ctx
    assert HTTPCLIENT_LINE in ctx


def test_prefix_collision_line_is_not_falsely_deduped(tmp_path, monkeypatch):
    # Lossless is EXACT-LINE, not substring: a fresh rule whose text is a
    # string-prefix of an unrelated mirror line (e.g. "- Prefer ./api" vs the
    # mirror's "- Prefer ./apiV2") must NOT read as already-delivered — the
    # mirror never carries "- Prefer ./api", so dropping it would lose a
    # convention the model never received. Build a preferred-imports profile
    # whose fresh value is a prefix of the mirror's value.
    from chameleon_mcp.conventions import render_conventions_md

    fresh = {
        "generation": 1,
        "conventions": {
            "imports": {
                "service": {
                    "preferred": [{"module": "./apiclient", "frequency": 10}],
                    "competing": [],
                }
            }
        },
    }
    mirror_conv = json.loads(json.dumps(fresh))
    mirror_conv["conventions"]["imports"]["service"]["preferred"][0]["module"] = "./apiclientV2"

    repo = _build_repo(tmp_path)
    (repo / ".chameleon" / "conventions.json").write_text(json.dumps(fresh))
    (repo / ".chameleon" / "principles.md").write_text("")  # isolate the imports section
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(render_conventions_md(mirror_conv))
    ctx = _session_start_context(repo, monkeypatch)
    # The fresh rule reaches the session verbatim; the mirror's differing value
    # is NOT what got matched.
    assert "- Prefer ./apiclient\n" in ctx or "- Prefer ./apiclient" in ctx
    assert "<chameleon-conventions>" in ctx


def test_decoy_mirror_reciting_a_rule_out_of_section_does_not_suppress_it(tmp_path, monkeypatch):
    # A hand-authored mirror that recites a still-current rule line under a
    # "deprecated / rejected" heading (not the real section header) must NOT
    # suppress that rule's hook injection: pruning is anchored to the section
    # header the mirror actually delivers, so a bullet floating in unrelated
    # prose is not treated as delivered.
    repo = _build_repo(tmp_path)
    (repo / ".chameleon" / "principles.md").write_text("")  # isolate the imports section
    _wire(repo)
    (repo / ".chameleon" / "conventions.md").write_text(
        "# Deprecated rules (no longer enforced)\n\n"
        "The following was proposed and REJECTED -- do not follow:\n"
        f"{HTTPCLIENT_LINE.replace('Use', '- Use')}\n\n"
        "We continue importing ./http directly everywhere.\n"
    )
    ctx = _session_start_context(repo, monkeypatch)
    # The real IMPORTS section header is absent from the decoy, so the rule
    # survives in the fresh injection rather than being silently dropped.
    assert HTTPCLIENT_LINE in ctx
    assert "<chameleon-conventions>" in ctx


def test_session_start_snapshots_delivered_idiom_gists(tmp_path, monkeypatch):
    # session_start writes a SessionStart-time snapshot of the mirror's
    # delivered idiom slugs whenever the wired import delivers gists.
    from chameleon_mcp.conventions import render_conventions_md
    from chameleon_mcp.core.idiom_store import IdiomRecord, upsert_idiom
    from chameleon_mcp.hook_helper import _MIRROR_IDIOMS_SNAPSHOT
    from chameleon_mcp.optouts import _safe_session_marker

    repo = _build_repo(tmp_path)
    _wire(repo)
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    (repo / ".chameleon" / "conventions.md").write_text(
        render_conventions_md(_CONV, _PRINCIPLES, idioms)
    )
    # The snapshot resolves each delivered gist NAME to its store slug; seed
    # the record _build_repo's raw idioms.md ("### wrap") describes.
    upsert_idiom(
        repo / ".chameleon",
        IdiomRecord(
            slug="wrap",
            title="wrap",
            rationale="Wrap fetches.",
            status="active",
            added_date="2026-07-15",
            rank=1,
        ),
    )
    _session_start_context(repo, monkeypatch)
    snap = (
        tmp_path
        / "data"
        / _compute_repo_id(repo)
        / _MIRROR_IDIOMS_SNAPSHOT.format(session=_safe_session_marker("s1"))
    )
    assert json.loads(snap.read_text()) == ["wrap"]
