"""CI-gated, env-var-free coverage for the MCP tool surface (tools.py + server.py).

These were previously exercised only by tests/qa_*.py, which require
CHAMELEON_TEST_*_REPO env vars and are not run in CI, so tools.py (the ~20
model-callable tools) and server.py had ZERO CI-gated coverage. A regression in
any tool's response envelope, error handling, or logic passed CI green.

This builds a small trusted fixture profile in tmp (no subprocess, no network,
no env dependency) and asserts every read-path tool returns the standard
envelope and survives. It is the safety net the tools.py / extraction refactors
depend on.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.profile.trust import grant_trust

ARCH = "service"
WITNESS = "service.ts"

# Every tool name registered in server.py, asserted present so a dropped/renamed
# registration is caught.
REGISTERED_TOOLS = [
    "detect_repo",
    "get_archetype",
    "get_pattern_context",
    "get_canonical_excerpt",
    "get_rules",
    "lint_file",
    "get_drift_status",
    "refresh_repo",
    "bootstrap_repo",
    "list_profiles",
    "merge_profiles",
    "teach_profile",
    "trust_profile",
    "disable_session",
    "pause_session",
    "propose_archetype_renames",
    "apply_archetype_renames",
    "teach_profile_structured",
    "daemon_status",
    "doctor",
]


@pytest.fixture
def trusted_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "service objects"}}})
    )
    (cham / "rules.json").write_text(
        json.dumps({"generation": 1, "rules": {"no-default-export": {"severity": "warn"}}})
    )
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "deadbeef"}}]},
            }
        )
    )
    (cham / "idioms.md").write_text("Always use the apiClient helper.\n")
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "COMMITTED").touch()
    (repo / WITNESS).write_text("export function makeService() {\n  return 1;\n}\n")
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _assert_envelope(result: dict):
    assert isinstance(result, dict)
    assert result.get("api_version") == "1"
    assert "data" in result and isinstance(result["data"], dict)


def test_server_imports_and_registers_every_tool():
    for name in REGISTERED_TOOLS:
        assert hasattr(server, name), f"server.py no longer defines tool {name!r}"
        assert callable(getattr(server, name))


def test_detect_repo(trusted_repo):
    _assert_envelope(tools.detect_repo(str(trusted_repo / WITNESS)))


def test_get_pattern_context(trusted_repo):
    res = tools.get_pattern_context(str(trusted_repo / WITNESS))
    _assert_envelope(res)
    assert "trust_state" in res["data"]["repo"]


def test_get_archetype(trusted_repo):
    _assert_envelope(tools.get_archetype(str(trusted_repo), str(trusted_repo / WITNESS)))


def test_get_canonical_excerpt(trusted_repo):
    res = tools.get_canonical_excerpt(str(trusted_repo), ARCH)
    _assert_envelope(res)
    assert "makeService" in (res["data"].get("content") or "")


def test_get_rules(trusted_repo):
    _assert_envelope(tools.get_rules(str(trusted_repo)))


def test_lint_file(trusted_repo):
    res = tools.lint_file(str(trusted_repo), ARCH, "export const x = 1;\n", file_path="x.ts")
    _assert_envelope(res)


def _seed_competing_import_profile(cham):
    """Give the service witness a non-empty ast_query (so lint_file's convention
    scan runs) plus a competing-import rule, and return the conventions dict."""
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {
                    ARCH: [
                        {
                            "witness": {"path": WITNESS, "sha_hint": "deadbeef"},
                            "normative_shape": {"ast_query": {"jsx_present": False}},
                        }
                    ]
                },
            }
        )
    )
    conv = {
        "generation": 1,
        "conventions": {
            "imports": {
                ARCH: {"competing": [{"over": "lodash", "preferred": "lodash-es"}]},
            }
        },
    }
    (cham / "conventions.json").write_text(json.dumps(conv))
    return conv


def test_lint_file_agrees_with_prewrite_on_string_embedded_import(trusted_repo):
    """The lint_file MCP tool (PostToolUse / daemon path) and the PreToolUse
    pre-write scan must agree: a competing import that only appears inside a
    string literal is not a real import, so neither path flags it."""
    from chameleon_mcp.prewrite_lint import banned_imports_in_content

    conv = _seed_competing_import_profile(trusted_repo / ".chameleon")
    content = "const code = \"import _ from 'lodash';\";\n"

    pre = banned_imports_in_content(
        content,
        language="typescript",
        archetype=ARCH,
        conventions=conv["conventions"],
    )
    assert pre == []

    res = tools.lint_file(str(trusted_repo), ARCH, content, file_path="x.ts")
    _assert_envelope(res)
    viols = res["data"].get("violations") or []
    assert not any(v.get("rule") == "import-preference-violation" for v in viols)


def test_lint_file_flags_real_competing_import(trusted_repo):
    """Guardrail for the convergence fix: a genuine competing import must still
    surface via the lint_file MCP tool."""
    _seed_competing_import_profile(trusted_repo / ".chameleon")

    res = tools.lint_file(str(trusted_repo), ARCH, "import _ from 'lodash';\n", file_path="x.ts")
    _assert_envelope(res)
    viols = res["data"].get("violations") or []
    assert any(v.get("rule") == "import-preference-violation" for v in viols)


def test_rename_preserves_and_renames_conventions_and_principles(trusted_repo):
    """Regression: apply_archetype_renames used to silently DROP conventions.json
    and principles.md (atomic_profile_commit replaces the whole dir and doesn't
    copy protocol files). It must preserve them and rename the conv keys."""
    from chameleon_mcp.profile import loader as _loader

    cham = trusted_repo / ".chameleon"
    (cham / "conventions.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "conventions": {
                    "naming": {ARCH: {"interface_prefix": {"pattern": "I", "consistency": 0.9}}}
                },
            }
        )
    )
    (cham / "principles.md").write_text("# principles\n\n1. Use the project wrapper.\n")
    _loader._PROFILE_CACHE.clear()

    res = tools.apply_archetype_renames(str(trusted_repo), {ARCH: "renamed-arch"})
    assert res.get("data", res).get("status") == "success"

    assert (cham / "conventions.json").is_file(), "rename dropped conventions.json"
    assert (cham / "principles.md").is_file(), "rename dropped principles.md"

    conv = json.loads((cham / "conventions.json").read_text())["conventions"]
    assert ARCH not in conv.get("naming", {})
    assert "renamed-arch" in conv.get("naming", {})
    assert "Use the project wrapper" in (cham / "principles.md").read_text()


def test_get_drift_status(trusted_repo):
    _assert_envelope(tools.get_drift_status(str(trusted_repo)))


def test_list_profiles(trusted_repo):
    _assert_envelope(tools.list_profiles())


def test_detect_repo_no_repo(tmp_path, monkeypatch):
    """A path with no repo/profile still returns a clean envelope, not a crash."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.detect_repo(str(tmp_path / "loose.ts")))


