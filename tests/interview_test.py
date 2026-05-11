"""Phase 2D.1 + 2D.4 regression tests.

Covers:
  - propose_archetype_renames: returns N suggestions, ranked by size
  - apply_archetype_renames: atomic, preserves canonical content
  - apply_archetype_renames: validation rejects bad targets
  - teach_profile_structured: renders canonical idiom format
  - teach_profile_structured: slug + size + status validation
  - server.py registers the three new tools

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/interview_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Isolated plugin data dir so we don't touch the user's real install.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_interview_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

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


from chameleon_mcp.tools import (  # noqa: E402
    apply_archetype_renames,
    bootstrap_repo,
    propose_archetype_renames,
    teach_profile,
    teach_profile_structured,
)


def _make_ts_repo(root: Path, *, components: int = 10, utils: int = 10) -> Path:
    """Build a minimal TS repo with two clearly-typed clusters."""
    (root / "package.json").write_text(
        '{"name": "x", "dependencies": {"typescript": "5.0.0"}}'
    )
    (root / "tsconfig.json").write_text(
        '{"compilerOptions": {"strict": true}}'
    )
    comp_dir = root / "src" / "components"
    comp_dir.mkdir(parents=True)
    for i in range(components):
        (comp_dir / f"c{i}.tsx").write_text(
            "export const C = () => <div>x</div>;\n"
        )
    util_dir = root / "src" / "utils"
    util_dir.mkdir()
    for i in range(utils):
        (util_dir / f"u{i}.ts").write_text(
            f"export const fn_{i} = () => {i};\n"
        )
    return root


# ---------------------------------------------------------------------------
section("Setup: bootstrap a synthetic TS repo")
TMP_REPO = Path(tempfile.mkdtemp(prefix="cham_interview_repo_"))
_make_ts_repo(TMP_REPO)
r = bootstrap_repo(str(TMP_REPO))
boot_data = r["data"]
t("bootstrap succeeds", boot_data["status"] == "success", str(boot_data))
t(
    "at least two archetypes detected",
    boot_data["archetypes_detected"] >= 2,
    f"got {boot_data['archetypes_detected']}",
)

archetypes_path = TMP_REPO / ".chameleon" / "archetypes.json"
canonicals_path = TMP_REPO / ".chameleon" / "canonicals.json"
summary_path = TMP_REPO / ".chameleon" / "profile.summary.md"
initial_archs = json.loads(archetypes_path.read_text())["archetypes"]
initial_names = sorted(initial_archs.keys())
print(f"  archetype names after bootstrap: {initial_names}")


# ---------------------------------------------------------------------------
section("propose_archetype_renames: shape + ranking")
p = propose_archetype_renames(str(TMP_REPO), top_n=8)
data = p["data"]
t("propose returns success", data.get("status") == "success", str(data))
t("response carries repo_id", isinstance(data.get("repo_id"), str) and len(data["repo_id"]) == 64)
t("response carries archetypes list", isinstance(data.get("archetypes"), list))
rows = data["archetypes"]
t("propose returned ≥2 rows", len(rows) >= 2, str(len(rows)))
t(
    "rows sorted by cluster_size descending",
    all(rows[i]["cluster_size"] >= rows[i+1]["cluster_size"] for i in range(len(rows)-1)),
)


# ---------------------------------------------------------------------------
section("propose_archetype_renames: each row has required fields")
row = rows[0]
for f in ("current_name", "cluster_size", "canonical_file", "paths_pattern", "suggested_alternatives"):
    t(f"row has {f!r}", f in row, str(row))
t(
    "suggested_alternatives is 3-5 items",
    3 <= len(row["suggested_alternatives"]) <= 5,
    str(row["suggested_alternatives"]),
)
t(
    "first alternative includes current name (no-rename option visible)",
    row["current_name"] in row["suggested_alternatives"],
)


# ---------------------------------------------------------------------------
section("propose_archetype_renames: top_n caps result count")
p_small = propose_archetype_renames(str(TMP_REPO), top_n=1)
t("top_n=1 returns one row", len(p_small["data"]["archetypes"]) == 1)


# ---------------------------------------------------------------------------
section("propose_archetype_renames: invalid top_n rejected")
for bad in (0, -1, 65, "x"):  # type: ignore[arg-type]
    bad_r = propose_archetype_renames(str(TMP_REPO), top_n=bad)
    t(f"top_n={bad!r} rejected", bad_r["data"].get("status") == "failed")


# ---------------------------------------------------------------------------
section("propose_archetype_renames: missing profile rejected")
no_prof = Path(tempfile.mkdtemp(prefix="cham_no_prof_"))
n = propose_archetype_renames(str(no_prof), top_n=4)
t("missing profile rejected", n["data"]["status"] == "failed")
shutil.rmtree(no_prof, ignore_errors=True)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: applies and preserves other data")
first = initial_names[0]
target = "my-renamed-archetype"
# Capture pre-state for the OTHER archetype's canonical content
other = initial_names[1]
pre_canonical_other = json.loads(canonicals_path.read_text())["canonicals"][other]

ap = apply_archetype_renames(str(TMP_REPO), {first: target})
ap_data = ap["data"]
t("apply returns success", ap_data["status"] == "success", str(ap_data))
t("renames_applied == 1", ap_data["renames_applied"] == 1)
t("new_profile_sha256 is a 64-char hex", len(ap_data.get("new_profile_sha256", "")) == 64)

post_archs = json.loads(archetypes_path.read_text())["archetypes"]
t("renamed key present", target in post_archs)
t("old key removed", first not in post_archs)
t("other archetype key untouched", other in post_archs)

post_canonicals = json.loads(canonicals_path.read_text())["canonicals"]
t("canonicals key renamed", target in post_canonicals)
t("old canonicals key removed", first not in post_canonicals)
t(
    "other archetype's canonical content unchanged (only keys move)",
    post_canonicals[other] == pre_canonical_other,
)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: renamed archetype data is byte-identical")
# Re-bootstrap a fresh repo so we can diff before/after at the value level.
fresh = Path(tempfile.mkdtemp(prefix="cham_fresh_"))
_make_ts_repo(fresh)
bootstrap_repo(str(fresh))
before_archs = json.loads((fresh / ".chameleon" / "archetypes.json").read_text())["archetypes"]
before_canonicals = json.loads((fresh / ".chameleon" / "canonicals.json").read_text())["canonicals"]

old_name = sorted(before_archs.keys())[0]
new_name = "shiny-new-name"
apply_archetype_renames(str(fresh), {old_name: new_name})
after_archs = json.loads((fresh / ".chameleon" / "archetypes.json").read_text())["archetypes"]
after_canonicals = json.loads((fresh / ".chameleon" / "canonicals.json").read_text())["canonicals"]

t(
    "renamed archetype value preserved bit-for-bit",
    after_archs[new_name] == before_archs[old_name],
)
t(
    "renamed canonical value preserved bit-for-bit",
    after_canonicals[new_name] == before_canonicals[old_name],
)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: profile.summary.md regenerated")
summary_after = (fresh / ".chameleon" / "profile.summary.md").read_text()
t(
    "summary mentions the new archetype name",
    f"**{new_name}**" in summary_after,
)
t(
    "summary no longer mentions the old archetype name",
    f"**{old_name}**" not in summary_after,
)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: validation")
bad_repo = Path(tempfile.mkdtemp(prefix="cham_bad_renames_"))
_make_ts_repo(bad_repo)
bootstrap_repo(str(bad_repo))
existing_names = sorted(
    json.loads((bad_repo / ".chameleon" / "archetypes.json").read_text())["archetypes"].keys()
)
a, b = existing_names[0], existing_names[1]

# Unknown source
r1 = apply_archetype_renames(str(bad_repo), {"no-such-arch": "anything"})
t("unknown source rejected", r1["data"]["status"] == "failed")

# Invalid target shape
r2 = apply_archetype_renames(str(bad_repo), {a: "Has Spaces"})
t("target with spaces rejected", r2["data"]["status"] == "failed")
r3 = apply_archetype_renames(str(bad_repo), {a: "1leading-digit"})
t("target with leading digit rejected", r3["data"]["status"] == "failed")

# Two renames colliding on the same target
r4 = apply_archetype_renames(str(bad_repo), {a: "x", b: "x"})
t("colliding targets rejected", r4["data"]["status"] == "failed")

# Target equal to an unrenamed existing name
r5 = apply_archetype_renames(str(bad_repo), {a: b})
t("target collides with existing non-renamed archetype", r5["data"]["status"] == "failed")

# No-op rename returns success with 0 applied
r6 = apply_archetype_renames(str(bad_repo), {a: a})
t("no-op rename succeeds with 0 applied", r6["data"]["status"] == "success" and r6["data"]["renames_applied"] == 0)

# Empty mapping is a clean no-op
r7 = apply_archetype_renames(str(bad_repo), {})
t("empty mapping succeeds with 0 applied", r7["data"]["status"] == "success" and r7["data"]["renames_applied"] == 0)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: swap rename (a→b, b→a)")
swap_repo = Path(tempfile.mkdtemp(prefix="cham_swap_"))
_make_ts_repo(swap_repo)
bootstrap_repo(str(swap_repo))
swap_names = sorted(
    json.loads((swap_repo / ".chameleon" / "archetypes.json").read_text())["archetypes"].keys()
)
sa, sb = swap_names[0], swap_names[1]
swap = apply_archetype_renames(str(swap_repo), {sa: sb, sb: sa})
# This must succeed: each source is being renamed out, so the "target
# collides with existing non-renamed archetype" rule doesn't fire.
t("swap rename succeeds", swap["data"]["status"] == "success", str(swap["data"]))
swap_archs = json.loads((swap_repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]
t("swap leaves both names present", sa in swap_archs and sb in swap_archs)


# ---------------------------------------------------------------------------
section("apply_archetype_renames: COMMITTED sentinel present after rename")
sentinel = bad_repo / ".chameleon" / "COMMITTED"
# Apply one valid rename to bad_repo so we can inspect post-state.
apply_archetype_renames(str(bad_repo), {a: "post-valid-name"})
t("COMMITTED sentinel exists", sentinel.is_file())


# ---------------------------------------------------------------------------
section("teach_profile_structured: renders canonical format")
struct_repo = Path(tempfile.mkdtemp(prefix="cham_struct_"))
_make_ts_repo(struct_repo)
bootstrap_repo(str(struct_repo))
idioms_path = struct_repo / ".chameleon" / "idioms.md"

r_struct = teach_profile_structured(
    str(struct_repo),
    slug="use-custom-query",
    rationale="Prefer useCustomQuery over useQuery for retry + error handling.",
    example="const { data } = useCustomQuery({ key: 'foo' });",
    counterexample="const { data } = useQuery({ key: 'foo' });",
    archetype="react-component",
)
t("structured teach succeeds", r_struct["data"]["status"] == "success", str(r_struct["data"]))

idioms_text = idioms_path.read_text()
t("slug heading written", "### use-custom-query" in idioms_text)
t("rationale text written", "Prefer useCustomQuery" in idioms_text)
t("example block written", "useCustomQuery({ key: 'foo' });" in idioms_text)
t("counterexample block written", "Counterexample:" in idioms_text and "useQuery({ key: 'foo' });" in idioms_text)
t("archetype line written", "Archetype: react-component" in idioms_text)
t("status active line written", "Status: active" in idioms_text)


# ---------------------------------------------------------------------------
section("teach_profile_structured: slug validation")
for bad_slug in ("AB", "ab", "1abc", "has space", "has_underscore", "way-too-long-" + "a" * 80):
    bad_r = teach_profile_structured(str(struct_repo), slug=bad_slug, rationale="x")
    t(f"bad slug {bad_slug!r} rejected", bad_r["data"]["status"] == "failed")

# Valid edge cases
ok = teach_profile_structured(str(struct_repo), slug="abc", rationale="three char slug ok")
t("3-char slug accepted (minimum)", ok["data"]["status"] == "success")
ok = teach_profile_structured(
    str(struct_repo),
    slug="a" + "b" * 63,  # 64 total
    rationale="64 char slug ok",
)
t("64-char slug accepted (max)", ok["data"]["status"] == "success")


# ---------------------------------------------------------------------------
section("teach_profile_structured: 50KB total cap")
big_rationale = "x" * 49_000
big_example = "y" * 1_500
big_counter = "z" * 500
# 49_000 + 1_500 + 500 = 51_000 > 50_000
over = teach_profile_structured(
    str(struct_repo),
    slug="too-big",
    rationale=big_rationale,
    example=big_example,
    counterexample=big_counter,
)
t("oversize structured idiom rejected", over["data"]["status"] == "failed" and "50KB" in over["data"]["error"])

# Just under the cap (leaving room for the rendered markdown wrapper —
# slug header, status line, fences — that teach_profile applies on top).
at_cap = teach_profile_structured(
    str(struct_repo),
    slug="just-fits",
    rationale="a" * 49_000,
    example="b" * 800,
)
t("under-cap structured idiom accepted", at_cap["data"]["status"] == "success", str(at_cap["data"]))


# ---------------------------------------------------------------------------
section("teach_profile_structured: empty rationale + bad status + bad archetype")
empty_rat = teach_profile_structured(str(struct_repo), slug="empty-rat", rationale="   ")
t("whitespace-only rationale rejected", empty_rat["data"]["status"] == "failed")

bad_st = teach_profile_structured(
    str(struct_repo), slug="bad-status", rationale="x", status="wat",
)
t("status not in {active, deprecated} rejected", bad_st["data"]["status"] == "failed")

bad_a = teach_profile_structured(
    str(struct_repo), slug="bad-arch", rationale="x", archetype="Bad Archetype Name",
)
t("archetype with spaces rejected", bad_a["data"]["status"] == "failed")


# ---------------------------------------------------------------------------
section("teach_profile_structured: deprecated status writes the right header")
dep = teach_profile_structured(
    str(struct_repo),
    slug="dep-test",
    rationale="don't do this any more",
    status="deprecated",
)
t("deprecated structured idiom succeeds", dep["data"]["status"] == "success")
text_after = idioms_path.read_text()
t("'Status: deprecated' present", "### dep-test" in text_after and "Status: deprecated" in text_after)


# ---------------------------------------------------------------------------
section("teach_profile_structured: backward-compat — free-form teach_profile still works")
free_r = teach_profile(str(struct_repo), "free-form idiom about banned imports")
t("free-form teach_profile still succeeds", free_r["data"]["status"] == "success")
text_after_free = idioms_path.read_text()
t("free-form text appears in idioms.md", "banned imports" in text_after_free)


# ---------------------------------------------------------------------------
section("teach_profile_structured: minimal call (no example/counterexample/archetype)")
mini = teach_profile_structured(
    str(struct_repo),
    slug="mini-idiom",
    rationale="just a one-liner",
)
t("minimal call succeeds", mini["data"]["status"] == "success")


# ---------------------------------------------------------------------------
section("Server tool registry includes new tools")
import chameleon_mcp.server as srv  # noqa: E402

registered = {tool.name for tool in srv.mcp._tool_manager.list_tools()}
for name in ("propose_archetype_renames", "apply_archetype_renames", "teach_profile_structured"):
    t(f"server registers {name!r}", name in registered, str(sorted(registered)))


# ---------------------------------------------------------------------------
section("Summary")
total = PASS + FAIL
print(f"\n  Total: {total}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
if FAIL:
    sys.exit(1)
sys.exit(0)
