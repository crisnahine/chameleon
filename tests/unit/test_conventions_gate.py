"""A headless, diff-scoped conventions gate for edits PreToolUse never saw.

Everything chameleon enforces runs inside an interactive session. That leaves a
real hole: human-authored commits, sessions with chameleon disabled, paused or
untrusted, and other agents entirely. architecture.md names CI as a rollout
stage and ships no mechanism for it.

Two constraints the design has to honor, both from the repo's own evidence:

- DIFF-SCOPED OR NOT AT ALL. Whole-blob counting measures accumulated load, not
  what a change did; the re-run measured pre-existing load at 5.2-5.7x the
  introduced count. A file-level gate would fail a PR for violations it
  inherited, which is how a gate gets switched off in a week.
- SOFT BY DEFAULT. lint_file is heuristic regex extraction, not a parser, and
  architecture.md deliberately keeps CI a build/lint pipeline rather than a
  correctness gate. The exit code is advisory unless a caller opts in.

The gate is a backstop, not a better arrival time: guidance that lands before
the write is strictly better, and this only covers writes that never got it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from chameleon_mcp.gate import GateResult, decide


def _f(rule="naming-convention-violation", severity="error", path="src/a.ts"):
    return {"rule": rule, "severity": severity, "file": path, "message": "m"}


class TestDecide:
    def test_clean_diff_passes(self):
        r = decide([], strict=False)
        assert isinstance(r, GateResult)
        assert r.exit_code == 0
        assert r.introduced == 0

    def test_findings_are_advisory_by_default(self):
        # Reports them, does not fail the build: the honest posture for a
        # heuristic extractor.
        r = decide([_f(), _f()], strict=False)
        assert r.introduced == 2
        assert r.exit_code == 0

    def test_strict_mode_fails_on_introduced_findings(self):
        r = decide([_f()], strict=True)
        assert r.exit_code == 1

    def test_strict_mode_still_passes_a_clean_diff(self):
        assert decide([], strict=True).exit_code == 0

    def test_per_edit_suppressed_rules_are_not_counted(self):
        # A rule the per-edit path strips before the model reads it cannot be
        # one this gate holds an author to.
        r = decide([_f(rule="cross-file-importers")], strict=True)
        assert r.introduced == 0
        assert r.exit_code == 0

    def test_findings_are_grouped_by_rule_for_reporting(self):
        r = decide([_f(rule="a"), _f(rule="a"), _f(rule="b")], strict=False)
        assert r.by_rule == {"a": 2, "b": 1}


class TestRender:
    def test_clean_render_says_so(self):
        from chameleon_mcp.gate import render_text

        out = render_text(decide([], strict=False))
        assert "no new convention violations" in out.lower()

    def test_render_names_rule_and_file(self):
        from chameleon_mcp.gate import render_text

        out = render_text(decide([_f(rule="naming-convention-violation")], strict=False))
        assert "naming-convention-violation" in out
        assert "src/a.ts" in out

    def test_render_states_the_scope_it_measured(self):
        # Anyone reading CI output must know this is introduced-only, or they
        # will read a low number as "the file is clean".
        from chameleon_mcp.gate import render_text

        out = render_text(decide([_f()], strict=False))
        assert "introduced" in out.lower()


class TestUntrusted:
    def test_untrusted_profile_is_reported_not_silently_clean(self):
        # lint_file returns zero convention findings without a trust grant, so a
        # gate that ignored trust would print a green build for a repo it never
        # actually linted -- the worst possible failure for a CI check.
        r = decide([], strict=True, trusted=False)
        assert r.exit_code == 2
        assert r.untrusted is True

    def test_trusted_is_the_default(self):
        assert decide([], strict=True).untrusted is False


@pytest.mark.parametrize("strict", [True, False])
def test_untrusted_never_reports_success(strict):
    assert decide([], strict=strict, trusted=False).exit_code != 0


class TestUnusable:
    def test_a_git_failure_never_renders_as_a_clean_build(self, tmp_path):
        """Regression: a bad base returned [] changed files and exited 0.

        A shallow CI clone missing origin/main, a typo'd --base, or no git at
        all made `git diff` exit non-zero; the loop never ran, and the gate
        printed "no new convention violations" at exit 0 for a diff nothing
        looked at. That is the silent pass the module exists to avoid.
        """
        from chameleon_mcp.gate import main

        (tmp_path / "f.ts").write_text("export const a = 1;\n", encoding="utf-8")
        code = main(["--repo", str(tmp_path), "--base", "no-such-ref", "--strict"])
        assert code == 2


# --- a path git C-quotes must not leave the change set silently ---------------


_HAS_GIT = shutil.which("git") is not None
_GIT = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


def _repo_with_non_ascii_dir(tmp_path):
    """A committed repo whose .ts file sits under an accented DIRECTORY."""
    repo = tmp_path / "r"
    (repo / "café" / "sub").mkdir(parents=True)
    (repo / "café" / "sub" / "plain.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "plain2.ts").write_text("export const b = 2;\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "one"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "café" / "sub" / "plain.ts").write_text("export const a = 2;\n", encoding="utf-8")
    (repo / "plain2.ts").write_text("export const b = 3;\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-qm", "two"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


@_GIT
def test_non_ascii_path_still_reaches_the_gate(tmp_path):
    """Without core.quotePath=false git emits `"caf\\303\\251/sub/plain.ts"`, and
    detect_language is a plain endswith -- the quoted name ends in `"`, resolves to
    no language, and the file leaves the change set. Nothing raises, nothing is
    recorded, and the gate prints "no new convention violations" at exit 0: a green
    build for a file it never linted, which is the one outcome this module refuses.
    One accented DIRECTORY component un-gates every ordinary file beneath it.
    """
    from chameleon_mcp.gate import _changed_source_files

    files = _changed_source_files(_repo_with_non_ascii_dir(tmp_path), "HEAD~1", "HEAD")
    assert "café/sub/plain.ts" in files
    assert "plain2.ts" in files
    assert not any(f.startswith('"') for f in files)


@_GIT
def test_run_git_does_not_c_quote_paths(tmp_path):
    """The shared helper carries the flag so no call site can forget it. Three
    consumers parse path output through it (the co-change dirty-partner guard, the
    contract-break numstat read, reconstruct_diff), and each would silently fail to
    match a quoted name."""
    from chameleon_mcp.judge import _run_git

    repo = _repo_with_non_ascii_dir(tmp_path)
    res = _run_git(["diff", "--name-only", "HEAD~1...HEAD"], cwd=repo)
    assert res is not None and res.returncode == 0
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    assert "café/sub/plain.ts" in lines
    assert not any(ln.startswith('"') for ln in lines)


# --- a path core.quotePath=false does NOT un-quote --------------------------


_ODD_NAMES = ['we"ird.ts', "back\\slash.ts", "new\nline.ts"]
_POSIX_NAMES = pytest.mark.skipif(
    os.name == "nt", reason="quotes, backslashes and newlines are illegal in Windows filenames"
)


def _commit_all(repo, message):
    for args in (["add", "-A"], ["commit", "-qm", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _repo_with_odd_names(tmp_path):
    repo = _init_repo(tmp_path / "r")
    for i, name in enumerate(_ODD_NAMES):
        (repo / name).write_text(f"export const a{i} = 1;\n", encoding="utf-8")
    _commit_all(repo, "one")
    for i, name in enumerate(_ODD_NAMES):
        (repo / name).write_text(f"export const a{i} = 2;\n", encoding="utf-8")
    _commit_all(repo, "two")
    return repo


@_GIT
@_POSIX_NAMES
def test_quoted_metacharacter_paths_still_reach_the_gate(tmp_path):
    """core.quotePath=false governs bytes >= 0x80 and nothing else.

    A name holding a quote, a backslash or a newline is still C-quoted with the
    flag set, and detect_language is a plain endswith -- a quoted name resolves
    to no language and leaves the change set with nothing raised. Only -z emits
    every path raw, which is why the file list is NUL-separated, not by line.
    """
    from chameleon_mcp.gate import _changed_source_files

    files = _changed_source_files(_repo_with_odd_names(tmp_path), "HEAD~1", "HEAD")
    assert sorted(files) == sorted(_ODD_NAMES)


@_GIT
@_POSIX_NAMES
def test_a_metacharacter_path_can_still_be_read_at_a_revision(tmp_path):
    """The other half of -z: a name the gate keeps has to round-trip back to git.

    A dropped path and a path git cannot resolve are the same silent hole, so
    the raw name from the file list must be the one `git show` accepts.
    """
    from chameleon_mcp.commit_scope import blob_at
    from chameleon_mcp.gate import _changed_source_files

    repo = _repo_with_odd_names(tmp_path)
    for rel in _changed_source_files(repo, "HEAD~1", "HEAD"):
        expected = f"export const a{_ODD_NAMES.index(rel)} = 2;\n"
        assert blob_at(repo, "HEAD", rel, max_bytes=1_000_000) == expected


@_GIT
def test_git_missing_from_path_exits_unusable_not_findings(tmp_path, monkeypatch):
    """Exit 1 under --strict means "this diff introduced violations".

    An unguarded spawn error exits 1 as well, and a CI step branching on the
    code cannot tell the two apart -- so it reads a broken gate as a failing one,
    or learns to ignore the code. Every way the gate cannot run lands on 2.
    """
    from chameleon_mcp.gate import main

    repo = _init_repo(tmp_path / "r")
    (repo / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    _commit_all(repo, "one")
    (repo / "a.ts").write_text("export const a = 2;\n", encoding="utf-8")
    _commit_all(repo, "two")

    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert main(["--repo", str(repo), "--base", "HEAD~1", "--strict"]) == 2


# --- collect(): the producer behind every decide() verdict --------------------


# Amazon's own published example key: the right shape for the scan to flag, and
# not a credential that could ever be live.
_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"  # chameleon-ignore secret-detected-in-content


def _profiled_repo(tmp_path, monkeypatch, *, trust=True):
    """A git repo carrying a minimal, optionally trusted profile for collect()."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")

    repo = _init_repo(tmp_path / "repo")
    cham = repo / ".chameleon"
    cham.mkdir()
    for name, body in (
        ("profile.json", {"generation": 1, "language": "typescript"}),
        ("archetypes.json", {"generation": 1, "archetypes": {"component": {"summary": "x"}}}),
        ("canonicals.json", {"generation": 1, "canonicals": {"component": []}}),
        ("conventions.json", {"generation": 1, "conventions": {}}),
        ("rules.json", {"generation": 1, "rules": {}}),
    ):
        (cham / name).write_text(json.dumps(body), encoding="utf-8")
    (cham / "idioms.md").write_text("# idioms\n\n## active\n", encoding="utf-8")
    (cham / "COMMITTED").touch()
    if trust:
        from chameleon_mcp.profile.trust import grant_trust
        from chameleon_mcp.tools import _compute_repo_id

        grant_trust(_compute_repo_id(repo), cham)
    return repo


