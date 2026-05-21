"""Pin the v0.5.14 bug-6 fix: brace expansion works in both dir AND basename.

Bug: paths_glob "{src,cypress}/**/*.{ts,tsx,js,jsx}" returned
`failed_unsupported_language` + "No source files found matching the
discovery glob". The old expander only handled the FIRST brace group,
so the second brace in the basename was passed through to
pathlib.glob — which doesn't expand braces — and returned zero
matches.

Fix: _expand_brace_groups now recursively expands ALL brace groups,
producing every combinatorial pattern. The orchestrator additionally
emits a clearer error when paths_glob has braces in the basename AND
still matched nothing, so a user who chose an empty scope gets a
specific message instead of the generic "No source files found".
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp.bootstrap.discovery import (  # noqa: E402
    _expand_brace_groups,
    _has_brace_in_basename,
    discover_files,
)
from chameleon_mcp.tools import bootstrap_repo  # noqa: E402

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


section("_expand_brace_groups handles all 4 cases")
t("no brace → identity", _expand_brace_groups("src/**/*.ts") == ["src/**/*.ts"])
t(
    "single brace in basename → expanded",
    sorted(_expand_brace_groups("src/**/*.{ts,tsx}")) == sorted(
        ["src/**/*.ts", "src/**/*.tsx"]
    ),
)
t(
    "single brace in dir → expanded",
    sorted(_expand_brace_groups("{src,cypress}/**/*.ts")) == sorted(
        ["src/**/*.ts", "cypress/**/*.ts"]
    ),
)
t(
    "double brace (dir + basename) → cross-product",
    sorted(_expand_brace_groups("{src,cypress}/**/*.{ts,tsx}")) == sorted(
        [
            "src/**/*.ts",
            "src/**/*.tsx",
            "cypress/**/*.ts",
            "cypress/**/*.tsx",
        ]
    ),
)


section("discover_files walks the cross-product (real filesystem)")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "src" / "b.tsx").write_text(
        "export const B = () => null;\n", encoding="utf-8"
    )
    (repo / "cypress").mkdir()
    (repo / "cypress" / "e2e.js").write_text(
        "export const e = 1;\n", encoding="utf-8"
    )
    (repo / "cypress" / "spec.jsx").write_text(
        "export const S = () => null;\n", encoding="utf-8"
    )
    # Out-of-scope (should NOT be picked up)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "ignore.ts").write_text(
        "export const x = 1;\n", encoding="utf-8"
    )
    # File NOT matched by glob (.py)
    (repo / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    files = discover_files(
        repo, paths_glob="{src,cypress}/**/*.{ts,tsx,js,jsx}"
    )
    names = sorted(p.name for p in files)
    t(
        "all 4 brace-expanded files discovered",
        names == ["a.ts", "b.tsx", "e2e.js", "spec.jsx"],
        str(names),
    )


section("bootstrap_repo succeeds on the double-brace pattern")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "src" / "b.tsx").write_text(
        "export const B = () => null;\n", encoding="utf-8"
    )

    resp = bootstrap_repo(str(repo), paths_glob="{src}/**/*.{ts,tsx}")
    data = resp.get("data", {})
    t(
        "bootstrap status=success on double-brace glob",
        data.get("status") == "success",
        f"status={data.get('status')!r}",
    )
    t(
        "files_processed >= 2 (both .ts and .tsx matched)",
        (data.get("files_processed") or 0) >= 2,
        f"files_processed={data.get('files_processed')}",
    )


section("orchestrator echoes the paths_glob when it matched nothing")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")
    # No .py / .rb files anywhere
    resp = bootstrap_repo(str(repo), paths_glob="src/**/*.{py,rb}")
    err = resp.get("data", {}).get("error", "")
    t(
        "error message echoes the offending glob",
        err and "src/**/*.{py,rb}" in err,
        f"error={err!r}",
    )


section("_has_brace_in_basename detection (no-regression guard)")
t(
    "brace in basename → True",
    _has_brace_in_basename("src/**/*.{ts,tsx}"),
)
t(
    "brace only in dir → False",
    not _has_brace_in_basename("{src,cypress}/**/*.ts"),
)


section("nested braces expand correctly (review finding)")
nested = sorted(_expand_brace_groups("{a,{b,c}}/*.ts"))
t(
    "nested {a,{b,c}}/*.ts → ['a/*.ts','b/*.ts','c/*.ts']",
    nested == sorted(["a/*.ts", "b/*.ts", "c/*.ts"]),
    str(nested),
)


section("malformed braces don't crash, pass through unchanged")
t(
    "unbalanced open brace → identity",
    _expand_brace_groups("foo{bar/*.ts") == ["foo{bar/*.ts"],
)
t(
    "empty body → identity",
    _expand_brace_groups("foo{}/*.ts") == ["foo{}/*.ts"],
)


section("exponential blowup is capped (review finding)")
# 4 alternatives × 6 levels = 4096 patterns; cap at 512 means we
# return at most 512 patterns instead of OOM/runaway.
pathological = (
    "{a,b,c,d}/{a,b,c,d}/{a,b,c,d}/{a,b,c,d}/{a,b,c,d}/{a,b,c,d}/*.ts"
)
out = _expand_brace_groups(pathological)
t(
    "pathological pattern bounded at cap (no OOM)",
    len(out) <= 512,
    f"got {len(out)} patterns (cap 512)",
)
t("cap output is non-empty (gave partial coverage)", len(out) > 0)


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
