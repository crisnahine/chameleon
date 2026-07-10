"""Real-runtime check: hooks end-to-end + a bootstrap/trust/refresh lifecycle.

Runs on POSIX and Windows. On Windows it is the proof that the hook stack and
the locking engine actually work natively (the import-smoke and unit tests can't
catch a hook that fails-open because its venv-python path is POSIX-only).

Phase 1 - hook plumbing: drive each hook through run-hook.cmd with synthetic
stdin. A hook whose python path is wrong fails-open to "{}" (still valid JSON),
so the real signal is the hook error log: it must record no "failed (python="
line. Output must also be a valid JSON object.

Phase 2 - lifecycle: bootstrap -> trust -> refresh a throwaway TS repo, which
exercises the cross-platform locking + atomic commit under the real engine.

Exit non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
MCP_DIR = PLUGIN_DIR / "mcp"
RUN_HOOK = PLUGIN_DIR / "hooks" / "run-hook.cmd"
IS_WINDOWS = sys.platform == "win32"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def _invoke_hook(name: str, payload: str, env: dict) -> subprocess.CompletedProcess:
    # run-hook.cmd is a polyglot: cmd.exe on Windows, bash elsewhere. Either way
    # it locates bash and execs hooks/<name>, which is the real entry point.
    if IS_WINDOWS:
        cmd = ["cmd", "/c", str(RUN_HOOK), name]
    else:
        cmd = ["bash", str(RUN_HOOK), name]
    return subprocess.run(
        cmd, input=payload, capture_output=True, text=True, env=env, timeout=60
    )


def phase1_hooks() -> None:
    print("Phase 1: hook plumbing (run-hook.cmd -> bash -> venv python)")
    log_dir = Path(tempfile.mkdtemp(prefix="cham_hooklog_"))
    log_file = log_dir / "hook_errors.log"

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
    env["CHAMELEON_HOOK_ERROR_LOG"] = str(log_file)
    env["CHAMELEON_PLUGIN_DATA"] = str(log_dir / "data")

    hooks = [
        ("session-start", '{"session_id":"ci","hook_event_name":"SessionStart"}'),
        (
            "preflight-and-advise",
            '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x.ts"},"session_id":"ci"}',
        ),
        (
            "posttool-recorder",
            '{"tool_name":"Bash","tool_input":{"command":"echo hi"},'
            '"tool_response":{"stdout":"hi"},"session_id":"ci"}',
        ),
        (
            "posttool-verify",
            '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x.ts"},'
            '"tool_response":{"content":"const x = 1;","success":true},"session_id":"ci"}',
        ),
        ("callout-detector", '{"user_prompt":"hello world","session_id":"ci"}'),
    ]

    for name, payload in hooks:
        try:
            res = _invoke_hook(name, payload, env)
        except Exception as e:  # noqa: BLE001
            check(f"hook:{name}", False, f"invoke raised {e!r}")
            continue
        out = (res.stdout or "").strip()
        ok_json = False
        try:
            obj = json.loads(out or "{}", strict=False)
            ok_json = isinstance(obj, dict)
        except Exception as e:  # noqa: BLE001
            check(f"hook:{name}.json", False, f"bad JSON: {e}; out[:80]={out[:80]!r}")
        else:
            check(f"hook:{name}.json", ok_json, f"valid JSON dict (rc={res.returncode})")

    # The load-bearing assertion: no hook fell into the python-failure branch.
    log_text = log_file.read_text(errors="ignore") if log_file.is_file() else ""
    bad = [ln for ln in log_text.splitlines() if "failed (python=" in ln]
    check(
        "hooks.python_actually_ran",
        not bad,
        "no 'failed (python=' in error log" if not bad else f"FAILURES: {bad}",
    )
    # session-start emits the using-chameleon skill when python runs; prove the
    # process produced real output, not just a fail-open {}.
    ss = _invoke_hook("session-start", '{"session_id":"ci2","hook_event_name":"SessionStart"}', env)
    check(
        "hooks.session_start_has_content",
        "chameleon" in (ss.stdout or "").lower(),
        f"stdout len={len(ss.stdout or '')}",
    )


def phase2_lifecycle() -> None:
    print("Phase 2: bootstrap -> trust -> refresh lifecycle (locking + atomic commit)")
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    data = tempfile.mkdtemp(prefix="cham_data_")
    os.environ["CHAMELEON_PLUGIN_DATA"] = data
    work = tempfile.mkdtemp(prefix="cham_life_")
    repo = Path(work) / "demo"
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "hooks").mkdir(parents=True)
    for i in range(4):
        (repo / "src" / "components" / f"Card{i}.tsx").write_text(
            "import React from 'react';\n"
            f"export function Card{i}() {{\n  return <div>{i}</div>;\n}}\n"
        )
    for i in range(3):
        (repo / "src" / "hooks" / f"useThing{i}.ts").write_text(
            "import { useState } from 'react';\n"
            f"export function useThing{i}() {{\n  return useState(0);\n}}\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )

    sys.path.insert(0, str(MCP_DIR))
    from chameleon_mcp.tools import bootstrap_repo, refresh_repo, trust_profile

    cham = repo / ".chameleon"
    committed = cham / "COMMITTED"

    bootstrap_repo(str(repo))
    check("lifecycle.bootstrap_committed", committed.is_file())
    check("lifecycle.archetypes", (cham / "archetypes.json").is_file())

    (cham / "idioms.md").open("a").write("\n- Always wrap fetches in apiClient.\n")
    trust_profile(str(repo), repo.name)
    check("lifecycle.trust_state", Path(data).is_dir())

    refresh_repo(str(repo), force=True)
    check("lifecycle.refresh_committed", committed.is_file())
    check(
        "lifecycle.idiom_preserved",
        "apiClient" in (cham / "idioms.md").read_text(),
    )
    # POSIX must leave no sidecar; Windows uses one for the rename lock.
    sidecar = repo / ".chameleon.winlock"
    if IS_WINDOWS:
        check("lifecycle.no_committed_lock_in_profile", not (cham / ".chameleon.winlock").exists())
    else:
        check("lifecycle.no_winlock_on_posix", not sidecar.exists())


def main() -> int:
    print(f"platform={sys.platform} python={sys.version.split()[0]}")
    phase1_hooks()
    phase2_lifecycle()
    print()
    if fails:
        print(f"RUNTIME CHECK FAILED: {fails}")
        return 1
    print("RUNTIME CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
