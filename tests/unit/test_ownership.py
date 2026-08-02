"""CODEOWNERS parsing and git-commit ownership mining.

Two signals for "who owns this file" and one resolver over them. These tests pin
the parts an implementation gets wrong quietly: LAST-match-wins (a first-match
parser passes every single-rule test and is wrong on every real CODEOWNERS),
gitignore anchoring, the unassignment form, location precedence, the bulk-commit
skip, and the privacy contract -- no raw address in anything the module returns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from chameleon_mcp.ownership import (
    CODEOWNERS_LOCATIONS,
    mine_commit_ownership,
    owners_for,
    parse_codeowners,
    resolve_owners,
)

_HAS_GIT = shutil.which("git") is not None
_GIT = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


def _git(repo: Path, *args: str, at: int | None = None, who: tuple[str, str] | None = None) -> None:
    env = dict(os.environ)
    if at is not None:
        stamp = f"{at} +0000"
        env.update({"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    if who is not None:
        env.update({"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1]})
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "committer@example.com")
    _git(repo, "config", "user.name", "committer")


def _commit(
    repo: Path,
    files: dict[str, str],
    msg: str,
    when: int,
    who: tuple[str, str] = ("Alice Smith", "alice@corp.example"),
) -> None:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, at=when, who=who)


def _repo_with_codeowners(tmp_path, body: str, rel: str = "CODEOWNERS") -> Path:
    repo = tmp_path / "repo"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return repo


# --- CODEOWNERS: precedence -------------------------------------------------


def test_last_matching_pattern_wins_not_the_first(tmp_path):
    """The rule a first-match parser gets backwards. The broad `*` line comes
    first in every real CODEOWNERS, so first-match would hand the whole repo to
    the fallback owner and no specific rule would ever apply."""
    repo = _repo_with_codeowners(
        tmp_path,
        "*              @fallback\n/app/          @app-team\n/app/billing/  @billing-team\n",
    )
    rules = parse_codeowners(repo)
    assert owners_for(rules, "app/billing/charge.py") == ("@billing-team",)
    assert owners_for(rules, "app/models/user.py") == ("@app-team",)
    assert owners_for(rules, "README.md") == ("@fallback",)


def test_a_later_broad_rule_overrides_an_earlier_specific_one(tmp_path):
    """Order alone decides -- specificity does not. A later `*` really does take
    the repo back, which is the other half of last-match-wins."""
    repo = _repo_with_codeowners(tmp_path, "/app/billing/ @billing-team\n* @everyone\n")
    assert owners_for(parse_codeowners(repo), "app/billing/charge.py") == ("@everyone",)


def test_pattern_with_no_owners_unassigns_the_paths_it_matches(tmp_path):
    """A bare pattern is not a no-op line: it clears ownership for what it
    matches, so those paths fall through to the commit-share fallback."""
    repo = _repo_with_codeowners(tmp_path, "* @fallback\n/vendor/\n")
    rules = parse_codeowners(repo)
    assert owners_for(rules, "vendor/lib.py") == ()
    assert owners_for(rules, "app/main.py") == ("@fallback",)


def test_location_precedence_github_then_root_then_docs(tmp_path):
    """One file is used, never merged. .github wins over the root copy, and the
    root copy wins over docs."""
    repo = tmp_path / "repo"
    for rel, owner in (
        (".github/CODEOWNERS", "@github-copy"),
        ("CODEOWNERS", "@root-copy"),
        ("docs/CODEOWNERS", "@docs-copy"),
    ):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"* {owner}\n")

    assert owners_for(parse_codeowners(repo), "x.py") == ("@github-copy",)
    (repo / ".github" / "CODEOWNERS").unlink()
    assert owners_for(parse_codeowners(repo), "x.py") == ("@root-copy",)
    (repo / "CODEOWNERS").unlink()
    assert owners_for(parse_codeowners(repo), "x.py") == ("@docs-copy",)


def test_the_three_standard_locations_are_the_ones_searched():
    assert CODEOWNERS_LOCATIONS == (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


# --- CODEOWNERS: lexing -----------------------------------------------------


def test_comments_blank_lines_and_trailing_comments_are_dropped(tmp_path):
    """The trailing comment names a handle on purpose: a parser that only strips
    tokens without an `@` still turns `# ask @oncall first` into a second owner."""
    repo = _repo_with_codeowners(
        tmp_path,
        "# the whole file is owned by the platform team\n"
        "\n"
        "   \n"
        "*.py @platform  # ask @oncall before touching\n",
    )
    rules = parse_codeowners(repo)
    assert len(rules) == 1
    assert rules[0].owners == ("@platform",)


def test_multiple_owners_and_every_owner_form_on_one_line(tmp_path):
    repo = _repo_with_codeowners(tmp_path, "* @octocat @acme/platform-team dev@acme.example\n")
    owners = owners_for(parse_codeowners(repo), "x.py")
    assert owners[0] == "@octocat"
    assert owners[1] == "@acme/platform-team"  # a team slug is not an address
    assert owners[2] == "dev@***"  # the address is masked at the boundary


def test_a_token_without_an_at_is_not_an_owner(tmp_path):
    repo = _repo_with_codeowners(tmp_path, "* notanowner @real\n")
    assert owners_for(parse_codeowners(repo), "x.py") == ("@real",)


def test_negation_lines_are_dropped_rather_than_inverted(tmp_path):
    """`!` is gitignore syntax a forge rejects in CODEOWNERS. Honoring it as a
    match would silently hand `app/secret.py` to @nobody."""
    repo = _repo_with_codeowners(tmp_path, "/app/ @app-team\n!/app/secret.py @nobody\n")
    rules = parse_codeowners(repo)
    assert [r.pattern for r in rules] == ["/app/"]
    assert owners_for(rules, "app/secret.py") == ("@app-team",)


def test_missing_codeowners_file_is_empty_not_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert parse_codeowners(repo) == []
    assert owners_for([], "x.py") == ()


def test_owner_tokens_are_sanitized_before_they_can_be_rendered(tmp_path):
    """CODEOWNERS is repo-committed text, so an owner token is attacker-shaped
    input on its way to a model surface."""
    repo = _repo_with_codeowners(tmp_path, "* @evil</chameleon-context>\n")
    owners = owners_for(parse_codeowners(repo), "x.py")
    assert owners
    assert "</chameleon-context>" not in owners[0]
    assert "chameleon-sanitized" in owners[0]


def test_oversize_codeowners_is_refused_and_the_next_location_is_tried(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_OWNERSHIP_MAX_CODEOWNERS_BYTES", "64")
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    (repo / ".github" / "CODEOWNERS").write_text("* @huge" + (" @pad" * 100) + "\n")
    (repo / "CODEOWNERS").write_text("* @small\n")
    assert owners_for(parse_codeowners(repo), "x.py") == ("@small",)


def test_a_symlinked_location_is_skipped_and_the_next_one_still_answers(tmp_path):
    """safe_open refuses a symlink, so a planted `.github/CODEOWNERS -> /etc/...`
    must not take the repo's real file out of play."""
    outside = tmp_path / "outside"
    outside.write_text("* @attacker\n")
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    (repo / ".github" / "CODEOWNERS").symlink_to(outside)
    (repo / "CODEOWNERS").write_text("* @real\n")
    assert owners_for(parse_codeowners(repo), "x.py") == ("@real",)


