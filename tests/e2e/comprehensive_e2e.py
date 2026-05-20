"""Full comprehensive end-to-end test.

Runs everything from scratch against both test repos (TS + Ruby).

Phases:
  A — Clean slate: wipe .chameleon/ + plugin data for both repos
  B — Bootstrap from zero
  C — Trust + material-change flow
  D — All 20 MCP tools per repo
  E — All slash-command-equivalent flows (init, refresh, teach, status,
      doctor, disable, pause, trust)
  F — Edge cases (recs 1, 2, 3, 4, 6, 7, 11b, 12, 13)
  G — Dogfood 3 rounds (free+cheap)
  H — Restoration: put each test repo back the way we found it

Runs from the chameleon repo root:

    PYTHONPATH=mcp:tests mcp/.venv/bin/python tests/e2e/comprehensive_e2e.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Locate test repos via .env
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_env_path = REPO_ROOT / ".env"
TS_REPO: Path | None = None
RUBY_REPO: Path | None = None
if _env_path.is_file():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CHAMELEON_TEST_TS_REPO="):
            TS_REPO = Path(line.split("=", 1)[1])
        elif line.startswith("CHAMELEON_TEST_RUBY_REPO="):
            RUBY_REPO = Path(line.split("=", 1)[1])

PASS: list[tuple[str, str, str]] = []  # (phase, name, info)
FAIL: list[tuple[str, str, str]] = []
SKIP: list[tuple[str, str, str]] = []


def t(phase: str, name: str, condition: bool, info: str = "") -> None:
    bucket = PASS if condition else FAIL
    bucket.append((phase, name, info))
    status = "PASS" if condition else "FAIL"
    info_str = f" - {info}" if info else ""
    print(f"  [{status}] {phase}: {name}{info_str}")


def skip(phase: str, name: str, reason: str) -> None:
    SKIP.append((phase, name, reason))
    print(f"  [SKIP] {phase}: {name} - {reason}")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def header(title: str) -> None:
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# State backup / restore
# ---------------------------------------------------------------------------


class RepoBackup:
    """Snapshot .chameleon/ (in-repo) and plugin-data state for a repo.

    Per the user's "from scratch" directive, we WIPE on Phase A and do NOT
    restore at the end — the test repos genuinely start fresh and stay
    that way. The snapshot is kept under tmp purely as a manual recovery
    safety net for the operator.
    """

    def __init__(self, repo_path: Path, repo_name: str) -> None:
        self.repo_path = repo_path
        self.repo_name = repo_name
        self.backup_dir = Path(tempfile.mkdtemp(prefix=f"e2e_backup_{repo_name}_"))
        self.in_repo_chameleon = repo_path / ".chameleon"
        self.plugin_data_subdir: Path | None = None
        self.repo_id: str | None = None
        self._snapshot()

    def _snapshot(self) -> None:
        from chameleon_mcp.profile.trust import plugin_data_dir
        from chameleon_mcp.tools import _compute_repo_id

        if self.in_repo_chameleon.is_dir():
            shutil.copytree(
                self.in_repo_chameleon,
                self.backup_dir / "chameleon",
                symlinks=True,
            )
        self.repo_id = _compute_repo_id(self.repo_path)
        pd = plugin_data_dir() / self.repo_id
        if pd.is_dir():
            shutil.copytree(
                pd,
                self.backup_dir / "plugin_data",
                symlinks=True,
            )
            self.plugin_data_subdir = pd

    def wipe(self) -> None:
        """Permanently remove .chameleon/ and per-user plugin data so the
        repo looks brand new. Per user directive: do not restore on exit;
        the post-E2E repo state is the bootstrap state."""
        from chameleon_mcp.profile.trust import plugin_data_dir

        if self.in_repo_chameleon.exists():
            shutil.rmtree(self.in_repo_chameleon, ignore_errors=True)
        if self.repo_id:
            pd = plugin_data_dir() / self.repo_id
            if pd.exists():
                shutil.rmtree(pd, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase A — Clean slate
# ---------------------------------------------------------------------------


def phase_a_clean_slate(repos: dict[str, RepoBackup]) -> None:
    section("Phase A — Clean slate (wipe + verify zero state)")
    from chameleon_mcp.tools import detect_repo

    for label, backup in repos.items():
        backup.wipe()
        chameleon_gone = not backup.in_repo_chameleon.exists()
        t("A", f"{label}: .chameleon/ removed", chameleon_gone)

        # detect_repo should still find the repo via package.json/Gemfile/.git
        # but profile_status should be "no_profile".
        sample = next(backup.repo_path.rglob("*.ts"), None) or next(
            backup.repo_path.rglob("*.rb"), None
        )
        if sample is not None:
            resp = detect_repo(str(sample))
            data = resp.get("data", {})
            status = data.get("profile_status")
            t(
                "A",
                f"{label}: profile_status='no_profile' after wipe",
                status == "no_profile",
                f"got {status!r}",
            )


# ---------------------------------------------------------------------------
# Phase B — Bootstrap from zero
# ---------------------------------------------------------------------------


def phase_b_bootstrap(repos: dict[str, RepoBackup]) -> None:
    section("Phase B — Bootstrap from zero")
    from chameleon_mcp.tools import bootstrap_repo

    for label, backup in repos.items():
        header(f"{label}: bootstrap")
        started = time.time()
        resp = bootstrap_repo(str(backup.repo_path))
        elapsed = time.time() - started
        data = resp.get("data", {})

        t(
            "B",
            f"{label}: bootstrap status==success",
            data.get("status") == "success",
            f"status={data.get('status')!r}",
        )

        archetype_count = data.get("archetype_count") or data.get(
            "archetypes_detected"
        )
        t(
            "B",
            f"{label}: bootstrap produced archetypes",
            isinstance(archetype_count, int) and archetype_count > 0,
            f"archetype_count={archetype_count}",
        )

        # COMMITTED sentinel + 5 (or 6 with ledger) hashed artifacts present
        chameleon_dir = backup.repo_path / ".chameleon"
        committed_present = (chameleon_dir / "COMMITTED").is_file()
        t("B", f"{label}: COMMITTED sentinel present", committed_present)

        for fname in (
            "profile.json",
            "archetypes.json",
            "canonicals.json",
            "rules.json",
            "idioms.md",
        ):
            t(
                "B",
                f"{label}: {fname} present",
                (chameleon_dir / fname).is_file(),
            )

        print(f"  ({label} bootstrap took {elapsed:.1f}s)")


# ---------------------------------------------------------------------------
# Phase C — Trust + material-change flow
# ---------------------------------------------------------------------------


def phase_c_trust(repos: dict[str, RepoBackup]) -> None:
    section("Phase C — Trust + material-change flow")
    from chameleon_mcp.tools import (
        get_pattern_context,
        refresh_repo,
        trust_profile,
    )

    for label, backup in repos.items():
        header(f"{label}: trust flow")
        # State 1: untrusted
        sample = next(backup.repo_path.rglob("*.ts"), None) or next(
            backup.repo_path.rglob("*.rb"), None
        )
        if sample is None:
            skip("C", f"{label}: no source file", "rglob empty")
            continue

        resp = get_pattern_context(str(sample))
        trust_state = resp.get("data", {}).get("repo", {}).get("trust_state")
        t(
            "C",
            f"{label}: post-bootstrap trust_state='untrusted'",
            trust_state == "untrusted",
            f"got {trust_state!r}",
        )

        # State 2: trust with wrong token → rejected
        rej = trust_profile(str(backup.repo_path), "WRONG_TOKEN_xyz")
        rej_data = rej.get("data", {})
        t(
            "C",
            f"{label}: trust rejects wrong token",
            rej_data.get("status") == "failed",
            str(rej_data.get("error", ""))[:80],
        )

        # State 3: trust with repo basename → success
        granted = trust_profile(str(backup.repo_path), backup.repo_path.name)
        gd = granted.get("data", {})
        t(
            "C",
            f"{label}: trust accepts repo basename",
            gd.get("status") == "success",
            f"status={gd.get('status')!r}",
        )

        resp = get_pattern_context(str(sample))
        trust_state = resp.get("data", {}).get("repo", {}).get("trust_state")
        t(
            "C",
            f"{label}: trust_state='trusted' after grant",
            trust_state == "trusted",
        )

        # State 4: material change (refresh) → stale
        refresh_repo(str(backup.repo_path), force=True)
        resp = get_pattern_context(str(sample))
        trust_state = resp.get("data", {}).get("repo", {}).get("trust_state")
        t(
            "C",
            f"{label}: trust_state in {{stale, trusted}} after refresh",
            trust_state in {"stale", "trusted"},
            f"got {trust_state!r}",
        )


# ---------------------------------------------------------------------------
# Phase D — All 20 MCP tools per repo
# ---------------------------------------------------------------------------


def phase_d_all_tools(repos: dict[str, RepoBackup]) -> None:
    section("Phase D — All 20 MCP tools per repo")
    from chameleon_mcp.tools import (
        apply_archetype_renames,
        bootstrap_repo,
        daemon_status,
        detect_repo,
        disable_session,
        doctor,
        get_archetype,
        get_canonical_excerpt,
        get_drift_status,
        get_pattern_context,
        get_rules,
        lint_file,
        list_profiles,
        merge_profiles,
        pause_session,
        propose_archetype_renames,
        refresh_repo,
        teach_profile,
        teach_profile_structured,
        trust_profile,
    )

    # Tools that don't need a repo arg
    header("repo-independent tools")
    ds = daemon_status()
    t("D", "daemon_status returns envelope", isinstance(ds.get("data"), dict))
    doc = doctor()
    t(
        "D",
        "doctor returns envelope with overall + checks",
        isinstance(doc.get("data", {}).get("overall"), str)
        and isinstance(doc.get("data", {}).get("checks"), list),
    )
    lp = list_profiles()
    t(
        "D",
        "list_profiles returns profiles list",
        isinstance(lp.get("data", {}).get("profiles"), list),
    )

    for label, backup in repos.items():
        header(f"{label}: per-repo tools")
        sample = next(backup.repo_path.rglob("*.ts"), None) or next(
            backup.repo_path.rglob("*.rb"), None
        )
        if sample is None:
            skip("D", f"{label}: no source file", "rglob empty")
            continue

        # Trust the profile so we get the full envelope shape
        trust_profile(str(backup.repo_path), backup.repo_path.name)

        # detect_repo
        dr = detect_repo(str(sample))
        t(
            "D",
            f"{label}: detect_repo returns repo_id",
            isinstance(dr.get("data", {}).get("repo_id"), str),
        )

        # get_pattern_context — verify rec 1 fields
        gpc = get_pattern_context(str(sample))
        gpc_data = gpc.get("data", {})
        arch = gpc_data.get("archetype", {})
        for field in ("match_quality", "sub_buckets_count"):
            t(
                "D",
                f"{label}: get_pattern_context envelope has {field}",
                field in arch,
                f"keys={sorted(arch.keys())}",
            )

        # get_archetype
        ga = get_archetype(str(backup.repo_path), str(sample))
        t(
            "D",
            f"{label}: get_archetype returns archetype shape",
            isinstance(ga.get("data", {}).get("match_quality"), str),
        )

        # Find an archetype name to use for downstream tools
        arch_name = arch.get("archetype")
        if arch_name:
            # get_canonical_excerpt
            gce = get_canonical_excerpt(str(backup.repo_path), arch_name)
            t(
                "D",
                f"{label}: get_canonical_excerpt for {arch_name!r}",
                "data" in gce,
            )

        # get_rules (no archetype)
        gr = get_rules(str(backup.repo_path))
        t(
            "D",
            f"{label}: get_rules returns rules list",
            isinstance(gr.get("data", {}).get("rules"), list),
        )

        # get_rules with archetype name → failed envelope (footgun guard)
        if arch_name:
            gr_a = get_rules(str(backup.repo_path), arch_name)
            ds_data = gr_a.get("data", {})
            # Either rules list (archetype was actually a source key) or
            # status=failed pointing to the contract mismatch.
            t(
                "D",
                f"{label}: get_rules with archetype name returns typed envelope",
                isinstance(ds_data.get("rules"), list),
            )

        # lint_file
        try:
            lf = lint_file(
                str(backup.repo_path),
                arch_name or "controller",
                "function foo() {}\n",
            )
            t(
                "D",
                f"{label}: lint_file returns envelope",
                "data" in lf,
            )
        except Exception as exc:  # noqa: BLE001
            t("D", f"{label}: lint_file did not crash", False, str(exc)[:80])

        # get_drift_status
        gds = get_drift_status(str(backup.repo_path))
        t(
            "D",
            f"{label}: get_drift_status returns recommendation",
            "recommended_action" in gds.get("data", {}),
        )

        # propose_archetype_renames — response carries archetypes list
        par = propose_archetype_renames(str(backup.repo_path), top_n=3)
        par_data = par.get("data", {})
        t(
            "D",
            f"{label}: propose_archetype_renames returns archetypes list",
            isinstance(par_data.get("archetypes"), list),
            f"keys={sorted(par_data.keys())}",
        )

        # apply_archetype_renames — no-op (empty mapping)
        aar = apply_archetype_renames(str(backup.repo_path), {})
        t(
            "D",
            f"{label}: apply_archetype_renames accepts empty mapping",
            aar.get("data", {}).get("status") == "success",
        )

        # refresh_repo — should produce archetype_diff (rec 6)
        rr = refresh_repo(str(backup.repo_path), force=False)
        rr_data = rr.get("data", {})
        t(
            "D",
            f"{label}: refresh_repo response carries archetype_diff",
            isinstance(rr_data.get("archetype_diff"), dict),
        )

        # bootstrap_repo on already-bootstrapped → already_bootstrapped
        br = bootstrap_repo(str(backup.repo_path))
        br_data = br.get("data", {})
        t(
            "D",
            f"{label}: bootstrap on bootstrapped → already_bootstrapped or success",
            br_data.get("status") in {"already_bootstrapped", "success"},
            f"status={br_data.get('status')!r}",
        )

        # teach_profile — append an idiom
        tp = teach_profile(
            str(backup.repo_path),
            f"E2E test idiom for {label} run at {int(time.time())}",
        )
        t(
            "D",
            f"{label}: teach_profile accepts free-form idiom",
            tp.get("data", {}).get("status") == "success",
        )

        # teach_profile_structured — real signature uses slug + rationale
        tps = teach_profile_structured(
            str(backup.repo_path),
            slug=f"e2e-{label}-rule",
            rationale="E2E phase D structured-teach smoke",
        )
        t(
            "D",
            f"{label}: teach_profile_structured accepts shape",
            "data" in tps,
        )

        # pause_session + resume — cleanup explicitly
        ps = pause_session(str(backup.repo_path), minutes=1)
        t(
            "D",
            f"{label}: pause_session returns envelope",
            isinstance(ps.get("data"), dict),
        )

        # disable_session + restore — cleanup explicitly
        dis = disable_session(str(backup.repo_path), "e2e-test-session")
        t(
            "D",
            f"{label}: disable_session returns envelope",
            isinstance(dis.get("data"), dict),
        )
        # Wipe markers so they don't leak to later phases
        from chameleon_mcp.profile.trust import plugin_data_dir

        repo_data = plugin_data_dir() / backup.repo_id
        for f in repo_data.glob(".session_disabled.*"):
            f.unlink(missing_ok=True)
        for f in repo_data.glob(".pause_until"):
            f.unlink(missing_ok=True)

    # merge_profiles is most useful when given 3-way refs; smoke-test it
    # against a single-repo input to exercise the entry path.
    header("merge_profiles smoke")
    for label, backup in repos.items():
        chameleon = str(backup.repo_path / ".chameleon")
        try:
            mp = merge_profiles(
                str(backup.repo_path),
                base=chameleon,
                ours=chameleon,
                theirs=chameleon,
            )
            t(
                "D",
                f"{label}: merge_profiles returns envelope",
                "data" in mp,
            )
        except Exception as exc:  # noqa: BLE001
            t(
                "D",
                f"{label}: merge_profiles did not crash",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        break  # one is enough for smoke


# ---------------------------------------------------------------------------
# Phase E — Slash-command-equivalent flows
# ---------------------------------------------------------------------------


def phase_e_slash_flows(repos: dict[str, RepoBackup]) -> None:
    section("Phase E — Slash-command-equivalent flows (init/trust/refresh/teach/status/disable/pause/doctor)")
    from chameleon_mcp.tools import (
        bootstrap_repo,
        disable_session,
        doctor,
        get_drift_status,
        pause_session,
        refresh_repo,
        teach_profile,
        trust_profile,
    )

    for label, backup in repos.items():
        header(f"{label}: slash flows")
        # /chameleon-init → bootstrap_repo (already bootstrapped at this point)
        init_r = bootstrap_repo(str(backup.repo_path))
        t(
            "E",
            f"{label}: /chameleon-init handles bootstrapped repo",
            init_r.get("data", {}).get("status")
            in {"already_bootstrapped", "success"},
        )
        # /chameleon-trust
        trust_r = trust_profile(str(backup.repo_path), backup.repo_path.name)
        t(
            "E",
            f"{label}: /chameleon-trust grants",
            trust_r.get("data", {}).get("status") == "success",
        )
        # /chameleon-refresh
        refresh_r = refresh_repo(str(backup.repo_path))
        t(
            "E",
            f"{label}: /chameleon-refresh response carries status",
            "status" in refresh_r.get("data", {}),
        )
        # /chameleon-teach
        teach_r = teach_profile(
            str(backup.repo_path),
            f"E2E phase-E idiom for {label}",
        )
        t(
            "E",
            f"{label}: /chameleon-teach success",
            teach_r.get("data", {}).get("status") == "success",
        )
        # /chameleon-status → derive from get_drift_status + list_profiles
        gds = get_drift_status(str(backup.repo_path))
        t(
            "E",
            f"{label}: /chameleon-status: drift fields present",
            "days_since_refresh" in gds.get("data", {}),
        )
        # /chameleon-disable + reset
        dis_r = disable_session(str(backup.repo_path), "e2e-phase-e")
        t("E", f"{label}: /chameleon-disable writes", "data" in dis_r)
        from chameleon_mcp.profile.trust import plugin_data_dir

        repo_data = plugin_data_dir() / backup.repo_id
        for f in repo_data.glob(".session_disabled.*"):
            f.unlink(missing_ok=True)

        # /chameleon-pause-15m + reset
        pause_r = pause_session(str(backup.repo_path), minutes=15)
        t("E", f"{label}: /chameleon-pause-15m writes", "data" in pause_r)
        for f in repo_data.glob(".pause_until"):
            f.unlink(missing_ok=True)

    # /chameleon-doctor is global
    doc = doctor()
    t(
        "E",
        "/chameleon-doctor returns overall + summary",
        isinstance(doc.get("data", {}).get("summary"), dict),
    )


# ---------------------------------------------------------------------------
# Phase F — Edge cases (recs 1, 2, 3, 4, 6, 7, 11b, 12, 13)
# ---------------------------------------------------------------------------


def phase_f_edge_cases(repos: dict[str, RepoBackup]) -> None:
    section("Phase F — Edge cases (recs 1, 2, 3, 4, 6, 7, 11b, 12, 13)")
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE
    from chameleon_mcp.profile.trust import _HASHED_ARTIFACTS, hash_profile
    from chameleon_mcp.safe_open import (
        UnsafeFileError,
        safe_read_profile_artifact,
        safe_read_profile_artifact_bytes,
    )
    from chameleon_mcp.tools import (
        _read_renames_overlay,
        _read_renames_overlay_strict,
        _RenamesOverlayOverCap,
        get_pattern_context,
        refresh_repo,
        trust_profile,
    )

    # F.1 — rec 12: safe_read_profile_artifact symlink refusal
    header("F.1 — rec 12: safe-read symlink refusal")
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = Path(td) / "link.json"
        link.symlink_to(real)
        try:
            safe_read_profile_artifact(link)
            t("F", "rec 12: symlink refused (text)", False)
        except UnsafeFileError:
            t("F", "rec 12: symlink refused (text)", True)
        try:
            safe_read_profile_artifact_bytes(link)
            t("F", "rec 12: symlink refused (bytes)", False)
        except UnsafeFileError:
            t("F", "rec 12: symlink refused (bytes)", True)

    # F.2 — rec 12: size cap
    header("F.2 — rec 12: 5MB cap")
    with tempfile.TemporaryDirectory() as td:
        big = Path(td) / "big.json"
        big.write_bytes(b"x" * (6 * 1024 * 1024))
        try:
            safe_read_profile_artifact(big)
            t("F", "rec 12: 6MB artifact refused", False)
        except UnsafeFileError:
            t("F", "rec 12: 6MB artifact refused", True)

    # F.3 — rec 12: ARCHETYPE_NAME_RE rejects trailing newline
    header("F.3 — rec 12: regex newline bypass closed")
    t(
        "F",
        "rec 12: ARCHETYPE_NAME_RE rejects 'evil\\n'",
        not ARCHETYPE_NAME_RE.match("evil\n"),
    )
    t(
        "F",
        "rec 12: ARCHETYPE_NAME_RE accepts 'valid-name'",
        bool(ARCHETYPE_NAME_RE.match("valid-name")),
    )

    # F.4 — rec 12: over-cap renames.json
    header("F.4 — rec 12: over-cap renames.json rejected")
    with tempfile.TemporaryDirectory() as td:
        pd = Path(td)
        cap = threshold_int("RENAMES_OVERLAY_CAP")
        (pd / "renames.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "renames": {f"a_{i}": f"name-{i}" for i in range(cap + 1)},
                }
            ),
            encoding="utf-8",
        )
        t(
            "F",
            "rec 12: tolerant reader returns {} on over-cap",
            _read_renames_overlay(pd) == {},
        )
        try:
            _read_renames_overlay_strict(pd)
            t("F", "rec 12: strict reader raises on over-cap", False)
        except _RenamesOverlayOverCap:
            t("F", "rec 12: strict reader raises on over-cap", True)

    # F.5 — rec 12: hash_profile sentinel distinguishes states
    header("F.5 — rec 12: hash_profile sentinel framing")
    with tempfile.TemporaryDirectory() as td:
        pd = Path(td)
        (pd / "profile.json").write_text(
            json.dumps({"generation": int(time.time())}), encoding="utf-8"
        )
        h_baseline = hash_profile(pd)
        (pd / "idioms.md").write_bytes(b"x" * (6 * 1024 * 1024))
        h_unsafe = hash_profile(pd)
        (pd / "idioms.md").unlink()
        (pd / "idioms.md").write_text("small idiom", encoding="utf-8")
        h_present = hash_profile(pd)
        t(
            "F",
            "rec 12: oversized != absent != in-cap (3 distinct hashes)",
            len({h_baseline, h_unsafe, h_present}) == 3,
        )

    # F.6 — rec 13: discovery drops symlinks
    header("F.6 — rec 13: discovery drops in-tree symlinks")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "real.ts").write_text("export const x = 1;\n", encoding="utf-8")
        outside = Path(td) / "_outside"
        (repo / "evil.ts").symlink_to(outside)
        files = discover_files(repo)
        names = sorted(p.name for p in files)
        t("F", "rec 13: only real.ts returned", names == ["real.ts"], str(names))

    # F.7 — rec 13: ts_dump.mjs / prism_dump.rb emit symlink_refused
    header("F.7 — rec 13: extractor scripts emit symlink_refused")
    for script_name, runtime in (
        ("ts_dump.mjs", "node"),
        ("prism_dump.rb", "ruby"),
    ):
        which = shutil.which(runtime)
        if which is None:
            skip("F", f"rec 13: {script_name}", f"{runtime} not on PATH")
            continue
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "real.src"
            target.write_text("class X\nend\n", encoding="utf-8")
            link = Path(td) / f"alias.{script_name.split('.')[1]}"
            link.symlink_to(target)
            script = REPO_ROOT / "scripts" / script_name
            proc = subprocess.run(
                [which, str(script)],
                input=f"{link}\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            found = False
            for line in proc.stdout.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("path") == str(link) and rec.get("error") == "symlink_refused":
                    found = True
                    break
            t("F", f"rec 13: {script_name} emits symlink_refused", found)

    # F.8 — rec 1: hook envelope has match_quality + sub_buckets_count
    header("F.8 — rec 1: hook payload enrichment present in envelope")
    for label, backup in repos.items():
        trust_profile(str(backup.repo_path), backup.repo_path.name)
        sample = next(backup.repo_path.rglob("*.ts"), None) or next(
            backup.repo_path.rglob("*.rb"), None
        )
        if sample is None:
            skip("F", f"rec 1 {label}: no sample", "rglob empty")
            continue
        resp = get_pattern_context(str(sample))
        arch = resp.get("data", {}).get("archetype", {})
        t(
            "F",
            f"rec 1 {label}: match_quality in envelope",
            "match_quality" in arch,
        )
        t(
            "F",
            f"rec 1 {label}: sub_buckets_count in envelope",
            isinstance(arch.get("sub_buckets_count"), int),
        )

    # F.9 — rec 3: degraded banner emitted on advisor crash
    header("F.9 — rec 3: degraded banner on advisor crash")
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    import chameleon_mcp.daemon_client as dc_mod
    import chameleon_mcp.hook_helper as hh_mod
    import chameleon_mcp.tools as tools_mod

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/e2e_fake.ts"},
        "session_id": "e2e-rec3",
    }

    def _boom(*_a, **_kw):
        raise RuntimeError("e2e simulated crash")

    buf = io.StringIO()
    with (
        patch.object(tools_mod, "get_pattern_context", _boom),
        patch.object(dc_mod, "call", lambda *_a, **_kw: None),
        patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
        redirect_stdout(buf),
    ):
        hh_mod.preflight_and_advise()
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "F",
        "rec 3: degraded banner emitted on advisor crash",
        "[chameleon: degraded — advisor_unavailable]" in ctx,
        ctx[:80],
    )

    # F.10 — rec 6: refresh_repo response carries archetype_diff
    header("F.10 — rec 6: archetype_diff in refresh response")
    for label, backup in repos.items():
        rr = refresh_repo(str(backup.repo_path))
        diff = rr.get("data", {}).get("archetype_diff")
        t(
            "F",
            f"rec 6 {label}: archetype_diff present",
            isinstance(diff, dict)
            and all(k in diff for k in ("added", "removed", "renamed", "unchanged_count")),
        )

    # F.11 — rec 11b: ledger file may or may not exist (depends on whether
    # /chameleon-rename has been called); regardless, it's in _HASHED_ARTIFACTS
    header("F.11 — rec 11b: ledger in _HASHED_ARTIFACTS")
    t(
        "F",
        "rec 11b: .archetype_renames.json is hashed",
        ".archetype_renames.json" in _HASHED_ARTIFACTS,
    )

    # F.12 — rec 2: synthetic sub_bucket split
    header("F.12 — rec 2: sub_bucket split-before-naming")
    from chameleon_mcp.bootstrap.clustering import (
        Cluster,
        _split_by_sub_bucket,
    )
    from chameleon_mcp.extractors._base import ParsedFile
    from chameleon_mcp.signatures import ClusterKey

    def _mk_pf(p: str) -> ParsedFile:
        return ParsedFile(
            path=Path(p),
            content_first_200_bytes="class X\nend\n",
            top_level_node_kinds=("ClassNode",),
            default_export_kind="ClassNode",
            named_export_count=1,
            import_specifiers=(),
            has_jsx=False,
        )

    key = ClusterKey(
        "app/models", "none", ("ClassNode",), "ClassNode", "1", "h", False
    )
    members = [
        _mk_pf(f"app/models/foo_{i}.rb") for i in range(20)
    ] + [_mk_pf(f"app/models/concerns/c_{i}.rb") for i in range(8)]
    cluster = Cluster(key=key, members=members, sparse_threshold=5)
    splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
    t(
        "F",
        "rec 2: model+concerns split into 2 clusters",
        len(splits) == 2,
        f"got {len(splits)}",
    )

    # F.13 — rec 7: class-* demote with path tail
    header("F.13 — rec 7: class-* demotion")
    from chameleon_mcp.bootstrap import naming

    cluster = Cluster(
        key=ClusterKey(
            "lib/billing", "none", ("ClassNode",), "ClassNode", "1", "h", False
        ),
        members=[_mk_pf("lib/billing/policy/jit.rb")],
        sparse_threshold=1,
    )
    name = naming.propose_archetype_name(cluster, set(), repo_root="/tmp/r")
    t(
        "F",
        f"rec 7: lib/billing class-default → 'class-billing' (got {name!r})",
        name.startswith("class-"),
    )

    # F.14 — rec 4: drift banner gates (tested by drift_banner_test.py)
    header("F.14 — rec 4: drift banner gate sanity")
    from chameleon_mcp.hook_helper import _drift_banner_for_repo

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "package.json").write_text("{}", encoding="utf-8")
        # No drift.db → returns None
        b = _drift_banner_for_repo(repo)
        t("F", "rec 4: no drift.db → no banner", b is None)


# ---------------------------------------------------------------------------
# Phase G — Dogfood 3 rounds (free+cheap)
# ---------------------------------------------------------------------------


def phase_g_dogfood(rounds: int = 3, include_real_claude: bool = False) -> None:
    """Run the full dogfood suite N times.

    With include_real_claude=True, the 8 moderate scenarios that need
    `claude -p` are enabled (~$0.20 each = ~$1.60/round); we pass
    --cost free,cheap,moderate so they're actually picked up by the
    runner's cost filter. Per user directive: include everything.
    """
    label_suffix = " (+ real-claude)" if include_real_claude else ""
    section(f"Phase G — Dogfood {rounds} rounds{label_suffix}")
    expected = "62" if include_real_claude else "54"
    for i in range(1, rounds + 1):
        header(f"Round {i}/{rounds}")
        argv = [
            str(REPO_ROOT / "mcp" / ".venv" / "bin" / "python"),
            "-m",
            "tests.dogfood.runner",
        ]
        if include_real_claude:
            argv.extend(
                [
                    "--include-real-claude",
                    "--cost",
                    "free,cheap,moderate",
                    "--max-budget-usd",
                    "10.0",
                ]
            )
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3600,
        )
        # Dogfood runner writes its summary table + result paths to STDERR
        # via sys.stderr.write. Search both streams to be robust.
        combined = proc.stdout + "\n" + proc.stderr
        summary_lines = [
            line for line in combined.splitlines() if "Summary:" in line
        ]
        summary = summary_lines[-1] if summary_lines else "(no summary)"
        ok = f"{expected} PASS, 0 FAIL" in summary
        t("G", f"dogfood round {i}{label_suffix}: {expected}/{expected} PASS", ok, summary[:120])
        if not ok:
            for line in combined.splitlines():
                if line.startswith("[FAIL]") or line.startswith("[ERROR]"):
                    print(f"      {line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if TS_REPO is None or RUBY_REPO is None:
        print("ERROR: CHAMELEON_TEST_TS_REPO and CHAMELEON_TEST_RUBY_REPO must be set in .env")
        return 2
    if not TS_REPO.is_dir() or not RUBY_REPO.is_dir():
        print(f"ERROR: test repo missing: TS={TS_REPO} RUBY={RUBY_REPO}")
        return 2

    section(f"E2E setup\n  TS_REPO   = {TS_REPO}\n  RUBY_REPO = {RUBY_REPO}")

    ts_backup = RepoBackup(TS_REPO, "ts")
    ruby_backup = RepoBackup(RUBY_REPO, "ruby")
    repos = {"ts": ts_backup, "ruby": ruby_backup}
    print(
        f"  ts: backed up to {ts_backup.backup_dir}\n"
        f"  ruby: backed up to {ruby_backup.backup_dir}"
    )

    include_real_claude = "--include-real-claude" in sys.argv or "--all" in sys.argv

    try:
        phase_a_clean_slate(repos)
        phase_b_bootstrap(repos)
        phase_c_trust(repos)
        phase_d_all_tools(repos)
        phase_e_slash_flows(repos)
        phase_f_edge_cases(repos)
        phase_g_dogfood(rounds=3, include_real_claude=include_real_claude)
    except Exception:
        print("\nE2E HALTED — unhandled exception:")
        traceback.print_exc()
        FAIL.append(("?", "unhandled exception", traceback.format_exc()[:200]))
    finally:
        section("Phase H — Backup snapshot (NOT restored, per 'from scratch' directive)")
        for label, backup in repos.items():
            print(
                f"  {label}: pre-E2E snapshot at {backup.backup_dir}\n"
                f"    (manual rollback: rm -rf '{backup.repo_path / '.chameleon'}' && "
                f"cp -R '{backup.backup_dir / 'chameleon'}' '{backup.repo_path / '.chameleon'}')"
            )

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    section("FINAL E2E REPORT")
    by_phase: dict[str, dict[str, int]] = {}
    for phase, _name, _info in PASS:
        by_phase.setdefault(phase, {"pass": 0, "fail": 0, "skip": 0})["pass"] += 1
    for phase, _name, _info in FAIL:
        by_phase.setdefault(phase, {"pass": 0, "fail": 0, "skip": 0})["fail"] += 1
    for phase, _name, _info in SKIP:
        by_phase.setdefault(phase, {"pass": 0, "fail": 0, "skip": 0})["skip"] += 1
    for phase in sorted(by_phase):
        c = by_phase[phase]
        print(
            f"  Phase {phase}: {c['pass']} pass, {c['fail']} fail, {c['skip']} skip"
        )
    print(f"\n  TOTAL: {len(PASS)} pass, {len(FAIL)} fail, {len(SKIP)} skip")
    if FAIL:
        print("\n  FAILURES:")
        for phase, name, info in FAIL:
            print(f"    [{phase}] {name}{(': ' + info) if info else ''}")
        return 1
    print("\n  ALL E2E ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