def test_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.doctor())


def test_daemon_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.daemon_status())


def test_nearest_canonical_entry_resolves_by_subbucket():
    """A dense archetype with witnesses in distinct sub-buckets injects the one
    nearest the edited file, not always entries[0]."""
    entries = [
        {"witness": {"path": "app/services/amazon_s3/create_download_link.rb"}},
        {"witness": {"path": "app/services/hubspot/upsert_contact.rb"}},
    ]
    # a hubspot edit gets the hubspot witness, not the first (s3) one
    chosen = tools._nearest_canonical_entry("app/services/hubspot/sync_lead.rb", entries)
    assert chosen["witness"]["path"] == "app/services/hubspot/upsert_contact.rb"
    # no path overlap -> falls back to entries[0]
    fallback = tools._nearest_canonical_entry("lib/unrelated/thing.rb", entries)
    assert fallback["witness"]["path"] == "app/services/amazon_s3/create_download_link.rb"
    # empty -> {}
    assert tools._nearest_canonical_entry("x.rb", []) == {}


def test_bootstrap_repo_blocked_when_lock_held(tmp_path, monkeypatch):
    """A second bootstrap of the same repo while the .bootstrap.lock is held
    returns a clean 'in progress' envelope instead of racing the clusterer."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.locks import acquire_advisory_lock
    from chameleon_mcp.profile.trust import repo_data_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    lock_dir = repo_data_dir(tools._compute_repo_id(repo.resolve()))
    lock_dir.mkdir(parents=True, exist_ok=True)
    with acquire_advisory_lock(lock_dir / ".bootstrap.lock"):
        res = tools.bootstrap_repo(str(repo))["data"]
    assert res.get("status") == "failed"
    assert "in progress" in (res.get("error") or "")
