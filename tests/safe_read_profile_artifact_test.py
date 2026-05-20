"""Unit tests for safe_open.safe_read_profile_artifact[_bytes].

Rec 12 introduced two new public helpers used by:
- profile.trust.hash_profile (bytes)
- profile.loader._safe_read_artifact (text)
- bootstrap.orchestrator._load_user_renames (text)
- tools._read_renames_overlay / _read_renames_overlay_strict (text)
- tools._refresh_repo_locked partial-bootstrap renames read (text)

These tests pin the security guarantees independently of the call sites.
Project convention: imperative tests, t(name, cond, info=...) for assertions,
exit 1 on any failure (matches tests/cold_start_init_test.py).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.bootstrap.orchestrator import _load_user_renames
from chameleon_mcp.profile.trust import hash_profile
from chameleon_mcp.safe_open import (
    UnsafeFileError,
    safe_read_profile_artifact,
    safe_read_profile_artifact_bytes,
)
from chameleon_mcp.tools import (
    _RenamesOverlayOverCap,
    _read_renames_overlay,
    _read_renames_overlay_strict,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _raises(callable_, *exc_types) -> tuple[bool, str]:
    try:
        callable_()
    except exc_types as exc:
        return True, type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        return False, f"wrong type: {type(exc).__name__}({exc})"
    return False, "no exception raised"


section("happy path")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "a.json"
    p.write_text('{"k": 1}', encoding="utf-8")
    t("text returns exact content", safe_read_profile_artifact(p) == '{"k": 1}')
    body = b'{"k": 1, "u": "\xc3\xa9"}'
    p.write_bytes(body)
    t("bytes returns exact bytes", safe_read_profile_artifact_bytes(p) == body)


section("symlink refusal (O_NOFOLLOW)")
with tempfile.TemporaryDirectory() as td:
    real = Path(td) / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = Path(td) / "link.json"
    link.symlink_to(real)
    ok, info = _raises(lambda: safe_read_profile_artifact(link), UnsafeFileError)
    t("text raises UnsafeFileError on symlink", ok, info)
    ok, info = _raises(lambda: safe_read_profile_artifact_bytes(link), UnsafeFileError)
    t("bytes raises UnsafeFileError on symlink", ok, info)


section("size cap")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "big.json"
    p.write_bytes(b"x" * 2000)
    ok, info = _raises(
        lambda: safe_read_profile_artifact(p, max_bytes=100),
        UnsafeFileError,
    )
    t("text refuses over-cap", ok, info)
    p.write_bytes(b"x" * 100)
    t(
        "text accepts at boundary (cap == size)",
        safe_read_profile_artifact(p, max_bytes=100) == "x" * 100,
    )


section("missing file passes FileNotFoundError through (not UnsafeFileError)")
ok, info = _raises(
    lambda: safe_read_profile_artifact(Path("/nonexistent/__test__.json")),
    FileNotFoundError,
)
t("missing -> FileNotFoundError", ok, info)


section("non-regular files refused")
with tempfile.TemporaryDirectory() as td:
    sub = Path(td) / "subdir"
    sub.mkdir()
    ok, info = _raises(lambda: safe_read_profile_artifact(sub), UnsafeFileError)
    t("directory refused", ok, info)


section("non-utf8 bytes decode with replacement (text reader)")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "bad.json"
    p.write_bytes(b"\xff\xfe abc")
    text = safe_read_profile_artifact(p)
    t("tail content survives invalid prefix", "abc" in text)


section("rec 12 integration: hash_profile sentinel distinguishes states")
with tempfile.TemporaryDirectory() as td:
    pd = Path(td)
    (pd / "profile.json").write_text(json.dumps({"generation": 1}), encoding="utf-8")
    h_baseline = hash_profile(pd)
    t("baseline hash non-empty", bool(h_baseline))

    (pd / "idioms.md").write_bytes(b"x" * (6 * 1024 * 1024))  # oversize
    h_unsafe = hash_profile(pd)
    t("oversized idioms.md produces distinct hash from absent", h_unsafe != h_baseline)

    (pd / "idioms.md").unlink()
    (pd / "idioms.md").write_text("small body", encoding="utf-8")
    h_present = hash_profile(pd)
    t(
        "present-in-cap is distinct from absent",
        h_present != h_baseline,
    )
    t(
        "present-in-cap is distinct from unsafe (no collision)",
        h_present != h_unsafe,
    )


section("rec 12 integration: overlay loaders drop trailing-newline values")
with tempfile.TemporaryDirectory() as td:
    pd = Path(td)
    (pd / "renames.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "renames": {"auto": "evil\n", "auto2": "good-name"},
            }
        ),
        encoding="utf-8",
    )
    r1 = _load_user_renames(pd)
    r2 = _read_renames_overlay(pd)
    t("orchestrator drops newline payload", r1 == {"auto2": "good-name"}, str(r1))
    t("tools drops newline payload", r2 == {"auto2": "good-name"}, str(r2))


section("rec 12 integration: strict overlay reader raises on over-cap")
with tempfile.TemporaryDirectory() as td:
    pd = Path(td)
    cap = threshold_int("RENAMES_OVERLAY_CAP")
    (pd / "renames.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "renames": {f"auto_{i}": f"name-{i}" for i in range(cap + 1)},
            }
        ),
        encoding="utf-8",
    )
    ok, info = _raises(lambda: _read_renames_overlay_strict(pd), _RenamesOverlayOverCap)
    t("strict reader raises on over-cap", ok, info)
    t("tolerant reader still returns {}", _read_renames_overlay(pd) == {})


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
