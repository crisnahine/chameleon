"""Verification of BUG-NEW-002: find_repo_root must prefer .chameleon ancestor
over closer language-marker ancestors.

Pre-v0.5.7 behavior: walking up, the first ancestor with ANY marker won.
A monorepo with `<root>/.chameleon` and `<root>/apps/web/package.json` would
return `apps/web/` (because package.json there), masking the root profile.

Post-fix: two-pass walk. Pass 1 finds the deepest .chameleon ancestor.
Pass 2 falls back to first non-.chameleon marker.
"""

import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.profile.loader import find_repo_root

# ---------------------------------------------------------------------------
# Case 1: monorepo with .chameleon at root and package.json at workspace
# ---------------------------------------------------------------------------
section("Monorepo: .chameleon at root, package.json at workspace")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "monorepo"
    (root / ".chameleon").mkdir(parents=True)
    (root / ".chameleon" / "COMMITTED").touch()
    (root / ".git").mkdir()
    (root / "package.json").write_text('{"name":"monorepo","private":true}')

    ws = root / "apps" / "web"
    ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"name":"web"}')

    src = ws / "src" / "main.tsx"
    src.parent.mkdir(parents=True)
    src.write_text("export const x = 1;")

    found = find_repo_root(src)
    t("deep workspace file resolves to .chameleon root",
      found is not None and found.resolve() == root.resolve(),
      f"got {found}")

# ---------------------------------------------------------------------------
# Case 2: nested .chameleon (sub-workspace has its own) — sub wins
# ---------------------------------------------------------------------------
section("Nested .chameleon: deepest wins")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "monorepo2"
    (root / ".chameleon").mkdir(parents=True)
    (root / ".chameleon" / "COMMITTED").touch()

    ws = root / "packages" / "ui"
    ws.mkdir(parents=True)
    (ws / ".chameleon").mkdir()
    (ws / ".chameleon" / "COMMITTED").touch()
    (ws / "package.json").write_text("{}")

    src = ws / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("")

    found = find_repo_root(src)
    t("nested .chameleon takes priority over outer .chameleon",
      found is not None and found.resolve() == ws.resolve(),
      f"got {found}")

# ---------------------------------------------------------------------------
# Case 3: no .chameleon anywhere — fall back to nearest language marker
# (preserves the v0.5.5 behavior for fresh repos)
# ---------------------------------------------------------------------------
section("No .chameleon anywhere: nearest language marker wins")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "fresh-repo"
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text("{}")

    src = root / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("")

    found = find_repo_root(src)
    t("simple repo with no .chameleon resolves to .git root",
      found is not None and found.resolve() == root.resolve(),
      f"got {found}")

# ---------------------------------------------------------------------------
# Case 4: file outside any repo
# ---------------------------------------------------------------------------
section("File outside any repo: None")

with tempfile.TemporaryDirectory() as tmp:
    bare = Path(tmp) / "bare" / "x.ts"
    bare.parent.mkdir(parents=True)
    bare.write_text("")

    found = find_repo_root(bare)
    t("file with no marker ancestors returns None",
      found is None,
      f"got {found}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} — {info}")
    sys.exit(1)
