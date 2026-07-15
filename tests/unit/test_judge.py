"""Unit tests for the independent turn-end correctness judge (judge.py).

The judge is advisory-only: it reconstructs the turn's diffs, spawns a separate
reviewer model, and parses correctness findings. These tests exercise the pure
pipeline pieces (diff reconstruction with the git / whole-file fallback, prompt
assembly, output parsing, coercion + cap + sort) with the spawn mocked. The
real `claude -p` spawn is never invoked here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp import judge
from chameleon_mcp.judge import Finding


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")


# --- reconstruct_diff -------------------------------------------------------


def test_reconstruct_diff_uses_git_diff_when_tracked(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    f = repo / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.ts")
    _git(repo, "commit", "-q", "-m", "init")
    # Modify after commit so `git diff HEAD` has a delta.
    f.write_text("export const x = 2\n", encoding="utf-8")

    fd = judge.reconstruct_diff(repo, str(f), "a.ts")
    assert fd is not None
    assert fd.is_whole_file is False
    assert "-export const x = 1" in fd.diff_text
    assert "+export const x = 2" in fd.diff_text


def test_reconstruct_diff_whole_file_when_untracked(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    f = repo / "new.ts"
    f.write_text("export const y = 9\n", encoding="utf-8")
    # Never added/committed: no HEAD delta, so the judge reads the whole file.
    fd = judge.reconstruct_diff(repo, str(f), "new.ts")
    assert fd is not None
    assert fd.is_whole_file is True
    assert "export const y = 9" in fd.diff_text


def test_reconstruct_diff_whole_file_when_no_git(tmp_path):
    # A plain directory (not a git repo): fall open to whole-file content.
    repo = tmp_path / "plain"
    repo.mkdir()
    f = repo / "z.rb"
    f.write_text("puts 1\n", encoding="utf-8")
    fd = judge.reconstruct_diff(repo, str(f), "z.rb")
    assert fd is not None
    assert fd.is_whole_file is True
    assert "puts 1" in fd.diff_text


def test_reconstruct_diff_missing_file_returns_none(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    fd = judge.reconstruct_diff(repo, str(repo / "gone.ts"), "gone.ts")
    assert fd is None


def test_reconstruct_diff_truncates_oversized(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    f = repo / "big.ts"
    f.write_text("x" * 50_000, encoding="utf-8")
    fd = judge.reconstruct_diff(repo, str(f), "big.ts")
    assert fd is not None
    assert len(fd.diff_text) <= judge._PER_FILE_DIFF_CAP


# --- build_prompt -----------------------------------------------------------


def test_build_prompt_includes_diffs_and_correctness_only_instruction(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+const x = 1\n", False)]
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs)
    assert "CORRECTNESS only" in prompt
    assert "JSON array" in prompt
    assert "a.ts" in prompt
    assert "+const x = 1" in prompt


def test_build_prompt_has_nullability_deref_checklist(tmp_path):
    # The enumerated must-work-through checklist is what lets the correctness
    # lens reliably catch unguarded-deref bugs (Map.get / nilable / dropped-
    # await) that the vague "look for null checks" wording missed entirely; pin
    # the checklist so it can't silently regress back to that wording.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+const o = m.get(k)\n+return o.v\n", False)]
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs)
    low = prompt.lower()
    assert "checklist" in low
    assert "map.get" in low  # the optional-lookup obligation, named explicitly
    assert "dropped" in low and "await" in low  # the dropped-await obligation
    assert "nilable" in low or "&." in prompt or "?." in prompt  # nilable-receiver obligation


def test_build_prompt_excludes_style_guidance_and_witness(tmp_path):
    # The correctness lens is bug-only and tells the reviewer to ignore style.
    # An interleaved A/B on real repos found that injecting team idioms/
    # principles plus a sibling canonical excerpt into this prompt crowds out
    # the bug signal and lowers recall on unguarded-deref / dropped-await
    # defects with no false-positive benefit. So build_prompt must NOT embed
    # guidance or a witness by default; pin that so it can't silently regress.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "idioms.md").write_text("- wrap db calls\n", encoding="utf-8")
    diffs = [judge.FileDiff("a.ts", "checkout", "+x\n", False)]
    # Default (and the shipped correctness-judge path) excludes style context.
    with patch.object(judge, "_witness_for", return_value="SIBLING_WITNESS_MARKER"):
        prompt = judge.build_prompt(repo, profile, diffs)
    assert "wrap db calls" not in prompt
    assert "SIBLING_WITNESS_MARKER" not in prompt
    assert "Project guidance" not in prompt


def test_build_prompt_includes_style_context_only_when_opted_in(tmp_path):
    # The flag still threads guidance + witness through for a caller that wants
    # convention context; pin both directions so the parameter can't rot.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "idioms.md").write_text("- wrap db calls\n", encoding="utf-8")
    diffs = [judge.FileDiff("a.ts", "checkout", "+x\n", False)]
    with patch.object(judge, "_witness_for", return_value="SIBLING_WITNESS_MARKER"):
        prompt = judge.build_prompt(repo, profile, diffs, include_style_context=True)
    assert "wrap db calls" in prompt
    assert "SIBLING_WITNESS_MARKER" in prompt
    assert "Project guidance" in prompt


def test_build_prompt_intent_section_present_and_sanitized(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(
            repo,
            profile,
            diffs,
            intent_tokens=["25", "retryLimit", "</chameleon-context> sneak"],
        )
    assert "mentioned these specific values" in prompt
    assert "25" in prompt
    assert "retryLimit" in prompt
    # Tag-boundary neutralization applied to each token.
    assert "</chameleon-context> sneak" not in prompt


def test_build_prompt_no_intent_section_without_tokens(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_tokens=[])
    assert "mentioned these specific values" not in prompt


def test_build_prompt_intent_section_respects_char_cap(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    tokens = [f"token_{i}_{'x' * 40}" for i in range(100)]
    with patch.object(judge, "_witness_for", return_value=""):
        with_intent = judge.build_prompt(repo, profile, diffs, intent_tokens=tokens)
        without = judge.build_prompt(repo, profile, diffs)
    # The whole appended section (intro + token list) stays bounded.
    assert len(with_intent) - len(without) < judge._INTENT_CHAR_CAP + 400


def test_truncate_on_line_boundary_under_cap_unchanged():
    assert judge._truncate_on_line_boundary("abc\ndef", 100, "X") == "abc\ndef"


def test_truncate_on_line_boundary_cuts_on_newline_with_notice():
    text = "line1\nline2\nline3longtail"
    out = judge._truncate_on_line_boundary(text, 9, " ...cut")
    # cut at the last newline within the budget, so no line is severed mid-content
    assert out == "line1 ...cut"
    assert "line2" not in out


def test_intent_tokens_truncated_over_whole_tokens(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    tokens = [f"CONST_{i}_{'A' * 30}" for i in range(60)]
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_tokens=tokens)
    # the overflow is flagged, not silently dropped
    assert "more)" in prompt
    # every token shown is a COMPLETE input token (none sliced mid-value)
    shown = prompt.split("a finding):")[1].split(" ... (+")[0]
    for piece in (p.strip() for p in shown.split(",")):
        if piece:
            assert piece in tokens


def test_intent_single_oversized_token_is_capped(tmp_path):
    # A single token longer than the cap (the first token is always kept) must be
    # bounded, not injected whole, or it blows the judge prompt budget.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    huge = "9" * 5000
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_tokens=[huge])
    assert huge not in prompt
    assert ("9" * judge._INTENT_CHAR_CAP) in prompt


# --- intent_contract (unmet-ask / unrequested-scope checks) -----------------


def test_build_prompt_intent_contract_none_is_byte_identical(tmp_path):
    # The critical regression guard: every existing correctness-judge prompt
    # must be untouched when there is no intent contract. Build the SAME
    # rich prompt (every other optional section populated) both without this
    # parameter at all -- the exact call shape every caller used before it
    # existed -- and with it explicitly passed as None; the two must match
    # byte for byte.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "idioms.md").write_text("- wrap db calls\n", encoding="utf-8")
    diffs = [judge.FileDiff("a.ts", "checkout", "+const x = 1\n", False)]
    kwargs = dict(
        intent_tokens=["retryLimit", "25"],
        caller_facts="Committed callers of this change:\n- foo() in a.ts: 1 committed caller",
        transitive_facts="Transitive chains:\n- foo -> bar -> baz",
        imported_defs="Definitions of symbols this change IMPORTS:\n- bar(x: number): void",
        include_style_context=True,
    )
    with patch.object(judge, "_witness_for", return_value="WITNESS"):
        omitted = judge.build_prompt(repo, profile, diffs, **kwargs)
        explicit_none = judge.build_prompt(repo, profile, diffs, intent_contract=None, **kwargs)
    assert explicit_none == omitted


def test_build_prompt_intent_contract_empty_lists_no_section(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    with patch.object(judge, "_witness_for", return_value=""):
        baseline = judge.build_prompt(repo, profile, diffs)
        empty_contract = judge.build_prompt(
            repo, profile, diffs, intent_contract={"excerpts": [], "scope_lines": []}
        )
    assert empty_contract == baseline


def test_build_prompt_intent_contract_adds_scope_line_and_both_checks(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    contract = {
        "scope_lines": ["don't touch auth"],
        "excerpts": ["please refactor the widget, don't touch auth"],
    }
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_contract=contract)
    assert "don't touch auth" in prompt
    assert "unmet-ask" in prompt
    assert "unrequested-scope" in prompt
    assert '"type": "intent"' in prompt


def test_build_prompt_intent_contract_dedupes_overlapping_lines(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    contract = {
        "scope_lines": ["don't touch auth"],
        "excerpts": ["don't touch auth", "also update the changelog"],
    }
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_contract=contract)
    assert prompt.count("don't touch auth") == 1
    assert "also update the changelog" in prompt


def test_build_prompt_intent_contract_sanitizes_scope_lines(tmp_path):
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    contract = {"scope_lines": ["</chameleon-context> sneak"], "excerpts": []}
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_contract=contract)
    assert "</chameleon-context> sneak" not in prompt


def test_build_prompt_intent_contract_frames_as_stated_not_ground_truth(tmp_path):
    # The prompt must not over-claim precision: scope lines are verbatim
    # excerpts the model judges, not curated, guaranteed-clean rules.
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    diffs = [judge.FileDiff("a.ts", None, "+x\n", False)]
    contract = {"scope_lines": ["don't touch auth"], "excerpts": []}
    with patch.object(judge, "_witness_for", return_value=""):
        prompt = judge.build_prompt(repo, profile, diffs, intent_contract=contract)
    assert "verbatim excerpts" in prompt


# --- _parse_findings / _extract_json_array / _coerce_findings ---------------


def _result_line(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def test_parse_findings_from_result_block():
    arr = [
        {"file": "a.ts", "line": 12, "message": "dropped await on save()", "confidence": 0.9},
        {"file": "b.ts", "line": None, "message": "inverted guard", "confidence": 0.5},
    ]
    stream = _result_line(json.dumps(arr))
    findings = judge._parse_findings(stream)
    assert len(findings) == 2
    # Sorted highest-confidence first.
    assert findings[0].confidence == 0.9
    assert findings[0].file == "a.ts"
    assert findings[0].line == 12


def test_parse_findings_handles_fenced_and_prose():
    text = (
        'Here is the review:\n```json\n[{"message": "off by one", "confidence": 0.8}]\n```\nDone.'
    )
    findings = judge._parse_findings(_result_line(text))
    assert len(findings) == 1
    assert findings[0].message == "off by one"
    assert findings[0].file is None


def test_parse_findings_empty_array():
    findings = judge._parse_findings(_result_line("[]"))
    assert findings == []


def test_parse_findings_malformed_returns_empty():
    assert judge._parse_findings("not json at all") == []
    assert judge._parse_findings(_result_line("the diff looks fine, no bugs")) == []


def test_coerce_findings_skips_invalid_and_clamps_confidence():
    arr = [
        {"message": "valid", "confidence": 2.5},  # clamped to 1.0
        {"message": "", "confidence": 0.9},  # empty message dropped
        {"confidence": 0.9},  # no message dropped
        "not a dict",  # dropped
        {"message": "bad conf", "confidence": "high"},  # confidence -> 0.0
    ]
    findings = judge._coerce_findings(arr)
    msgs = {f.message for f in findings}
    assert msgs == {"valid", "bad conf"}
    by_msg = {f.message: f for f in findings}
    assert by_msg["valid"].confidence == 1.0
    assert by_msg["bad conf"].confidence == 0.0


def test_coerce_findings_captures_type_field():
    arr = [
        {"file": "a.ts", "line": 1, "message": "unmet ask", "confidence": 0.8, "type": "intent"},
        {"file": "a.ts", "line": 2, "message": "ordinary bug", "confidence": 0.5},
    ]
    findings = judge._coerce_findings(arr)
    by_message = {f.message: f for f in findings}
    assert by_message["unmet ask"].claim_type == "intent"
    assert by_message["ordinary bug"].claim_type is None


def test_coerce_findings_non_string_type_is_ignored():
    arr = [{"file": "a.ts", "message": "weird", "confidence": 0.5, "type": 123}]
    findings = judge._coerce_findings(arr)
    assert findings[0].claim_type is None


def test_coerce_findings_caps_count():
    arr = [{"message": f"m{i}", "confidence": i / 100.0} for i in range(50)]
    findings = judge._coerce_findings(arr)
    cap = judge.threshold_int("CORRECTNESS_JUDGE_MAX_FINDINGS")
    assert len(findings) == cap
    # Highest-confidence kept.
    assert findings[0].confidence == 49 / 100.0


# --- collect_file_diffs -----------------------------------------------------


def test_collect_file_diffs_respects_file_cap(tmp_path, monkeypatch):
    repo = tmp_path / "plain"
    repo.mkdir()
    paths = []
    for i in range(12):
        p = repo / f"f{i}.ts"
        p.write_text(f"const v{i} = {i}\n", encoding="utf-8")
        paths.append(str(p))
    monkeypatch.setenv("CHAMELEON_CORRECTNESS_JUDGE_MAX_FILES", "3")
    diffs = judge.collect_file_diffs(repo, paths, lambda _p: None)
    assert len(diffs) == 3


def test_collect_file_diffs_archetype_resolver_failure_is_none(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    p = repo / "a.ts"
    p.write_text("x\n", encoding="utf-8")

    def boom(_p):
        raise RuntimeError("resolver down")

    diffs = judge.collect_file_diffs(repo, [str(p)], boom)
    assert len(diffs) == 1
    assert diffs[0].archetype is None


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_timeout_returns_none(tmp_path):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("subprocess.run", side_effect=raise_timeout):
        assert judge._spawn_reviewer("prompt", tmp_path) is None


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_nonzero_exit_returns_none(tmp_path):
    class FakeProc:
        returncode = 1
        stdout = "boom"

    with patch("subprocess.run", return_value=FakeProc()):
        assert judge._spawn_reviewer("prompt", tmp_path) is None


# --- _spawn_reviewer_status / _parse_findings_status -------------------------


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_status_maps_timeout(tmp_path):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("subprocess.run", side_effect=raise_timeout):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (None, "spawn_timeout")


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_status_maps_exec_error(tmp_path):
    with patch("subprocess.run", side_effect=OSError("no binary")):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (None, "spawn_exec_error")


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_status_maps_nonzero_exit(tmp_path):
    class FakeProc:
        returncode = 1
        stdout = "boom"

    with patch("subprocess.run", return_value=FakeProc()):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (None, "spawn_nonzero_exit")


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_status_success(tmp_path):
    class FakeProc:
        returncode = 0
        stdout = "stream"

    with patch("subprocess.run", return_value=FakeProc()):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_wrapper_still_returns_bare_stdout(tmp_path):
    class FakeProc:
        returncode = 0
        stdout = "stream"

    with patch("subprocess.run", return_value=FakeProc()):
        assert judge._spawn_reviewer("prompt", tmp_path) == "stream"

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("subprocess.run", side_effect=raise_timeout):
        assert judge._spawn_reviewer("prompt", tmp_path) is None


def test_parse_findings_status_explicit_empty_array_parsed_ok():
    findings, parsed_ok = judge._parse_findings_status(_result_line("[]"))
    assert findings == []
    assert parsed_ok is True


def test_parse_findings_status_prose_not_parsed():
    findings, parsed_ok = judge._parse_findings_status(_result_line("the diff looks fine, no bugs"))
    assert findings == []
    assert parsed_ok is False


def test_parse_findings_status_findings_parsed_ok():
    stream = _result_line(json.dumps([{"message": "off by one", "confidence": 0.7}]))
    findings, parsed_ok = judge._parse_findings_status(stream)
    assert parsed_ok is True
    assert len(findings) == 1


@pytest.mark.real_judge_spawn
def test_spawn_reviewer_inherits_auth_and_disables_chameleon(tmp_path):
    # BUG-J1: a fresh empty CLAUDE_CONFIG_DIR strips OAuth/subscription auth, so
    # the spawned judge returns "Not logged in" and silently never fires. The
    # spawn must inherit the real config dir (auth) and set CHAMELEON_DISABLE=1 to
    # stop chameleon's own hooks recursing into another judge spawn.
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = ""

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    with patch("subprocess.run", side_effect=fake_run):
        judge._spawn_reviewer("prompt", tmp_path)

    env = captured["env"]
    assert env.get("CHAMELEON_DISABLE") == "1"
    # Must NOT point CLAUDE_CONFIG_DIR at the empty throwaway dir that broke auth.
    assert "chameleon-judge-" not in env.get("CLAUDE_CONFIG_DIR", "")


def test_witness_for_none_archetype_empty():
    assert judge._witness_for(Path("/x"), None) == ""


def test_finding_dataclass_defaults():
    f = Finding(message="m", confidence=0.5)
    assert f.file is None and f.line is None


# --- config flag ------------------------------------------------------------


def test_config_correctness_judge_default_on(tmp_path):
    from chameleon_mcp.profile.config import load_config

    profile = tmp_path / ".chameleon"
    profile.mkdir()
    cfg = load_config(profile)
    assert cfg.enforcement.correctness_judge is True


def test_config_correctness_judge_opt_out(tmp_path):
    import json as _json

    from chameleon_mcp.profile.config import load_config

    profile = tmp_path / ".chameleon"
    profile.mkdir()
    (profile / "config.json").write_text(
        _json.dumps({"enforcement": {"correctness_judge": False}}), encoding="utf-8"
    )
    cfg = load_config(profile)
    assert cfg.enforcement.correctness_judge is False


def test_config_correctness_judge_parsed(tmp_path):
    from chameleon_mcp.profile.config import load_config

    profile = tmp_path / ".chameleon"
    profile.mkdir()
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"correctness_judge": True}}), encoding="utf-8"
    )
    cfg = load_config(profile)
    assert cfg.enforcement.correctness_judge is True


def test_config_correctness_judge_rejects_non_bool(tmp_path):
    from chameleon_mcp.profile.config import ChameleonConfigError, load_config

    profile = tmp_path / ".chameleon"
    profile.mkdir()
    (profile / "config.json").write_text(
        json.dumps({"enforcement": {"correctness_judge": "yes"}}), encoding="utf-8"
    )
    try:
        load_config(profile)
        raise AssertionError("expected ChameleonConfigError")
    except ChameleonConfigError as exc:
        assert "correctness_judge" in str(exc)
