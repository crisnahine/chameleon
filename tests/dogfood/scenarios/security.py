"""Phase 17.x: security scenarios."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx, suffix: str = "ts_minimal") -> Path:
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / suffix
    shutil.copytree(src, dest)
    return dest


def _set_env(ctx, plugin_data_override: Path | None = None) -> dict:
    saved = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    pd = plugin_data_override or ctx.plugin_data_dir
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(pd)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 17.1  Witness path traversal blocked
# ---------------------------------------------------------------------------

def _run_witness_path_traversal_blocked(ctx) -> Result:
    """Replace a canonical witness source file with a symlink to /etc/hostname.

    get_canonical_excerpt (via safe_open) must refuse to follow the symlink and
    must NOT return content from /etc/hostname.
    """
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx, "traversal_repo")
    canonicals_path = repo / ".chameleon" / "canonicals.json"
    if not canonicals_path.is_file():
        return Result(status="SKIP", notes="fixture has no .chameleon/canonicals.json")

    try:
        canonicals_data = json.loads(canonicals_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Result(status="SKIP", notes="canonicals.json is not valid JSON")

    # Find the first canonical witness path
    first_archetype: str | None = None
    first_witness_rel: str | None = None
    for archetype, entries in canonicals_data.get("canonicals", {}).items():
        for entry in entries:
            wp = entry.get("witness", {}).get("path")
            if wp:
                first_archetype = archetype
                first_witness_rel = wp
                break
        if first_archetype:
            break

    if first_witness_rel is None:
        return Result(status="SKIP", notes="no witness path found in canonicals.json")

    witness_abs = repo / first_witness_rel
    # Prefer /etc/hostname (small, safe); fall back to /etc/shells
    symlink_target = Path("/etc/hostname")
    if not symlink_target.exists():
        symlink_target = Path("/etc/shells")
    if not symlink_target.exists():
        return Result(status="SKIP", notes="no suitable /etc/* target for symlink test")

    # Read a few bytes of the target to know what to look for in the content
    try:
        target_bytes = symlink_target.read_bytes()[:64]
    except OSError:
        return Result(status="SKIP", notes=f"could not read symlink target {symlink_target}")

    # Replace witness file with a symlink to the external target
    if witness_abs.is_file() or witness_abs.is_symlink():
        witness_abs.unlink()
    try:
        witness_abs.symlink_to(symlink_target)
    except (OSError, NotImplementedError) as exc:
        return Result(
            status="SKIP",
            notes=f"could not create symlink at {witness_abs}: {exc}",
        )

    pd = ctx.plugin_data_dir / "pd_traversal"
    pd.mkdir(parents=True, exist_ok=True)

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp.tools import (  # type: ignore[import]
            get_canonical_excerpt,
            trust_profile,
        )

        # Bootstrap and trust (the COMMITTED sentinel is already there from the copy)
        trust_profile(str(repo), repo.name)

        resp = get_canonical_excerpt(str(repo), first_archetype)
    finally:
        _restore_env(saved)

    data = resp.get("data", {})
    content = data.get("content") or ""

    # Verify: content must not include anything from the symlink target
    # Check if any byte-sequence from the target appears in the content
    try:
        target_text = target_bytes.decode("utf-8", errors="replace").strip()
    except Exception:
        target_text = ""

    if target_text and target_text[:8] in content:
        return Result(
            status="FAIL",
            notes=(
                f"get_canonical_excerpt returned content from {symlink_target} "
                f"(first 8 chars {target_text[:8]!r} found in content); "
                f"symlink traversal NOT blocked"
            ),
        )

    # Also verify the call either returned empty content or a no_witness / failed status
    status_field = data.get("status", "")
    if content and status_field not in ("failed", "no_witness", ""):
        # Content was returned — must be empty string (safe_open raised UnsafeFileError)
        if content.strip():
            return Result(
                status="FAIL",
                notes=(
                    f"non-empty content returned despite symlink witness: "
                    f"content={content[:80]!r}; status={status_field!r}"
                ),
            )

    return Result(
        status="PASS",
        notes=(
            f"symlink witness {first_witness_rel} -> {symlink_target}: "
            f"content={content[:40]!r} (empty/safe), status={status_field!r} — traversal blocked"
        ),
    )


# ---------------------------------------------------------------------------
# 17.2  /tmp planted profile refused
# ---------------------------------------------------------------------------

def _run_tmp_planted_profile_refused(ctx) -> Result:
    """Plant .chameleon/ in /tmp; verify find_repo_root returns None for temp paths."""
    _ensure_mcp_on_path(ctx)

    # The CHAMELEON_ALLOW_TMP_REPO must NOT be set for this test to be meaningful.
    # Use an isolated env without that override.
    saved = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    os.environ.pop("CHAMELEON_ALLOW_TMP_REPO", None)
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)

    # Create a minimal planted .chameleon profile in /tmp
    planted_dir = Path("/tmp") / ".chameleon_dogfood_test_17_2"
    planted_sentinel = planted_dir / "COMMITTED"
    planted_profile = planted_dir / "profile.json"
    try:
        planted_dir.mkdir(parents=True, exist_ok=True)
        planted_sentinel.write_text("1", encoding="utf-8")
        planted_profile.write_text(
            json.dumps({"version": 1, "schema_version": 1, "archetypes": {}}),
            encoding="utf-8",
        )
        # Also create a plausible-looking .ts file under /tmp for the test
        test_ts = Path("/tmp") / "_dogfood_test_17_2.ts"
        test_ts.write_text("export const x = 1;\n", encoding="utf-8")

        try:
            from chameleon_mcp.profile.loader import find_repo_root  # type: ignore[import]
            result_root = find_repo_root(test_ts)
        finally:
            # Clean up /tmp pollution
            try:
                shutil.rmtree(planted_dir, ignore_errors=True)
            except OSError:
                pass
            try:
                test_ts.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if result_root is not None:
        return Result(
            status="FAIL",
            notes=(
                f"find_repo_root returned {result_root!r} for a /tmp path; "
                f"expected None (temp dirs must be refused)"
            ),
        )

    return Result(
        status="PASS",
        notes="find_repo_root returned None for /tmp path (temp dir refused as expected)",
    )


# ---------------------------------------------------------------------------
# 17.3  PYTHONPATH inheritance dropped
# ---------------------------------------------------------------------------

def _run_pythonpath_inheritance_dropped(ctx) -> Result:
    """All 4 hook scripts must use the isolated PYTHONPATH form.

    The safe form is:  PYTHONPATH="${MCP_DIR}" \\
    The unsafe form is: PYTHONPATH="${MCP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \\

    A hostile caller-inherited PYTHONPATH would let a planted package shadow
    chameleon_mcp imports. The hooks must drop any inherited value.
    """
    hook_names = (
        "preflight-and-advise",
        "posttool-recorder",
        "session-start",
        "callout-detector",
    )

    safe_pattern = 'PYTHONPATH="${MCP_DIR}"'
    unsafe_pattern_fragment = "PYTHONPATH:+"  # present in the unsafe form

    failures: list[str] = []
    skipped: list[str] = []

    for hook_name in hook_names:
        hook_path = ctx.plugin_root / "hooks" / hook_name
        if not hook_path.is_file():
            skipped.append(hook_name)
            continue

        text = hook_path.read_text(encoding="utf-8", errors="replace")

        has_safe = safe_pattern in text
        has_unsafe = unsafe_pattern_fragment in text

        if has_unsafe:
            failures.append(
                f"{hook_name}: contains unsafe PYTHONPATH inheritance "
                f"({unsafe_pattern_fragment!r})"
            )
        elif not has_safe:
            failures.append(
                f"{hook_name}: does not set PYTHONPATH at all (no {safe_pattern!r})"
            )
        # else: safe_pattern present and no unsafe fragment — good

    if skipped and len(skipped) == len(hook_names):
        return Result(status="SKIP", notes=f"all hook scripts missing: {skipped}")

    if failures:
        return Result(status="FAIL", notes="; ".join(failures))

    checked = [h for h in hook_names if h not in skipped]
    return Result(
        status="PASS",
        notes=(
            f"all {len(checked)} hook(s) use isolated PYTHONPATH form"
            + (f"; {len(skipped)} skipped (missing): {skipped}" if skipped else "")
        ),
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="17.1",
        name="witness path traversal blocked",
        family="security",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_witness_path_traversal_blocked,
    ),
    Scenario(
        id="17.2",
        name="/tmp planted profile refused",
        family="security",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_tmp_planted_profile_refused,
    ),
    Scenario(
        id="17.3",
        name="PYTHONPATH inheritance dropped in all hooks",
        family="security",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_pythonpath_inheritance_dropped,
    ),
]