def _two_commits(repo, before, after):
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "a.ts").write_text(before, encoding="utf-8")
    _commit_all(repo, "one")
    (repo / "src" / "a.ts").write_text(after, encoding="utf-8")
    _commit_all(repo, "two")


@_GIT
def test_collect_counts_only_what_the_diff_introduced(tmp_path, monkeypatch):
    """Two violations present, one of them inherited -- the count is one.

    decide() is pure and takes `trusted` as an argument. collect() is what
    produces both, and where the baseline subtraction actually happens. A gate
    reporting the file's whole load here would fail an author for what they
    inherited, which is how a gate gets switched off in a week.
    """
    from chameleon_mcp.gate import collect, main

    repo = _profiled_repo(tmp_path, monkeypatch)
    _two_commits(
        repo,
        f'export const old = "{_EXAMPLE_KEY}";\n',
        f'export const old = "{_EXAMPLE_KEY}";\nexport const fresh = "{_EXAMPLE_KEY}";\n',
    )

    rows, trusted = collect(repo, "HEAD~1", "HEAD")
    assert trusted is True
    assert [r.get("rule") for r in rows] == ["secret-detected-in-content"]
    assert rows[0].get("file") == "src/a.ts"
    assert main(["--repo", str(repo), "--base", "HEAD~1", "--strict"]) == 1


