"""Unit tests for the deterministic caller-contract signature diff (SP4).

The core compares a callable's OLD vs NEW positional-parameter contract and
flags only a NARROWING (more required positional args), which is what breaks an
existing positional caller. Param dicts mirror the dump shape:
``{"name": str, "optional": bool, "kind": str}`` with kind in
{positional, destructured, rest} (TS) / {positional, optional, rest, keyword,
keyword_rest} (Ruby).

The FP guards are the point: a Ruby required-keyword addition, a new optional
positional, and a rest param must NOT be flagged.
"""

from __future__ import annotations

from chameleon_mcp.signature_diff import diff_file_contracts


def _p(name, kind="positional", optional=False):
    return {"name": name, "kind": kind, "optional": optional}


def test_added_required_positional_is_a_break():
    old = {"foo": [_p("a")]}
    new = {"foo": [_p("a"), _p("b")]}
    breaks = diff_file_contracts(old, new)
    assert len(breaks) == 1
    assert breaks[0].name == "foo"
    assert breaks[0].old_required_positional == 1
    assert breaks[0].new_required_positional == 2


def test_added_optional_positional_is_not_a_break():
    old = {"foo": [_p("a")]}
    new = {"foo": [_p("a"), _p("b", optional=True)]}
    assert diff_file_contracts(old, new) == []


def test_optional_flipped_to_required_is_a_break():
    old = {"foo": [_p("a"), _p("b", optional=True)]}
    new = {"foo": [_p("a"), _p("b", optional=False)]}
    breaks = diff_file_contracts(old, new)
    assert len(breaks) == 1
    assert breaks[0].new_required_positional == 2


def test_ruby_required_keyword_added_is_not_a_break():
    # The mandatory FP guard: a required KEYWORD arg is not positional, so it
    # must not increment the positional-required count.
    old = {"m": [_p("a")]}
    new = {"m": [_p("a"), _p("k", kind="keyword", optional=False)]}
    assert diff_file_contracts(old, new) == []


def test_ruby_optional_positional_kind_is_positional_but_not_required():
    # Ruby emits kind "optional" for a defaulted positional; it is a positional
    # SLOT but never required.
    old = {"m": [_p("a")]}
    new = {"m": [_p("a"), _p("b", kind="optional", optional=True)]}
    assert diff_file_contracts(old, new) == []


def test_ts_destructured_required_arg_is_a_break():
    old = {"f": [_p("a")]}
    new = {"f": [_p("a"), _p("{}", kind="destructured", optional=False)]}
    breaks = diff_file_contracts(old, new)
    assert len(breaks) == 1


def test_added_rest_param_is_not_a_break():
    old = {"f": [_p("a")]}
    new = {"f": [_p("a"), _p("rest", kind="rest", optional=True)]}
    assert diff_file_contracts(old, new) == []


def test_name_only_in_new_is_not_a_break():
    # New code is not a contract break (no prior callers can rely on it).
    old = {}
    new = {"f": [_p("a"), _p("b")]}
    assert diff_file_contracts(old, new) == []


def test_name_only_in_old_is_not_a_break():
    old = {"f": [_p("a")]}
    new = {}
    assert diff_file_contracts(old, new) == []


def test_removed_required_positional_is_not_flagged():
    # Locked scope: only NARROWING (required increase) is flagged; a removed
    # required positional (the widening direction) stays the LLM judge's job.
    old = {"f": [_p("a"), _p("b")]}
    new = {"f": [_p("a")]}
    assert diff_file_contracts(old, new) == []


def test_unchanged_contract_is_not_flagged():
    old = {"f": [_p("a"), _p("b", optional=True)]}
    new = {"f": [_p("a"), _p("b", optional=True)]}
    assert diff_file_contracts(old, new) == []


# ---------------------------------------------------------------------------
# compute_contract_breaks — orchestration with injected parse/git/callers
# ---------------------------------------------------------------------------

from chameleon_mcp.signature_diff import ContractFinding, compute_contract_breaks  # noqa: E402


