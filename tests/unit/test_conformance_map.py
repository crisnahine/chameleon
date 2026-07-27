"""Calibration already knows which committed files deviate; keep the answer.

``calibrate_block_rules`` samples up to CALIBRATION_MAX_FILES real committed
files, lints each against its archetype's recalibrated baseline, and builds
``flagged[rule]`` as a set of relative paths. Only ``len(flagged[rule])`` ever
reaches enforcement.json, so every refresh computes a per-file, per-rule,
per-archetype conformance picture and throws it away.

That picture answers the migration question the repo's own A/B found chameleon
strongest on -- majority-lags-convention -- which nothing else can answer today:
``get_shadow_report`` counts would-blocks on edits actually MADE, not a standing
scan of the committed tree.

Capturing it costs no new scan and no new latency: it is a write of data already
in memory when the pass ends.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.enforcement_calibration import calibrate_block_rules


class _Loaded:
    """Minimal LoadedProfile stand-in for the sampler and baselines."""

    def __init__(self, archetypes=None, canonicals=None, conventions=None):
        self.archetypes = archetypes or {}
        self.canonicals = canonicals or {}
        self.conventions = conventions or {}
        self.profile = {}
        self.rules = {}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    (tmp_path / ".chameleon").mkdir()
    return tmp_path


def test_conformance_out_is_optional_and_return_shape_is_unchanged(repo):
    # Existing callers pass nothing and must be unaffected.
    verdicts = calibrate_block_rules(repo, _Loaded())
    assert isinstance(verdicts, dict)


def test_conformance_out_is_populated_when_requested(repo):
    out: dict = {}
    calibrate_block_rules(repo, _Loaded(), conformance_out=out)
    # Even an empty repo must produce the container, so a consumer can tell
    # "scanned, nothing flagged" from "never scanned".
    assert isinstance(out, dict)
    assert "rows" in out
    assert "sampled" in out
    assert isinstance(out["rows"], list)


def test_rows_carry_archetype_path_and_rule(repo, monkeypatch):
    import chameleon_mcp.enforcement_calibration as ec

    monkeypatch.setattr(ec, "_sample_files", lambda *a, **k: [("app/a.rb", "service")])
    monkeypatch.setattr(ec, "_archetype_baselines", lambda *a, **k: {})
    monkeypatch.setattr(
        ec,
        "_violations_for_file",
        lambda *a, **k: [{"rule": "naming-convention-violation", "severity": "error"}],
    )
    out: dict = {}
    calibrate_block_rules(repo, _Loaded(), conformance_out=out)
    assert out["rows"] == [
        {"archetype": "service", "path": "app/a.rb", "rule": "naming-convention-violation"}
    ]
    assert out["sampled"] == 1


def test_rows_are_deterministic(repo, monkeypatch):
    import chameleon_mcp.enforcement_calibration as ec

    monkeypatch.setattr(
        ec,
        "_sample_files",
        lambda *a, **k: [("app/b.rb", "service"), ("app/a.rb", "service")],
    )
    monkeypatch.setattr(ec, "_archetype_baselines", lambda *a, **k: {})
    monkeypatch.setattr(
        ec,
        "_violations_for_file",
        lambda *a, **k: [{"rule": "naming-convention-violation", "severity": "error"}],
    )
    first: dict = {}
    second: dict = {}
    calibrate_block_rules(repo, _Loaded(), conformance_out=first)
    calibrate_block_rules(repo, _Loaded(), conformance_out=second)
    assert first["rows"] == second["rows"]
    # Sorted, so a set's iteration order cannot churn the artifact between runs.
    assert [r["path"] for r in first["rows"]] == ["app/a.rb", "app/b.rb"]


def test_a_non_block_eligible_rule_is_not_recorded(repo, monkeypatch):
    # The pass only tracks BLOCK_ELIGIBLE_RULES; the map must not claim more
    # coverage than the scan actually has.
    import chameleon_mcp.enforcement_calibration as ec

    monkeypatch.setattr(ec, "_sample_files", lambda *a, **k: [("app/a.rb", "service")])
    monkeypatch.setattr(ec, "_archetype_baselines", lambda *a, **k: {})
    monkeypatch.setattr(ec, "_violations_for_file", lambda *a, **k: [{"rule": "not-a-block-rule"}])
    out: dict = {}
    calibrate_block_rules(repo, _Loaded(), conformance_out=out)
    assert out["rows"] == []


class TestReadSurface:
    """get_conformance_map is a MODEL surface, so rows cross the sanitize
    boundary and the response is bounded like every sibling browsing read."""

    def _write(self, monkeypatch, tmp_path, rows):
        import json

        from chameleon_mcp import tools

        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setattr(tools, "_resolve_repo_arg", lambda _r: (None, "rid"))
        d = tmp_path / "data" / "rid"
        d.mkdir(parents=True, exist_ok=True)
        (d / "conformance_map.json").write_text(
            json.dumps({"rows": rows, "sampled": len(rows), "truncated": False}),
            encoding="utf-8",
        )
        return tools

    def test_rows_are_returned_and_counted(self, tmp_path, monkeypatch):
        tools = self._write(
            monkeypatch, tmp_path, [{"archetype": "svc", "path": "app/a.rb", "rule": "naming"}]
        )
        data = tools.get_conformance_map("/repo")["data"]
        assert data["count"] == 1
        assert data["rows"][0]["path"] == "app/a.rb"
        assert data["scanned"] is True

    def test_the_response_is_capped_and_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_CONFORMANCE_MAP_MAX_ROWS", "2")
        rows = [{"archetype": "svc", "path": f"app/{i}.rb", "rule": "naming"} for i in range(9)]
        tools = self._write(monkeypatch, tmp_path, rows)
        data = tools.get_conformance_map("/repo")["data"]
        assert data["count"] == 2
        assert data["total"] == 9
        assert data["rows_truncated"] is True
