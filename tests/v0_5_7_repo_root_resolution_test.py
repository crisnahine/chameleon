"""BUG-NEW-002 (v0.5.7-redo): find_repo_root prefers .chameleon ancestor
over a closer language manifest, but only when one is genuinely
upstream (not a stray test fixture)."""

import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.profile.loader import find_repo_root

# Case 1: monorepo with .chameleon at root, package.json at workspace.
section("Monorepo: .chameleon at root masks workspace package.json")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "monorepo"
    (root / ".chameleon").mkdir(parents=True)
    (root / ".chameleon" / "COMMITTED").touch()
    (root / ".git").mkdir()
    (root / "package.json").write_text("{}")

    ws = root / "apps" / "web"
    ws.mkdir(parents=True)
    (ws / "package.json").write_text("{}")
    src = ws / "src" / "main.tsx"
    src.parent.mkdir(parents=True)
    src.write_text("")

    found = find_repo_root(src)
    t("monorepo workspace file -> root with .chameleon",
      found is not None and found.resolve() == root.resolve(),
      f"got {found}")

# Case 2: nested .chameleon (sub-workspace has its own).
section("Nested .chameleon wins")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "mono"
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
    t("nested .chameleon wins", found is not None and found.resolve() == ws.resolve(),
      f"got {found}")

# Case 3: no .chameleon anywhere, just .git + package.json.
section("Fresh repo with no .chameleon: nearest marker wins")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "fresh"
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text("{}")
    src = root / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("")
    found = find_repo_root(src)
    t("fresh repo resolves to root", found is not None and found.resolve() == root.resolve(),
      f"got {found}")

# Case 4: workspace with own package.json but parent has neither .git nor .chameleon.
section("Workspace with closer package.json, no upstream .chameleon")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "no-cham"
    root.mkdir()
    (root / "package.json").write_text("{}")
    ws = root / "apps" / "web"
    ws.mkdir(parents=True)
    (ws / "package.json").write_text("{}")
    src = ws / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("")
    found = find_repo_root(src)
    # Without an upstream .chameleon, behavior should match pre-fix:
    # nearest marker (workspace/package.json) wins.
    t("workspace with no upstream .chameleon -> workspace itself",
      found is not None and found.resolve() == ws.resolve(),
      f"got {found}")

# Case 5: file outside any repo.
section("Outside any repo")
with tempfile.TemporaryDirectory() as tmp:
    bare = Path(tmp) / "bare" / "x.ts"
    bare.parent.mkdir(parents=True)
    bare.write_text("")
    found = find_repo_root(bare)
    t("no markers anywhere -> None", found is None, f"got {found}")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
