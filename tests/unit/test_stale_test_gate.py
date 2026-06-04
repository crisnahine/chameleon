"""Tests for the Stop-time stale-test advisory wiring in hook_helper.

These drive ``_stale_test_advisory_lines`` directly with a minimal config, a fake
preloaded profile, and a real EnforcementState pointing at files on disk, so the
gate's config/opt-out/sanitization behavior is covered without standing up the
full stop_backstop handler.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chameleon_mcp.enforcement import EnforcementState, FileState
from chameleon_mcp.hook_helper import _stale_test_advisory_lines


def _cfg(mode="shadow", stale_test_advisory=True):
    return SimpleNamespace(mode=mode, stale_test_advisory=stale_test_advisory)


def _profile(conventions: dict):
    return SimpleNamespace(conventions={"conventions": conventions})


def _state_for(paths: list[Path]) -> EnforcementState:
    st = EnforcementState()
    for p in paths:
        st.files[str(p)] = FileState()
    return st


def _touch(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _make_resolver(mapping: dict[str, str]):
    """Patch the in-hook resolver to a fixed name->archetype map by basename rel."""

    def factory(repo_root, daemon_state):
        def resolve(abs_path):
            return mapping.get(Path(abs_path).name)

        return resolve

    return factory


def test_off_mode_emits_nothing(tmp_path):
    src = _touch(tmp_path, "src/user.ts", "export function getUser() {}\n")
    _touch(tmp_path, "src/user.test.ts", "x\n")
    lines = _stale_test_advisory_lines(
        repo_root=tmp_path,
        state=_state_for([src]),
        cfg=_cfg(mode="off"),
        preloaded=_profile({"test_pairing": {"service": {"frequency": 0.9}}}),
        daemon_state={"available": False},
    )
    assert lines == []


def test_disabled_flag_emits_nothing(tmp_path):
    src = _touch(tmp_path, "src/user.ts", "export function getUser() {}\n")
    _touch(tmp_path, "src/user.test.ts", "x\n")
    lines = _stale_test_advisory_lines(
        repo_root=tmp_path,
        state=_state_for([src]),
        cfg=_cfg(stale_test_advisory=False),
        preloaded=_profile({"test_pairing": {"service": {"frequency": 0.9}}}),
        daemon_state={"available": False},
    )
    assert lines == []


def test_no_test_pairing_data_emits_nothing(tmp_path):
    src = _touch(tmp_path, "src/user.ts", "export function getUser() {}\n")
    lines = _stale_test_advisory_lines(
        repo_root=tmp_path,
        state=_state_for([src]),
        cfg=_cfg(),
        preloaded=_profile({"test_pairing": {}}),
        daemon_state={"available": False},
    )
    assert lines == []


def test_flags_unsynced_test_with_exports(tmp_path):
    src = _touch(tmp_path, "src/user.ts", "export function getUser() {}\n")
    _touch(tmp_path, "src/user.test.ts", "x\n")
    with patch(
        "chameleon_mcp.hook_helper._archetype_resolver",
        _make_resolver({"user.ts": "service"}),
    ):
        lines = _stale_test_advisory_lines(
            repo_root=tmp_path,
            state=_state_for([src]),
            cfg=_cfg(),
            preloaded=_profile(
                {
                    "test_pairing": {"service": {"frequency": 0.9}},
                    "key_exports": {"service": ["getUser"]},
                }
            ),
            daemon_state={"available": False},
        )
    body = "\n".join(lines)
    assert "user.test.ts" in body
    assert "getUser" in body
    assert "advisory" in body.lower()
    # The advisory names its opt-out directive so the model can suppress it.
    assert "chameleon-ignore tests" in body


def test_inline_ignore_directive_opts_out(tmp_path):
    src = _touch(
        tmp_path, "src/user.ts", "// chameleon-ignore tests\nexport function getUser() {}\n"
    )
    _touch(tmp_path, "src/user.test.ts", "x\n")
    with patch(
        "chameleon_mcp.hook_helper._archetype_resolver",
        _make_resolver({"user.ts": "service"}),
    ):
        lines = _stale_test_advisory_lines(
            repo_root=tmp_path,
            state=_state_for([src]),
            cfg=_cfg(),
            preloaded=_profile(
                {
                    "test_pairing": {"service": {"frequency": 0.9}},
                    "key_exports": {"service": ["getUser"]},
                }
            ),
            daemon_state={"available": False},
        )
    assert lines == []


def test_truncates_to_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_STALE_TEST_ADVISORY_MAX_FILES", "2")
    srcs = []
    for i in range(4):
        s = _touch(tmp_path, f"src/m{i}.ts", "export function go() {}\n")
        _touch(tmp_path, f"src/m{i}.test.ts", "x\n")
        srcs.append(s)
    with patch(
        "chameleon_mcp.hook_helper._archetype_resolver",
        _make_resolver({f"m{i}.ts": "service" for i in range(4)}),
    ):
        lines = _stale_test_advisory_lines(
            repo_root=tmp_path,
            state=_state_for(srcs),
            cfg=_cfg(),
            preloaded=_profile({"test_pairing": {"service": {"frequency": 0.9}}}),
            daemon_state={"available": False},
        )
    body = "\n".join(lines)
    assert "and 2 more" in body


def test_missing_preloaded_fails_open(tmp_path):
    src = _touch(tmp_path, "src/user.ts", "export function getUser() {}\n")
    lines = _stale_test_advisory_lines(
        repo_root=tmp_path,
        state=_state_for([src]),
        cfg=_cfg(),
        preloaded=None,
        daemon_state={"available": False},
    )
    assert lines == []
