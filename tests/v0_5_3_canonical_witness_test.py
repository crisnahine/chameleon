"""Regression test for v0.5.3 Bug A — silent empty `get_canonical_excerpt`
when the archetype exists in archetypes.json but its witness was dropped
at bootstrap time.

Background
----------
v0.5.2 Bug 5 added a typed `failed` envelope for the path-shape vs
repo_id-shape mix-up. The "valid repo, valid archetype name, but the
canonical witness was rejected by the bootstrap scanner" path stayed
silent — `get_canonical_excerpt` returned

    {"content": "", "witness_path": null, "truncated": false,
     "sha_hint": null}

with no `status` field, indistinguishable from "the repo I/O blew up
mid-load" or "I forgot to bootstrap this repo." Confirmed on mastodon
(`class`), excalidraw (`cluster-f5192077`), plane (`cluster-000c659d`).

Fix
---
Return a typed `no_witness` envelope when the archetype name is in
archetypes.json but canonicals.json has no usable entry for it.
Promote "unknown archetype name" to a distinct `failed (archetype not
found)` envelope; leave the existing `failed (repo_id not found)`
envelope alone for unresolvable repos.

Backward compat
---------------
The legacy `content / witness_path / truncated / sha_hint` keys stay
in every envelope shape, just `null` / `false` when not applicable, so
callers reading by name don't crash.

Run
---
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_3_canonical_witness_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Isolate plugin data so trust grants we make below don't leak into the
# rest of the test suite. Mirrors v0_5_2_tools_test.py's setup.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_3_canon_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA


PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# Eager imports — surface syntax errors before fixtures are set up.
from chameleon_mcp.tools import (  # noqa: E402
    _compute_repo_id,
    bootstrap_repo,
    get_canonical_excerpt,
    trust_profile,
)


def _make_minimal_ts_repo(name: str) -> Path:
    """Synthetic TS repo with enough files to bootstrap and produce at
    least one archetype. Returns the absolute path.
    """
    root = Path(tempfile.mkdtemp(prefix=f"v053_{name}_"))
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    src = root / "src"
    src.mkdir()
    for i in range(6):
        (src / f"r{i}.ts").write_text(
            f"export class R{i} {{ get() {{ return {i}; }} }}\n"
        )
    return root


def _drop_canonical_for(repo: Path, archetype_name: str) -> None:
    """Simulate bootstrap-time witness rejection: remove the archetype's
    entry from canonicals.json post-bootstrap.

    Generation counters across the 4 artifacts stay equal (we only edit
    the inner `canonicals` dict), so loader.py's cross-file generation
    check still passes. The COMMITTED sentinel is untouched because it
    lives in the .chameleon/ dir, not inside any artifact JSON.
    """
    cpath = repo / ".chameleon" / "canonicals.json"
    canonicals = json.loads(cpath.read_text(encoding="utf-8"))
    canonicals["canonicals"].pop(archetype_name, None)
    cpath.write_text(json.dumps(canonicals, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture: one bootstrapped TS repo, two archetype names — one keeps its
# witness, the other gets its witness dropped post-bootstrap.
# ---------------------------------------------------------------------------
repo = _make_minimal_ts_repo("witness-envelope")
try:
    section("Fixture: bootstrap + simulate witness rejection")

    # Bootstrap so the .chameleon/ profile exists.
    b = bootstrap_repo(str(repo))["data"]
    t(
        "bootstrap_repo succeeds (fixture precondition)",
        b.get("status") == "success",
        f"got {b}",
    )

    archetypes_json = json.loads(
        (repo / ".chameleon" / "archetypes.json").read_text(encoding="utf-8")
    )
    canonicals_json = json.loads(
        (repo / ".chameleon" / "canonicals.json").read_text(encoding="utf-8")
    )
    arch_names = list(archetypes_json.get("archetypes", {}).keys())
    t(
        "bootstrap produced at least one archetype (fixture precondition)",
        len(arch_names) >= 1,
        f"got archetypes={arch_names}",
    )

    # The synthetic TS files all share one signature so the bootstrap
    # may collapse them into a single archetype. That's fine: we drop
    # the witness for one name, and use the same name for the
    # `no_witness` test. For the happy-path regression test we
    # re-bootstrap a fresh repo (below) so the witness is intact.
    arch_no_witness = arch_names[0]
    # Sanity-check: before we drop it, the archetype HAS a witness.
    # Verify-before: archetype `arch_no_witness` carries a witness path
    # populated by the bootstrap scanner.
    pre = canonicals_json["canonicals"].get(arch_no_witness, [])
    t(
        "fixture: archetype has a witness BEFORE drop (verify-before)",
        bool(pre) and bool(pre[0].get("witness", {}).get("path")),
        f"got pre={pre}",
    )

    # Drop the witness, simulating "bootstrap scanner rejected all
    # candidates" (secrets, length, confidence).
    _drop_canonical_for(repo, arch_no_witness)

    # Verify-after: canonicals.json no longer has an entry for
    # `arch_no_witness`, but archetypes.json still does.
    post_canonicals = json.loads(
        (repo / ".chameleon" / "canonicals.json").read_text(encoding="utf-8")
    )
    post_archetypes = json.loads(
        (repo / ".chameleon" / "archetypes.json").read_text(encoding="utf-8")
    )
    t(
        "fixture: archetype still in archetypes.json after drop",
        arch_no_witness in post_archetypes.get("archetypes", {}),
        f"got archetypes keys={list(post_archetypes.get('archetypes', {}).keys())}",
    )
    t(
        "fixture: archetype dropped from canonicals.json (verify-after)",
        arch_no_witness not in post_canonicals.get("canonicals", {}),
        f"got canonicals keys={list(post_canonicals.get('canonicals', {}).keys())}",
    )

    # ---------------------------------------------------------------
    # Case 1 — valid repo + valid archetype name + dropped witness:
    # expect `status: "no_witness"`.
    # ---------------------------------------------------------------
    section("Case 1 — dropped witness returns status: no_witness")

    # Verify-before (pre-fix): `get_canonical_excerpt` returned
    # `{"content": "", "witness_path": null, "truncated": false,
    # "sha_hint": null}` with NO `status` field — callers couldn't tell
    # this apart from an I/O failure.
    # Verify-after: typed `no_witness` envelope with reason + identity
    # echo (archetype_name, repo_id) + legacy fields nulled out.
    r = get_canonical_excerpt(str(repo), arch_no_witness)["data"]

    t(
        "Case 1: status is 'no_witness' (verify-after; pre-fix had no status)",
        r.get("status") == "no_witness",
        f"got {r}",
    )
    t(
        "Case 1: reason mentions confidence threshold / secrets",
        isinstance(r.get("reason"), str)
        and "confidence threshold" in r["reason"]
        and "secrets" in r["reason"],
        f"got reason={r.get('reason')!r}",
    )
    t(
        "Case 1: archetype_name echoed back for caller identity",
        r.get("archetype_name") == arch_no_witness,
        f"got archetype_name={r.get('archetype_name')!r}",
    )
    repo_id = _compute_repo_id(repo.resolve())
    t(
        "Case 1: repo_id echoed back for caller identity",
        r.get("repo_id") == repo_id,
        f"got repo_id={r.get('repo_id')!r}",
    )
    # Backward-compat: legacy fields must still be present so existing
    # consumers don't KeyError. Values must be the null forms.
    t(
        "Case 1: legacy `content` key still present (backward-compat)",
        "content" in r and r["content"] is None,
        f"got content={r.get('content')!r}",
    )
    t(
        "Case 1: legacy `witness_path` key still present (backward-compat)",
        "witness_path" in r and r["witness_path"] is None,
        f"got witness_path={r.get('witness_path')!r}",
    )
    t(
        "Case 1: legacy `truncated` key still present (backward-compat)",
        "truncated" in r and r["truncated"] is False,
        f"got truncated={r.get('truncated')!r}",
    )
    t(
        "Case 1: legacy `sha_hint` key still present (backward-compat)",
        "sha_hint" in r and r["sha_hint"] is None,
        f"got sha_hint={r.get('sha_hint')!r}",
    )
    # Pre-fix the response was missing `status` entirely; assert the
    # absence of the legacy silent-empty shape so a future regression
    # can't reintroduce the bug.
    t(
        "Case 1: response is NOT the pre-fix silent-empty envelope",
        r.get("content") != "" or r.get("status") == "no_witness",
        f"got {r}",
    )

    # ---------------------------------------------------------------
    # Case 2 — unknown archetype name: expect `status: "failed",
    # error: "archetype not found"`.
    # ---------------------------------------------------------------
    section("Case 2 — unknown archetype name returns failed/archetype not found")

    # Verify-before (pre-fix): `get_canonical_excerpt(repo, "bogus")`
    # returned the same silent-empty envelope, conflating "name does
    # not exist" with "name exists but has no witness."
    # Verify-after: explicit `failed (archetype not found)` envelope so
    # the using-chameleon skill can distinguish typo-from-LLM vs.
    # legitimately-witnessless archetype.
    r = get_canonical_excerpt(str(repo), "definitely-not-an-archetype-xyz")["data"]

    t(
        "Case 2: status is 'failed'",
        r.get("status") == "failed",
        f"got {r}",
    )
    t(
        "Case 2: error is 'archetype not found' (distinct from repo_id error)",
        r.get("error") == "archetype not found",
        f"got error={r.get('error')!r}",
    )
    t(
        "Case 2: archetype_name echoed (so caller sees their typo)",
        r.get("archetype_name") == "definitely-not-an-archetype-xyz",
        f"got archetype_name={r.get('archetype_name')!r}",
    )
    # Backward-compat: legacy fields still present.
    t(
        "Case 2: legacy `content` key still present (backward-compat)",
        "content" in r and r["content"] is None,
        f"got content={r.get('content')!r}",
    )
    t(
        "Case 2: legacy `witness_path` key still present (backward-compat)",
        "witness_path" in r and r["witness_path"] is None,
        f"got witness_path={r.get('witness_path')!r}",
    )

    # ---------------------------------------------------------------
    # Case 3 — unresolvable repo_id: expect `status: "failed",
    # error: "repo_id not found"` (v0.5.2 Bug 5 regression check).
    # ---------------------------------------------------------------
    section("Case 3 — unknown repo_id returns failed/repo_id not found")

    # Verify-before (pre-v0.5.2): path-shaped garbage returned silent-
    # empty. v0.5.2 Bug 5 made it `failed (repo_id not found)`. Bug A
    # MUST keep that envelope intact for unresolvable repos.
    # Verify-after: unknown 64-char hex still yields the v0.5.2 envelope.
    r = get_canonical_excerpt("0" * 64, "any-archetype")["data"]

    t(
        "Case 3: status is 'failed' (v0.5.2 Bug 5 still intact)",
        r.get("status") == "failed",
        f"got {r}",
    )
    t(
        "Case 3: error is 'repo_id not found' (distinct from archetype error)",
        r.get("error") == "repo_id not found",
        f"got error={r.get('error')!r}",
    )
    t(
        "Case 3: legacy `content` key still present (backward-compat)",
        "content" in r and r["content"] is None,
        f"got content={r.get('content')!r}",
    )

    # ---------------------------------------------------------------
    # Case 4 — regression: archetype WITH witness still returns the
    # legacy content envelope (no `status` field), proving the fix
    # doesn't break the happy path.
    # ---------------------------------------------------------------
    section("Case 4 — archetype with witness still returns content (regression)")
finally:
    shutil.rmtree(repo, ignore_errors=True)


# A separate fresh repo so the witness is intact for the regression test.
repo_with_witness = _make_minimal_ts_repo("witness-intact")
try:
    bootstrap_repo(str(repo_with_witness))["data"]
    archetypes_json = json.loads(
        (repo_with_witness / ".chameleon" / "archetypes.json").read_text(encoding="utf-8")
    )
    arch_with_witness = list(archetypes_json.get("archetypes", {}).keys())[0]

    # Verify-before: pre-fix path returned `{content: <code>, ...}`.
    # Verify-after: post-fix MUST still return content, NOT a typed
    # envelope (the happy path takes precedence over the new branches).
    r = get_canonical_excerpt(str(repo_with_witness), arch_with_witness)["data"]

    t(
        "Case 4: status is NOT set for happy path (legacy envelope intact)",
        "status" not in r,
        f"got {r}",
    )
    t(
        "Case 4: content is a non-empty string",
        isinstance(r.get("content"), str) and len(r["content"]) > 0,
        f"got content={r.get('content')!r}",
    )
    t(
        "Case 4: witness_path points to a real file under src/",
        isinstance(r.get("witness_path"), str)
        and r["witness_path"].startswith("src/"),
        f"got witness_path={r.get('witness_path')!r}",
    )
    t(
        "Case 4: sha_hint is populated for the regression case",
        isinstance(r.get("sha_hint"), str) and len(r["sha_hint"]) > 0,
        f"got sha_hint={r.get('sha_hint')!r}",
    )
    t(
        "Case 4: truncated is False for short witness file",
        r.get("truncated") is False,
        f"got truncated={r.get('truncated')!r}",
    )

    # Cross-check: same repo, but ask for an unknown archetype —
    # confirms Case 2's envelope behaves the same on a repo that has
    # NOT had its canonicals tampered with. (Stronger evidence that the
    # `archetype not found` branch is keyed off archetypes.json, not
    # off whether canonicals.json has the row.)
    r = get_canonical_excerpt(str(repo_with_witness), "totally-bogus")["data"]
    t(
        "Case 4 cross-check: unknown archetype on intact repo still fails clean",
        r.get("status") == "failed" and r.get("error") == "archetype not found",
        f"got {r}",
    )

    # Cross-check: repo_id form takes the same path as repo-path form
    # for the `no_witness` envelope. (Mirror Case 1 using repo_id.)
    section("Case 1b — repo_id form returns same no_witness envelope")
    repo_id_intact = _compute_repo_id(repo_with_witness.resolve())
    trust_profile(str(repo_with_witness), repo_with_witness.name)
    # Drop the witness for this repo too.
    _drop_canonical_for(repo_with_witness, arch_with_witness)
    r = get_canonical_excerpt(repo_id_intact, arch_with_witness)["data"]
    t(
        "Case 1b: repo_id form yields status='no_witness' (Bug 1 form parity)",
        r.get("status") == "no_witness",
        f"got {r}",
    )
    t(
        "Case 1b: repo_id form echoes the resolved repo_id",
        r.get("repo_id") == repo_id_intact,
        f"got repo_id={r.get('repo_id')!r}",
    )

finally:
    shutil.rmtree(repo_with_witness, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
