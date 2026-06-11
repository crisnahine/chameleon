"""The unsafe-root (temp / world-writable) guard must hold at every entry point.

find_repo_root already refused tmp-rooted repos, so hooks never loaded a
profile written there — but bootstrap_repo and refresh_repo resolved their
path argument directly and happily wrote/updated a profile the hooks then
refused with no explanation anywhere. detect_repo compounded it by reporting
a bare ``profile_status: "no_repo"`` with no reason. These tests pin:

  - bootstrap_repo refuses an unsafe root with a failed envelope naming the
    CHAMELEON_ALLOW_TMP_REPO=1 opt-out, and writes nothing.
  - refresh_repo refuses the same way (it had the same hole).
  - detect_repo carries a ``reason`` field instead of a bare no_repo.
  - CHAMELEON_ALLOW_TMP_REPO=1 still bypasses the guard consistently at all
    three entry points.

pytest's tmp_path lives under the platform temp dir, so a repo built there is
a genuine guard trigger — no monkeypatching of the guard itself.
"""

from __future__ import annotations

import subprocess

import pytest

from chameleon_mcp import tools

OPT_OUT = "CHAMELEON_ALLOW_TMP_REPO"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Scratch plugin-data dir + cleared caches; the opt-out env is NOT set."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv(OPT_OUT, raising=False)
    from chameleon_mcp.profile import loader as _loader

    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


def _make_ts_repo(root, name="repo"):
    repo = root / name
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"comp{i}.ts").write_text(
            f"export const Comp{i} = () => {{ return {i}; }};\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _assert_refusal_envelope(data):
    assert data["status"] == "failed"
    assert "unsafe_root" in data["error"]
    assert f"{OPT_OUT}=1" in data["error"]


class TestBootstrapRefusesUnsafeRoot:
    def test_bootstrap_refuses_tmp_root_with_reason(self, tmp_path):
        repo = _make_ts_repo(tmp_path)
        data = tools.bootstrap_repo(str(repo))["data"]
        _assert_refusal_envelope(data)

    def test_refused_bootstrap_writes_no_profile(self, tmp_path):
        repo = _make_ts_repo(tmp_path)
        tools.bootstrap_repo(str(repo))
        assert not (repo / ".chameleon").exists(), (
            "a refused bootstrap must not leave a profile the hooks then refuse to load"
        )

    def test_bootstrap_force_does_not_bypass_guard(self, tmp_path):
        repo = _make_ts_repo(tmp_path)
        data = tools.bootstrap_repo(str(repo), force=True)["data"]
        _assert_refusal_envelope(data)


class TestRefreshRefusesUnsafeRoot:
    def test_refresh_refuses_tmp_root_with_reason(self, tmp_path):
        repo = _make_ts_repo(tmp_path)
        data = tools.refresh_repo(str(repo))["data"]
        _assert_refusal_envelope(data)


class TestDetectRepoCarriesReason:
    def test_detect_repo_names_the_refusal(self, tmp_path):
        repo = _make_ts_repo(tmp_path)
        data = tools.detect_repo(str(repo / "src" / "comp0.ts"))["data"]
        assert data["profile_status"] == "no_repo"
        assert data["trust_state"] == "n/a"
        assert "unsafe_root" in data.get("reason", "")
        assert f"{OPT_OUT}=1" in data["reason"]

    def test_plain_no_repo_has_no_reason(self, tmp_path, monkeypatch):
        # A path with no repo markers at all is an ordinary no_repo, not a
        # guard refusal; it must not grow a misleading reason field.
        monkeypatch.setenv(OPT_OUT, "1")
        bare = tmp_path / "bare" / "sub"
        bare.mkdir(parents=True)
        data = tools.detect_repo(str(bare / "loose.ts"))["data"]
        assert data["profile_status"] == "no_repo"
        assert "reason" not in data


class TestOptOutBypassesEverywhere:
    def test_bootstrap_detect_refresh_all_proceed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(OPT_OUT, "1")
        repo = _make_ts_repo(tmp_path)

        boot = tools.bootstrap_repo(str(repo))["data"]
        assert boot["status"] == "success"

        detected = tools.detect_repo(str(repo / "src" / "comp0.ts"))["data"]
        assert detected["repo_root"] == str(repo.resolve())
        assert detected["profile_status"] != "no_repo"

        refreshed = tools.refresh_repo(str(repo))["data"]
        assert refreshed["status"] in ("noop", "success", "partial")
