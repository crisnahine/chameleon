"""Refresh must re-cluster across an engine-version change, not silently noop.

A profile stamped by an older engine (e.g. 0.5.7) can have unchanged files yet
out-of-date clustering after an upgrade. The refresh noop short-circuit keys on
file mtimes only, so without an engine-version guard a user keeps stale analysis
until a manual force-refresh.
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp import tools as t


def _profile(tmp_path: Path, engine_version: str | None) -> Path:
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    body: dict = {"schema_version": 8, "generation": 1, "archetypes": {}}
    if engine_version is not None:
        body["engine_min_version"] = engine_version
    (pd / "archetypes.json").write_text(json.dumps(body), encoding="utf-8")
    return pd


def test_engine_version_changed_true_on_mismatch(tmp_path):
    pd = _profile(tmp_path, "0.5.7")
    assert t._engine_version_changed(pd, "1.5.0") is True


def test_engine_version_changed_false_on_match(tmp_path):
    pd = _profile(tmp_path, "1.5.0")
    assert t._engine_version_changed(pd, "1.5.0") is False


def test_engine_version_changed_false_when_absent(tmp_path):
    # No engine stamp at all -> can't prove a mismatch, so don't force a rerun.
    pd = _profile(tmp_path, None)
    assert t._engine_version_changed(pd, "1.5.0") is False


def test_engine_version_changed_false_when_no_profile(tmp_path):
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    assert t._engine_version_changed(pd, "1.5.0") is False


def _seed_repo(tmp_path: Path, monkeypatch, engine_version: str) -> Path:
    """A minimal Ruby repo + .chameleon profile + index_db row, noop-eligible
    (files unchanged) so only the engine-version guard can force a rerun."""
    from chameleon_mcp import index_db

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    # index_db caches a module-level connection that ignores the env once opened;
    # drop it so upsert/get_repo bind to this test's isolated data dir.
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    repo = tmp_path / "repo"
    (repo / "app" / "models").mkdir(parents=True)
    # Gemfile alone selects the Ruby extractor; keep exactly one discoverable .rb
    # so files_indexed=1 gives cardinality_match (noop-eligible).
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    (repo / "app" / "models" / "user.rb").write_text(
        "class User < ApplicationRecord\nend\n", encoding="utf-8"
    )
    pd = repo / ".chameleon"
    pd.mkdir()
    stamp = {"schema_version": 8, "engine_min_version": engine_version, "archetypes": {}}
    (pd / "profile.json").write_text(json.dumps(stamp), encoding="utf-8")
    (pd / "archetypes.json").write_text(json.dumps(stamp), encoding="utf-8")
    (pd / "conventions.json").write_text(
        json.dumps({"schema_version": 8, "rules": {}}), encoding="utf-8"
    )
    # all core artifacts present + complete so the repair guard doesn't force a
    # rebuild on the noop path (the generated indexes are part of that set)
    (pd / "canonicals.json").write_text(json.dumps({"schema_version": 8}), encoding="utf-8")
    (pd / "rules.json").write_text(json.dumps({"schema_version": 8}), encoding="utf-8")
    (pd / "calls_index.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    (pd / "function_catalog.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    (pd / "profile.summary.md").write_text("# summary\n", encoding="utf-8")
    (pd / "principles.md").write_text(
        "# principles\n\n## anti-hallucination protocol\n\n- Don't invent symbols.\n",
        encoding="utf-8",
    )

    repo_root = repo.resolve()
    repo_id = t._compute_repo_id(repo_root)
    # files_indexed=1 matches the single discoverable .rb (cardinality_match),
    # last_seen far in the future so nothing_newer -> noop-eligible.
    index_db.upsert_repo(
        repo_id,
        str(repo_root),
        archetype_count=0,
        files_indexed=1,
        bootstrap_ms=1,
        profile_sha256="seed",
        last_seen_at="2096-01-01T00:00:00Z",
    )
    return repo_root


def test_refresh_rebootstraps_on_engine_version_change(tmp_path, monkeypatch):
    """The guard must sit ABOVE the noop short-circuit: even with unchanged
    files (noop-eligible), a stale engine stamp forces a full re-bootstrap."""
    repo_root = _seed_repo(tmp_path, monkeypatch, engine_version="0.5.0")
    called: dict = {}

    def fake_bootstrap(path, *, force=False, paths_glob=None, **kw):
        called["force"] = force
        return {"data": {"status": "rebootstrapped"}}

    monkeypatch.setattr(t, "bootstrap_repo", fake_bootstrap)
    out = t._refresh_repo_locked(repo_root, force=False)
    assert called.get("force") is True
    assert out["data"]["status"] == "rebootstrapped"


def test_drift_status_flags_engine_version_mismatch(tmp_path, monkeypatch):
    """get_drift_status recommends /chameleon-refresh when the profile was built
    by a different engine version, even absent a drift or age signal. This is the
    user-facing half of the version-aware refresh: the refresh re-clusters, but
    nothing prompts the user without this signal."""
    from chameleon_mcp import index_db

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    repo = tmp_path / "repo"
    pd = repo / ".chameleon"
    pd.mkdir(parents=True)
    pd.joinpath("archetypes.json").write_text(
        json.dumps({"schema_version": 8, "engine_min_version": "0.5.0", "archetypes": {}}),
        encoding="utf-8",
    )
    out = t.get_drift_status(str(repo.resolve())).get("data", {})
    assert out.get("engine_version_mismatch") is True
    action = (out.get("recommended_action") or "").lower()
    assert "refresh" in action and "engine" in action


def test_session_drift_banner_fires_on_engine_mismatch(tmp_path, monkeypatch):
    """At SessionStart the drift banner must also fire on an engine-version
    mismatch (stronger than edit-observation drift), so the user is prompted to
    refresh after an upgrade even with no recorded edits."""
    from chameleon_mcp import hook_helper, index_db

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    repo = tmp_path / "repo"
    pd = repo / ".chameleon"
    pd.mkdir(parents=True)
    pd.joinpath("archetypes.json").write_text(
        json.dumps({"schema_version": 8, "engine_min_version": "0.5.0", "archetypes": {}}),
        encoding="utf-8",
    )
    banner = hook_helper._drift_banner_for_repo(repo.resolve(), session_id="s1")
    assert banner is not None
    low = banner.lower()
    assert "refresh" in low and ("engine" in low or "upgrad" in low)


def test_drift_status_no_engine_flag_when_versions_match(tmp_path, monkeypatch):
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    repo = tmp_path / "repo"
    pd = repo / ".chameleon"
    pd.mkdir(parents=True)
    pd.joinpath("archetypes.json").write_text(
        json.dumps(
            {"schema_version": 8, "engine_min_version": ENGINE_MIN_VERSION, "archetypes": {}}
        ),
        encoding="utf-8",
    )
    out = t.get_drift_status(str(repo.resolve())).get("data", {})
    assert out.get("engine_version_mismatch") is False


def test_refresh_noops_when_engine_matches(tmp_path, monkeypatch):
    """Same noop-eligible setup but a current engine stamp must still noop —
    the guard fires only on a genuine mismatch, not on every refresh."""
    from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION

    repo_root = _seed_repo(tmp_path, monkeypatch, engine_version=ENGINE_MIN_VERSION)

    def fake_bootstrap(path, *, force=False, paths_glob=None, **kw):
        raise AssertionError("bootstrap_repo must NOT be called when engine matches")

    monkeypatch.setattr(t, "bootstrap_repo", fake_bootstrap)
    out = t._refresh_repo_locked(repo_root, force=False)
    assert out["data"]["status"] == "noop"


def test_principles_incomplete_detects_missing_protocol(tmp_path):
    """principles.md is generated content; a copy missing the always-on
    anti-hallucination protocol (stale pre-1.4.0 or hand-stripped) must be
    detected so refresh re-derives it instead of noop/partial preserving it."""
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    pd.joinpath("principles.md").write_text("# principles\n\n1. foo\n", encoding="utf-8")
    assert t._principles_incomplete(pd) is True
    pd.joinpath("principles.md").write_text(
        "# principles\n\n## anti-hallucination protocol\n\n- Don't invent symbols.\n",
        encoding="utf-8",
    )
    assert t._principles_incomplete(pd) is False


def test_principles_incomplete_when_absent(tmp_path):
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    assert t._principles_incomplete(pd) is True


def _complete_profile(tmp_path):
    pd = tmp_path / ".chameleon"
    pd.mkdir(exist_ok=True)
    for name in (
        "archetypes.json",
        "canonicals.json",
        "rules.json",
        "conventions.json",
        "calls_index.json",
        "function_catalog.json",
    ):
        pd.joinpath(name).write_text(json.dumps({"schema_version": 8}), encoding="utf-8")
    pd.joinpath("profile.json").write_text(
        json.dumps({"generation": 1, "schema_version": 8}), encoding="utf-8"
    )
    pd.joinpath("profile.summary.md").write_text("# summary\n", encoding="utf-8")
    pd.joinpath("principles.md").write_text(
        "# principles\n\n## anti-hallucination protocol\n\n- x\n", encoding="utf-8"
    )
    return pd


def test_profile_needs_rederive_false_when_complete(tmp_path):
    pd = _complete_profile(tmp_path)
    assert t._profile_needs_rederive(pd) is False


def test_profile_needs_rederive_on_missing_core_artifact(tmp_path):
    for missing in ("archetypes.json", "canonicals.json", "rules.json", "profile.summary.md"):
        pd = _complete_profile(tmp_path)
        pd.joinpath(missing).unlink()
        assert t._profile_needs_rederive(pd) is True, missing


def test_profile_needs_rederive_on_corrupt_json(tmp_path):
    pd = _complete_profile(tmp_path)
    pd.joinpath("archetypes.json").write_text("STALE not json\n", encoding="utf-8")
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_on_stale_principles(tmp_path):
    pd = _complete_profile(tmp_path)
    pd.joinpath("principles.md").write_text("# principles\n1. foo\n", encoding="utf-8")
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_on_unsupported_schema_manifest(tmp_path):
    # A too-new / unsupported schema_version in profile.json is rejected at read
    # time; a plain refresh must REPAIR it (re-derive) rather than noop, or the
    # user has no slash-command recovery path (BUG-A1).
    pd = _complete_profile(tmp_path)
    pd.joinpath("profile.json").write_text(
        json.dumps({"generation": 1, "schema_version": 999}), encoding="utf-8"
    )
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_on_corrupt_manifest(tmp_path):
    pd = _complete_profile(tmp_path)
    pd.joinpath("profile.json").write_text("{ not json", encoding="utf-8")
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_on_missing_manifest(tmp_path):
    pd = _complete_profile(tmp_path)
    pd.joinpath("profile.json").unlink()
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_on_noninteger_schema(tmp_path):
    pd = _complete_profile(tmp_path)
    pd.joinpath("profile.json").write_text(
        json.dumps({"generation": 1, "schema_version": "999"}), encoding="utf-8"
    )
    assert t._profile_needs_rederive(pd) is True


def test_profile_needs_rederive_false_for_supported_older_schema(tmp_path):
    # An OLDER supported schema loads fine and must NOT force a re-derive.
    pd = _complete_profile(tmp_path)
    pd.joinpath("profile.json").write_text(
        json.dumps({"generation": 1, "schema_version": 5}), encoding="utf-8"
    )
    assert t._profile_needs_rederive(pd) is False


def test_maybe_preserve_trust_honors_always(tmp_path, monkeypatch):
    """A non-structurally-identical refresh still preserves trust when the repo
    config sets trust.auto_preserve_when='always'."""
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import grant_trust, trust_state_for

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_data"))
    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    repo = tmp_path / "repo"
    pd = repo / ".chameleon"
    pd.mkdir(parents=True)
    pd.joinpath("config.json").write_text(
        '{"$schema":"chameleon-config-0.6.0","trust":{"auto_preserve_when":"always"}}',
        encoding="utf-8",
    )
    rid = t._compute_repo_id(repo.resolve())
    grant_trust(rid, pd)  # a prior trust record must exist

    pre = {"trust_record_existed": True, "repo_id": rid, "structural_hashes": {}}
    envelope = {"data": {"archetype_diff": {"added": ["new-archetype"]}}}  # NOT identical
    t._maybe_preserve_trust_across_refresh(repo.resolve(), pre, envelope)

    assert envelope["data"].get("trust_preserved") is True
    assert envelope["data"].get("trust_preserve_reason") == "always"
    assert trust_state_for(rid) is not None


# --------------------------------------------------------------------------
# _persisted_paths_glob — the docstring promises any error returns None, so a
# profile.json holding non-UTF8 bytes must not leak UnicodeDecodeError into
# refresh (qa25 P2)


def test_persisted_paths_glob_returns_none_on_non_utf8_profile(tmp_path: Path):
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    (pd / "profile.json").write_bytes(b'{"discovery": {"paths_glob": "\xff\xfe**/*.rb"}}')
    assert t._persisted_paths_glob(pd) is None


def test_persisted_paths_glob_reads_valid_profile(tmp_path: Path):
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    (pd / "profile.json").write_text(
        json.dumps({"discovery": {"paths_glob": "app/**/*.rb"}}), encoding="utf-8"
    )
    assert t._persisted_paths_glob(pd) == "app/**/*.rb"
