"""Multi-root Stop backstop tests (roadmap #3: coordinator-root dead spot).

stop_backstop fans the turn-end gate pipeline out over EVERY workspace root the
session touched, discovered from the per-file enforcement state (keyed by each
edited file's own workspace repo_id, not cwd). These pin the discovery grouping,
the per-workspace block, per-repo trust (never unioned), the model-spawn budget,
the only_files scoping, and the CHAMELEON_MULTIROOT_STOP kill switch.

Isolation: a real two-workspace coordinator layout under tmp_path, with
find_repo_root / _compute_repo_id / trust / suppression / plugin-data patched so
stop_backstop reaches real EnforcementState saved under each workspace's own
plugin-data dir.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "metrics-isolated"))
    # Multi-root is the default; make it explicit so an ambient env can't flip it.
    monkeypatch.delenv("CHAMELEON_MULTIROOT_STOP", raising=False)
    monkeypatch.setenv("CHAMELEON_ENFORCE", "1")


@pytest.fixture
def coord(tmp_path):
    """A coordinator with two profiled+trusted workspaces (web, api) sharing one
    session. Returns a namespace with roots, repo_ids, data dirs, file paths, and
    a mutable ``granted`` set the patched trust record honors per-root.

    find_repo_root maps a path to the workspace whose dir it lives under (else the
    coordinator); _compute_repo_id maps a root to a stable id. State for each
    workspace is saved under its own plugin-data dir, exactly as the per-edit
    hooks write it.
    """
    stack = ExitStack()
    session_id = "s-multi"

    coord_root = tmp_path / "mono"
    web = coord_root / "apps" / "web"
    api = coord_root / "services" / "api"
    roots = {"web": web, "api": api, "coord": coord_root}
    ids = {
        str(web.resolve()): "id_web",
        str(api.resolve()): "id_api",
        str(coord_root.resolve()): "id_coord",
    }

    for name, root in (("web", web), ("api", api)):
        pd = root / ".chameleon"
        pd.mkdir(parents=True, exist_ok=True)
        pd.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": "enforce", "stop_block_cap": 3}}), encoding="utf-8"
        )
        pd.joinpath("profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    web_file = str(web / "src" / "user.ts")
    api_file = str(api / "app" / "models.py")
    for f in (web_file, api_file):
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        Path(f).write_text("x = 1\n", encoding="utf-8")

    granted = {str(web.resolve()), str(api.resolve())}

    def _find_repo_root(p):
        rp = str(Path(p).resolve())
        if rp.startswith(str(web.resolve())):
            return web
        if rp.startswith(str(api.resolve())):
            return api
        if rp.startswith(str(coord_root.resolve())):
            return coord_root
        return None

    def _compute_repo_id(root):
        return ids.get(str(Path(root).resolve()), "id_unknown")

    rec = MagicMock()
    rec.grants_root.side_effect = lambda r: str(Path(r).resolve()) in granted
    rec.hash_for_root.return_value = ""

    from chameleon_mcp.profile import trust as _trust

    stack.enter_context(
        patch("chameleon_mcp.profile.loader.find_repo_root", side_effect=_find_repo_root)
    )
    stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", side_effect=_compute_repo_id))
    stack.enter_context(patch.object(_trust, "trust_state_for", return_value=rec))
    stack.enter_context(patch.object(_trust, "profile_diverged_from_grant", return_value=False))
    stack.enter_context(patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None))
    stack.enter_context(patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path))

    ns = MagicMock()
    ns.session_id = session_id
    ns.roots = roots
    ns.ids = ids
    ns.web_file = web_file
    ns.api_file = api_file
    ns.granted = granted
    ns.tmp = tmp_path
    ns.data = {
        "web": tmp_path / "id_web",
        "api": tmp_path / "id_api",
        "coord": tmp_path / "id_coord",
    }
    try:
        yield ns
    finally:
        stack.close()


def _arm(data_dir, session_id, file_path, *, level=2):
    st = EnforcementState()
    st.files[file_path] = FileState(level=level, blockable_unresolved=True)
    save_state(st, data_dir, session_id)


def _run_stop(coord, *, cwd=None, is_subagent=False, env=None, still_blockable=True):
    payload = {
        "session_id": coord.session_id,
        "cwd": str(cwd or coord.roots["coord"]),
        "hook_event_name": "SubagentStop" if is_subagent else "Stop",
        "stop_hook_active": False,
    }
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env or {}, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=still_blockable),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def test_coordinator_root_blocks_on_workspace_violation(coord):
    # THE headline: cwd is the profile-less coordinator (ungranted), an armed
    # violation lives in a workspace. Today's single-root path died at the trust
    # gate; the multi-root path must discover the workspace state and block.
    _arm(coord.data["web"], coord.session_id, coord.web_file)
    out = _run_stop(coord)
    assert out.get("decision") == "block"
    assert "user.ts" in out.get("reason", "")


def test_untrusted_workspace_is_not_gated(coord):
    # apps/api armed but NOT granted -> its violation must not block (per-repo
    # trust, never unioned) even though apps/web (granted) shares the session.
    coord.granted.discard(str(coord.roots["api"].resolve()))
    _arm(coord.data["api"], coord.session_id, coord.api_file)
    out = _run_stop(coord)
    assert out.get("decision") != "block"


def test_untrusted_excluded_trusted_still_blocks(coord):
    coord.granted.discard(str(coord.roots["api"].resolve()))
    _arm(coord.data["api"], coord.session_id, coord.api_file)
    _arm(coord.data["web"], coord.session_id, coord.web_file)
    out = _run_stop(coord)
    assert out.get("decision") == "block"
    assert "user.ts" in out.get("reason", "")  # trusted workspace surfaced
    assert "models.py" not in out.get("reason", "")  # untrusted never surfaced


def test_kill_switch_restores_single_root_dead_spot(coord):
    # With MULTIROOT_STOP=0, cwd=coordinator resolves to the coordinator root,
    # which is not granted -> today's dead spot (no block), proving the switch.
    _arm(coord.data["web"], coord.session_id, coord.web_file)
    out = _run_stop(coord, env={"CHAMELEON_MULTIROOT_STOP": "0"})
    assert out.get("decision") != "block"


def test_healed_workspace_does_not_block(coord):
    # An armed file that re-lints clean must not block (still_blockable False).
    _arm(coord.data["web"], coord.session_id, coord.web_file)
    out = _run_stop(coord, still_blockable=False)
    assert out.get("decision") != "block"


def test_enforce_off_env_does_not_block(coord):
    _arm(coord.data["web"], coord.session_id, coord.web_file)
    out = _run_stop(coord, env={"CHAMELEON_ENFORCE": "0"})
    assert out.get("decision") != "block"


def test_sibling_repo_edit_gated_from_other_cwd(coord):
    # cwd resolves to apps/web, but the armed violation is in services/api.
    # The sibling workspace must still be gated.
    _arm(coord.data["api"], coord.session_id, coord.api_file)
    out = _run_stop(coord, cwd=coord.roots["web"])
    assert out.get("decision") == "block"
    assert "models.py" in out.get("reason", "")


def test_discover_groups_by_per_file_workspace(coord):
    # A single shared state file whose entries span two workspaces regroups by
    # each file's own find_repo_root, yielding two RootWork with the right files.
    st = EnforcementState()
    st.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    st.files[coord.api_file] = FileState(level=0, blockable_unresolved=False)
    # Both entries live in ONE data dir (shared-repo_id topology).
    save_state(st, coord.data["web"], coord.session_id)

    from chameleon_mcp.hook_helper import _discover_stop_roots

    roots = _discover_stop_roots(coord.roots["coord"], coord.session_id)
    by_root = {str(g["ws_root"].resolve()): g for g in roots}
    assert str(coord.roots["web"].resolve()) in by_root
    assert str(coord.roots["api"].resolve()) in by_root
    assert coord.web_file in by_root[str(coord.roots["web"].resolve())]["files"]
    assert by_root[str(coord.roots["web"].resolve())]["has_armed"] is True
    # Armed-bearing root ranks first.
    assert roots[0]["has_armed"] is True


def test_unknown_session_id_does_not_glob_other_repos(coord):
    # A degenerate empty session_id collapses to the "unknown" marker; discovery
    # must NOT glob (which would pull unrelated repos in) -- cwd root only.
    st = EnforcementState()
    st.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    save_state(st, coord.tmp / "id_web", "")  # marker "unknown"

    from chameleon_mcp.hook_helper import _discover_stop_roots

    roots = _discover_stop_roots(coord.roots["coord"], None)
    # Only the cwd root (coordinator) is present; the web state file is not globbed.
    assert all(str(g["ws_root"].resolve()) == str(coord.roots["coord"].resolve()) for g in roots)


def test_only_files_scopes_stop_gates(coord):
    # _stop_gates(only_files={api_file}) must re-lint ONLY that file even when the
    # loaded state carries both workspaces' files.
    st = EnforcementState()
    st.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    st.files[coord.api_file] = FileState(level=2, blockable_unresolved=True)
    save_state(st, coord.data["api"], coord.session_id)

    seen = []

    def _fake_relint(repo_root, path, **kw):
        seen.append(path)
        return True

    from chameleon_mcp import hook_helper

    with patch.object(hook_helper, "_stop_file_still_blockable", side_effect=_fake_relint):
        out = hook_helper._stop_gates(
            payload={},
            repo_root=coord.roots["api"],
            repo_id="id_api",
            session_id=coord.session_id,
            is_subagent=False,
            repo_data=coord.data["api"],
            daemon_state={"available": True},
            only_files={coord.api_file},
        )
    assert seen == [coord.api_file]  # web_file was scoped out
    assert out.get("decision") == "block"


def test_allow_model_spawn_false_suppresses_every_reviewer(coord):
    # A non-first root (allow_model_spawn=False) must skip ALL three claude -p
    # spawn sites -- correctness route/gate, multi-lens, AND the standalone
    # duplication gate -- so the whole Stop pays for at most one reviewer.
    # Nothing armed here, so the pass reaches the advisory pipeline.
    st = EnforcementState()
    st.files[coord.api_file] = FileState(level=0, blockable_unresolved=False)
    save_state(st, coord.data["api"], coord.session_id)

    from chameleon_mcp import hook_helper

    with (
        patch.object(hook_helper, "_correctness_judge_route") as route,
        patch.object(hook_helper, "_correctness_judge_gate") as judge,
        patch.object(hook_helper, "_multi_lens_review_lines") as lens,
        patch.object(hook_helper, "_duplication_advisory_lines") as dup,
    ):
        hook_helper._stop_gates(
            payload={},
            repo_root=coord.roots["api"],
            repo_id="id_api",
            session_id=coord.session_id,
            is_subagent=False,
            repo_data=coord.data["api"],
            daemon_state={"available": True},
            only_files=set(),
            allow_model_spawn=False,
        )
    route.assert_not_called()
    judge.assert_not_called()
    lens.assert_not_called()
    dup.assert_not_called()


def test_shared_cap_is_per_workspace_not_starved(coord):
    # Two workspaces (web, api) both armed, sharing ONE repo_data (the api dir),
    # both blockable. The anti-loop cap must be charged PER WORKSPACE: after web
    # exhausts its 3 blocks, api must STILL block on its own budget rather than
    # being starved by web having consumed a shared counter.
    # Both files recorded in one shared state file (shared-repo_id topology).
    st = EnforcementState()
    st.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    st.files[coord.api_file] = FileState(level=2, blockable_unresolved=True)
    save_state(st, coord.data["web"], coord.session_id)

    # Make BOTH ws_roots resolve their state to the SAME shared data dir by
    # pointing api's repo_id at web's dir name. Simplest: run discovery-driven
    # stops and count per-file blocks over many turns.
    from chameleon_mcp.hook_helper import _discover_stop_roots

    # Sanity: discovery groups both workspaces off the one shared state file.
    roots = _discover_stop_roots(coord.roots["coord"], coord.session_id)
    ws_paths = {str(g["ws_root"].resolve()) for g in roots}
    assert str(coord.roots["web"].resolve()) in ws_paths
    assert str(coord.roots["api"].resolve()) in ws_paths

    blocked = []
    for _ in range(8):
        out = _run_stop(coord)
        r = out.get("reason", "") if out.get("decision") == "block" else ""
        if "user.ts" in r:
            blocked.append("web")
        elif "models.py" in r:
            blocked.append("api")
        else:
            blocked.append("none")
    # Each workspace gets its own cap of 3; neither is starved by the other.
    assert blocked.count("web") == 3
    assert blocked.count("api") == 3


def test_per_root_block_counter_survives_state_roundtrip():
    # The new per-workspace counter must round-trip and be present + absent-safe
    # (old state files without the field load as an empty map).
    st = EnforcementState()
    st.stop_hook_blocks_by_root = {"wshash": 2}
    st.stop_hook_blocks = 1
    d = st.to_dict()
    assert d["stop_hook_blocks_by_root"] == {"wshash": 2}
    back = EnforcementState.from_dict(d)
    assert back.stop_hook_blocks_by_root == {"wshash": 2}
    assert back.stop_hook_blocks == 1
    # Legacy file (field absent) -> empty map, scalar preserved.
    legacy = EnforcementState.from_dict({"stop_hook_blocks": 3})
    assert legacy.stop_hook_blocks_by_root == {}
    assert legacy.stop_hook_blocks == 3


def test_effective_stop_blocks_reconciles_scalar_and_per_root():
    # The block budget is the MAX of the legacy scalar and the per-workspace map,
    # so a workspace capped under either representation stays capped and a
    # single<->multi mode flip cannot re-arm a spent cap.
    from chameleon_mcp.enforcement import EnforcementState
    from chameleon_mcp.hook_helper import _effective_stop_blocks

    st = EnforcementState()
    st.stop_hook_blocks = 3  # legacy / prior single-root session
    st.stop_hook_blocks_by_root = {"wsA": 1}
    assert _effective_stop_blocks(st, "wsA") == 3  # scalar dominates
    st.stop_hook_blocks = 0
    st.stop_hook_blocks_by_root = {"wsA": 2}
    assert _effective_stop_blocks(st, "wsA") == 2  # per-root dominates
    assert _effective_stop_blocks(st, "unknown") == 0
    # Corrupt values coerce to 0 rather than raising.
    st.stop_hook_blocks_by_root = {"wsA": "x"}
    assert _effective_stop_blocks(st, "wsA") == 0


def test_legacy_scalar_cap_survives_multiroot_read(coord):
    # A workspace that spent its cap under the legacy scalar (a prior single-root
    # phase / an old state file) must stay capped when a later multi-root Stop
    # reads the per-workspace counter -- the reconciliation prevents a mode flip
    # from re-arming the spent cap.
    from chameleon_mcp.enforcement import EnforcementState, save_state

    st = EnforcementState()
    st.stop_hook_blocks = 3  # cap spent under the scalar
    st.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    save_state(st, coord.data["web"], coord.session_id)
    out = _run_stop(coord)  # multi-root discovery (cwd = coordinator)
    assert out.get("decision") != "block"  # cap_reached via the reconciled read


def test_corrupt_block_counter_fails_open_not_crash(tmp_path):
    # A committed/tampered state file with a non-numeric or negative per-workspace
    # count must fail open (drop the entry) rather than raise -- load_state is
    # contractually fail-open, and a bare int("x") would crash the Stop hook.
    import json as _json

    from chameleon_mcp.enforcement import EnforcementState, _state_path, load_state

    # from_dict drops the bad entries, keeps the good ones.
    st = EnforcementState.from_dict(
        {"stop_hook_blocks_by_root": {"good": 2, "bad": "notanumber", "neg": -1, "coerce": "3"}}
    )
    assert st.stop_hook_blocks_by_root == {"good": 2, "coerce": 3}
    # A non-dict value for the field also fails open.
    assert (
        EnforcementState.from_dict({"stop_hook_blocks_by_root": "garbage"}).stop_hook_blocks_by_root
        == {}
    )

    # load_state on a torn file with a bad value returns a fresh state, no raise.
    p = _state_path(tmp_path, "sid")
    p.write_text(_json.dumps({"stop_hook_blocks_by_root": {"w": "notanumber"}}), encoding="utf-8")
    st2 = load_state(tmp_path, "sid")
    assert st2.stop_hook_blocks_by_root == {}


def test_discovery_keys_by_repo_data_and_ws_root(coord):
    # A repo_id shift writes the SAME ws_root's armed state under TWO repo_data
    # dirs. Discovery must produce TWO groups (one per repo_data) so BOTH state
    # files' armed entries are gated, not a lossy merge that keeps one.
    web_file_2 = str(coord.roots["web"] / "src" / "other.ts")
    Path(web_file_2).write_text("y = 2\n", encoding="utf-8")
    # Same ws_root (web), two different repo_data dirs (id_web and id_coord).
    st1 = EnforcementState()
    st1.files[coord.web_file] = FileState(level=2, blockable_unresolved=True)
    save_state(st1, coord.tmp / "id_web", coord.session_id)
    st2 = EnforcementState()
    st2.files[web_file_2] = FileState(level=2, blockable_unresolved=True)
    save_state(st2, coord.tmp / "id_coord", coord.session_id)

    from chameleon_mcp.hook_helper import _discover_stop_roots

    roots = _discover_stop_roots(coord.roots["coord"], coord.session_id)
    web_groups = [
        g for g in roots if str(g["ws_root"].resolve()) == str(coord.roots["web"].resolve())
    ]
    # Two groups for web: one per repo_data, so neither state file's armed entry is dropped.
    datas = {str(g["repo_data"]) for g in web_groups}
    assert str(coord.tmp / "id_web") in datas
    assert str(coord.tmp / "id_coord") in datas