@_GIT
def test_collect_reports_nothing_when_the_diff_carries_no_new_violation(tmp_path, monkeypatch):
    """The clean case has to reach exit 0 through a real lint of a real file.

    Otherwise the test above proves only that something raised, and an empty
    file list would read the same as a diff that was actually checked.
    """
    from chameleon_mcp.gate import collect, main

    repo = _profiled_repo(tmp_path, monkeypatch)
    _two_commits(
        repo,
        f'export const old = "{_EXAMPLE_KEY}";\n',
        f'export const old = "{_EXAMPLE_KEY}";\nexport const two = 2;\n',
    )

    rows, trusted = collect(repo, "HEAD~1", "HEAD")
    assert (rows, trusted) == ([], True)
    assert main(["--repo", str(repo), "--base", "HEAD~1", "--strict"]) == 0


@_GIT
def test_a_stubbed_lint_is_unusable_never_clean(tmp_path, monkeypatch):
    """A torn profile makes lint_file STUB: zero violations and no error.

    That is indistinguishable from a clean file, so counting it as clean is how
    a CI gate goes green on a repo it never linted. The reason has to reach the
    operator too: an exit code alone does not say what to fix.
    """
    from chameleon_mcp.gate import GateUnusable, collect, main

    repo = _profiled_repo(tmp_path, monkeypatch)
    _two_commits(repo, "export const a = 1;\n", "export const a = 2;\n")
    (repo / ".chameleon" / "archetypes.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(GateUnusable) as exc:
        collect(repo, "HEAD~1", "HEAD")
    assert "profile failed to load" in str(exc.value)
    assert main(["--repo", str(repo), "--base", "HEAD~1"]) == 2


@_GIT
def test_an_ungranted_profile_stops_collection_rather_than_passing(tmp_path, monkeypatch):
    """Without a grant lint_file returns no convention findings at all, so the
    loop has to stop on the trust status rather than run to completion and
    report the empty result as a clean diff."""
    from chameleon_mcp.gate import collect, main

    repo = _profiled_repo(tmp_path, monkeypatch, trust=False)
    _two_commits(repo, "export const a = 1;\n", "export const a = 2;\n")

    assert collect(repo, "HEAD~1", "HEAD") == ([], False)
    assert main(["--repo", str(repo), "--base", "HEAD~1"]) == 2


@_GIT
def test_an_unreadable_baseline_is_not_read_as_an_absent_one(tmp_path, monkeypatch):
    """An empty baseline says "every row in this file is introduced".

    It may only be reached when git confirms the path was absent at the base
    commit. Here the baseline blob is over the size cap and the current one is
    under it -- a commit that shrinks a generated file -- so the baseline is
    unknown, not absent. Scoring its inherited rows as introduced is the
    pre-existing-load inflation the diff scoping exists to remove.
    """
    from chameleon_mcp.gate import collect

    repo = _profiled_repo(tmp_path, monkeypatch)
    padding = "// " + ("x" * 400) + "\n"
    _two_commits(
        repo,
        f'export const old = "{_EXAMPLE_KEY}";\n{padding}',
        f'export const old = "{_EXAMPLE_KEY}";\n',
    )
    monkeypatch.setenv("CHAMELEON_GATE_MAX_FILE_BYTES", "200")

    assert collect(repo, "HEAD~1", "HEAD") == ([], True)
