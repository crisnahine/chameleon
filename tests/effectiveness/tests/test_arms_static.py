"""Static-conventions arm: parsing, CLAUDE.md rendering, idempotency, wire-up."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.effectiveness.arms import arm_env, parse_arm_models, parse_arms
from tests.effectiveness.static_conventions import (
    StaticConventionsError,
    render_static_conventions,
)

_SENTINEL = "<!-- chameleon-static-conventions -->"


def _write_profile(worktree: Path) -> None:
    """Committed-profile shape the renderer reads: summary + conventions +
    principles + idioms. The conventions cover every dimension class the
    SessionStart block renders a section for that the old bespoke renderer
    DROPPED (naming, inheritance, class_contract, required_guards), so the
    render tests catch a regression to the under-informed control."""
    cham = worktree / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "profile.summary.md").write_text(
        "# chameleon profile summary\n\n"
        "Language: typescript\n\n"
        "## 2 archetypes detected\n\n"
        "- **component** (cluster_size 12) — canonical: `src/components/dashboard-info.tsx`\n"
        "- **service** (cluster_size 8) — canonical: `src/services/user.service.ts`\n",
        encoding="utf-8",
    )
    (cham / "conventions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "conventions": {
                    "imports": {
                        "component": {
                            "preferred": [
                                {"module": "@/lib/api-client", "frequency": 12, "total": 20},
                                {"module": "zod", "frequency": 22, "total": 20},
                            ],
                            "competing": [{"preferred": "@/lib/api-client", "over": "axios"}],
                        },
                        "service": {
                            "preferred": [
                                {"module": "@/lib/api-client", "frequency": 5, "total": 8}
                            ],
                            "competing": [],
                        },
                    },
                    "naming": {
                        "component": {
                            "interface_prefix": {"pattern": "I", "consistency": 0.97},
                            "file_naming": {
                                "casing": "kebab-case",
                                "casing_consistency": 0.92,
                                "suffix": ".tsx",
                            },
                        }
                    },
                    "inheritance": {"service": {"dominant_base": "BaseService", "frequency": 0.96}},
                    "class_contract": {
                        "service": {
                            "base": "BaseService",
                            "decorators": ["Injectable"],
                            "dsl_macros": [],
                            "required_methods": ["execute", "validate"],
                        }
                    },
                    "required_guards": {"controller": {"required_guards": ["authenticate_user!"]}},
                    "method_calls": {"service": {"common_top5": ["validates", "belongs_to"]}},
                    "key_exports": {
                        "component": ["DashboardInfo", "AdminGuard"],
                        "migration": ["AddUsersTable"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (cham / "principles.md").write_text(
        "# principles\n\n"
        "1. Never invent an import path; copy one from a sibling file.\n"
        "2. Money amounts are integer cents end to end.\n\n"
        "## Anti-hallucination protocol\n\n"
        "- Before importing a symbol, verify it exists with search_codebase.\n"
        "- Copy import paths from sibling files, never from memory.\n",
        encoding="utf-8",
    )
    (cham / "idioms.md").write_text(
        "# idioms\n\n"
        "## active\n\n"
        "### money-is-integer-cents\n"
        "Language: typescript\n"
        "Status: active (added 2026-06-12)\n"
        "Money amounts are integer cents end to end.\n\n"
        "## deprecated\n\n"
        "### old-fetch-wrapper\n"
        "Language: typescript\n"
        "Status: deprecated\n"
        "Superseded by apiGet/apiPost.\n",
        encoding="utf-8",
    )


# --- arm parsing --------------------------------------------------------------


def test_parse_arms_static_fields():
    off, static, shadow = parse_arms("off,static,shadow", None)
    assert [s.name for s in (off, static, shadow)] == ["off", "static", "shadow"]
    assert static.disable_env is True
    assert static.static_conventions is True
    assert static.base_mode == "shadow"  # mode is irrelevant under CHAMELEON_DISABLE
    assert off.static_conventions is False
    assert shadow.static_conventions is False
    assert off.disable_env is True and shadow.disable_env is False


def test_arm_env_static_sets_disable():
    static = next(s for s in parse_arms("off,static,shadow", None) if s.name == "static")
    base: dict[str, str] = {}
    assert arm_env(static, base)["CHAMELEON_DISABLE"] == "1"
    assert base == {}  # base env never mutated


def test_parse_arm_models_accepts_static():
    assert parse_arm_models("static=opus") == {"static": "opus"}
    specs = parse_arms("off,static", None, {"static": "opus"})
    static = next(s for s in specs if s.name == "static")
    assert static.model == "opus"


def test_static_is_not_a_toggle_base():
    # A toggle pairs off a live (non-disabled) arm; static runs with
    # CHAMELEON_DISABLE=1 so it can never host a feature flip.
    specs = parse_arms("off,static,shadow", "counterexample")
    paired = next(s for s in specs if "~" in s.name)
    assert paired.name.startswith("shadow~")


# --- rendering ----------------------------------------------------------------


def test_render_writes_claude_md_with_profile_knowledge(tmp_path):
    _write_profile(tmp_path)
    text = render_static_conventions(tmp_path)
    on_disk = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert on_disk == text
    assert _SENTINEL in text
    assert "## Codebase conventions" in text
    # profile.summary.md content (an archetype name known to be in the fixture)
    assert "component" in text
    # conventions.json: competing + preferred imports, DSL calls, key exports
    assert "Use @/lib/api-client, not axios" in text
    assert "Prefer zod" in text
    assert "Prefer @/lib/api-client" in text
    assert "Common DSL: belongs_to, validates" in text
    assert "DashboardInfo" in text
    # migration exports are never reuse candidates (mirrors the session block)
    assert "AddUsersTable" not in text
    # the sections the old bespoke renderer dropped, now via the shared block
    assert "Prefix interfaces with I (97%, enforced)" in text
    assert "component files use kebab-case, suffix .tsx (92%)" in text
    assert "Inherit BaseService (96%, enforced)" in text
    assert "service: @Injectable, extends BaseService, define execute, validate" in text
    assert "before_action :authenticate_user!" in text
    # principles.md: numbered principles + the anti-hallucination protocol
    assert "Never invent an import path" in text
    assert "verify it exists with search_codebase" in text
    # idioms.md: active section in, deprecated section out
    assert "money-is-integer-cents" in text
    assert "old-fetch-wrapper" not in text


def test_conventions_section_equals_sessionstart_block(tmp_path):
    """Exact-equality parity: the static arm's conventions section IS
    format_conventions_for_session's output for the profile's own data. A
    future SessionStart section the renderer failed to carry would break
    this, so the under-informed-control gap cannot silently re-open. The
    expected value is computed WITHOUT the render pipeline's sanitization —
    on a clean profile every sanitizer is an identity transform, so any
    difference is a real divergence."""
    from chameleon_mcp.conventions import format_conventions_for_session

    _write_profile(tmp_path)
    text = render_static_conventions(tmp_path)

    cham = tmp_path / ".chameleon"
    data = json.loads((cham / "conventions.json").read_text(encoding="utf-8"))
    principles = (cham / "principles.md").read_text(encoding="utf-8")
    expected = format_conventions_for_session(data, principles_text=principles)
    assert expected  # fixture must produce a non-empty session block
    assert expected in text
    for header in (
        "IMPORTS (enforce):",
        "NAMING:",
        "INHERITANCE:",
        "CONTRACT:",
        "AUTHZ (advisory):",
        "PATTERNS:",
        "REUSE:",
        "PRINCIPLES:",
        "ANTI-HALLUCINATION PROTOCOL:",
    ):
        assert header in expected, f"fixture data no longer feeds {header!r}"


def test_render_is_deterministic_and_bounded(tmp_path):
    _write_profile(tmp_path)
    first = render_static_conventions(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    second = render_static_conventions(tmp_path)
    assert first == second
    # summary/idioms are locally capped; the conventions section is bounded
    # upstream by the session block's own per-section caps
    assert len(first.splitlines()) <= 300


def test_render_twice_is_idempotent(tmp_path):
    _write_profile(tmp_path)
    first = render_static_conventions(tmp_path)
    second = render_static_conventions(tmp_path)
    assert second == first
    on_disk = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert on_disk == first
    assert on_disk.count(_SENTINEL) == 1
    assert on_disk.count("## Codebase conventions") == 1


def test_render_appends_under_existing_claude_md(tmp_path):
    _write_profile(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# my project\n\nRun `npm test` before pushing.\n")
    text = render_static_conventions(tmp_path)
    assert text.startswith("# my project")
    assert "Run `npm test` before pushing." in text
    assert text.index("## Codebase conventions") > text.index("# my project")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == text


# --- fail-loud (eval infrastructure, never a silent empty control) -------------


def test_missing_profile_dir_raises(tmp_path):
    with pytest.raises(StaticConventionsError, match="chameleon"):
        render_static_conventions(tmp_path)


def test_missing_summary_raises(tmp_path):
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "profile.summary.md").unlink()
    with pytest.raises(StaticConventionsError, match="profile.summary.md"):
        render_static_conventions(tmp_path)


def test_empty_summary_raises(tmp_path):
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "profile.summary.md").write_text("  \n")
    with pytest.raises(StaticConventionsError, match="profile.summary.md"):
        render_static_conventions(tmp_path)


def test_corrupt_conventions_raises(tmp_path):
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "conventions.json").write_text("{not json")
    with pytest.raises(StaticConventionsError, match="conventions.json"):
        render_static_conventions(tmp_path)


def test_conventions_wrong_shape_raises(tmp_path):
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "conventions.json").write_text('["a", "b"]')
    with pytest.raises(StaticConventionsError, match="conventions.json"):
        render_static_conventions(tmp_path)


def test_missing_idioms_is_tolerated(tmp_path):
    # bootstrap always scaffolds idioms.md, but a profile without taught idioms
    # is legitimate; the control is still summary + conventions, never empty.
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "idioms.md").unlink()
    text = render_static_conventions(tmp_path)
    assert "Use @/lib/api-client, not axios" in text
    assert "money-is-integer-cents" not in text


def test_missing_principles_is_tolerated(tmp_path):
    # principles.md follows SessionStart's own tolerance (safe_prose_text):
    # the chameleon arm renders no PRINCIPLES section for such a profile, so
    # the control must not either — raising would make it stricter than the
    # treatment, and inventing the section would over-inform it.
    _write_profile(tmp_path)
    (tmp_path / ".chameleon" / "principles.md").unlink()
    text = render_static_conventions(tmp_path)
    assert "Use @/lib/api-client, not axios" in text
    assert "PRINCIPLES:" not in text
    assert "ANTI-HALLUCINATION PROTOCOL:" not in text


# --- wire-up: prepare_cell renders + commits it before the baseline ------------


def _seed_repo_with_profile(tmp_path: Path) -> Path:
    from tests.journey.harness.fixtures import setup_fixture

    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "a.ts").write_text("export const a = 1;\n")
    _write_profile(seed)
    (seed / ".chameleon" / "config.json").write_text("{}\n")
    work_dir, _ = setup_fixture("fix", seed, tmp_path / "working")
    return work_dir


def test_prepare_cell_static_arm_commits_claude_md(tmp_path):
    from tests.effectiveness.worktrees import changed_files, prepare_cell

    repo = _seed_repo_with_profile(tmp_path)
    static = next(s for s in parse_arms("off,static", None) if s.name == "static")
    wt = tmp_path / "wt-static"
    baseline = prepare_cell(fixture_repo=repo, dest=wt, arm=static, setup_fn=None)
    text = (wt / "CLAUDE.md").read_text(encoding="utf-8")
    assert _SENTINEL in text
    # committed as arm setup: the session diff must not show it as task output
    assert changed_files(wt, baseline) == []


def test_prepare_cell_other_arms_write_no_claude_md(tmp_path):
    from tests.effectiveness.worktrees import prepare_cell

    repo = _seed_repo_with_profile(tmp_path)
    off, shadow = parse_arms("off,shadow", None)
    prepare_cell(fixture_repo=repo, dest=tmp_path / "wt-off", arm=off, setup_fn=None)
    prepare_cell(fixture_repo=repo, dest=tmp_path / "wt-shadow", arm=shadow, setup_fn=None)
    assert not (tmp_path / "wt-off" / "CLAUDE.md").exists()
    assert not (tmp_path / "wt-shadow" / "CLAUDE.md").exists()