def test_break_with_committed_callers_is_reported():
    old = {"src/a.ts": {"foo": [_p("a")]}}
    new = {"src/a.ts": {"foo": [_p("a"), _p("b")]}}
    callers = {("src/a.ts", "foo"): {"callers": [{"path": "src/b.ts", "line": 5}], "total": 1}}
    out = compute_contract_breaks(
        ["src/a.ts"],
        old_params_fn=lambda rel: old.get(rel, {}),
        new_params_fn=lambda rel: new.get(rel, {}),
        callers_fn=lambda rel, name: callers.get((rel, name)),
    )
    assert len(out) == 1
    assert isinstance(out[0], ContractFinding)
    assert out[0].name == "foo"
    assert out[0].rel == "src/a.ts"
    assert out[0].caller_total == 1
    assert out[0].new_required_positional == 2


def test_break_with_no_committed_callers_is_suppressed():
    # The decisive FP guard: a narrowing with no committed caller breaks nothing.
    old = {"src/a.ts": {"foo": [_p("a")]}}
    new = {"src/a.ts": {"foo": [_p("a"), _p("b")]}}
    out = compute_contract_breaks(
        ["src/a.ts"],
        old_params_fn=lambda rel: old.get(rel, {}),
        new_params_fn=lambda rel: new.get(rel, {}),
        callers_fn=lambda rel, name: None,
    )
    assert out == []


def test_callers_with_zero_total_is_suppressed():
    old = {"src/a.ts": {"foo": [_p("a")]}}
    new = {"src/a.ts": {"foo": [_p("a"), _p("b")]}}
    out = compute_contract_breaks(
        ["src/a.ts"],
        old_params_fn=lambda rel: old.get(rel, {}),
        new_params_fn=lambda rel: new.get(rel, {}),
        callers_fn=lambda rel, name: {"callers": [], "total": 0},
    )
    assert out == []


def test_parse_failure_yields_no_findings_no_crash():
    def boom(rel):
        raise RuntimeError("parse blew up")

    out = compute_contract_breaks(
        ["src/a.ts"],
        old_params_fn=boom,
        new_params_fn=boom,
        callers_fn=lambda rel, name: {"callers": [{"path": "x"}], "total": 1},
    )
    assert out == []


def test_multiple_files_each_diffed_independently():
    old = {"a.ts": {"f": [_p("x")]}, "b.ts": {"g": [_p("y")]}}
    new = {"a.ts": {"f": [_p("x"), _p("z")]}, "b.ts": {"g": [_p("y")]}}  # only a.ts narrows
    callers = {
        ("a.ts", "f"): {"callers": [{"path": "c.ts", "line": 1}], "total": 1},
        ("b.ts", "g"): {"callers": [{"path": "c.ts", "line": 2}], "total": 1},
    }
    out = compute_contract_breaks(
        ["a.ts", "b.ts"],
        old_params_fn=lambda rel: old.get(rel, {}),
        new_params_fn=lambda rel: new.get(rel, {}),
        callers_fn=lambda rel, name: callers.get((rel, name)),
    )
    assert [f.rel for f in out] == ["a.ts"]


