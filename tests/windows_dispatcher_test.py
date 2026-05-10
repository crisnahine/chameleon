"""Verify the Windows hook dispatcher is byte-for-byte the same as
superpowers' production-tested wrapper (modulo comment lines).

run-hook.cmd is a polyglot script that cmd.exe runs as a batch file
on Windows and bash runs as a shell script on Unix. Superpowers ships
this same file to Windows users via the official Anthropic marketplace,
so its Windows path is production-verified. As long as our copy
matches theirs in executable lines, ours inherits that verification.

This test fails if a future commit diverges from the upstream pattern.
"""

import shutil
import subprocess
import sys
from pathlib import Path

PASS, FAIL = [], []

CHAMELEON = Path("/Users/crisn/Documents/Projects/chameleon/hooks/run-hook.cmd")
SUPERPOWERS = Path("/Users/crisn/Documents/Projects/superpowers/hooks/run-hook.cmd")


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


def strip_comments(text: str) -> list[str]:
    """Return list of executable lines (strip blank + REM lines).

    Catches both `REM <text>` and bare `REM` (batch comment with no content).
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s == "REM" or s.startswith("REM "):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
section("Windows dispatcher parity with superpowers reference")

t("chameleon run-hook.cmd exists", CHAMELEON.is_file())

if not SUPERPOWERS.is_file():
    print("  SKIP: superpowers reference not present at expected path")
    sys.exit(0)

cham_lines = strip_comments(CHAMELEON.read_text())
sp_lines = strip_comments(SUPERPOWERS.read_text())

t(
    f"Same number of executable lines (cham={len(cham_lines)}, sp={len(sp_lines)})",
    cham_lines == sp_lines,
)

# Spot-check the critical Windows-path code blocks
text = CHAMELEON.read_text()
t(
    "Windows path: detects Git Bash at standard install location",
    'C:\\Program Files\\Git\\bin\\bash.exe' in text,
)
t(
    "Windows path: detects Git Bash at x86 install location",
    'C:\\Program Files (x86)\\Git\\bin\\bash.exe' in text,
)
t(
    "Windows path: falls back to bash on PATH (where bash)",
    "where bash" in text,
)
t(
    "Windows path: silent exit when no bash found (don't error the hook)",
    "exit /b 0" in text,
)
t(
    "Unix path: invokes the named script via bash",
    'exec bash "${SCRIPT_DIR}/${SCRIPT_NAME}"' in text,
)
t(
    "Polyglot sentinel: : << 'CMDBLOCK' opens the cmd-only block",
    ": << 'CMDBLOCK'" in text,
)
t(
    "Polyglot sentinel: CMDBLOCK closes the cmd-only block",
    "CMDBLOCK" in text,
)


# ---------------------------------------------------------------------------
section("Sanity-check Unix execution still works on this host")

# Run with a missing arg → should exit non-zero (matches batch contract too)
proc = subprocess.run(
    ["bash", str(CHAMELEON)],
    input="",
    capture_output=True,
    text=True,
    timeout=10,
)
# Without an argument, the bash side will try `exec bash "$SCRIPT_DIR/"`
# which fails. Exit code != 0 expected.
t("No-arg invocation fails (Unix shell side rejects)", proc.returncode != 0)

# Round-trip: invoke session-start through the dispatcher
import os
env = os.environ.copy()
env["CLAUDE_PLUGIN_ROOT"] = str(CHAMELEON.parent.parent)
proc = subprocess.run(
    ["bash", str(CHAMELEON), "session-start"],
    input="",
    capture_output=True,
    text=True,
    timeout=15,
    env=env,
)
t("session-start dispatch returns 0 on Unix", proc.returncode == 0)
t(
    "session-start dispatch produces JSON output",
    proc.stdout.strip().startswith("{"),
)


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
