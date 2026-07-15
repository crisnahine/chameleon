"""Rendered idioms.md view: byte determinism, format goldens, parser round-trips."""

from __future__ import annotations

import hashlib

from chameleon_mcp.core.idiom_store import (
    IdiomRecord,
    read_view_digest,
    regenerate_views,
    render_idioms_md,
    upsert_idiom,
)


def _profile(tmp_path):
    p = tmp_path / "repo" / ".chameleon"
    p.mkdir(parents=True)
    (p / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    # The mirror render needs a conventions object to hang the sections on.
    (p / "conventions.json").write_text(
        '{"generation": 1, "conventions": {"naming": {"summary": "kebab-case files"}}}'
    )
    return p


def _two_records():
    newer = IdiomRecord(
        slug="use-api-client",
        title="use-api-client",
        rationale="Always use the apiClient helper for HTTP calls.",
        languages=["typescript"],
        archetypes=["service"],
        status="active",
        added_date="2026-07-14",
        examples=["const r = apiClient.get('/x');"],
        rank=1,
    )
    older = IdiomRecord(
        slug="no-raw-sql",
        title="no-raw-sql",
        rationale="Use the query builder; raw SQL strings bypass the sanitizer.",
        languages=[],
        status="deprecated",
        added_date="2026-06-01",
        deprecated_date="2026-07-01",
        rank=2,
    )
    return [newer, older]


GOLDEN = (
    "# idioms\n"
    "\n"
    "## active\n"
    "\n"
    "### use-api-client\n"
    "Language: typescript\n"
    "Status: active (added 2026-07-14)\n"
    "Archetype: service\n"
    "Always use the apiClient helper for HTTP calls.\n"
    "\n"
    "Example:\n"
    "```\n"
    "const r = apiClient.get('/x');\n"
    "```\n"
    "\n"
    "## deprecated\n"
    "\n"
    "### no-raw-sql\n"
    "Status: deprecated 2026-07-01\n"
    "Use the query builder; raw SQL strings bypass the sanitizer.\n"
)


def test_render_matches_golden_and_is_deterministic():
    recs = _two_records()
    text = render_idioms_md(recs)
    assert text == GOLDEN
    assert render_idioms_md(list(recs)) == text


def test_block_parser_and_gist_renderer_round_trip():
    from chameleon_mcp.tools import _parse_idiom_blocks, render_idiom_gists

    text = render_idioms_md(_two_records())
    # Active-only block parser sees exactly the active titles.
    _pre, blocks = _parse_idiom_blocks(text)
    assert [b[0] for b in blocks] == ["use-api-client"]
    assert blocks[0][1] == "service"
    # Mirror gist grammar renders the active title + gist.
    gists = render_idiom_gists(text)
    assert gists.startswith("- use-api-client: Always use the apiClient helper")


def test_fenced_deprecated_heading_cannot_truncate():
    """The v3 live bug: a fenced '## deprecated' line inside an example truncated
    every later idiom on read. The store renders examples inside fences and the
    fence-aware parsers must still see all active blocks."""
    from chameleon_mcp.tools import _parse_idiom_blocks

    tricky = IdiomRecord(
        slug="fence-trap",
        title="fence-trap",
        rationale="Example contains a heading-looking line.",
        examples=["## deprecated\nnot a real section"],
        status="active",
        added_date="2026-07-14",
        rank=1,
    )
    survivor = IdiomRecord(
        slug="survivor",
        title="survivor",
        rationale="Must still be visible after the fenced heading.",
        status="active",
        added_date="2026-07-14",
        rank=2,
    )
    text = render_idioms_md([tricky, survivor])
    _pre, blocks = _parse_idiom_blocks(text)
    assert [b[0] for b in blocks] == ["fence-trap", "survivor"]


def test_heading_lines_in_rationale_are_escaped():
    rec = IdiomRecord(
        slug="heady",
        title="heady",
        rationale="First line.\n## active\nSecond line.",
        status="active",
        added_date="2026-07-14",
        rank=1,
    )
    text = render_idioms_md([rec])
    assert "\n\\## active\n" in text


def test_regenerate_views_writes_file_mirror_and_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    profile = _profile(tmp_path)
    for rec in _two_records():
        upsert_idiom(profile, rec)
    text = regenerate_views(profile)
    assert (profile / "idioms.md").read_text(encoding="utf-8") == text
    assert read_view_digest(profile) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    mirror = (profile / "conventions.md").read_text(encoding="utf-8")
    assert "TEAM IDIOMS" in mirror
    assert "- use-api-client:" in mirror
    assert "no-raw-sql" not in mirror  # deprecated idioms never ride the mirror
