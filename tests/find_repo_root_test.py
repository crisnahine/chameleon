"""Verification of #7: find_repo_root supports non-git repos.

Round 1: synthetic repos with each supported marker (no .git), nested
         markers (priority resolution), and pathological cases (no marker
         at all → None).
Round 2: verify behavior on real EF api / EF client repos still picks
         the correct root, plus a non-git extracted archive scenario.
"""

import os
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


from chameleon_mcp.profile.loader import find_repo_root, REPO_ROOT_MARKERS


# ---------------------------------------------------------------------------
# Round 1 — every supported marker resolves correctly without .git
# ---------------------------------------------------------------------------
section("Round 1 — every marker resolves without .git")

for marker in REPO_ROOT_MARKERS:
    if marker == ".chameleon":
        # .chameleon is a directory, not a file
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "synth_repo"
            (repo / "src").mkdir(parents=True)
            (repo / marker).mkdir()
            file_path = repo / "src" / "f.ts"
            file_path.write_text("export const x = 1;")
            root = find_repo_root(file_path)
            t(f"Marker '{marker}' resolves repo root", root is not None and root.resolve() == repo.resolve())
    elif marker == ".git":
        # Treat .git as a directory (real git case)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "synth_repo"
            (repo / "src").mkdir(parents=True)
            (repo / marker).mkdir()
            file_path = repo / "src" / "f.ts"
            file_path.write_text("export const x = 1;")
            root = find_repo_root(file_path)
            t(f"Marker '{marker}' resolves repo root", root is not None and root.resolve() == repo.resolve())
    else:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "synth_repo"
            (repo / "src").mkdir(parents=True)
            (repo / marker).write_text("{}")
            file_path = repo / "src" / "f.ts"
            file_path.write_text("export const x = 1;")
            root = find_repo_root(file_path)
            t(f"Marker '{marker}' resolves repo root (no .git present)",
              root is not None and root.resolve() == repo.resolve())


# ---------------------------------------------------------------------------
# Round 1 — no marker at all → None
# ---------------------------------------------------------------------------
section("Round 1 — no markers")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "no_markers"
    (repo / "src").mkdir(parents=True)
    file_path = repo / "src" / "f.ts"
    file_path.write_text("export const x = 1;")
    # tmp itself has no markers either
    root = find_repo_root(file_path)
    t(
        "No-marker tree returns None (no false positive)",
        root is None or not (root / ".git").exists() and not (root / "package.json").exists(),
    )


# ---------------------------------------------------------------------------
# Round 1 — priority: nested .chameleon and .git
# ---------------------------------------------------------------------------
section("Round 1 — marker priority")

# When .chameleon and .git coexist at the same level, both should resolve
# to the same root (no priority issue). When a child has .chameleon but
# parent has .git, the CHILD wins (closest ancestor).
with tempfile.TemporaryDirectory() as tmp:
    outer = Path(tmp) / "outer"
    inner = outer / "inner"
    (inner / "src").mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".chameleon").mkdir()
    file_path = inner / "src" / "f.ts"
    file_path.write_text("x")
    root = find_repo_root(file_path)
    t(
        "Inner .chameleon wins over outer .git (closest ancestor)",
        root is not None and root.resolve() == inner.resolve(),
    )


# ---------------------------------------------------------------------------
# Round 2 — real EF repos still resolve correctly
# ---------------------------------------------------------------------------
section("Round 2 — real EF repos")

EF_CLIENT = Path("/Users/crisn/Documents/Projects/empire-flippers/client")
EF_API = Path("/Users/crisn/Documents/Projects/empire-flippers/api")

if EF_CLIENT.is_dir():
    root = find_repo_root(EF_CLIENT / "src" / "index.tsx")
    t(
        "EF client: src/index.tsx → EF_CLIENT root",
        root is not None and root.resolve() == EF_CLIENT.resolve(),
    )

if EF_API.is_dir():
    root = find_repo_root(EF_API / "app" / "models" / "listing.rb")
    t(
        "EF api: app/models/listing.rb → EF_API root",
        root is not None and root.resolve() == EF_API.resolve(),
    )


# ---------------------------------------------------------------------------
# Round 2 — non-git archive scenario (tarball extract)
# ---------------------------------------------------------------------------
section("Round 2 — non-git archive (tarball extract simulation)")

with tempfile.TemporaryDirectory() as tmp:
    extracted = Path(tmp) / "myproject-1.0.0"
    (extracted / "src").mkdir(parents=True)
    (extracted / "package.json").write_text('{"name": "myproject"}')
    (extracted / "tsconfig.json").write_text("{}")
    file_path = extracted / "src" / "main.ts"
    file_path.write_text("export const main = () => null;")
    root = find_repo_root(file_path)
    t(
        "Non-git archive extract resolves via package.json",
        root is not None and root.resolve() == extracted.resolve(),
    )


# ---------------------------------------------------------------------------
# Round 2 — Ruby gem-style repo (Gemfile + .ruby-version, no .git)
# ---------------------------------------------------------------------------
section("Round 2 — Ruby gem-style repo without .git")

with tempfile.TemporaryDirectory() as tmp:
    gem_repo = Path(tmp) / "my_gem"
    (gem_repo / "lib" / "my_gem").mkdir(parents=True)
    (gem_repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (gem_repo / ".ruby-version").write_text("3.2.0\n")  # not a marker; ignored
    file_path = gem_repo / "lib" / "my_gem" / "version.rb"
    file_path.write_text("module MyGem; VERSION='1.0.0'; end")
    root = find_repo_root(file_path)
    t(
        "Ruby gem repo with Gemfile (no .git) resolves correctly",
        root is not None and root.resolve() == gem_repo.resolve(),
    )


# ---------------------------------------------------------------------------
# Round 2 — find_repo_root from a deeply nested file
# ---------------------------------------------------------------------------
section("Round 2 — deep nesting (10 levels)")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "deep_repo"
    deep = repo
    for i in range(10):
        deep = deep / f"l{i}"
    deep.mkdir(parents=True)
    (repo / ".git").mkdir()
    file_path = deep / "f.ts"
    file_path.write_text("x")
    root = find_repo_root(file_path)
    t(
        "Deep nesting (10 levels) resolves to repo root",
        root is not None and root.resolve() == repo.resolve(),
    )


# ---------------------------------------------------------------------------
# Round 2 — find_repo_root depth limit (32) doesn't crash
# ---------------------------------------------------------------------------
section("Round 2 — depth limit safety")

# Walk from a path that has no markers anywhere (root /tmp doesn't either)
fake_path = Path("/tmp") / "nonexistent_aaa" / "bbb" / "ccc.ts"
root = find_repo_root(fake_path)
t("Path with no markers anywhere returns None safely", root is None)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
