class _FakeProc:
    def __init__(self, out: str, code: int = 0):
        self.returncode = code
        self.stdout = out


def _wire(monkeypatch, tmp_path, *, numstat, name_status, active, violations, diff=""):
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
        if "--unified=0" in args:
            return _FakeProc(diff)
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
        lambda repo, fp: {"data": {"found": True, "importers": [{"name": "x", "count": 1}]}},
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


def test_typecheck_opt_in_unset_records_unavailable_and_stays_eligible(tmp_path, monkeypatch):
    from chameleon_mcp import tools, typecheck

    monkeypatch.delenv(typecheck.ALLOW_ENV, raising=False)
    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["typecheck"]["status"] == "unavailable"
    # Unavailable is a recorded fact, never a routing reason.
    assert v["auto_pass_eligible"] is True


def test_typecheck_errors_on_changed_file_route_to_human(tmp_path, monkeypatch):
    from chameleon_mcp import tools, typecheck

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(typecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(
        typecheck,
        "run_tsc",
        lambda root: {"status": "errors", "files": ["src/a.ts"], "diagnostics": 1},
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["typecheck"]["status"] == "errors"
    assert v["auto_pass_eligible"] is False
    assert v["risk"] == "high"
    assert any("type error" in r for r in v["reasons"])


def test_typecheck_clean_adds_no_reason(tmp_path, monkeypatch):
    from chameleon_mcp import tools, typecheck

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(typecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(typecheck, "run_tsc", lambda root: {"status": "clean", "files": []})

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["typecheck"]["status"] == "clean"
    assert not any("type error" in r for r in v["reasons"])
    assert v["auto_pass_eligible"] is True


def test_typecheck_raising_falls_back_to_unavailable(tmp_path, monkeypatch):
    from chameleon_mcp import tools, typecheck

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(typecheck, "is_enabled", lambda: True)

    def boom(root):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(typecheck, "run_tsc", boom)

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["typecheck"]["status"] == "unavailable"
    assert v["auto_pass_eligible"] is True


def test_unreadable_fanout_on_covered_file_reads_unknown(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(
        tools,
        "query_symbol_importers",
        lambda repo, fp: {"data": {"found": False, "importers": []}},
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["auto_pass_eligible"] is False
    assert any("unknown" in r for r in v["reasons"])


def test_uncovered_extension_contributes_zero_not_unknown(tmp_path, monkeypatch):
    # The blast-radius gate covers JS/TS + Python (reverse index) and Ruby
    # (constant index); a file in an unsupported language is uncovered by design
    # and must not read as unknown blast radius.
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tcmd/server/main.go\n",
        name_status="M\tcmd/server/main.go\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(
        tools,
        "query_symbol_importers",
        lambda repo, fp: {"data": {"found": False, "importers": []}},
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert not any("unknown" in r for r in v["reasons"])
    assert v["auto_pass_eligible"] is True


def test_ruby_file_blast_radius_is_counted(tmp_path, monkeypatch):
    # A .rb file is covered via the constant index; its blast radius routes
    # through query_symbol_importers and must count, not read 0 like an
    # unsupported extension (the cross-matrix autopass/Ruby consistency fix).
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tapp/services/payment_processor.rb\n",
        name_status="M\tapp/services/payment_processor.rb\n",
        active=[],
        violations=[],
    )
    monkeypatch.setattr(
        tools,
        "query_symbol_importers",
        lambda repo, fp: {"data": {"found": True, "importers": [{"count": 13}]}},
    )

    v = tools.get_autopass_verdict("rid")["data"]
    assert v["facts"]["blast_radius"] == 13


def test_importers_query_raising_reads_unknown_not_zero(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="20\t5\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
    )

    def boom(repo, fp):
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(tools, "query_symbol_importers", boom)

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["auto_pass_eligible"] is False
    assert any("unknown" in r for r in v["reasons"])


def test_removed_guard_in_diff_routes_through_tool(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    diff = (
        "diff --git a/src/a.ts b/src/a.ts\n"
        "--- a/src/a.ts\n"
        "+++ b/src/a.ts\n"
        "@@ -1,1 +1,1 @@\n"
        "-app.use(csrfProtection)\n"
        "+app.use(logger)\n"
    )
    _wire(
        monkeypatch,
        tmp_path,
        numstat="1\t1\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
        diff=diff,
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["facts"]["removed_guard_lines"] == 1
    assert v["auto_pass_eligible"] is False
    assert any("guard" in r for r in v["reasons"])


def test_oversized_diff_truncates_the_content_scan(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_AUTOPASS_MAX_DIFF_BYTES", "64")
    _wire(
        monkeypatch,
        tmp_path,
        numstat="1\t1\tsrc/a.ts\n",
        name_status="M\tsrc/a.ts\n",
        active=[],
        violations=[],
        diff="x" * 200,
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v["facts"]["diff_scan_truncated"] is True


def test_failed_git_diff_degrades_to_human(tmp_path, monkeypatch):
    # An unresolvable base_ref makes git exit nonzero with empty output. That
    # must read "change set unknown -> needs human", never "empty diff -> safe".
    from chameleon_mcp import judge, tools

    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda repo: (tmp_path, "rid"))

    def fake_git(args, *, cwd):
        if "rev-parse" in args:
            return _FakeProc("true\n")
        return _FakeProc("", code=128)

    monkeypatch.setattr(judge, "_run_git", fake_git)

    v = tools.get_autopass_verdict("rid", base_ref="bogus-ref")["data"]

    assert v["status"] == "degraded"
    assert v["reason"] == "git_diff_failed"
    assert v["auto_pass_eligible"] is False
    assert v["risk"] == "high"


def test_empty_diff_on_successful_git_is_not_degraded(tmp_path, monkeypatch):
    # A genuinely empty diff (git succeeded, no changes) keeps its meaning.
    from chameleon_mcp import tools

    _wire(
        monkeypatch,
        tmp_path,
        numstat="",
        name_status="",
        active=[],
        violations=[],
    )

    v = tools.get_autopass_verdict("rid")["data"]

    assert v.get("status") != "degraded"
    assert v["changed_files"] == []


def test_empty_base_ref_degrades_instead_of_auto_passing(tmp_path, monkeypatch):
    # base_ref="" makes the range spec "...HEAD", which git accepts with empty
    # output; without the input guard that reads as "no changes, eligible".
    from chameleon_mcp import judge, tools

    monkeypatch.setattr(tools, "_resolve_repo_arg", lambda repo: (tmp_path, "rid"))
    monkeypatch.setattr(judge, "_run_git", lambda args, *, cwd: _FakeProc("true\n"))

    for bad in ("", "   ", None):
        v = tools.get_autopass_verdict("rid", base_ref=bad)["data"]
        assert v["status"] == "degraded"
        assert v["reason"] == "invalid_base_ref"
        assert v["auto_pass_eligible"] is False
