"""Cross-root resurface-discard fix (appendix D section 3-4, phase-4 task 2).

review_ledger.compute_resurface only COMPUTES resurface candidates; it never
writes the terminal ``resurfaced`` transition. hook_helper.stop_backstop
commits ``mark_resurfaced`` for a root's candidates only AFTER its multi-root
loop confirms the loop did not later discard that root's output for a block.
These tests drive the real ``stop_backstop`` -> ``stop_gates`` chain (not
review_ledger's API in isolation) over a real two-workspace coordinator,
mirroring test_stop_multiroot.py's harness shape: real EnforcementState,
patched find_repo_root/trust/suppression/plugin-data.

The headline repro (appendix D section 3): root A (web) heals its armed
violation and has an unaddressed HIGH finding eligible to resurface; root B
(api) still blocks. Before this fix, A's resurface line packed into its own
output AND the ledger row was flipped to the terminal ``resurfaced`` status
before B's block was known -- then the whole Stop discarded every
non-blocking root's advisories for B's block, silently burning A's one-shot
resurface forever. After the fix, A's candidate stays ``pending``.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "metrics-isolated"))
    monkeypatch.delenv("CHAMELEON_MULTIROOT_STOP", raising=False)
    monkeypatch.setenv("CHAMELEON_ENFORCE", "1")


@pytest.fixture
def coord(tmp_path):
    """A coordinator with two profiled+trusted workspaces (web, api) sharing one
    session -- the same shape as test_stop_multiroot.py's ``coord`` fixture."""
    stack = ExitStack()
    session_id = "s-crossroot"

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


