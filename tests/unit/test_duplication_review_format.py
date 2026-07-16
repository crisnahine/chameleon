"""Tests for the duplication lens's per-finding claim template (_claim_for)."""

from __future__ import annotations

from chameleon_mcp.duplication_review import Finding
from chameleon_mcp.stop.lenses.duplication import _claim_for


def test_claim_names_both_sides():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb")
    claim = _claim_for(f)
    assert "renamed" in claim and "original" in claim and "app/b.rb" in claim


def test_claim_pins_exact_template():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb")
    assert _claim_for(f) == ("renamed (app/a.rb:7) re-implements original (app/b.rb) — reuse it.")
