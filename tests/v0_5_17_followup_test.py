"""Tests for the three open issues from the v0.5.16 external report.

Bug 1: get_rules — drop `archetype=` from the public schema; legacy
       callers still resolve via **kwargs but get a deprecation note.
Bug 2: disable_session — refuse marker write for unknown sessions
       unless force=True is passed.
Bug 3: daemon doctor check — "not running but lazy-spawnable" is ok,
       not warn.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp.tools import (  # noqa: E402
    bootstrap_repo,
    disable_session,
    doctor,
    get_rules,
    trust_profile,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _build_tiny_repo(td: Path) -> Path:
    repo = td / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"u_{i}.ts").write_text(f"export const x{i} = {i};\n", encoding="utf-8")
    return repo


section("Bug 1: get_rules removes 'archetype' from public schema")
sig = inspect.signature(get_rules)
public_params = [
    p.name for p in sig.parameters.values()
    if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
]
t(
    "public params are exactly ['repo', 'source']",
    public_params == ["repo", "source"],
    str(public_params),
)
t(
    "function still accepts **kwargs for back-compat",
    any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()),
)

with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_repo(Path(td))
    bootstrap_repo(str(repo))

    # source= is canonical
    r = get_rules(str(repo), "eslint")["data"]
    t("get_rules(repo, 'eslint') still works", isinstance(r.get("rules"), list))
    t("no deprecation when source= used", "deprecation" not in r)

    # archetype= still resolves but emits deprecation
    r = get_rules(str(repo), archetype="eslint")["data"]
    t(
        "legacy archetype= kwarg still resolves",
        isinstance(r.get("rules"), list),
    )
    t(
        "deprecation field cites v0.5.17 removal",
        "deprecation" in r and "v0.5.17" in r["deprecation"],
        r.get("deprecation", "")[:80],
    )

    # Unknown kwargs now error explicitly
    r = get_rules(str(repo), nonsense="x")["data"]
    t(
        "unknown kwarg returns failed envelope",
        r.get("status") == "failed" and "unexpected keyword" in r.get("error", ""),
        r.get("error", "")[:80],
    )


section("Bug 2: disable_session refuses unknown sessions unless force=True")
with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_repo(Path(td))
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)

    # Unknown session, no force → REFUSE (no marker written)
    r = disable_session(str(repo), "never-seen-session-x")["data"]
    t(
        "unknown session REFUSED without force",
        r.get("status") == "failed" and r.get("session_unknown_to_chameleon") is True,
        f"status={r.get('status')!r}",
    )
    t(
        "error message tells caller about force=True override",
        "force=True" in r.get("error", ""),
        r.get("error", "")[:80],
    )

    # Same call WITH force → marker written + warned
    r = disable_session(str(repo), "never-seen-session-x", force=True)["data"]
    t(
        "unknown session ACCEPTED with force=True",
        r.get("status") == "success",
        f"status={r.get('status')!r}",
    )
    t("marker write reports forced=True", r.get("forced") is True)
    t(
        "warning still surfaces even when forced",
        "warning" in r and "force=True" in r["warning"],
        r.get("warning", "")[:80],
    )


section("Bug 3: doctor reports daemon=ok when lazy (not warn)")
# Wipe any daemon socket so daemon_status reports not-alive
subprocess.run(["pkill", "-f", "chameleon_mcp.daemon"], capture_output=True, check=False)
time.sleep(0.5)

doc = doctor()["data"]
daemon_check = next(c for c in doc["checks"] if c["name"] == "daemon")
t(
    "daemon check level is 'ok' (lazy) or 'ok' (alive)",
    daemon_check.get("status") == "ok",
    str(daemon_check),
)


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