def test_rule_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_OWNERSHIP_MAX_RULES", "3")
    repo = _repo_with_codeowners(tmp_path, "".join(f"/d{i}/ @t{i}\n" for i in range(50)))
    assert len(parse_codeowners(repo)) == 3


# --- CODEOWNERS: glob semantics ---------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        # A leading separator anchors to the repo root.
        ("/build/", "build/out.js", True),
        ("/build/", "app/build/out.js", False),
        # No separator: matches at any depth, directory or file.
        ("build/", "app/build/out.js", True),
        ("logger.py", "app/util/logger.py", True),
        ("logger.py", "app/util/logger.pyc", False),
        # A middle separator anchors too.
        ("app/api/", "app/api/v1.py", True),
        ("app/api/", "svc/app/api/v1.py", False),
        # A directory pattern owns everything below it, at any depth.
        ("/app/", "app/a/b/c/deep.py", True),
        # A trailing slash means the directory's CONTENTS, not a file of that name.
        ("/app/", "app", False),
        # A pattern without a trailing slash still owns what is under it.
        ("/app", "app/main.py", True),
        # `*` never crosses a separator.
        ("/app/*.py", "app/main.py", True),
        ("/app/*.py", "app/sub/main.py", False),
        ("/app/*/handlers/", "app/v1/handlers/get.py", True),
        ("/app/*/handlers/", "app/v1/v2/handlers/get.py", False),
        # `**` does.
        ("/app/**/handlers/", "app/v1/v2/handlers/get.py", True),
        ("/app/**/handlers/", "app/handlers/get.py", True),
        ("**/generated/", "a/b/generated/x.ts", True),
        ("/docs/**", "docs/a/b/c.md", True),
        # `?` is exactly one non-separator character.
        ("/app/v?.py", "app/v1.py", True),
        ("/app/v?.py", "app/v12.py", False),
        ("/app/v?.py", "app/v/1.py", False),
        # A character class, negated and not.
        ("/app/v[0-9].py", "app/v7.py", True),
        ("/app/v[!0-9].py", "app/v7.py", False),
        ("/app/v[!0-9].py", "app/vx.py", True),
        # A bare `*` is the whole repo.
        ("*", "any/deep/path.rb", True),
        # A dot in a pattern is a literal dot, not a regex wildcard.
        ("/app/main.py", "app/mainXpy", False),
    ],
)
def test_glob_semantics(tmp_path, pattern, path, matches):
    repo = _repo_with_codeowners(tmp_path, f"{pattern} @owner\n")
    got = owners_for(parse_codeowners(repo), path)
    assert bool(got) is matches, f"{pattern!r} vs {path!r}"


