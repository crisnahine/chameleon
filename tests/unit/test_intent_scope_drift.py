"""Intent scope-drift (C3.4 / SP6).

intent_capture stores only checkable tokens, never prompt prose. The identifier
tokens are the scope anchors the request named. At turn end, a changed file whose
path shares no word with any named identifier is a candidate unrequested change.
The check stays quiet unless the request named enough identifiers AND at least one
changed file matched (so the turn is plausibly the captured work), to avoid noise.
"""

from __future__ import annotations

from chameleon_mcp.intent_capture import identifier_tokens, scope_drift_files


def test_flags_unrelated_changed_file():
    intent = ["AuthService", "login"]
    changed = ["src/auth/service.ts", "src/auth/login.ts", "src/utils/logger.ts"]
    drift = scope_drift_files(intent, changed, min_intent_tokens=2)
    assert drift == ["src/utils/logger.ts"]


def test_camel_and_snake_and_path_tokens_match():
    # "AuthService" -> {auth, service}; the snake/path file shares "service".
    intent = ["AuthService"]
    changed = ["src/auth_service_helper.rb", "src/billing/invoice.rb"]
    drift = scope_drift_files(intent, changed, min_intent_tokens=1)
    assert drift == ["src/billing/invoice.rb"]


def test_quiet_below_min_intent_tokens():
    # One weak identifier is not enough scope signal to flag anything.
    intent = ["x"]
    changed = ["src/a.ts", "src/b.ts"]
    assert scope_drift_files(intent, changed, min_intent_tokens=2) == []


def test_quiet_when_no_changed_file_overlaps():
    # Nothing in the turn matched the request -> likely the captured intent was
    # for other work; stay silent rather than flag the whole turn.
    intent = ["AuthService", "login"]
    changed = ["src/billing/invoice.ts", "src/reports/export.ts"]
    assert scope_drift_files(intent, changed, min_intent_tokens=2) == []


def test_quiet_when_all_changed_files_overlap():
    intent = ["AuthService", "login"]
    changed = ["src/auth/service.ts", "src/auth/login.ts"]
    assert scope_drift_files(intent, changed, min_intent_tokens=2) == []


def test_respects_max_flagged_cap():
    intent = ["AuthService", "login"]
    changed = ["src/auth/service.ts"] + [f"src/unrelated/m{i}.ts" for i in range(10)]
    drift = scope_drift_files(intent, changed, min_intent_tokens=2, max_flagged=3)
    assert len(drift) == 3


def test_generic_path_tokens_do_not_create_spurious_overlap():
    # "src", "index", and the extension must not count as scope overlap.
    intent = ["AuthService", "login"]
    changed = ["src/index.ts"]
    assert scope_drift_files(intent, changed, min_intent_tokens=2) == []


def test_identifier_tokens_returns_only_identifiers_bucket():
    entries = [
        {
            "ts": 100,
            "tokens": {
                "numerals": ["200", "3"],
                "identifiers": ["AuthService", "login"],
                "quoted": ["some message"],
            },
        }
    ]
    assert identifier_tokens(entries) == ["AuthService", "login"]


def test_identifier_tokens_dedupes_and_filters_by_since():
    entries = [
        {"ts": 50, "tokens": {"identifiers": ["Old"]}},
        {"ts": 150, "tokens": {"identifiers": ["AuthService", "AuthService", "login"]}},
    ]
    assert identifier_tokens(entries, since_ts=100) == ["AuthService", "login"]


def test_identifier_tokens_skips_suppressed_entries():
    entries = [
        {"ts": 100, "secret_suppressed": True, "tokens": {"identifiers": ["Secret"]}},
        {"ts": 100, "tokens": {"identifiers": ["Public"]}},
    ]
    assert identifier_tokens(entries) == ["Public"]


# --- turn-end advisory wiring ------------------------------------------------


def _drift_setup(tmp_path, monkeypatch, *, prompt: str):
    from types import SimpleNamespace

    from chameleon_mcp import intent_capture

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "auth").mkdir(parents=True)
    (repo_root / "src" / "utils").mkdir(parents=True)
    auth = repo_root / "src" / "auth" / "service.ts"
    auth.write_text("x", encoding="utf-8")
    logger = repo_root / "src" / "utils" / "logger.ts"
    logger.write_text("x", encoding="utf-8")
    repo_data = tmp_path / "data" / "rid"
    repo_data.mkdir(parents=True)
    if prompt:
        intent_capture.capture_intent(repo_data, "s1", prompt)
    state = SimpleNamespace(files={str(auth): None, str(logger): None})
    return repo_root, repo_data, state, SimpleNamespace


def test_advisory_fires_for_unrelated_changed_file(tmp_path, monkeypatch):
    from chameleon_mcp import hook_helper

    repo_root, repo_data, state, NS = _drift_setup(
        tmp_path, monkeypatch, prompt="update authService and validateLogin"
    )
    cfg = NS(mode="shadow", intent_scope_advisory=True)
    lines = hook_helper._scope_drift_advisory_lines(
        repo_root=repo_root, repo_data=repo_data, session_id="s1", state=state, cfg=cfg
    )
    assert lines and "scope drift" in lines[0]
    assert "logger.ts" in lines[0]
    assert "service.ts" not in lines[0]


def test_advisory_off_when_config_disabled(tmp_path, monkeypatch):
    from chameleon_mcp import hook_helper

    repo_root, repo_data, state, NS = _drift_setup(
        tmp_path, monkeypatch, prompt="update authService and validateLogin"
    )
    cfg = NS(mode="shadow", intent_scope_advisory=False)
    assert (
        hook_helper._scope_drift_advisory_lines(
            repo_root=repo_root, repo_data=repo_data, session_id="s1", state=state, cfg=cfg
        )
        == []
    )


def test_advisory_off_when_mode_off(tmp_path, monkeypatch):
    from chameleon_mcp import hook_helper

    repo_root, repo_data, state, NS = _drift_setup(
        tmp_path, monkeypatch, prompt="update authService and validateLogin"
    )
    cfg = NS(mode="off", intent_scope_advisory=True)
    assert (
        hook_helper._scope_drift_advisory_lines(
            repo_root=repo_root, repo_data=repo_data, session_id="s1", state=state, cfg=cfg
        )
        == []
    )


def test_advisory_quiet_when_no_intent_captured(tmp_path, monkeypatch):
    from chameleon_mcp import hook_helper

    repo_root, repo_data, state, NS = _drift_setup(tmp_path, monkeypatch, prompt="")
    cfg = NS(mode="shadow", intent_scope_advisory=True)
    assert (
        hook_helper._scope_drift_advisory_lines(
            repo_root=repo_root, repo_data=repo_data, session_id="s1", state=state, cfg=cfg
        )
        == []
    )


def test_advisory_ignores_non_string_path_entries(tmp_path, monkeypatch):
    # A non-string key in state.files must not abort the whole advisory.
    from chameleon_mcp import hook_helper

    repo_root, repo_data, state, NS = _drift_setup(
        tmp_path, monkeypatch, prompt="update authService and validateLogin"
    )
    state.files[12345] = None
    cfg = NS(mode="shadow", intent_scope_advisory=True)
    lines = hook_helper._scope_drift_advisory_lines(
        repo_root=repo_root, repo_data=repo_data, session_id="s1", state=state, cfg=cfg
    )
    assert lines and "logger.ts" in lines[0]
