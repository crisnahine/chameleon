"""Regression tests for the 2026-06-21 silent-failure audit fixes.

Each test pins a confirmed finding so the silent failure cannot return:

- Findings 1/2/3: the hook enforcement gates read the MAIN worktree's profile in
  a linked worktree (`_enf_profile_dir`), so deny/block/backstop are not silently
  off while trust reads "trusted".
- Finding 6: `_pick_ancestor_or_freshest` returns the freshest sibling clone on a
  descendant-count tie, not the shorter path.
- Findings 7/8: `_normalize_git_url` collapses ssh/https on any host and strips
  the port (IPv6-safe).
- Finding 5: `find_repo_root_with_refusal` does not cache a no-marker result and
  re-stats the key dir, so an out-of-band `.chameleon` self-heals.
- Findings 4/11: a malformed config records an observable degraded check-event
  while staying fail-open.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import hook_helper, index_db, repo_id
from chameleon_mcp.enforcement_calibration import active_block_rules
from chameleon_mcp.profile import loader
from chameleon_mcp.profile.config import load_config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    loader._REPO_ROOT_CACHE.clear()
    yield
    loader._REPO_ROOT_CACHE.clear()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_enforce_main(root: Path) -> Path:
    root.mkdir(parents=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@t.t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "app").mkdir()
    (root / "app" / "m.rb").write_text("class M; end\n", encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    cham = root / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(
        '{"schema_version": 8, "language": "ruby"}', encoding="utf-8"
    )
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
    # enforce mode + the language-independent secret rule active
    (cham / "config.json").write_text('{"enforcement": {"mode": "enforce"}}', encoding="utf-8")
    (cham / "enforcement.json").write_text(
        json.dumps({"block_rules": {"secret-detected-in-content": {"active": True}}}),
        encoding="utf-8",
    )
    return root


class TestWorktreeEnforcementReadsResolveToMain:
    """Findings 1/2/3: enforcement gate reads must see the main profile in a worktree."""

    def test_enf_profile_dir_resolves_worktree_to_main(self, tmp_path):
        main = _make_enforce_main(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        assert hook_helper._enf_profile_dir(wt) == main / ".chameleon"
        # identity off the worktree path
        assert hook_helper._enf_profile_dir(main) == main / ".chameleon"

    def test_active_block_rules_nonempty_via_resolved_dir_in_worktree(self, tmp_path):
        main = _make_enforce_main(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # the bug: the RAW worktree path has no enforcement.json -> empty set
        assert active_block_rules(wt / ".chameleon") == set()
        # the fix: through _enf_profile_dir the gate sees the main's active rules
        assert active_block_rules(hook_helper._enf_profile_dir(wt)) == {
            "secret-detected-in-content"
        }

    def test_enforce_mode_seen_via_resolved_dir_in_worktree(self, tmp_path):
        main = _make_enforce_main(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)
        # raw worktree path -> missing config -> default "shadow" (the bug)
        assert load_config(wt / ".chameleon").enforcement.mode == "shadow"
        # resolved -> the real "enforce"
        assert load_config(hook_helper._enf_profile_dir(wt)).enforcement.mode == "enforce"


class TestSiblingClonePicksFreshest:
    """Finding 6: a descendant-count tie keeps the freshest (first) candidate."""

    def test_nested_returns_ancestor_regardless_of_order(self, tmp_path):
        anc = tmp_path / "repo"
        desc = anc / "workspaces" / "pkg"
        desc.mkdir(parents=True)
        # ancestor wins by descendant count whether it is first or last
        assert index_db._pick_ancestor_or_freshest([str(anc), str(desc)]) == str(anc)
        assert index_db._pick_ancestor_or_freshest([str(desc), str(anc)]) == str(anc)

    def test_siblings_return_freshest_first_not_shortest_path(self, tmp_path):
        # two non-nested clones (no ancestor); candidates arrive freshest-first.
        fresh_long = tmp_path / "work" / "project-clone"
        old_short = tmp_path / "a"
        fresh_long.mkdir(parents=True)
        old_short.mkdir()
        # freshest (first) wins, NOT the shorter path (the pre-fix bug returned old_short)
        assert index_db._pick_ancestor_or_freshest([str(fresh_long), str(old_short)]) == str(
            fresh_long
        )


class TestRepoIdNormalization:
    """Finding 8: an explicit port on a well-known host is stripped, IPv6-safe.

    (Finding 7 -- forcing https for ALL hosts -- was deliberately dropped: it
    would change repo_id for every self-hosted-ssh remote, a broad migration not
    worth a niche dual-clone benefit. Self-hosted hosts keep their scheme.)
    """

    def test_well_known_host_port_stripped_and_lowercased(self, tmp_path):
        a = repo_id._normalize_git_url("ssh://git@github.com:22/Acme/Web.git")
        b = repo_id._normalize_git_url("https://github.com:443/Acme/Web")
        c = repo_id._normalize_git_url("git@github.com:Acme/Web.git")
        assert a == b == c == "https://github.com/acme/web"

    def test_self_hosted_scheme_preserved(self, tmp_path):
        # finding 7 dropped: a self-hosted host keeps its scheme (no collapse).
        assert (
            repo_id._normalize_git_url("https://git.company.io/team/api")
            == "https://git.company.io/team/api"
        )
        # ssh stays ssh (scheme not forced for unknown hosts)
        assert repo_id._normalize_git_url("ssh://git.company.io/team/api").startswith("ssh://")

    def test_ipv6_literal_not_corrupted(self, tmp_path):
        assert repo_id._strip_port("[::1]:22") == "[::1]"
        assert repo_id._strip_port("[::1]") == "[::1]"
        assert repo_id._strip_port("github.com:22") == "github.com"
        assert repo_id._strip_port("github.com") == "github.com"
        # the port-strip used for host matching never corrupts the bracketed
        # literal; the unknown host keeps its scheme and explicit port.
        assert repo_id._normalize_git_url("ssh://git@[::1]:22/x/y") == "ssh://[::1]:22/x/y"


class TestRepoRootCacheFreshness:
    """Finding 5: no-marker results are not cached; key-dir mtime self-heals."""

    def test_out_of_band_chameleon_is_seen_after_negative(self, tmp_path):
        sub = tmp_path / "proj" / "src"
        sub.mkdir(parents=True)
        # no marker anywhere up to root -> (None, None), NOT cached
        root, reason = loader.find_repo_root_with_refusal(sub / "f.rb")
        assert root is None
        # a profile created out-of-band must now be seen (was masked when cached)
        (tmp_path / "proj" / ".chameleon").mkdir()
        root2, _ = loader.find_repo_root_with_refusal(sub / "f.rb")
        assert root2 == (tmp_path / "proj").resolve()


class TestConfigMalformedObservable:
    """Findings 4/11: a malformed config records a degraded check-event, fail-open."""

    def test_note_emits_check_event_for_config_error(self, monkeypatch):
        from chameleon_mcp.profile.config import ChameleonConfigError

        events = []
        monkeypatch.setattr(
            hook_helper,
            "_emit_check_event",
            lambda *a, **k: events.append((a, k)),
        )
        # returns True for a config error (so the caller can surface it at the
        # edit surface), and records the check-event
        assert (
            hook_helper._note_if_config_malformed(
                ChameleonConfigError("bad mode"), "rid", "sid", "pretool_secret_deny"
            )
            is True
        )
        assert len(events) == 1
        # never raises, returns False, and ignores non-config exceptions
        assert (
            hook_helper._note_if_config_malformed(ValueError("other"), "rid", "sid", "x") is False
        )
        assert len(events) == 1

    def test_preflight_surfaces_malformed_config_banner_at_edit_surface(self):
        # When a deny gate swallows a torn config.json, the per-edit block must
        # carry a visible degraded banner (not just an off-surface check-event),
        # and it stays fail-open. Guard the wiring + the banner text.
        import inspect

        src = inspect.getsource(hook_helper.preflight_and_advise)
        # the deny-gate catch captures the malformed-config signal into the flag
        assert "_cfg_malformed = (" in src
        assert '"pretool_secret_deny",' in src
        assert "or _cfg_malformed" in src
        # and the advisory block surfaces it
        assert "if _cfg_malformed:" in src
        assert "block += _CONFIG_MALFORMED_BANNER" in src
        assert "Enforcement degraded" in hook_helper._CONFIG_MALFORMED_BANNER
        assert "OFF for this edit" in hook_helper._CONFIG_MALFORMED_BANNER

    def test_cfg_malformed_flag_initialized_before_setup_try(self):
        # Regression: _cfg_malformed must be bound OUTSIDE the setup try (before
        # find_repo_root / _compute_repo_id, which can raise), or the deny-gate
        # except handlers hit `... or _cfg_malformed` on an unbound name and the
        # whole hook fails to {} (UnboundLocalError).
        import inspect

        src = inspect.getsource(hook_helper.preflight_and_advise)
        init_at = src.index("_cfg_malformed = False")
        try_at = src.index("from chameleon_mcp.profile.loader import find_repo_root")
        assert init_at < try_at, "_cfg_malformed must be initialized before the setup try"

    def test_no_archetype_path_surfaces_banner_when_config_malformed(self):
        # The no-archetype early-return (a new .env etc., the prime leak target)
        # must also emit the degraded banner when the deny was skipped fail-open,
        # not a bare {}.
        import inspect

        src = inspect.getsource(hook_helper.preflight_and_advise)
        na = src.index("if not archetype_name:")
        tail = src[na : na + 900]
        assert "if _cfg_malformed:" in tail
        assert "_CONFIG_MALFORMED_BANNER" in tail
