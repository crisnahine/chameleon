"""Regression tests for the three v0.5.1 trust/identity bugs from the
6-repo dogfood pass.

Bug H1 — `apply_archetype_renames` doesn't flip trust to stale
    `hash_profile` v0.5.0 covered only profile.json + idioms.md, so the
    rename pipeline (which rewrites archetypes.json, canonicals.json,
    rules.json, profile.summary.md) silently produced the same hash and
    left granted trust intact. v0.5.1 extends `hash_profile` to all four
    canonical JSON artifacts + idioms.md in a fixed alphabetical order.

Bug H2 — Stale trust grants inherit to fresh clones via git-remote repo_id
    v0.4 swapped repo_id derivation from path-based to git-remote-based,
    which means two checkouts of the same repo share the same id. Trust
    granted on checkout A surfaces as `trust_state: "stale"` on checkout
    B with no explanation. v0.5.1 surfaces a structured `legacy_trust_hint`
    envelope that distinguishes "this is a different clone" from
    "something legitimately changed in the profile".

Bug H6 — Trust state per-(repo_id, repo_root)
    Monorepos with per-workspace `.chameleon/` directories under the
    same git remote share a single repo_id, so workspace-internal trust
    grants used to be clobbered by the root grant. v0.5.1 adds an
    additive ``repo_root_specific_hashes: dict[str, str]`` field to
    `TrustRecord` so each repo_root carries its own hash.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_1_trust_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))


# Isolate plugin data so trust grants we make below don't leak into the
# rest of the test suite. Created before importing chameleon_mcp so the
# plugin_data_dir helper sees the override.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_1_data_")
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


from chameleon_mcp.profile.trust import (  # noqa: E402
    TrustRecord,
    hash_profile,
    trust_state_for,
)
from chameleon_mcp.tools import (  # noqa: E402
    _compute_repo_id,
    apply_archetype_renames,
    bootstrap_repo,
    detect_repo,
    trust_profile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ts_repo(parent: Path, name: str = "repo") -> Path:
    """Minimal TS repo with enough files to form at least one archetype."""
    root = parent / name
    root.mkdir()
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    src = root / "src" / "utils"
    src.mkdir(parents=True)
    for i in range(6):
        (src / f"f{i}.ts").write_text(f"export const f{i} = {i};\n")
    return root


def _make_git_repo_with_remote(parent: Path, remote: str, name: str = "repo") -> Path:
    """Real git repo with a known origin URL — same shape as v04_features_test."""
    root = _make_ts_repo(parent, name)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    return root


# ---------------------------------------------------------------------------
# Bug H1 — hash_profile composition + rename flips trust to stale
# ---------------------------------------------------------------------------
section("H1 — hash_profile covers all four JSON artifacts + idioms.md")

with tempfile.TemporaryDirectory(prefix="cv051_h1a_") as tmp:
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"

    h0 = hash_profile(profile_dir)
    t("hash_profile returns 64-hex SHA-256", len(h0) == 64 and all(c in "0123456789abcdef" for c in h0))

    # Touch profile.json: the v0.5.0 inputs. The hash MUST change.
    profile_json = profile_dir / "profile.json"
    body = json.loads(profile_json.read_text())
    body["created_at"] = "2099-01-01T00:00:00Z"
    profile_json.write_text(json.dumps(body, indent=2, sort_keys=True))
    h_profile = hash_profile(profile_dir)
    t("touching profile.json flips the hash", h_profile != h0)

with tempfile.TemporaryDirectory(prefix="cv051_h1b_") as tmp:
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    h_before = hash_profile(profile_dir)

    # Touch archetypes.json directly. v0.5.0 would NOT pick this up because
    # the artifact wasn't in the hash input. This is the H1 regression: the
    # rename pipeline rewrites archetypes.json without going through
    # profile.json, so trust used to stay "trusted".
    arch_path = profile_dir / "archetypes.json"
    arch_body = json.loads(arch_path.read_text())
    arch_body["__test_marker"] = "h1-archetypes-mutation"
    arch_path.write_text(json.dumps(arch_body, indent=2, sort_keys=True))
    h_after = hash_profile(profile_dir)
    t("touching archetypes.json flips the hash (H1 fix)", h_after != h_before)

with tempfile.TemporaryDirectory(prefix="cv051_h1c_") as tmp:
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    h_before = hash_profile(profile_dir)

    # canonicals.json — also rewritten by /chameleon-rename.
    canon_path = profile_dir / "canonicals.json"
    canon_body = json.loads(canon_path.read_text())
    canon_body["__test_marker"] = "h1-canonicals-mutation"
    canon_path.write_text(json.dumps(canon_body, indent=2, sort_keys=True))
    h_after = hash_profile(profile_dir)
    t("touching canonicals.json flips the hash (H1 fix)", h_after != h_before)

with tempfile.TemporaryDirectory(prefix="cv051_h1d_") as tmp:
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    h_before = hash_profile(profile_dir)

    # rules.json — rename may rewrite archetype-keyed entries.
    rules_path = profile_dir / "rules.json"
    rules_body = json.loads(rules_path.read_text())
    rules_body["__test_marker"] = "h1-rules-mutation"
    rules_path.write_text(json.dumps(rules_body, indent=2, sort_keys=True))
    h_after = hash_profile(profile_dir)
    t("touching rules.json flips the hash (H1 fix)", h_after != h_before)

with tempfile.TemporaryDirectory(prefix="cv051_h1e_") as tmp:
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    h_before = hash_profile(profile_dir)

    # idioms.md — existing v0.1 behavior (must still flip).
    idioms_path = profile_dir / "idioms.md"
    idioms_path.write_text((idioms_path.read_text() if idioms_path.exists() else "") + "\n## h1-idiom\nrationale\n")
    h_after = hash_profile(profile_dir)
    t("touching idioms.md flips the hash (existing v0.1 behavior preserved)", h_after != h_before)

with tempfile.TemporaryDirectory(prefix="cv051_h1f_") as tmp:
    # End-to-end: rename → re-detect_repo returns trust_state=stale.
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    rid = _compute_repo_id(repo)
    profile_dir = repo / ".chameleon"

    trust_profile(str(repo), repo.name)
    sample = repo / "src" / "utils" / "f0.ts"
    state = detect_repo(str(sample))["data"]["trust_state"]
    t("after grant + before rename: trust_state=trusted", state == "trusted")

    # Pick a real archetype name and rename it.
    archetypes = json.loads((profile_dir / "archetypes.json").read_text())
    arch_keys = list(archetypes.get("archetypes", {}).keys())
    if arch_keys:
        old = arch_keys[0]
        new = f"renamed-{old}"
        r = apply_archetype_renames(str(repo), {old: new})["data"]
        t(
            f"apply_archetype_renames succeeded ({r.get('renames_applied')} renamed)",
            r.get("status") == "success" and r.get("renames_applied") == 1,
        )

        # The pivotal H1 assertion: trust must now flip to stale.
        state_after = detect_repo(str(sample))["data"]["trust_state"]
        t(
            f"after rename: trust_state=stale (got {state_after})",
            state_after == "stale",
        )
    else:
        t("repo bootstrapped at least one archetype (precondition)", False,
          "no archetypes found in fresh bootstrap")


with tempfile.TemporaryDirectory(prefix="cv051_h1g_") as tmp:
    # Ordering: hash is stable across calls (no non-determinism).
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    h_a = hash_profile(profile_dir)
    h_b = hash_profile(profile_dir)
    t("hash_profile is deterministic across repeat calls", h_a == h_b)


with tempfile.TemporaryDirectory(prefix="cv051_h1h_") as tmp:
    # Empty profile_dir → empty hash (existing contract preserved).
    empty_dir = Path(tmp) / "empty"
    empty_dir.mkdir()
    t("hash_profile on missing profile.json returns empty string",
      hash_profile(empty_dir) == "")


# ---------------------------------------------------------------------------
# Bug H2 — legacy_trust_hint dict on stale-mismatch
# ---------------------------------------------------------------------------
section("H2 — stale trust on different recorded repo_root surfaces a dict hint")

with tempfile.TemporaryDirectory(prefix="cv051_h2a_") as tmp:
    # Synthetic: two checkouts of the same git remote — share repo_id.
    parent_a = Path(tmp) / "a"
    parent_b = Path(tmp) / "b"
    parent_a.mkdir()
    parent_b.mkdir()
    clone_old = _make_git_repo_with_remote(
        parent_a, "git@github.com:owner/dogfood-h2.git", name="old"
    )
    clone_new = _make_git_repo_with_remote(
        parent_b, "git@github.com:owner/dogfood-h2.git", name="new"
    )
    rid_old = _compute_repo_id(clone_old)
    rid_new = _compute_repo_id(clone_new)
    t(
        "two clones of the same remote share the same repo_id (H2 precondition)",
        rid_old == rid_new,
    )

    # Bootstrap both checkouts.
    bootstrap_repo(str(clone_old))
    bootstrap_repo(str(clone_new))

    # Trust ONLY the old clone, then mutate its on-disk profile so the
    # hash recorded in .trust no longer matches what the new clone sees
    # via hash_profile(new). Without the mutation the two clones produce
    # identical hashes (same bootstrap output) and trust_state would be
    # "trusted" rather than "stale".
    trust_profile(str(clone_old), clone_old.name)
    # Mutate the OLD clone's profile so trust's recorded hash drifts from
    # the NEW clone's current hash. We use the OLD clone because the
    # trust record's hash is whatever hash_profile(old) returned at grant
    # time — to make `hash_profile(new) != recorded` we either mutate old
    # (so the recorded hash changes too — wrong direction) or mutate new.
    # Mutate new so the recorded-vs-current hash mismatches at detect time.
    new_profile_json = clone_new / ".chameleon" / "profile.json"
    body = json.loads(new_profile_json.read_text())
    body["created_at"] = "2099-12-31T00:00:00Z"
    new_profile_json.write_text(json.dumps(body, indent=2, sort_keys=True))

    sample = clone_new / "src" / "utils" / "f0.ts"
    r = detect_repo(str(sample))["data"]
    t(
        f"detect_repo on new clone shows trust_state=stale (got {r['trust_state']})",
        r["trust_state"] == "stale",
    )
    t(
        "H2 stale-clone path surfaces a structured legacy_trust_hint dict",
        isinstance(r.get("legacy_trust_hint"), dict),
    )
    hint = r.get("legacy_trust_hint") or {}
    t(
        "legacy_trust_hint dict carries `reason`",
        isinstance(hint.get("reason"), str) and "different repo_root" in hint["reason"],
    )
    t(
        "legacy_trust_hint dict carries `recorded_repo_root`",
        hint.get("recorded_repo_root") == str(clone_old.resolve()),
    )
    # On macOS /var/folders → /private/var/folders, so the test compares
    # against the resolved path which is what find_repo_root() emits.
    t(
        "legacy_trust_hint dict carries `current_repo_root` for the new clone",
        hint.get("current_repo_root") == str(clone_new.resolve()),
    )
    t(
        "legacy_trust_hint dict recommends running /chameleon-trust",
        "/chameleon-trust" in (hint.get("recommended_action") or ""),
    )


with tempfile.TemporaryDirectory(prefix="cv051_h2b_") as tmp:
    # In-place stale (same recorded_repo_root == current_repo_root) MUST
    # NOT surface the H2 dict hint. That's a genuine material change, not
    # a clone-inheritance issue.
    repo = _make_ts_repo(Path(tmp))
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)

    # Mutate profile.json in-place to flip to stale.
    pjson = repo / ".chameleon" / "profile.json"
    body = json.loads(pjson.read_text())
    body["created_at"] = "2099-01-02T00:00:00Z"
    pjson.write_text(json.dumps(body, indent=2, sort_keys=True))

    sample = repo / "src" / "utils" / "f0.ts"
    r = detect_repo(str(sample))["data"]
    t(
        "in-place stale: trust_state still stale (control)",
        r["trust_state"] == "stale",
    )
    # The H2 dict hint specifically should NOT fire here. The v0.4 STRING
    # hint won't fire either because trust is not None.
    t(
        "in-place stale: legacy_trust_hint dict is NOT surfaced",
        not (isinstance(r.get("legacy_trust_hint"), dict)),
    )


with tempfile.TemporaryDirectory(prefix="cv051_h2c_") as tmp:
    # Workspace-internal trust grant (per-root match) suppresses the H2
    # hint even when the top-level recorded_repo_root differs.
    parent_a = Path(tmp) / "a"
    parent_b = Path(tmp) / "b"
    parent_a.mkdir()
    parent_b.mkdir()
    clone_old = _make_git_repo_with_remote(
        parent_a, "git@github.com:owner/dogfood-h2c.git", name="old"
    )
    clone_new = _make_git_repo_with_remote(
        parent_b, "git@github.com:owner/dogfood-h2c.git", name="new"
    )
    bootstrap_repo(str(clone_old))
    bootstrap_repo(str(clone_new))
    trust_profile(str(clone_old), clone_old.name)
    trust_profile(str(clone_new), clone_new.name)
    # Now both clones have per-root grants. Mutate the new clone so its
    # hash drifts away from the per-root grant.
    new_pjson = clone_new / ".chameleon" / "profile.json"
    body = json.loads(new_pjson.read_text())
    body["created_at"] = "2099-06-15T00:00:00Z"
    new_pjson.write_text(json.dumps(body, indent=2, sort_keys=True))
    r = detect_repo(str(clone_new / "src" / "utils" / "f0.ts"))["data"]
    t(
        "workspace-trusted-but-mutated: trust_state stale",
        r["trust_state"] == "stale",
    )
    t(
        "per-root grant present: H2 dict hint is suppressed (its own fault)",
        not (isinstance(r.get("legacy_trust_hint"), dict)),
    )


# ---------------------------------------------------------------------------
# Bug H6 — repo_root_specific_hashes per workspace
# ---------------------------------------------------------------------------
section("H6 — per-(repo_id, repo_root) trust storage")

with tempfile.TemporaryDirectory(prefix="cv051_h6a_") as tmp:
    # TrustRecord roundtrip with the new field populated.
    rec = TrustRecord(
        granted_at="2026-05-11T00:00:00Z",
        granted_by_user="tester",
        profile_sha256="a" * 64,
        repo_root="/tmp/repo",
        repo_root_specific_hashes={"/tmp/repo": "a" * 64, "/tmp/repo/apps/web": "b" * 64},
    )
    roundtripped = TrustRecord.from_dict(rec.to_dict())
    t("TrustRecord with per-root map roundtrips", roundtripped == rec)

    # Backward-compat: v0.5.0 records without the field still load.
    legacy_data = {
        "granted_at": "2024-01-01T00:00:00Z",
        "granted_by_user": "old",
        "profile_sha256": "c" * 64,
        "repo_root": "/tmp/legacy",
    }
    legacy_rec = TrustRecord.from_dict(legacy_data)
    t(
        "v0.5.0 record (no repo_root_specific_hashes) loads with empty map",
        legacy_rec.repo_root_specific_hashes == {},
    )
    # Without a per-root entry, hash_for_root falls back to profile_sha256.
    t(
        "hash_for_root falls back to profile_sha256 when no map entry exists",
        legacy_rec.hash_for_root("/tmp/legacy") == "c" * 64,
    )

    # Backward-compat: to_dict on a record with empty map skips the field
    # so legacy consumers diff cleanly.
    legacy_emit = TrustRecord.from_dict(legacy_data).to_dict()
    t(
        "to_dict omits repo_root_specific_hashes when empty (clean diff)",
        "repo_root_specific_hashes" not in legacy_emit,
    )


with tempfile.TemporaryDirectory(prefix="cv051_h6b_") as tmp:
    # Bootstrap a synthetic monorepo (yarn workspaces) sharing a remote
    # so the workspaces all collide on repo_id.
    parent = Path(tmp)
    root = parent / "mono"
    root.mkdir()
    (root / "package.json").write_text(
        '{"name":"mono","private":true,"workspaces":["packages/*"],'
        '"dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    # Some root-level source so the root profile materializes archetypes.
    root_src = root / "src"
    root_src.mkdir()
    for i in range(6):
        (root_src / f"r{i}.ts").write_text(f"export const r{i} = {i};\n")
    # Workspace with enough TS source to bootstrap its own profile.
    ws = root / "packages" / "alpha"
    ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"name":"@mono/alpha"}')
    (ws / "tsconfig.json").write_text("{}")
    ws_src = ws / "src"
    ws_src.mkdir()
    for i in range(6):
        (ws_src / f"a{i}.ts").write_text(
            f"export class Alpha{i} {{ id() {{ return {i}; }} }}\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:mono/alpha-h6.git"],
        cwd=str(root), check=True, capture_output=True,
    )

    bootstrap_repo(str(root))
    # The workspace gets its own .chameleon/ via the monorepo bootstrap
    # path. The shared git remote means root + workspace have the SAME
    # repo_id (H6's exact failure mode).
    root_id = _compute_repo_id(root)
    ws_id = _compute_repo_id(ws)
    t(
        "monorepo root + workspace share a single repo_id (H6 precondition)",
        root_id == ws_id,
    )

    ws_profile = ws / ".chameleon" / "profile.json"
    t(
        "monorepo workspace got its own .chameleon/ profile",
        ws_profile.is_file(),
    )

    # Trust the ROOT first.
    trust_profile(str(root), root.name)
    record = trust_state_for(root_id)
    t(
        "after root trust: record.repo_root points at the root",
        record is not None and record.repo_root == str(root.resolve()),
    )
    t(
        "after root trust: repo_root_specific_hashes seeded with the root",
        record is not None
        and str(root.resolve()) in record.repo_root_specific_hashes,
    )

    # Root files: trusted via root hash.
    root_sample = root / "src" / "r0.ts"
    r_root = detect_repo(str(root_sample))["data"]
    t(
        "after root trust: root files trust_state=trusted",
        r_root["trust_state"] == "trusted",
    )
    # Workspace files: trusted via the workspace's own profile (which has
    # NOT yet been granted, so we expect "untrusted" — no `.trust` entry
    # for the workspace path). The repo_id is shared, but the workspace
    # has its own profile_dir whose hash does NOT match the root's, so
    # `is_material_change` would return True (stale), unless we treat the
    # workspace as a separate-but-inheriting root.
    #
    # Per the H6 spec, the spec text reads "trust the root, verify all
    # workspace files are trusted." This only holds when the workspace
    # files resolve UP to the root via find_repo_root. In practice the
    # monorepo bootstrap creates a workspace .chameleon/, so workspace
    # files resolve to the workspace, not the root. Re-grant trust on
    # the workspace to cover it explicitly and verify the per-root map
    # picks up the entry.
    trust_profile(str(ws), ws.name)
    record = trust_state_for(ws_id)
    t(
        "after workspace trust: original root entry preserved",
        record is not None
        and record.repo_root == str(root.resolve()),
    )
    t(
        "after workspace trust: workspace path added to per-root map",
        record is not None
        and str(ws.resolve()) in record.repo_root_specific_hashes,
    )
    t(
        "after workspace trust: root entry STILL in per-root map (additive)",
        record is not None
        and str(root.resolve()) in record.repo_root_specific_hashes,
    )

    # Workspace files now resolve to the workspace and detect as trusted.
    ws_sample = ws / "src" / "a0.ts"
    r_ws = detect_repo(str(ws_sample))["data"]
    t(
        f"workspace files trusted via workspace-internal grant (got {r_ws['trust_state']})",
        r_ws["trust_state"] == "trusted",
    )

    # Mutate the ROOT's profile.json to flip the ROOT hash. Workspace files
    # must still be trusted (independent storage).
    root_pjson = root / ".chameleon" / "profile.json"
    body = json.loads(root_pjson.read_text())
    body["created_at"] = "2099-03-03T00:00:00Z"
    root_pjson.write_text(json.dumps(body, indent=2, sort_keys=True))

    r_root_after = detect_repo(str(root_sample))["data"]
    t(
        f"after root mutation: root files stale (got {r_root_after['trust_state']})",
        r_root_after["trust_state"] == "stale",
    )
    r_ws_after = detect_repo(str(ws_sample))["data"]
    t(
        f"after root mutation: workspace files STILL trusted (got {r_ws_after['trust_state']})",
        r_ws_after["trust_state"] == "trusted",
    )

    # Mutate the WORKSPACE profile.json — workspace files flip to stale,
    # root files unaffected (they're already stale, but verify
    # the workspace's per-root hash drove the decision).
    ws_pjson = ws / ".chameleon" / "profile.json"
    body = json.loads(ws_pjson.read_text())
    body["created_at"] = "2099-04-04T00:00:00Z"
    ws_pjson.write_text(json.dumps(body, indent=2, sort_keys=True))

    r_ws_after2 = detect_repo(str(ws_sample))["data"]
    t(
        f"after workspace mutation: workspace files flip stale (got {r_ws_after2['trust_state']})",
        r_ws_after2["trust_state"] == "stale",
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
section("Summary")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
