"""get_autopass_verdict must always return a fan_out recommendation (on success
AND degraded paths) so the pr-review skill never has to read env to decide
fan-out. The kill switch CHAMELEON_REVIEW_FANOUT=0 forces recommended=False."""

from __future__ import annotations

from chameleon_mcp import tools


def test_fanout_helper_thresholds(monkeypatch):
    monkeypatch.delenv("CHAMELEON_REVIEW_FANOUT", raising=False)
    assert tools._fan_out_block(3, 50)["recommended"] is False
    assert tools._fan_out_block(20, 50)["recommended"] is True  # files over 8
    assert tools._fan_out_block(3, 500)["recommended"] is True  # lines over 400


def test_fanout_kill_switch(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_FANOUT", "0")
    block = tools._fan_out_block(50, 5000)
    assert block["recommended"] is False
    assert "disabled" in block["reason"]


def test_fanout_block_shape():
    block = tools._fan_out_block(9, 10)
    assert set(block) == {"recommended", "files_changed", "lines_changed", "reason"}
