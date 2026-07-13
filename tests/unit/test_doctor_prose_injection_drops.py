"""doctor must surface a prose-injection-drop warning, not just timestamped hook
errors.

loader.safe_prose_text / load_profile_dir print a plain stderr warning (no
leading ``[timestamp]`` anchor) when idioms.md / principles.md / conventions.md
is dropped from context for tripping the prompt-injection scan. The hook
wrappers redirect a hook's raw stderr straight into ``.hook_errors.log``, so an
unanchored line either has "nothing to attach to" (dropped entirely) or gets
misattributed as a continuation of an unrelated timestamped entry -- either way
the one diagnostic tool meant to surface a live poisoning event stayed silent.
"""

from __future__ import annotations

from chameleon_mcp import tools


def _checks(tmp_path, monkeypatch, log_text: str) -> list[dict]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / ".hook_errors.log").write_text(log_text, encoding="utf-8")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    return tools.doctor().get("data", {}).get("checks", [])


def test_doctor_surfaces_orphaned_injection_drop_with_no_anchor(tmp_path, monkeypatch):
    # A line with no preceding in-window anchor "has nothing to attach to" in
    # the anchor-grouping pass, so recent_hook_errors reports a clean log --
    # exactly the silent-discard bug.
    checks = _checks(
        tmp_path,
        monkeypatch,
        "chameleon: principles.md dropped from context: contains a prompt-injection "
        "pattern (re-derive or re-teach with safe prose)\n",
    )
    rhe = next((c for c in checks if c["name"] == "recent_hook_errors"), None)
    assert rhe is not None and rhe["status"] == "ok"

    pid = next((c for c in checks if c["name"] == "prose_injection_drops"), None)
    assert pid is not None
    assert pid["status"] == "warn"
    assert any("principles.md dropped from context" in ln for ln in pid["detail"])


def test_doctor_surfaces_injection_drop_misattributed_as_continuation(tmp_path, monkeypatch):
    # The line immediately follows a real anchor, so the anchor-grouping pass
    # folds it into that unrelated entry instead of identifying it as its own
    # distinct event -- the dedicated check must still call it out.
    checks = _checks(
        tmp_path,
        monkeypatch,
        "[2026-07-13T10:00:00Z] preflight-and-advise failed (python=/usr/bin/python3)\n"
        "chameleon: idioms.md dropped from context: contains a prompt-injection, "
        "secret, or dangerous pattern (re-run /chameleon-teach with safe prose)\n",
    )
    pid = next((c for c in checks if c["name"] == "prose_injection_drops"), None)
    assert pid is not None
    assert any("idioms.md dropped from context" in ln for ln in pid["detail"])


def test_doctor_omits_injection_drops_check_when_none_logged(tmp_path, monkeypatch):
    checks = _checks(
        tmp_path,
        monkeypatch,
        "[2026-07-13T10:00:00Z] preflight-and-advise failed (python=/usr/bin/python3)\n",
    )
    assert not any(c["name"] == "prose_injection_drops" for c in checks)