# ---------------------------------------------------------------------------
# Integration: real extractor parse + git materialization (needs node)
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from chameleon_mcp.judge import _run_git  # noqa: E402
from chameleon_mcp.signature_diff import (  # noqa: E402
    callables_at_ref,
    contract_breaks,
    format_contract_advisory,
    parse_callables,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


pytestmark_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@pytestmark_node
def test_parse_callables_real_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    f = tmp_path / "m.ts"
    f.write_text("export function foo(a: number, b: string) {\n  return a + b\n}\n")
    callables = parse_callables(tmp_path, f)
    assert "foo" in callables
    assert len(callables["foo"]) == 2


@pytestmark_node
def test_contract_breaks_end_to_end_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    src = repo / "a.ts"
    src.write_text("export function foo(a: number) {\n  return a\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    # Narrow the contract in the working tree: add a required positional.
    src.write_text("export function foo(a: number, b: number) {\n  return a + b\n}\n")

    callers = {("a.ts", "foo"): {"callers": [{"path": "b.ts", "line": 3}], "total": 1}}
    findings = contract_breaks(
        repo,
        ["a.ts"],
        old_ref="HEAD",
        new_ref=None,  # working tree
        callers_fn=lambda rel, name: callers.get((rel, name)),
        run_git=_run_git,
    )
    assert len(findings) == 1
    assert findings[0].name == "foo"
    assert findings[0].old_required_positional == 1
    assert findings[0].new_required_positional == 2
    lines = format_contract_advisory(findings)
    assert any("foo()" in ln and "b.ts:3" in ln for ln in lines)


@pytestmark_node
def test_callables_at_missing_ref_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "x.ts").write_text("export const y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # A file absent at HEAD yields an empty contract (new code is not a break).
    assert callables_at_ref(repo, "nope.ts", "HEAD", _run_git) == {}


# ---------------------------------------------------------------------------
# autopass wiring: caller_contract_breaks routes a human
# ---------------------------------------------------------------------------

from chameleon_mcp.autopass import build_autopass_verdict, classify_change  # noqa: E402


def test_classify_change_contract_break_routes_high_risk():
    verdict = classify_change({"caller_contract_breaks": 1})
    assert verdict["auto_pass_eligible"] is False
    assert verdict["risk"] == "high"
    assert any("contract" in r.lower() for r in verdict["reasons"])


def test_classify_change_no_contract_break_is_quiet():
    verdict = classify_change({"caller_contract_breaks": 0})
    assert not any("contract" in r.lower() for r in verdict["reasons"])


def test_build_autopass_verdict_threads_contract_break_count():
    # Minimal real adapters; a single modified file, no other routing signal.
    numstat = "1\t0\tsrc/a.ts\n"
    name_status = "M\tsrc/a.ts\n"
    verdict = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda rel: False,
        importers_of=lambda rel: 0,
        block_findings_for=lambda rel: 0,
        caller_contract_breaks=2,
    )
    assert verdict["facts"]["caller_contract_breaks"] == 2
    assert verdict["auto_pass_eligible"] is False
    assert any("contract" in r.lower() for r in verdict["reasons"])


# ---------------------------------------------------------------------------
# get_autopass_verdict end-to-end with contract break (real git + faked index)
# ---------------------------------------------------------------------------


class _FakeIndex:
    def __init__(self, mapping):
        self._m = mapping

    def callers_of(self, rel, name):
        return self._m.get((rel, name))


@pytestmark_node
def test_get_autopass_verdict_routes_contract_break(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import calls_index as _ci
    from chameleon_mcp import tools

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function foo(a: number) {\n  return a\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Narrow the contract on a feature branch (a committed range vs base).
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.ts").write_text("export function foo(a: number, b: number) {\n  return a + b\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "narrow foo")

    fake = _FakeIndex({("a.ts", "foo"): {"callers": [{"path": "b.ts", "line": 4}], "total": 1}})
    monkeypatch.setattr(_ci, "load_calls_index", lambda root: fake)

    # The calls-index-derived contract-break signal is trust-gated (it must not
    # leak committed index contents from an untrusted profile), so grant trust.
    # Isolate the trust write to tmp so the suite never touches the real
    # ~/.local/share/chameleon (the documented test-isolation guarantee).
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    (repo / ".chameleon").mkdir(exist_ok=True)
    from chameleon_mcp.profile.trust import grant_trust as _grant
    from chameleon_mcp.tools import _compute_repo_id as _crid

    _grant(_crid(repo), repo / ".chameleon")
    result = tools.get_autopass_verdict(str(repo), base_ref="main")
    data = result["data"]
    assert data["facts"]["caller_contract_breaks"] == 1
    assert data["auto_pass_eligible"] is False
    assert any("contract" in r.lower() for r in data["reasons"])
    assert data["contract_breaks"][0]["name"] == "foo"


@pytestmark_node
def test_get_autopass_verdict_no_calls_index_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import calls_index as _ci
    from chameleon_mcp import tools

    repo = tmp_path / "repo_noidx"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function foo(a: number) {\n  return a\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.ts").write_text("export function foo(a: number, b: number) {\n  return a + b\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "narrow foo")

    monkeypatch.setattr(_ci, "load_calls_index", lambda root: None)
    result = tools.get_autopass_verdict(str(repo), base_ref="main")
    data = result["data"]
    # No index -> no caller facts -> no contract break (fail-open).
    assert data["facts"]["caller_contract_breaks"] == 0
    assert data["contract_breaks"] == []


# ---------------------------------------------------------------------------
# get_contract_breaks MCP tool (standalone, for pr-review)
# ---------------------------------------------------------------------------


@pytestmark_node
def test_get_contract_breaks_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import calls_index as _ci
    from chameleon_mcp import tools

    repo = tmp_path / "repo_tool"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function foo(a: number) {\n  return a\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.ts").write_text("export function foo(a: number, b: number) {\n  return a + b\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "narrow")

    fake = _FakeIndex({("a.ts", "foo"): {"callers": [{"path": "b.ts", "line": 4}], "total": 1}})
    monkeypatch.setattr(_ci, "load_calls_index", lambda root: fake)

    (repo / ".chameleon").mkdir(exist_ok=True)
    from chameleon_mcp.profile.trust import grant_trust as _grant
    from chameleon_mcp.tools import _compute_repo_id as _crid

    _grant(_crid(repo), repo / ".chameleon")
    result = tools.get_contract_breaks(str(repo), base_ref="main")
    data = result["data"]
    assert data["status"] == "ok"
    assert len(data["findings"]) == 1
    assert data["findings"][0]["name"] == "foo"
    assert data["findings"][0]["new_required_positional"] == 2


def test_get_contract_breaks_non_git_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import tools

    plain = tmp_path / "plain_cb"
    plain.mkdir()
    (plain / ".chameleon").mkdir(exist_ok=True)
    from chameleon_mcp.profile.trust import grant_trust as _grant
    from chameleon_mcp.tools import _compute_repo_id as _crid

    _grant(_crid(plain), plain / ".chameleon")
    result = tools.get_contract_breaks(str(plain), base_ref="main")
    assert result["data"]["status"] in ("degraded", "failed")


@pytestmark_node
def test_contract_diff_uses_merge_base_not_base_tip(tmp_path, monkeypatch):
    # base_ref (main) independently changes foo's arity AFTER the branch point.
    # Three-dot/merge-base semantics must compare the feature against the branch
    # point (foo with 1 req), not against main's diverged tip (foo with 3 req).
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import calls_index as _ci
    from chameleon_mcp import tools

    repo = tmp_path / "repo_mb"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function foo(a: number) {\n  return a\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge-base: foo(a)")

    # Branch off the merge-base.
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.ts").write_text("export function foo(a: number, b: number) {\n  return a + b\n}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature: foo(a, b)  -- narrows 1->2")

    # main diverges and ALSO widens foo to 3 required positionals.
    _git(repo, "checkout", "-q", "main")
    (repo / "a.ts").write_text(
        "export function foo(a: number, b: number, c: number) {\n  return a + b + c\n}\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main: foo(a, b, c)")
    _git(repo, "checkout", "-q", "feature")

    fake = _FakeIndex({("a.ts", "foo"): {"callers": [{"path": "b.ts", "line": 4}], "total": 1}})
    monkeypatch.setattr(_ci, "load_calls_index", lambda root: fake)

    # The calls-index-derived contract-break signal is trust-gated (it must not
    # leak committed index contents from an untrusted profile), so grant trust.
    # Isolate the trust write to tmp so the suite never touches the real
    # ~/.local/share/chameleon (the documented test-isolation guarantee).
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    (repo / ".chameleon").mkdir(exist_ok=True)
    from chameleon_mcp.profile.trust import grant_trust as _grant
    from chameleon_mcp.tools import _compute_repo_id as _crid

    _grant(_crid(repo), repo / ".chameleon")
    result = tools.get_autopass_verdict(str(repo), base_ref="main")
    data = result["data"]
    # vs merge-base foo(a)=1: feature foo(a,b)=2 -> narrowing detected (1->2).
    # vs main tip foo(a,b,c)=3: 2 < 3 -> would be MISSED. The fix must detect it.
    assert data["facts"]["caller_contract_breaks"] == 1
    assert data["contract_breaks"][0]["old_required_positional"] == 1
    assert data["contract_breaks"][0]["new_required_positional"] == 2


@pytestmark_node
def test_contract_breaks_end_to_end_ruby(tmp_path):
    repo = tmp_path / "repo_rb"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    src = repo / "calc.rb"
    src.write_text("class Calc\n  def add(a)\n    a\n  end\nend\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Narrow: add a required positional.
    src.write_text("class Calc\n  def add(a, b)\n    a + b\n  end\nend\n")

    callers = {("calc.rb", "add"): {"callers": [{"path": "main.rb", "line": 2}], "total": 1}}
    findings = contract_breaks(
        repo,
        ["calc.rb"],
        old_ref="HEAD",
        new_ref=None,
        callers_fn=lambda rel, name: callers.get((rel, name)),
        run_git=_run_git,
    )
    assert len(findings) == 1
    assert findings[0].name == "add"


@pytestmark_node
def test_ruby_required_keyword_added_no_false_positive_real(tmp_path):
    # End-to-end FP guard: adding a required KEYWORD arg in Ruby must not flag.
    repo = tmp_path / "repo_rb_kw"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    src = repo / "svc.rb"
    src.write_text("class Svc\n  def run(a)\n    a\n  end\nend\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    src.write_text("class Svc\n  def run(a, mode:)\n    [a, mode]\n  end\nend\n")

    callers = {("svc.rb", "run"): {"callers": [{"path": "main.rb", "line": 2}], "total": 1}}
    findings = contract_breaks(
        repo,
        ["svc.rb"],
        old_ref="HEAD",
        new_ref=None,
        callers_fn=lambda rel, name: callers.get((rel, name)),
        run_git=_run_git,
    )
    assert findings == []


@pytestmark_node
def test_contract_breaks_batches_multiple_files(tmp_path):
    repo = tmp_path / "repo_multi"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function af(x: number) { return x }\n")
    (repo / "b.ts").write_text("export function bf(y: number) { return y }\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Both narrow.
    (repo / "a.ts").write_text("export function af(x: number, z: number) { return x + z }\n")
    (repo / "b.ts").write_text("export function bf(y: number, z: number) { return y + z }\n")

    callers = {
        ("a.ts", "af"): {"callers": [{"path": "c.ts", "line": 1}], "total": 1},
        ("b.ts", "bf"): {"callers": [{"path": "c.ts", "line": 2}], "total": 1},
    }
    findings = contract_breaks(
        repo,
        ["a.ts", "b.ts"],
        old_ref="HEAD",
        new_ref=None,
        callers_fn=lambda rel, name: callers.get((rel, name)),
        run_git=_run_git,
    )
    assert {f.name for f in findings} == {"af", "bf"}


def _ruby_available():
    import shutil

    return shutil.which("ruby") is not None


@pytestmark_node
def test_contract_breaks_polyglot_ts_and_ruby(tmp_path):
    if not _ruby_available():
        import pytest as _pt

        _pt.skip("ruby not available")
    repo = tmp_path / "repo_poly"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "a.ts").write_text("export function af(x: number) { return x }\n")
    (repo / "c.rb").write_text("class C\n  def cm(a)\n    a\n  end\nend\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "a.ts").write_text("export function af(x: number, z: number) { return x + z }\n")
    (repo / "c.rb").write_text("class C\n  def cm(a, b)\n    a + b\n  end\nend\n")

    callers = {
        ("a.ts", "af"): {"callers": [{"path": "x.ts", "line": 1}], "total": 1},
        ("c.rb", "cm"): {"callers": [{"path": "x.rb", "line": 1}], "total": 1},
    }
    findings = contract_breaks(
        repo,
        ["a.ts", "c.rb"],
        old_ref="HEAD",
        new_ref=None,
        callers_fn=lambda rel, name: callers.get((rel, name)),
        run_git=_run_git,
    )
    assert {f.name for f in findings} == {"af", "cm"}