def _run_stop(coord, relint, *, cwd=None, env=None):
    """Drive the real stop_backstop, with per-path control over the live
    re-lint verdict (``relint(repo_root, path, **kw) -> bool``) so root A and
    root B can heal/stay-blocked independently -- test_stop_multiroot.py's own
    ``_run_stop`` only supports one uniform verdict for every file."""
    payload = {
        "session_id": coord.session_id,
        "cwd": str(cwd or coord.roots["coord"]),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env or {}, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", side_effect=relint),
        patch("chameleon_mcp.stop.scheduler.launch_job", return_value=False),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def _record_high_finding(repo_id: str, ws_root, *, file_rel: str, claim: str):
    from chameleon_mcp import review_ledger
    from chameleon_mcp.core.finding import Finding, compute_match_key

    finding = Finding(
        id=compute_match_key(claim, file_rel, "correctness"),
        kind="correctness",
        severity="high",
        confidence=0.9,
        file=file_rel,
        span=(1, 1),
        claim=claim,
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    review_ledger.record_findings(repo_id, str(ws_root), [finding])
    return finding


def test_resurface_discarded_when_a_later_root_blocks(coord):
    """The appendix D section 3 repro: A heals + has a resurface candidate,
    B still blocks. The Stop must emit ONLY B's block reason, and A's
    finding must stay pending -- mark_resurfaced must NOT have been called
    for it.

    Hardened so this cannot pass for the trivial reason "A never ran": a spy
    on compute_resurface proves root A WAS gated and DID compute A's candidate
    (so the discard is a real deferred-commit, not a skipped root)."""
    from chameleon_mcp import review_ledger

    _arm(coord.data["web"], coord.session_id, coord.web_file)
    _arm(coord.data["api"], coord.session_id, coord.api_file)

    web_repo_id = coord.ids[str(coord.roots["web"].resolve())]
    finding = _record_high_finding(
        web_repo_id, coord.roots["web"], file_rel="src/user.ts", claim="root A unaddressed bug"
    )

    def _relint(repo_root, path, **kw):
        # web (root A) heals; api (root B) stays blockable.
        return path == coord.api_file

    real_compute = review_ledger.compute_resurface
    computed_for: list[str] = []

    def _spy_compute(repo_id, ws_root):
        result = real_compute(repo_id, ws_root)
        if result.match_keys:
            computed_for.append(repo_id)
        return result

    with patch.object(review_ledger, "compute_resurface", side_effect=_spy_compute):
        out = _run_stop(coord, _relint)

    assert out.get("decision") == "block"
    assert "models.py" in out.get("reason", "")
    # A's resurface line must not have leaked into the block emission either.
    assert "unaddressed high-severity" not in out.get("reason", "")

    # Root A really was gated and its candidate really was computed -- the
    # finding stays pending because the COMMIT was deferred and then dropped,
    # not because A never ran.
    assert web_repo_id in computed_for
    rows = review_ledger._read_findings_rows(web_repo_id)
    assert rows[finding.match_key]["status"] == "pending"


def test_resurface_committed_when_no_later_root_blocks(coord):
    """Positive control: same setup, but B also heals (nothing blocks). A's
    finding IS marked resurfaced exactly once, and its line reaches the
    merged advisory output."""
    from chameleon_mcp import review_ledger

    _arm(coord.data["web"], coord.session_id, coord.web_file)
    _arm(coord.data["api"], coord.session_id, coord.api_file)

    web_repo_id = coord.ids[str(coord.roots["web"].resolve())]
    finding = _record_high_finding(
        web_repo_id, coord.roots["web"], file_rel="src/user.ts", claim="root A unaddressed bug"
    )

    def _relint(repo_root, path, **kw):
        return False  # both roots heal -- nothing blocks

    out = _run_stop(coord, _relint)

    assert out.get("decision") != "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "unaddressed high-severity" in ctx
    assert "src/user.ts:1" in ctx
    # No extra top-level Stop header is prepended (part-2 fix): the emission is
    # the pre-Task-2 additive join of already-[🦎]-headered blocks, so it
    # starts with a <chameleon-context> wrapper, not a bare top header line.
    assert ctx.lstrip().startswith("<chameleon-context>")
    assert "turn-end review" not in ctx  # the removed added header never appears

    rows = review_ledger._read_findings_rows(web_repo_id)
    assert rows[finding.match_key]["status"] == "resurfaced"


def test_ceiling_packs_resurface_over_lower_priority_advisory(coord):
    """Ranked packing: a Stop-emission ceiling too small for both the
    resurface item (priority 1) and the test-run-reminder advisory
    (priority 4, fired here by the coordinator's own edited-source-file
    state) packs the higher-priority resurface line and OMITS the reminder
    whole -- never a crash, never a truncated fragment."""
    from chameleon_mcp import review_ledger
    from chameleon_mcp.core.budget import approx_tokens

    _arm(coord.data["web"], coord.session_id, coord.web_file)

    web_repo_id = coord.ids[str(coord.roots["web"].resolve())]
    _record_high_finding(
        web_repo_id, coord.roots["web"], file_rel="src/user.ts", claim="ceiling test bug"
    )

    # Compute the EXACT ceiling that fits the resurface block alone, leaving
    # zero headroom for anything else. header=None on the Stop path adds NO
    # top-level header/disclaimer (each block keeps its own header), so the
    # ceiling seeds from 0 and equals the resurface block's own cost.
    # compute_resurface is pure, so this dry call does not disturb the real
    # one stop_backstop makes below.
    pre = review_ledger.compute_resurface(web_repo_id, str(coord.roots["web"]))
    resurface_block = "<chameleon-context>\n" + "\n".join(pre.lines) + "\n</chameleon-context>"
    tight_ceiling = approx_tokens(resurface_block)

    def _relint(repo_root, path, **kw):
        return False  # heals -- falls through to the advisory pipeline

    out = _run_stop(
        coord,
        _relint,
        cwd=coord.roots["web"],
        env={"CHAMELEON_STOP_RENDER_TOKEN_CEILING": str(tight_ceiling)},
    )

    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "unaddressed high-severity" in ctx  # higher-priority item packed
    assert "no passing test run this turn" not in ctx  # lower-priority item omitted whole
    assert "no passing test run this t" not in ctx  # not even a truncated fragment

    # The omitted-for-space reminder carries no findings of its own, but the
    # packed resurface item's candidate must still be left uncommitted here
    # (this test only exercises the ceiling; the commit path is exercised by
    # the two tests above) -- still pending is the ONLY correct state before
    # stop_backstop's post-loop commit runs, and it has already run by the
    # time we read this, so assert what stop_backstop actually committed:
    # the item that survived the ceiling.
    rows = review_ledger._read_findings_rows(web_repo_id)
    for row in rows.values():
        if row.get("claim") == "ceiling test bug":
            assert row["status"] == "resurfaced"


def test_assembler_exception_falls_back_to_additive_join_not_crash(coord):
    """A bug in the ranked packer must not erase advisories this pass already
    computed -- stop_gates falls back to the pre-ranked additive join rather
    than a bare crash or an empty {} that silently drops real content. No
    resurface commit rides this fallback path (the caller sees no
    ``_resurface_committed_keys``), so the candidate stays pending -- exactly
    like an item the ranked packer itself would have omitted for space."""
    from chameleon_mcp import review_ledger

    _arm(coord.data["web"], coord.session_id, coord.web_file)
    web_repo_id = coord.ids[str(coord.roots["web"].resolve())]
    finding = _record_high_finding(
        web_repo_id, coord.roots["web"], file_rel="src/user.ts", claim="fallback path bug"
    )

    def _relint(repo_root, path, **kw):
        return False  # heals -- reaches the advisory pipeline

    def _boom(*a, **k):
        raise RuntimeError("packer exploded")

    payload = {
        "session_id": coord.session_id,
        "cwd": str(coord.roots["web"]),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", side_effect=_relint),
        patch("chameleon_mcp.stop.scheduler.launch_job", return_value=False),
        patch("chameleon_mcp.stop.assemble.assemble_stop_context", side_effect=_boom),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    result = json.loads(s) if s else {}

    ctx = (result.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "unaddressed high-severity" in ctx  # fallback still surfaces the content

    rows = review_ledger._read_findings_rows(web_repo_id)
    assert rows[finding.match_key]["status"] == "pending"  # never committed on this path
