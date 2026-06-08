"""Tests for format_duplication_advisory (Task 8)."""

from __future__ import annotations

from chameleon_mcp.duplication_review import Finding, format_duplication_advisory


def test_format_lines():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb")
    lines = format_duplication_advisory([f])
    assert any("renamed" in ln and "original" in ln and "app/b.rb" in ln for ln in lines)
    assert lines[0].startswith("[\U0001f98e chameleon:")


def test_empty():
    assert format_duplication_advisory([]) == []


def test_format_plural():
    findings = [
        Finding("fn1", "a.rb", 1, "x", "orig1", "b.rb"),
        Finding("fn2", "a.rb", 2, "x", "orig2", "c.rb"),
    ]
    lines = format_duplication_advisory(findings)
    assert "2 possible duplicates" in lines[0]
    assert len(lines) == 3  # header + 2 finding lines


def test_format_singular():
    f = Finding("fn1", "a.rb", 1, "x", "orig1", "b.rb")
    lines = format_duplication_advisory([f])
    assert "1 possible duplicate" in lines[0]
    assert "duplicates" not in lines[0]
