"""Idiom provenance (C4.2): per-idiom ``Source:`` line.

Auto-derived idioms are written from untrusted repo content; recording the
evidence file(s) + the ref they were derived from makes a poisoned idiom
traceable and lets the trust gate show each idiom's origin before approval.
The line is optional and back-compatible: an idioms.md without it parses with
``source=None``.
"""

from __future__ import annotations

import json


def _setup_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    from chameleon_mcp.conventions import empty_conventions

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "conventions.json").write_text(
        json.dumps(empty_conventions(generation=1)), encoding="utf-8"
    )
    return repo


def _data(res):
    return res.get("data", res) if isinstance(res, dict) else res


def test_parse_captures_source_and_keeps_it_out_of_rationale():
    from chameleon_mcp.idiom_coverage import parse_idiom_blocks

    text = (
        "## active\n\n"
        "### prov-idiom\n"
        "Status: active (added 2026-06-19)\n"
        "Source: src/a.ts @ abc1234\n"
        "use the wrapper, not the raw client\n"
    )
    blocks = parse_idiom_blocks(text)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["source"] == "src/a.ts @ abc1234"
    assert "Source:" not in b.get("rationale", "")
    assert "abc1234" not in b.get("rationale", "")
    assert "use the wrapper" in b.get("rationale", "")


def test_parse_sourceless_idiom_is_back_compatible():
    from chameleon_mcp.idiom_coverage import parse_idiom_blocks

    text = "## active\n\n### old-idiom\nStatus: active (added 2026-01-01)\nuse the wrapper\n"
    blocks = parse_idiom_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["source"] is None


def test_structured_idiom_renders_source_line(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_profile_structured(
            str(repo),
            slug="prov-idiom",
            rationale="use the wrapper",
            source="src/lib/api.ts, src/lib/http.ts @ abc1234",
        )
    )
    assert res["status"] == "success"
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "Source: src/lib/api.ts, src/lib/http.ts @ abc1234" in idioms


def test_structured_idiom_without_source_has_no_source_line(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_profile_structured(str(repo), slug="no-prov", rationale="use the wrapper")
    )
    assert res["status"] == "success"
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "Source:" not in idioms


def test_trust_gate_section_preserves_source(tmp_path, monkeypatch):
    # The trust-gate summary dumps the raw active idioms section, so a Source line
    # reaches the reviewer for inspection before /chameleon-trust.
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    _data(
        tools.teach_profile_structured(
            str(repo),
            slug="prov-idiom",
            rationale="use the wrapper",
            source="src/lib/api.ts @ abc1234",
        )
    )
    idioms_text = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    from chameleon_mcp.profile.summary import extract_idioms_section

    active = extract_idioms_section(idioms_text, "## active")
    assert "Source: src/lib/api.ts @ abc1234" in active


def test_source_newline_injection_is_collapsed(tmp_path, monkeypatch):
    # A newline-bearing source must not forge a second idiom heading: the Source
    # line is single-line metadata, so its whitespace (including newlines) is
    # collapsed before rendering.
    from chameleon_mcp import tools
    from chameleon_mcp.idiom_coverage import parse_idiom_blocks

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_profile_structured(
            str(repo),
            slug="real-idiom",
            rationale="use the wrapper",
            source="src/a.ts\n### injected-fake\nStatus: active (added 2026-01-01)",
        )
    )
    assert res["status"] == "success"
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    # No forged slug heading at the start of any line.
    assert not any(ln.lstrip().startswith("### injected") for ln in idioms.splitlines())
    blocks = parse_idiom_blocks(idioms)
    assert [b["slug"] for b in blocks] == ["real-idiom"]


def test_server_wrapper_forwards_source(tmp_path, monkeypatch):
    # Regression: the MCP surface must forward ``source`` to the tools impl. The
    # flat wrapper previously omitted the param, leaving the documented source=
    # path unreachable over MCP even though the impl rendered it. The operation
    # now routes through the chameleon_lifecycle dispatcher, whose params dict
    # must carry source through intact.
    from chameleon_mcp import server

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        server.chameleon_lifecycle(
            action="teach_profile_structured",
            params={
                "repo": str(repo),
                "slug": "wrapper-prov",
                "rationale": "use the wrapper",
                "source": "src/lib/http.ts @ abc1234",
            },
        )
    )
    assert res["status"] == "success"
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "Source: src/lib/http.ts @ abc1234" in idioms


def test_deprecated_idiom_carries_source(tmp_path, monkeypatch):
    # Provenance is preserved on the deprecated (audit-history) path too.
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_profile_structured(
            str(repo),
            slug="dep-idiom",
            rationale="the old way",
            status="deprecated",
            source="src/legacy.ts @ abc1234",
        )
    )
    assert res["status"] == "success"
    idioms = (repo / ".chameleon" / "idioms.md").read_text(encoding="utf-8")
    assert "Source: src/legacy.ts @ abc1234" in idioms
