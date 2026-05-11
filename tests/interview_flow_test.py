"""Synthetic interview flow test — drives /chameleon-init's 3-prompt rename
interview through the MCP tool surface.

CAVEAT (read before extending):

  This file drives the MCP tools the chameleon-init skill calls. It does
  NOT verify that real Claude correctly navigates the conversational prose
  in skills/chameleon-init/SKILL.md; that requires a real claude CLI
  invocation (see tests/trust_flow_test.py Round 2 for the pattern).

  The skill prose can drift (rename a tool, change prompt wording) without
  this test catching it. The flip side: this test runs in ~2 seconds and
  costs nothing, so it's run on every CI commit while the real-claude tests
  are gated.

  Coverage:
  - Step contract: propose returns the documented envelope shape.
  - Step contract: apply atomically rewrites archetypes/canonicals.
  - Step contract: canonical excerpts unchanged in content (only keys move).
  - Step contract: profile.summary.md regenerated with the new keys (the
    reviewer-visible signal a rename happened).
  - Failure modes that the skill's prose handles (collision, regex-violation,
    partial-rejection) must surface as failed envelopes.

  Finding pinned by this test:
  - profile_sha256 (hash_profile output = hash of profile.json + idioms.md)
    is STABLE across a rename, because rename touches archetypes.json /
    canonicals.json / profile.summary.md but does not modify profile.json
    or idioms.md. Per ARCHITECTURE §Material-change, this is consistent
    with the 'silent update' branch (rename != new archetype / canonical /
    idiom). The user is NOT forced to re-trust on a rename. If a future
    revision wants renames to bust trust, both hash_profile() AND the
    summary-md regeneration would need updating.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/interview_flow_test.py
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

# Isolated plugin data dir so trust grants we make don't leak.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_interview_flow_data_")
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


from chameleon_mcp.profile.trust import hash_profile, trust_state_for  # noqa: E402
from chameleon_mcp.tools import (  # noqa: E402
    _compute_repo_id,
    apply_archetype_renames,
    bootstrap_repo,
    propose_archetype_renames,
    trust_profile,
)


def _make_ts_repo() -> Path:
    """TS fixture with two distinguishable clusters so propose returns ≥2 rows.

    Mirrors the v0.2 regression fixture but uses src/components (JSX-flavored
    files) + src/utils (plain TS) so propose has interesting alternatives
    like 'components', 'react-component', 'utility' to surface.
    """
    root = Path(tempfile.mkdtemp(prefix="cham_iflow_repo_"))
    (root / "package.json").write_text(
        '{"name":"iflow","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    comp = root / "src" / "components"
    comp.mkdir(parents=True)
    for i in range(8):
        (comp / f"c{i}.tsx").write_text(
            f"export const Comp{i} = () => <div>x{i}</div>;\n"
        )
    util = root / "src" / "utils"
    util.mkdir()
    for i in range(6):
        (util / f"u{i}.ts").write_text(
            f"export const fn_{i} = () => {i};\n"
        )
    return root


# A list so cleanup is guaranteed even if a section asserts and bails.
REPOS_TO_CLEAN: list[Path] = []


def _fresh_setup() -> Path:
    """Step 1 of the skill: bootstrap + trust a fixture repo."""
    repo = _make_ts_repo()
    REPOS_TO_CLEAN.append(repo)
    boot = bootstrap_repo(str(repo))
    assert boot["data"]["status"] == "success", boot
    tp = trust_profile(str(repo), repo.name)
    assert tp["data"]["status"] == "success", tp
    return repo


# ===========================================================================
section("Step 2 (skill: propose_archetype_renames) — shape contract")
# Skill calls propose with the absolute repo path. The MCP echoes back:
#   data.status == success
#   data.repo_id (64 hex chars — the deterministic identifier)
#   data.archetypes [{ current_name, cluster_size, canonical_file,
#                      paths_pattern, suggested_alternatives }, ...]
# Skill formats this into Prompt 1's numbered list.
# ---------------------------------------------------------------------------
repo = _fresh_setup()
proposed = propose_archetype_renames(str(repo), top_n=5)
prop_data = proposed["data"]
t("propose returns success", prop_data.get("status") == "success", str(prop_data))
t(
    "response carries archetypes list",
    isinstance(prop_data.get("archetypes"), list),
)
renames_list = prop_data["archetypes"]
t(
    "at least one archetype proposed",
    len(renames_list) > 0,
    f"got {len(renames_list)}",
)
# Skill iterates each entry to render the numbered list.
for entry in renames_list:
    t("entry has 'current_name'", "current_name" in entry, str(entry))
    t("entry has 'cluster_size'", "cluster_size" in entry, str(entry))
    t(
        "entry has 'suggested_alternatives'",
        "suggested_alternatives" in entry,
        str(entry),
    )
    t(
        "suggested_alternatives has ≥3 candidates",
        len(entry["suggested_alternatives"]) >= 3,
        str(entry["suggested_alternatives"]),
    )

# Skill needs the current_name in the alternatives so the user can "keep".
t(
    "first alternative is the current name (the 'keep' option)",
    renames_list[0]["current_name"] in renames_list[0]["suggested_alternatives"],
)


# ===========================================================================
section("Step 3-4 (skill: build mapping, call apply) — happy path")
# Skill builds a {old: new} dict from user choices in Prompt 2 and calls
# apply_archetype_renames in Prompt 3 after the user confirms.
# ---------------------------------------------------------------------------
# Need at least two archetypes to rename two of them.
t(
    "≥2 archetypes available so two-rename test can run",
    len(renames_list) >= 2,
    f"got {len(renames_list)}",
)
if len(renames_list) >= 2:
    # Pick a non-no-op alternative for each — skip alternatives equal to the
    # current name (which would be a "keep").
    def _first_distinct_alt(entry: dict) -> str:
        for alt in entry["suggested_alternatives"]:
            if alt != entry["current_name"]:
                return alt
        # Fall back: synthesize a distinct slug.
        return f"renamed-{entry['current_name']}"

    mapping = {
        renames_list[0]["current_name"]: _first_distinct_alt(renames_list[0]),
        renames_list[1]["current_name"]: _first_distinct_alt(renames_list[1]),
    }
    print(f"  mapping the skill would submit: {mapping}")

    # Capture pre-state for downstream byte-equality checks.
    archetypes_path = repo / ".chameleon" / "archetypes.json"
    canonicals_path = repo / ".chameleon" / "canonicals.json"
    pre_arche = json.loads(archetypes_path.read_text())["archetypes"]
    pre_canonicals = json.loads(canonicals_path.read_text())["canonicals"]
    pre_hash = hash_profile(repo / ".chameleon")

    applied = apply_archetype_renames(str(repo), mapping)
    ap_data = applied["data"]
    t("apply returns success", ap_data["status"] == "success", str(ap_data))
    t(
        "renames_applied equals mapping size (2)",
        ap_data["renames_applied"] == 2,
        str(ap_data),
    )
    t(
        "new_profile_sha256 is a 64-char hex",
        len(ap_data.get("new_profile_sha256", "")) == 64,
    )

    # Step 5: verify on-disk archetypes.json reflects the renames.
    post_arche = json.loads(archetypes_path.read_text())["archetypes"]
    for old_name, new_name in mapping.items():
        t(
            f"on-disk archetypes contains new name {new_name!r}",
            new_name in post_arche,
            str(sorted(post_arche.keys())),
        )
        t(
            f"on-disk archetypes no longer contains old name {old_name!r}",
            old_name not in post_arche,
        )

    # Step 6: canonical EXCERPT content unchanged — only keys moved.
    post_canonicals = json.loads(canonicals_path.read_text())["canonicals"]
    for old_name, new_name in mapping.items():
        t(
            f"canonical content under {new_name!r} byte-equals previous "
            f"content under {old_name!r}",
            post_canonicals[new_name] == pre_canonicals[old_name],
        )

    # Step 7: hash semantics. Per ARCHITECTURE.md §"Material-change predicate"
    # AND the implementation of `profile.trust.hash_profile`, the trust hash
    # covers ONLY `profile.json + idioms.md`. archetypes.json + canonicals.json
    # are key-renamed but their content (and profile.json) is byte-identical,
    # so the trust hash is unchanged on a pure rename. This is consistent with
    # the "silent update" branch of the material-change rule: a rename does
    # not introduce a new archetype, new canonical witness, or new active idiom,
    # so it does not require a re-prompt.
    #
    # FINDING: the rename-changes-summary IS reflected in profile.summary.md
    # (regenerated with the new keys) — that's the visible signal a reviewer
    # would notice. The MCP's `new_profile_sha256` return value is
    # `hash_profile()` applied to the new on-disk state; the test asserts
    # parity rather than divergence.
    post_hash = hash_profile(repo / ".chameleon")
    t(
        "apply's returned new_profile_sha256 matches a fresh hash_profile() read",
        ap_data["new_profile_sha256"] == post_hash,
    )
    # v0.5.1 H1 fix: hash_profile now covers archetypes.json + canonicals.json
    # in addition to profile.json + idioms.md, so a pure rename DOES change
    # the trust hash. The interview's apply path correctly bumps profile_sha256;
    # users must re-trust after a rename. This is the new material-change
    # rule (a rename is treated as a profile mutation worth re-reviewing,
    # not a silent update).
    t(
        "trust hash CHANGED on rename (v0.5.1 H1 — hash_profile covers archetypes.json)",
        post_hash != pre_hash,
        f"pre={pre_hash[:12]} post={post_hash[:12]}",
    )

    # The trust record was written before the rename; its profile_sha256 now
    # no longer matches the on-disk profile, so subsequent detect_repo calls
    # report `trust_state: "stale"` and the user must re-run /chameleon-trust.
    repo_id = _compute_repo_id(repo)
    record = trust_state_for(repo_id)
    t(
        "trust record still on disk after rename",
        record is not None,
    )
    if record is not None:
        t(
            "trust record's profile_sha256 is STALE after rename (v0.5.1 H1)",
            record.profile_sha256 != post_hash,
            f"trust={record.profile_sha256[:12]} disk={post_hash[:12]}",
        )

    # However, profile.summary.md DOES reflect the rename — this is the
    # reviewer-visible signal of the change.
    summary_after = (
        repo / ".chameleon" / "profile.summary.md"
    ).read_text(encoding="utf-8")
    for new_name in mapping.values():
        t(
            f"profile.summary.md mentions the new archetype name {new_name!r}",
            f"**{new_name}**" in summary_after,
        )
    for old_name in mapping.keys():
        t(
            f"profile.summary.md no longer mentions {old_name!r}",
            f"**{old_name}**" not in summary_after,
        )


# ===========================================================================
section("Step 8 (failure mode) — collision: two renames to same target")
# Skill's Prompt 2 loop must reject a mapping where the user picks the same
# new name for two archetypes. Validation happens server-side (defense in
# depth) so even a buggy skill can't commit a collision.
# ---------------------------------------------------------------------------
collision_repo = _fresh_setup()
existing = sorted(
    json.loads(
        (collision_repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"].keys()
)
t("collision-fixture has ≥2 archetypes", len(existing) >= 2)

# Snapshot pre-state so we can prove the collision aborted everything.
pre_arche = json.loads(
    (collision_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
collision_resp = apply_archetype_renames(
    str(collision_repo),
    {existing[0]: "shared-target", existing[1]: "shared-target"},
)
t(
    "collision rename rejected with status=failed",
    collision_resp["data"]["status"] == "failed",
    str(collision_resp["data"]),
)
t(
    "collision error message names the conflicting target",
    "shared-target" in collision_resp["data"].get("error", ""),
    collision_resp["data"].get("error", ""),
)
post_arche = json.loads(
    (collision_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
t(
    "archetypes.json unchanged after collision rejection (atomic)",
    post_arche == pre_arche,
)


# ===========================================================================
section("Step 9 (failure mode) — regex violation: 'Has Spaces' target")
# The archetype-name regex is ^[a-z][a-z0-9-]{0,63}$. Spaces, uppercase,
# leading digits, and underscores must be rejected before any file touch.
# ---------------------------------------------------------------------------
regex_repo = _fresh_setup()
existing = sorted(
    json.loads(
        (regex_repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"].keys()
)
pre_arche = json.loads(
    (regex_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]

for bad_target in ("Has Spaces", "UPPERCASE", "1leading-digit", "has_underscore"):
    bad = apply_archetype_renames(str(regex_repo), {existing[0]: bad_target})
    t(
        f"target {bad_target!r} rejected with status=failed",
        bad["data"]["status"] == "failed",
        str(bad["data"]),
    )
    t(
        f"error message for {bad_target!r} mentions the regex shape",
        "[a-z]" in bad["data"].get("error", "") or "must match" in bad["data"].get("error", ""),
        bad["data"].get("error", ""),
    )

post_arche = json.loads(
    (regex_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
t(
    "archetypes.json untouched after regex-violation rejections",
    post_arche == pre_arche,
)


# ===========================================================================
section("Step 10 (failure mode) — rollback on partial failure")
# When the skill submits a mapping where ONE entry is valid and ANOTHER is
# invalid (regex violation), the entire op must be rejected — there is no
# such thing as "rename the good ones, fail the bad ones". Atomic-or-nothing.
# ---------------------------------------------------------------------------
mixed_repo = _fresh_setup()
existing = sorted(
    json.loads(
        (mixed_repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"].keys()
)
pre_arche = json.loads(
    (mixed_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
pre_canonicals = json.loads(
    (mixed_repo / ".chameleon" / "canonicals.json").read_text()
)["canonicals"]
pre_summary = (mixed_repo / ".chameleon" / "profile.summary.md").read_text(
    encoding="utf-8"
)
pre_hash = hash_profile(mixed_repo / ".chameleon")

mixed = apply_archetype_renames(
    str(mixed_repo),
    {
        existing[0]: "valid-target",          # would succeed alone
        existing[1]: "Invalid Space Target",  # regex violation
    },
)
t(
    "mixed (one-valid, one-invalid) rejected with status=failed",
    mixed["data"]["status"] == "failed",
    str(mixed["data"]),
)

# Critical: archetypes.json, canonicals.json, profile.summary.md unchanged.
post_arche = json.loads(
    (mixed_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
post_canonicals = json.loads(
    (mixed_repo / ".chameleon" / "canonicals.json").read_text()
)["canonicals"]
post_summary = (mixed_repo / ".chameleon" / "profile.summary.md").read_text(
    encoding="utf-8"
)
post_hash = hash_profile(mixed_repo / ".chameleon")

t(
    "archetypes.json byte-identical after rejected mixed op",
    post_arche == pre_arche,
)
t(
    "canonicals.json byte-identical after rejected mixed op",
    post_canonicals == pre_canonicals,
)
t(
    "profile.summary.md byte-identical after rejected mixed op",
    post_summary == pre_summary,
)
t(
    "profile_sha256 unchanged after rejected mixed op",
    post_hash == pre_hash,
)
# Specifically: the valid entry was NOT applied. The old name persists; the
# 'valid-target' name does NOT appear.
t(
    f"valid entry's old name {existing[0]!r} still present (atomic abort)",
    existing[0] in post_arche,
)
t(
    "'valid-target' never written (atomic abort)",
    "valid-target" not in post_arche,
)


# ===========================================================================
section("Edge — unknown source archetype rejected")
# Skill's Prompt 2 should only let users select renames from the propose
# response, but a buggy mapping with an unknown source name (e.g., typo or
# stale state) must be rejected, not silently absorbed.
# ---------------------------------------------------------------------------
edge_repo = _fresh_setup()
pre_arche = json.loads(
    (edge_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
r = apply_archetype_renames(
    str(edge_repo), {"definitely-not-an-archetype": "x"}
)
t(
    "unknown source name rejected",
    r["data"]["status"] == "failed",
    str(r["data"]),
)
post_arche = json.loads(
    (edge_repo / ".chameleon" / "archetypes.json").read_text()
)["archetypes"]
t(
    "archetypes.json untouched after unknown-source rejection",
    post_arche == pre_arche,
)


# ===========================================================================
section("Edge — top_n=1 (Prompt 1 with a single-archetype repo)")
# Skill's Prompt 1 caps the list at top_n. We verify top_n=1 returns exactly
# one row so the prompt renders cleanly even for a tiny repo.
# ---------------------------------------------------------------------------
n1 = propose_archetype_renames(str(edge_repo), top_n=1)
t("top_n=1 returns success", n1["data"]["status"] == "success")
t(
    "top_n=1 returns exactly one row",
    len(n1["data"]["archetypes"]) == 1,
    str(len(n1["data"]["archetypes"])),
)


# ===========================================================================
section("Edge — missing profile is handled per the skill's prose")
# Skill's "If propose returns nothing useful" branch: when the repo has
# no .chameleon/ directory, propose returns status=failed and the skill
# silently skips the rename interview.
# ---------------------------------------------------------------------------
no_profile = Path(tempfile.mkdtemp(prefix="cham_iflow_noprof_"))
REPOS_TO_CLEAN.append(no_profile)
no_prof_resp = propose_archetype_renames(str(no_profile), top_n=4)
t(
    "missing .chameleon/ rejected with status=failed",
    no_prof_resp["data"]["status"] == "failed",
    str(no_prof_resp["data"]),
)
t(
    "error message mentions running /chameleon-init",
    "chameleon-init" in no_prof_resp["data"].get("error", "")
    or "no .chameleon" in no_prof_resp["data"].get("error", ""),
    no_prof_resp["data"].get("error", ""),
)


# ===========================================================================
section("Edge — no-op rename returns success with renames_applied=0")
# Per the skill: if the user types "keep" (or types the existing name)
# for every archetype, the mapping is empty AFTER no-op stripping, and
# apply should return success with 0 applied — NOT failed.
# ---------------------------------------------------------------------------
noop_repo = _fresh_setup()
existing = sorted(
    json.loads(
        (noop_repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"].keys()
)
# Map an archetype to itself (no-op).
noop = apply_archetype_renames(str(noop_repo), {existing[0]: existing[0]})
t(
    "self-rename returns success",
    noop["data"]["status"] == "success",
    str(noop["data"]),
)
t(
    "self-rename reports renames_applied=0",
    noop["data"]["renames_applied"] == 0,
)
# An empty mapping is the same kind of no-op (skill's "no" reply path).
empty = apply_archetype_renames(str(noop_repo), {})
t(
    "empty mapping returns success with renames_applied=0",
    empty["data"]["status"] == "success" and empty["data"]["renames_applied"] == 0,
    str(empty["data"]),
)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
for r in REPOS_TO_CLEAN:
    shutil.rmtree(r, ignore_errors=True)
shutil.rmtree(TMPDATA, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