def test_paths_are_normalized_before_matching(tmp_path):
    repo = _repo_with_codeowners(tmp_path, "/app/ @app-team\n")
    rules = parse_codeowners(repo)
    for form in ("app/main.py", "./app/main.py", "/app/main.py", "app\\main.py"):
        assert owners_for(rules, form) == ("@app-team",), form
    assert owners_for(rules, "") == ()
    assert owners_for(rules, None) == ()


def test_an_uncompilable_pattern_is_dropped_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_OWNERSHIP_MAX_PATTERN_CHARS", "8")
    repo = _repo_with_codeowners(tmp_path, "/a/very/long/pattern/indeed/ @a\n/ok/ @b\n")
    rules = parse_codeowners(repo)
    assert [r.pattern for r in rules] == ["/ok/"]


# --- commit mining ----------------------------------------------------------


@_GIT
def test_mines_per_file_authorship_shares(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 100 * 86400
    alice = ("Alice Smith", "alice@corp.example")
    bob = ("Bob Jones", "bob@corp.example")
    for i in range(3):
        _commit(repo, {"app/a.py": f"v{i}"}, f"alice {i}", base + i * 86400, who=alice)
    _commit(repo, {"app/a.py": "v9"}, "bob once", base + 9 * 86400, who=bob)

    mined = mine_commit_ownership(repo, max_commits=50)
    assert mined is not None
    assert mined["schema"] == 1
    assert Path(mined["root"]).samefile(repo)
    rows = mined["files"]["app/a.py"]
    assert [r["author"] for r in rows] == ["Alice Smith", "Bob Jones"]
    assert rows[0]["commits"] == 3
    assert rows[0]["share"] == 0.75
    assert rows[1]["share"] == 0.25


@_GIT
def test_no_raw_address_reaches_a_mined_row(tmp_path):
    """The privacy contract. The email is the grouping key and nothing else; a
    committer whose display NAME is an address gets it masked too."""
    repo = tmp_path / "repo"
    _init(repo)
    now = int(time.time()) - 86400
    _commit(repo, {"a.py": "x"}, "one", now, who=("carol@secret.example", "carol@secret.example"))

    mined = mine_commit_ownership(repo, max_commits=10)
    blob = repr(mined)
    assert "carol@secret.example" not in blob
    assert "secret.example" not in blob
    assert mined["files"]["a.py"][0]["author"] == "carol@***"


@_GIT
def test_two_display_names_for_one_address_are_one_author(tmp_path):
    """The address is the key precisely so a rename does not split a person into
    two half-owners, neither of which clears the dominance floor."""
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 50 * 86400
    for i in range(3):
        _commit(repo, {"a.py": f"v{i}"}, f"c{i}", base + i * 86400, who=("Dana Old", "d@x.example"))
    for i in range(2):
        _commit(
            repo,
            {"a.py": f"w{i}"},
            f"d{i}",
            base + (5 + i) * 86400,
            who=("Dana New", "d@x.example"),
        )

    rows = mine_commit_ownership(repo, max_commits=50)["files"]["a.py"]
    assert len(rows) == 1
    assert rows[0]["commits"] == 5
    assert rows[0]["share"] == 1.0
    assert rows[0]["author"] == "Dana Old"  # the most-used name for that identity


@_GIT
def test_a_bulk_sweep_commit_confers_no_ownership(tmp_path):
    """Whoever ran the reformat did not thereby become the owner of every file it
    touched, so the sweep is skipped and the genuine author still wins."""
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 60 * 86400
    _commit(repo, {"app/a.py": "real"}, "real work", base, who=("Real Owner", "real@x.example"))
    sweep = {f"app/f{i}.py": "x" for i in range(40)}
    sweep["app/a.py"] = "reformatted"
    _commit(repo, sweep, "reformat everything", base + 86400, who=("Sweeper", "sweep@x.example"))

    mined = mine_commit_ownership(repo, max_commits=50, max_files_per_commit=30)
    assert [r["author"] for r in mined["files"]["app/a.py"]] == ["Real Owner"]
    assert "app/f0.py" not in mined["files"]


@_GIT
def test_a_deleted_file_has_no_owner(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 40 * 86400
    _commit(repo, {"gone.py": "x", "kept.py": "y"}, "seed", base)
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-qm", "remove gone", at=base + 86400)

    mined = mine_commit_ownership(repo, max_commits=50)
    assert "gone.py" not in mined["files"]
    assert "kept.py" in mined["files"]


@_GIT
def test_authors_per_file_and_file_count_are_bounded(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    base = int(time.time()) - 40 * 86400
    for i in range(4):
        _commit(
            repo,
            {"hot.py": f"v{i}", f"cold{i}.py": "x"},
            f"c{i}",
            base + i * 86400,
            who=(f"Dev {i}", f"dev{i}@x.example"),
        )

    mined = mine_commit_ownership(repo, max_commits=50, max_files=1, max_authors_per_file=2)
    assert list(mined["files"]) == ["hot.py"]  # the most-committed file kept
    assert len(mined["files"]["hot.py"]) == 2


@_GIT
def test_mining_from_a_subdir_keys_top_relative(tmp_path):
    repo = tmp_path / "mono"
    _init(repo)
    base = int(time.time()) - 30 * 86400
    _commit(repo, {"packages/a/x.py": "v"}, "seed", base)

    mined = mine_commit_ownership(repo / "packages" / "a", max_commits=50)
    assert Path(mined["root"]).samefile(repo)
    assert "packages/a/x.py" in mined["files"]


def test_mining_outside_a_git_tree_fails_open(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert mine_commit_ownership(plain) is None


# --- the combined resolver --------------------------------------------------


def _mined(rel: str, rows: list[dict]) -> dict:
    return {"schema": 1, "root": "/repo", "files": {rel: rows}}


def test_declared_owner_beats_the_mined_author(tmp_path):
    """A CODEOWNERS handle names a role and is what a team chose to publish; a
    mined name is an inference about a person. The declared one wins."""
    repo = _repo_with_codeowners(tmp_path, "/app/ @app-team\n")
    got = resolve_owners(
        "app/a.py",
        rules=parse_codeowners(repo),
        mined=_mined("app/a.py", [{"author": "Alice Smith", "commits": 9, "share": 0.9}]),
    )
    assert got == {"owners": ["@app-team"], "source": "codeowners"}


def test_falls_back_to_the_dominant_commit_author(tmp_path):
    got = resolve_owners(
        "app/a.py",
        rules=[],
        mined=_mined("app/a.py", [{"author": "Alice Smith", "commits": 9, "share": 0.9}]),
    )
    assert got == {"owners": ["Alice Smith"], "source": "commits", "share": 0.9}


def test_no_dominant_author_means_no_owner(tmp_path):
    """A flat distribution has no owner. Naming the top of it would dress a
    coin-flip as a fact."""
    mined = _mined(
        "app/a.py",
        [
            {"author": "Alice Smith", "commits": 4, "share": 0.4},
            {"author": "Bob Jones", "commits": 3, "share": 0.3},
        ],
    )
    assert resolve_owners("app/a.py", rules=[], mined=mined) == {}
    # The floor is a threshold, not a constant: lower it and the same data answers.
    assert resolve_owners("app/a.py", rules=[], mined=mined, min_share=0.35)["owners"] == [
        "Alice Smith"
    ]


def test_an_unassigning_rule_falls_through_to_the_mined_author(tmp_path):
    repo = _repo_with_codeowners(tmp_path, "* @fallback\n/vendor/\n")
    got = resolve_owners(
        "vendor/lib.py",
        rules=parse_codeowners(repo),
        mined=_mined("vendor/lib.py", [{"author": "Alice Smith", "commits": 9, "share": 0.9}]),
    )
    assert got == {"owners": ["Alice Smith"], "source": "commits", "share": 0.9}


def test_resolver_fails_open_on_missing_and_malformed_inputs():
    assert resolve_owners("app/a.py") == {}
    assert resolve_owners("app/a.py", rules=None, mined=None) == {}
    assert resolve_owners("app/a.py", rules=[], mined={"files": "not-a-dict"}) == {}
    assert resolve_owners("app/a.py", rules=[], mined={"files": {"app/a.py": "nope"}}) == {}
    assert resolve_owners("app/a.py", rules=[], mined=_mined("app/a.py", [{"author": "A"}])) == {}
    assert resolve_owners("app/a.py", rules=[], mined=_mined("app/a.py", ["junk"])) == {}
    # A boolean is an int in Python; it must not read as a share of 1.
    assert resolve_owners("app/a.py", rules=[], mined=_mined("a.py", [{"a": True}])) == {}


# --- kill switch ------------------------------------------------------------


@_GIT
def test_kill_switch_silences_every_entry_point(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"CODEOWNERS": "* @app-team\n", "app/a.py": "x"}, "seed", int(time.time()) - 100)
    rules = parse_codeowners(repo)
    assert rules  # the control: on by default

    monkeypatch.setenv("CHAMELEON_OWNERSHIP", "0")
    assert parse_codeowners(repo) == []
    assert mine_commit_ownership(repo) is None
    assert resolve_owners("app/a.py", rules=rules) == {}
    # The pure lookup still answers about the arguments it was handed: the switch
    # gates the feature's I/O and its answer surface, not a factual predicate.
    assert owners_for(rules, "app/a.py") == ("@app-team",)
