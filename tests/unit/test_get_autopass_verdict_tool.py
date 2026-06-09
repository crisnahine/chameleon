class _FakeProc:
    def __init__(self, out: str, code: int = 0):
        self.returncode = code
        self.stdout = out


def _wire(monkeypatch, tmp_path, *, numstat, name_status, active, violations):
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import judge, safe_open, tools

    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda repo: (tmp_path, "rid"))

    def fake_git(args, *, cwd):
        if "rev-parse" in args:
            return _FakeProc("true\n")
        if "--numstat" in args:
            return _FakeProc(numstat)
        if "--name-status" in args:
            return _FakeProc(name_status)
        return _FakeProc("")

    monkeypatch.setattr(judge, "_run_git", fake_git)
    monkeypatch.setattr(ec, "active_block_rules", lambda pd: set(active))
    monkeypatch.setattr(
        tools,
        "get_archetype",
        lambda repo, fp: {"data": {"archetype": "service", "match_quality": "ast"}},
    )
    monkeypatch.setattr(
        tools,
        "query_symbol_importers",
        lambda repo, fp: {"data": {"importers": [{"name": "x", "count": 1}]}},
    )
    monkeypatch.setattr(safe_open, "safe_read_text", lambda root, rel, **k: "code")
    monkeypatch.setattr(
        tools, "lint_file", lambda repo, arch, content, fp: {"data": {"violations": violations}}
    )


def test_get_autopass_verdict_clean_change_is_eligible(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=["phantom-import"],
        violations=[],
    )

    out = tools.get_autopass_verdict("rid")
    v = out["data"]

    assert v["auto_pass_eligible"] is True
    assert v["advisory"] is True
    assert v["changed_files"] == ["src/a.ts"]


def test_get_autopass_verdict_grounded_finding_routes_to_human(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=["phantom-import"],
        violations=[{"rule": "phantom-import"}],
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["auto_pass_eligible"] is False
    assert any("blocking finding" in r for r in v["reasons"])


def test_get_autopass_verdict_unresolved_repo_degrades_to_human(monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda repo: (None, None))

    v = tools.get_autopass_verdict("bad")["data"]

    assert v["auto_pass_eligible"] is False
    assert v["status"] == "degraded"


def test_get_autopass_verdict_non_git_path_degrades_to_human(tmp_path, monkeypatch):
    # A path that resolves but is NOT a git work tree must degrade to "needs
    # human", not read git's empty output as "no changes -> safe to auto-pass".
    from chameleon_mcp import tools

    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda repo: (tmp_path, "rid"))

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["auto_pass_eligible"] is False
    assert v["status"] == "degraded"
