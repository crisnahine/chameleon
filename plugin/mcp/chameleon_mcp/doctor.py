"""Installation triage: the checks behind `/chameleon-doctor`.

Lives outside tools.py because the dependency runs strictly one way. doctor
CONSUMES the tool surface -- it reads a profile, a daemon status, a launcher
path -- and nothing in that surface consumes doctor back. Re-exporting it from
tools.py would have created exactly the two-module cycle scripts/check-import-cycle.py
exists to refuse, so the dispatcher reaches this module directly instead
(see _ACTION_MODULES in server.py).

Every check is advisory and fail-open: doctor runs when the install is broken,
which is the only time anyone calls it, so a check that cannot answer reports
that it cannot answer rather than raising and taking the whole report down.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.tools import _envelope, daemon_status, list_profiles

# The prose-injection-drop line shape, shared with the writers that emit it
# (profile/loader.py's two whole-artifact drops and core/idiom_store.py's
# per-record drop) so the two cannot drift apart unnoticed. They did once: the
# pattern required the artifact name to be one unbroken token, every writer
# grew a parenthesised profile path, and this check silently matched nothing
# for releases while its fixtures -- written against the older wording -- kept
# passing. Keyed on the two fixed anchors all three share, and covered by a
# test that matches it against the writers' real stderr rather than a literal.
INJECTION_DROP_RE = re.compile(r"chameleon: .+ dropped from context:")


def _chameleon_version_or_unknown() -> str:
    try:
        from importlib.metadata import version

        return version("chameleon-mcp")
    except Exception:
        pass
    try:
        from chameleon_mcp import __version__

        return __version__
    except Exception:
        return "unknown"


def _conflict_marked_artifacts(profile_dir: Path) -> list[str]:
    """Names of profile artifacts carrying unresolved git conflict markers.

    Only the markdown artifacts and the sentinel need the scan: a marker-laden
    JSON artifact already fails its parse and reads as corrupt everywhere.
    """
    found: list[str] = []
    for name in ("COMMITTED", "idioms.md", "principles.md", "profile.summary.md"):
        p = profile_dir / name
        try:
            if not p.is_file():
                continue
            head = p.read_bytes()[:262_144]
        except OSError:
            continue
        anchored = b"\n" + head
        if b"\n<<<<<<< " in anchored and b"\n>>>>>>> " in anchored:
            found.append(name)
    return found


def _recent_preflight_rows(repo_id: str | None, *, keep=None) -> list[dict]:
    """The tail of this repo's PreToolUse metric rows, newest last.

    Shared by the two dead-install checks that read the same stream and differ
    only in which rows they keep: advisory_emission asks whether archetype
    resolution works (trusted source-file rows), advisory_suppressed asks
    whether anything was injected at all (every row). One window, one place to
    fix if the row source or the bound ever changes.
    """
    rows: list[dict] = []
    if not repo_id:
        return rows
    # Deferred like every other non-stdlib import in this module: doctor runs on
    # a broken install, so a module that will not import must degrade to an
    # empty window rather than take the whole report down.
    from chameleon_mcp.metrics import _metrics_path
    from chameleon_mcp.shadow_report import _iter_rows

    window = threshold_int("DOCTOR_PREFLIGHT_WINDOW_ROWS")
    for row in _iter_rows(_metrics_path()):
        if row.get("hook") != "preflight-and-advise":
            continue
        if row.get("repo_id") != repo_id:
            continue
        if keep is not None and not keep(row):
            continue
        rows.append(row)
        if len(rows) > window:
            rows.pop(0)
    return rows


def doctor(repo: str | None = None) -> dict:
    """Triage report for chameleon installation health.

    Returns a structured envelope with subsystem checks. Each check
    has a status (ok | warn | error) and a brief message.

    ``repo`` optionally targets the PER-REPO checks (``config_json``,
    ``production_ref``, and the ``profile_artifacts`` / ``judge_spawn_health`` /
    ``advisory_emission`` dead-install detectors) at a specific repo root, so a
    caller like ``/chameleon-status`` gets config for the repo it is statusing
    rather than whatever the process cwd resolves to. When omitted they resolve
    from cwd (the original behavior). The global plumbing checks (python, hooks,
    HMAC key, daemon) ignore it.
    """
    import platform
    import shutil
    import sys
    from pathlib import Path

    checks: list[dict] = []
    # A malformed repo arg (embedded null byte, un-stattable path) must not crash
    # the tool -- doctor's contract is a clean envelope. A null byte propagates as
    # ValueError out of find_repo_root's realpath/lstat (Path.exists() does NOT
    # raise on it under 3.13), so reject it here and fall back to the cwd-scoped
    # behavior (the no-arg default) rather than crashing.
    _target_root: Path | None = None
    if repo and isinstance(repo, str) and "\x00" not in repo:
        try:
            _target_root = Path(repo)
        except (OSError, ValueError):
            _target_root = None

    py = sys.version_info
    if py >= (3, 11):
        checks.append(
            {
                "name": "python_version",
                "status": "ok",
                "detail": f"{py.major}.{py.minor}.{py.micro}",
            }
        )
    else:
        checks.append(
            {
                "name": "python_version",
                "status": "error",
                "detail": f"{py.major}.{py.minor}.{py.micro} (need >= 3.11)",
            }
        )

    bash_path = shutil.which("bash")
    if bash_path:
        checks.append({"name": "bash_on_path", "status": "ok", "detail": bash_path})
    else:
        checks.append(
            {
                "name": "bash_on_path",
                "status": "error",
                "detail": "bash not on PATH; hooks will not run",
            }
        )

    # The hooks resolve `timeout || gtimeout` and degrade to uncapped python
    # when neither exists, so a missing binary is no longer fatal — but it does
    # remove the external wall-clock cap, so report it (matching the wrapper's
    # resolution order). gtimeout ships with Homebrew coreutils on macOS.
    timeout_path = shutil.which("timeout") or shutil.which("gtimeout")
    # Ask the wrapper's question, not PATH's. Every hook wrapper hard-disables
    # the external cap on Git Bash / MSYS / Cygwin ("MINGW*|MSYS*|CYGWIN*)
    # TIMEOUT_BIN=''") because the `timeout` PATH resolves there is Windows'
    # timeout.exe, which takes no command and would break the wrapper. shutil.which
    # finds that same timeout.exe and would report the cap present on a host where
    # no wrapper will ever invoke it -- a health surface certifying a guard the
    # code deliberately removed, which is the failure doctor exists to catch.
    system = (platform.system() or "").upper()
    caps_disabled_by_host = os.name == "nt" or system.startswith(("MINGW", "MSYS", "CYGWIN"))
    if caps_disabled_by_host:
        checks.append(
            {
                "name": "timeout_on_path",
                "status": "warn",
                "detail": (
                    "hook wrappers skip timeout(1) on Windows/Git Bash by design "
                    "(the PATH `timeout` is timeout.exe, which takes no command), so "
                    "there is no external wall-clock cap on this host; in-process "
                    "timeouts apply"
                ),
            }
        )
    elif timeout_path:
        checks.append({"name": "timeout_on_path", "status": "ok", "detail": timeout_path})
    else:
        checks.append(
            {
                "name": "timeout_on_path",
                "status": "warn",
                "detail": (
                    "neither timeout(1) nor gtimeout on PATH; hooks still run but "
                    "without an external wall-clock cap (in-process timeouts apply). "
                    "macOS: brew install coreutils"
                ),
            }
        )

    # The bundled MCP server launches via `uvx` (.mcp.json runs
    # `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`), so every MCP tool --
    # /chameleon-init, refresh, status, and the codebase-comprehension queries --
    # is gated on `uvx` resolving. This is distinct from hook_interpreter_deps:
    # the hook ladder can win on the bundled venv or a version-named python3.x
    # with no uv present, so a machine can pass that check while the MCP server
    # cannot start at all. Probe `uvx` explicitly so a green report never hides a
    # dead MCP surface.
    uvx_path = shutil.which("uvx")
    if uvx_path:
        checks.append({"name": "mcp_server_launcher", "status": "ok", "detail": uvx_path})
    elif shutil.which("uv"):
        checks.append(
            {
                "name": "mcp_server_launcher",
                "status": "warn",
                "detail": (
                    f"`uv` is on PATH ({shutil.which('uv')}) but `uvx` is not; the MCP "
                    "server is launched as `uvx`, so put uv's tool shim on PATH or the "
                    "MCP tools will not load"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "mcp_server_launcher",
                "status": "error",
                "detail": (
                    "neither `uvx` nor `uv` on PATH; the bundled MCP server cannot launch, "
                    "so chameleon's MCP tools (/chameleon-init, refresh, status, and "
                    "codebase queries) are unavailable. Install uv: "
                    "https://docs.astral.sh/uv/getting-started/installation/"
                ),
            }
        )

    try:
        from chameleon_mcp.profile.trust import plugin_data_dir

        data_dir = plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"name": "plugin_data_writable", "status": "ok", "detail": str(data_dir)})
    except Exception as exc:
        checks.append(
            {
                "name": "plugin_data_writable",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )

    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root_env:
        plugin_root = Path(plugin_root_env)
        hook_dir = plugin_root / "hooks"
        for hook_name in (
            "preflight-and-advise",
            "posttool-recorder",
            "posttool-verify",
            "session-start",
            "callout-detector",
            "stop-backstop",
        ):
            hpath = hook_dir / hook_name
            if hpath.is_file() and os.access(hpath, os.X_OK):
                checks.append({"name": f"hook_{hook_name}", "status": "ok", "detail": "executable"})
            elif hpath.is_file():
                checks.append(
                    {
                        "name": f"hook_{hook_name}",
                        "status": "error",
                        "detail": "exists but not executable",
                    }
                )
            else:
                checks.append({"name": f"hook_{hook_name}", "status": "error", "detail": "missing"})
    else:
        checks.append(
            {
                "name": "hooks",
                "status": "warn",
                "detail": "CLAUDE_PLUGIN_ROOT not set; cannot locate hook scripts",
            }
        )

    # Hook interpreter probe. Every hook resolves its Python through one shared
    # ladder (hooks/_resolve-python.sh): the bundled venv, then version-named
    # python3.x, then `uv run` (the same dep-complete resolver the MCP server
    # uses via uvx), and only a version-validated bare python3 — never a blind
    # one, since macOS's /usr/bin/python3 is 3.9.x, below the >=3.11 floor and
    # without chameleon's deps. Run that exact resolver here so doctor reports
    # the command the hooks actually pick, then confirm the winner is >=3.11 and
    # imports the deps. The hot-path hooks are stdlib-only, but the hook-spawned
    # refresh imports the extractors and falls back to `uv run` when the winner
    # lacks deps, so a >=3.11-but-depless winner with uv present is a warn,
    # without uv an error.
    if plugin_root_env:
        try:
            import subprocess as _subp

            mcp_dir = Path(plugin_root_env) / "mcp"
            resolver = Path(plugin_root_env) / "hooks" / "_resolve-python.sh"
            bash = shutil.which("bash")
            hook_cmd: list[str] | None = None
            if resolver.is_file() and bash:
                res = _subp.run(
                    [bash, str(resolver), str(mcp_dir)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res.returncode == 0:
                    hook_cmd = [ln for ln in res.stdout.splitlines() if ln.strip()]
            probe_env = {**os.environ, "PYTHONPATH": str(mcp_dir)}
            if not bash or not resolver.is_file():
                checks.append(
                    {
                        "name": "hook_interpreter_deps",
                        "status": "warn",
                        "detail": (
                            "cannot probe the hook interpreter: "
                            f"{'bash not on PATH' if not bash else 'resolver script missing'}"
                        ),
                    }
                )
            elif not hook_cmd:
                checks.append(
                    {
                        "name": "hook_interpreter_deps",
                        "status": "error",
                        "detail": (
                            "no Python >=3.11 resolves for the hooks and `uv` is not on PATH; "
                            "enforcement and guidance are OFF. Install uv or a Python >=3.11."
                        ),
                    }
                )
            else:
                cmd_str = " ".join(hook_cmd)
                ver = _subp.run(
                    [*hook_cmd, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=probe_env,
                )
                ver_str = ver.stdout.strip() if ver.returncode == 0 else "?"
                probe = _subp.run(
                    [*hook_cmd, "-c", "import xxhash, yaml, detect_secrets, chameleon_mcp"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=probe_env,
                )
                if probe.returncode == 0:
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "ok",
                            "detail": f"hooks resolve `{cmd_str}` (Python {ver_str}); imports deps",
                        }
                    )
                elif shutil.which("uv"):
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "warn",
                            "detail": (
                                f"hooks resolve `{cmd_str}` (Python {ver_str}); missing deps "
                                "(e.g. xxhash) — the hook-spawned refresh falls back to `uv run`. "
                                "Create mcp/.venv with the deps to remove the fallback."
                            ),
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "error",
                            "detail": (
                                f"hooks resolve `{cmd_str}` (Python {ver_str}); cannot import "
                                "chameleon deps and `uv` is not on PATH; hook-spawned "
                                "refresh/bootstrap will fail. Install uv or create mcp/.venv."
                            ),
                        }
                    )
        except Exception as exc:
            checks.append(
                {
                    "name": "hook_interpreter_deps",
                    "status": "warn",
                    "detail": f"could not probe hook interpreter: {type(exc).__name__}: {exc}",
                }
            )

    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key

        _ensure_hmac_key()
        checks.append({"name": "hmac_key", "status": "ok", "detail": "exists and owner-readable"})
    except Exception as exc:
        checks.append(
            {"name": "hmac_key", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    try:
        ds = daemon_status()
        if ds.get("data", {}).get("alive"):
            checks.append(
                {"name": "daemon", "status": "ok", "detail": f"alive (pid={ds['data'].get('pid')})"}
            )
        else:
            checks.append(
                {"name": "daemon", "status": "ok", "detail": "lazy (will spawn on next hook)"}
            )
    except Exception as exc:
        checks.append(
            {"name": "daemon", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    log_env = os.environ.get("CHAMELEON_HOOK_ERROR_LOG")
    if log_env:
        log = Path(log_env)
    else:
        # Same precedence as the hook wrappers' LOG_DIR fallback: a
        # CHAMELEON_PLUGIN_DATA override (tests, isolated sessions) keeps its
        # hook errors out of the real data dir, and doctor reads where the
        # wrappers wrote.
        data_env = os.environ.get("CHAMELEON_PLUGIN_DATA")
        base = Path(data_env) if data_env else Path.home() / ".local" / "share" / "chameleon"
        log = base / ".hook_errors.log"
    if log.is_file():
        try:
            import re as _re
            from datetime import datetime as _dt
            from datetime import timedelta as _td
            from datetime import timezone as _tz

            try:
                from datetime import UTC as _UTC  # type: ignore[attr-defined]
            except ImportError:  # pragma: no cover - Python <3.11
                _UTC = _tz.utc  # type: ignore[assignment]  # noqa: UP017

            cutoff = _dt.now(_UTC) - _td(hours=72)
            ts_re = _re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")
            # Group lines into ENTRIES (one timestamped anchor line plus every
            # continuation line up to the next anchor), then window/slice by
            # entry, not by raw line. The previous line-based approach kept a
            # flat `recent` list and glued any non-timestamped line onto it
            # whenever it was non-empty -- so a continuation line that
            # actually belonged to an OUT-OF-WINDOW (or unparseable-timestamp)
            # anchor got misattributed to whatever real entry preceded it in
            # the file, and `recent[-5:]` could then surface that unrelated
            # continuation (e.g. a benign first-run-setup banner) while the
            # anchor line of the real recent error it displaced fell outside
            # the slice entirely.
            raw_lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            # Injection drops are reported by their own check below. Holding
            # them out here stops a burst of them from pushing real hook errors
            # out of the last-5 tail, and stops the same line being reported
            # twice now that it carries an anchor of its own.
            error_lines = [ln for ln in raw_lines if not INJECTION_DROP_RE.search(ln)]
            entries: list[list[str] | None] = []
            for line in error_lines:
                m = ts_re.match(line)
                if m:
                    try:
                        when = _dt.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_UTC)
                    except ValueError:
                        when = None
                    entries.append([line] if when is not None and when >= cutoff else None)
                    continue
                # Continuation line: attach to the entry that most recently
                # started, IN-WINDOW ONLY -- an out-of-window (or dropped/
                # unparseable) anchor is represented by `None` above, so its
                # own continuation lines are dropped with it instead of
                # bleeding into a different, real in-window entry. A line
                # before any anchor at all has nothing to attach to.
                if entries and entries[-1] is not None:
                    entries[-1].append(line)
            recent_entries = [e for e in entries if e]
            tail = [ln for e in recent_entries[-5:] for ln in e]

            from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _san

            if tail:
                # Log lines can embed repo-derived text (an exception message
                # carrying a symbol name, a path from a hostile fixture), and
                # this detail reaches the model surface — sanitize each line.
                tail = [_san(ln) for ln in tail]
                # The log is installation-wide: entries may come from other
                # repos (deleted QA fixtures included). Say so, or a fresh-repo
                # user reads a stale unrelated error as their repo's problem.
                tail = [
                    "installation-wide hook error log (last 72h; entries may be from other repos):"
                ] + tail
                checks.append({"name": "recent_hook_errors", "status": "warn", "detail": tail})
            else:
                checks.append(
                    {
                        "name": "recent_hook_errors",
                        "status": "ok",
                        "detail": "no errors in the last 72h",
                    }
                )

            # Prose-injection-drop warnings (loader.safe_prose_text /
            # load_profile_dir's idioms.md guard / idiom_store's per-record drop)
            # reach this log as raw stderr, redirected by the hook wrappers
            # (`2>>"${LOG_FILE}"`). They get their own check because a live
            # poisoning event correctly blocked at the read path is the ONE
            # diagnostic doctor exists to surface, and the anchor-grouping pass
            # would otherwise fold each one into whatever unrelated entry
            # preceded it.
            #
            # Age them like everything else. The writers anchor their lines now,
            # so a drop windows on its own timestamp; a line an older install
            # wrote has none, and falls back to the log's own mtime -- a log
            # nothing has written to in days cannot evidence a live drop, and
            # surfacing one forever leaves doctor permanently warn on a repo
            # with nothing wrong. An undateable line is surfaced rather than
            # hidden: for this class, over-reporting is the safe direction.
            try:
                log_mtime = _dt.fromtimestamp(log.stat().st_mtime, _UTC)
            except OSError:
                log_mtime = None
            injection_drops: list[str] = []
            for line in raw_lines:
                if not INJECTION_DROP_RE.search(line):
                    continue
                m = ts_re.match(line)
                when = log_mtime
                if m:
                    try:
                        when = _dt.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_UTC)
                    except ValueError:
                        when = log_mtime
                if when is not None and when < cutoff:
                    continue
                injection_drops.append(_san(line))
            injection_drops = injection_drops[-5:]
            if injection_drops:
                checks.append(
                    {
                        "name": "prose_injection_drops",
                        "status": "warn",
                        "detail": [
                            "prose artifact(s) dropped for prompt-injection at the read path "
                            "(installation-wide log; entries may be from other repos):"
                        ]
                        + injection_drops,
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "name": "recent_hook_errors",
                    "status": "warn",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        checks.append({"name": "recent_hook_errors", "status": "ok", "detail": "no errors logged"})

    # Walk up to the repo root so doctor reports the repo's config even when the
    # session cwd is a subdirectory (a monorepo workspace, app/ under a Rails
    # repo). Reading Path.cwd()/.chameleon directly reported a configured repo as
    # unconfigured from any subdir, contradicting the rest of the status flow
    # (which resolves the repo via a file path that walks to root).
    from chameleon_mcp.profile.loader import find_repo_root

    _doctor_base = _target_root or Path.cwd()
    try:
        _doctor_root = find_repo_root(_doctor_base) or _doctor_base
    except (OSError, ValueError):
        # A pathological base (unresolvable / too-long path) must not crash the
        # per-repo checks: fall back to cwd so the plumbing checks still report.
        _doctor_root = Path.cwd()
    # A linked worktree has no .chameleon of its own; every check below reads
    # the main worktree's committed config/artifacts instead.
    from chameleon_mcp.worktree import resolve_profile_root

    _doctor_root = resolve_profile_root(_doctor_root)

    # The profile is committed, so two developers refreshing on parallel branches
    # collide in generated JSON. A merge driver exists for exactly that, but
    # arming it takes a .gitattributes AND a `git config` registration nothing
    # performs -- so a repo can sit for months believing it is covered. Neither
    # half is checked anywhere else, and a half-armed setup is the worst state:
    # git honors merge=chameleon, finds no driver, and falls back to the plain
    # text merge the attributes existed to avoid.
    try:
        import subprocess

        _ga = _doctor_root / ".gitattributes"
        _ga_armed = _ga.is_file() and "merge=chameleon" in _ga.read_text(
            encoding="utf-8", errors="replace"
        )
        _drv = subprocess.run(
            ["git", "config", "--get", "merge.chameleon.driver"],
            cwd=str(_doctor_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        _drv_armed = _drv.returncode == 0 and bool(_drv.stdout.strip())
        if _ga_armed and _drv_armed:
            checks.append(
                {"name": "profile_merge_driver", "status": "ok", "detail": "attributes + driver"}
            )
        elif not _ga_armed and not _drv_armed:
            checks.append(
                {
                    "name": "profile_merge_driver",
                    "status": "warn",
                    "detail": (
                        "not set up; two branches that both refresh will conflict in "
                        ".chameleon JSON. See .gitattributes-template"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "profile_merge_driver",
                    "status": "error",
                    "detail": (
                        f"half-armed (.gitattributes={_ga_armed}, driver registered="
                        f"{_drv_armed}); git will fall back to a plain text merge"
                    ),
                }
            )
    except Exception:
        checks.append(
            {"name": "profile_merge_driver", "status": "warn", "detail": "could not determine"}
        )
    # Whether chameleon is guarding this repo AT ALL. `.chameleon/.skip` silences
    # every hook -- the per-edit advisory and the security deny gates alike --
    # and unlike a pause it never expires. Nothing else reports it, so a user who
    # inherited the file in a clone sees a plugin that is merely quiet. Reported
    # through is_chameleon_suppressed rather than a bare stat so doctor and the
    # hooks can never disagree about what counts as suppressed.
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.tools import _compute_repo_id

        _sup = is_chameleon_suppressed(_doctor_root, _compute_repo_id(_doctor_root), None)
        if _sup is None:
            checks.append({"name": "suppression", "status": "ok", "detail": "active"})
        else:
            checks.append(
                {
                    "name": "suppression",
                    "status": "warn",
                    "detail": (
                        f"chameleon is NOT guarding this repo ({_sup}); "
                        "every hook, including the security deny gates, is silent"
                    ),
                }
            )
    except Exception:
        checks.append({"name": "suppression", "status": "warn", "detail": "could not determine"})

    cwd_config = _doctor_root / ".chameleon" / "config.json"
    if cwd_config.is_file():
        try:
            from chameleon_mcp.profile.config import (
                ChameleonConfigError,
                load_config,
            )

            cfg = load_config(cwd_config.parent)
            detail = {
                "schema_version": cfg.schema_version,
                "canonical_ref": cfg.canonical_ref,
                "branch_pinning_enabled": cfg.branch_pinning_enabled,
                "production_ref": cfg.production_ref,
                "auto_refresh.enabled": cfg.auto_refresh.enabled,
                "auto_refresh.drift_threshold": cfg.auto_refresh.drift_threshold,
                "auto_refresh.max_age_hours": cfg.auto_refresh.max_age_hours,
                "trust.auto_preserve_when": cfg.trust.auto_preserve_when,
                "auto_rename": cfg.auto_rename,
            }
            checks.append({"name": "config_json", "status": "ok", "detail": detail})
            # A locked production_ref that no longer resolves means every
            # bootstrap/refresh silently degrades to working-tree derivation
            # — worth a loud check of its own (deleted branch, renamed
            # remote, shallow clone without the ref).
            if cfg.production_ref:
                try:
                    from chameleon_mcp.production_ref import resolve_production_ref

                    _doctor_resolved = resolve_production_ref(_doctor_root, cfg.production_ref)
                    if _doctor_resolved is None:
                        checks.append(
                            {
                                "name": "production_ref",
                                "status": "warn",
                                "detail": (
                                    f"production_ref {cfg.production_ref!r} does not "
                                    "resolve to a commit in this repo; derivation "
                                    "falls back to the working tree. Fetch the "
                                    "branch, fix the name in .chameleon/config.json, "
                                    "or remove the key."
                                ),
                            }
                        )
                    else:
                        checks.append(
                            {
                                "name": "production_ref",
                                "status": "ok",
                                "detail": (
                                    f"locked to {_doctor_resolved.ref} @ "
                                    f"{_doctor_resolved.sha[:12]}"
                                ),
                            }
                        )
                except Exception as _prod_exc:  # noqa: BLE001
                    checks.append(
                        {
                            "name": "production_ref",
                            "status": "warn",
                            "detail": f"{type(_prod_exc).__name__}: {_prod_exc}",
                        }
                    )
        except ChameleonConfigError as cfg_exc:
            checks.append(
                {
                    "name": "config_json",
                    "status": "error",
                    "detail": (
                        f"{cwd_config} is present but malformed: "
                        f"{type(cfg_exc).__name__}: {cfg_exc}. config.json "
                        "features are inactive until fixed (built-in defaults apply)."
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "config_json",
                    "status": "warn",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        checks.append(
            {
                "name": "config_json",
                "status": "ok",
                "detail": (
                    "no .chameleon/config.json — using defaults. ON by default: "
                    "auto_refresh (drift_threshold=0.2, max_age_hours=168), "
                    "auto_rename, one-time trust (it persists across profile "
                    "changes and a refresh never re-prompts), and enforcement.mode "
                    "'enforce' (calibrated block rules deny for real on a trusted "
                    "repo; set 'shadow' to log-only or 'off' for advisory). OFF by "
                    "default: canonical_ref (branch pinning) and production_ref "
                    "(ref-pinned derivation; auto-locked at init/refresh for "
                    "origin-backed repos). Add a config.json to change these. To "
                    "restore re-prompting when the profile changes after a grant, "
                    "set CHAMELEON_TRUST_REVALIDATE=1 in the environment."
                ),
            }
        )

    # Dead-install detectors: an install can pass every plumbing check above
    # while chameleon does nothing — generated artifacts missing so resolution
    # is degenerate, every turn-end reviewer spawn failing, or a trusted repo
    # whose edits never resolve an archetype. Each detector fails open: absence
    # of a profile / attestations / metrics is healthy-unknown, never a warn.
    # Resolve to the REPO ROOT (the walk-up _doctor_root already used by
    # config_json/production_ref), not the raw cwd: reading `cwd/.chameleon`
    # from a subdir reported a corrupt-artifact repo as clean (false-clean).
    cwd_root = _doctor_root
    cwd_profile_dir = cwd_root / ".chameleon"
    try:
        cwd_repo_id: str | None = _compute_repo_id(cwd_root)
    except Exception:
        cwd_repo_id = None

    try:
        from chameleon_mcp.bootstrap.transaction import is_committed as _tx_is_committed

        if (cwd_profile_dir / "profile.json").is_file() and not _tx_is_committed(cwd_profile_dir):
            # Artifacts present but the COMMITTED sentinel is missing or
            # marker-laden: a torn transaction or unresolved merge. Say so
            # directly — the index loaders now refuse this state uniformly,
            # and interpreting their refusal per-artifact would misreport it
            # as "stale schema or oversize" while the repo_states section of
            # this same doctor run reads the root as having no profile.
            checks.append(
                {
                    "name": "profile_artifacts",
                    "status": "warn",
                    "detail": (
                        "profile is uncommitted (COMMITTED sentinel missing or "
                        "conflict-marked): a torn transaction or unresolved merge. "
                        "Every loader refuses this state; run /chameleon-refresh "
                        "to regenerate."
                    ),
                }
            )
        elif (cwd_profile_dir / "profile.json").is_file():
            import json as _json

            artifact_problems: list[str] = []
            _lang = None
            try:
                from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

                _pj = _json.loads((cwd_profile_dir / "profile.json").read_text(encoding="utf-8"))
                _lang = _pj.get("language")
                _sv = _pj.get("schema_version")
                # A schema_version this engine cannot load (non-int or > max)
                # PARSES as valid JSON, so a plain parse check reads it as healthy
                # while the load path refuses it and the hooks fail open. Flag it.
                if _sv is not None and (isinstance(_sv, bool) or not isinstance(_sv, int)):
                    artifact_problems.append("profile.json schema_version is not an integer")
                elif isinstance(_sv, int) and _sv > MAX_SUPPORTED_SCHEMA_VERSION:
                    artifact_problems.append(
                        f"profile.json schema_version {_sv} exceeds engine max "
                        f"{MAX_SUPPORTED_SCHEMA_VERSION} (upgrade chameleon)"
                    )
            except Exception:
                # A corrupt profile.json is itself a dead-install signal, not a
                # reason to silently narrow the checked set to the base pair.
                artifact_problems.append("profile.json corrupt")
            # The core generated artifacts every profile writes, for all three
            # languages: a corrupt or missing one silently degrades archetype
            # resolution, conventions, and enforcement while every plumbing check
            # above stays green -- the exact false-clean this detector exists to
            # catch. (calls_index / function_catalog back the cross-file facts;
            # the rest back per-edit resolution.)
            expected_artifacts = [
                "archetypes.json",
                "canonicals.json",
                "conventions.json",
                "rules.json",
                "enforcement.json",
                "calls_index.json",
                "function_catalog.json",
            ]
            # Language-specific cross-file indexes: TS and Python both emit the
            # export/reverse import graph; Ruby emits a constant index instead.
            # (The old code only added these for TypeScript, so a corrupt Python
            # exports_index or a corrupt Ruby constant_index was silent-clean.)
            if _lang in ("typescript", "python"):
                expected_artifacts += ["exports_index.json", "reverse_index.json"]
            elif _lang == "ruby":
                expected_artifacts += ["constant_index.json"]
            # A parseable artifact can still be STALE-SCHEMA: an index built by an
            # older engine (e.g. a pre-v2.41 calls_index at schema_version 1) is
            # valid JSON, so a plain parse check reads it "ok" while the loader
            # rejects the schema and every calls / cross-file tool silently
            # degrades to zero facts -- the exact false-clean this detector exists
            # to catch, one layer deeper than a missing/corrupt file. Call the real
            # loader (present file + None return = stale schema or oversize, both
            # of which /chameleon-refresh repairs); each loader honors its own
            # readable-version set (counterexamples reads {1,2}), so this never
            # false-flags a version the engine still accepts. Fail-open: if the
            # loaders can't be imported, fall back to the parse-only check.
            _schema_loaders: dict = {}
            try:
                from chameleon_mcp.calls_index import load_calls_index as _ld_calls
                from chameleon_mcp.constant_index import load_constant_index as _ld_const
                from chameleon_mcp.counterexamples import load_counterexamples as _ld_cx
                from chameleon_mcp.symbol_index import (
                    load_exports_index as _ld_exports,
                )
                from chameleon_mcp.symbol_index import (
                    load_reverse_index as _ld_reverse,
                )
                from chameleon_mcp.symbol_signatures import (
                    load_symbol_signatures as _ld_sigs,
                )

                _schema_loaders = {
                    "calls_index.json": _ld_calls,
                    "exports_index.json": _ld_exports,
                    "reverse_index.json": _ld_reverse,
                    "constant_index.json": _ld_const,
                    "symbol_signatures.json": _ld_sigs,
                    "counterexamples.json": _ld_cx,
                }
            except Exception:
                _schema_loaders = {}

            def _schema_stale(art: str) -> bool:
                loader = _schema_loaders.get(art)
                if loader is None:
                    return False
                try:
                    return loader(cwd_root) is None
                except Exception:
                    return False

            for art in expected_artifacts:
                apath = cwd_profile_dir / art
                if not apath.is_file():
                    artifact_problems.append(f"{art} missing")
                    continue
                try:
                    _json.loads(apath.read_text(encoding="utf-8"))
                except Exception:
                    artifact_problems.append(f"{art} corrupt")
                    continue
                if _schema_stale(art):
                    artifact_problems.append(
                        f"{art} unreadable by this engine (stale schema or oversize)"
                    )
            # Advisory artifacts (nearby-signatures, counterexamples): validate
            # IF PRESENT (a present-but-corrupt one degrades those features), but
            # do not require presence -- an older profile may predate them.
            for art in ("symbol_signatures.json", "counterexamples.json"):
                apath = cwd_profile_dir / art
                if apath.is_file():
                    try:
                        _json.loads(apath.read_text(encoding="utf-8"))
                    except Exception:
                        artifact_problems.append(f"{art} corrupt")
                        continue
                    if _schema_stale(art):
                        artifact_problems.append(
                            f"{art} unreadable by this engine (stale schema or oversize)"
                        )
            # The per-artifact checks above are parse-only for the CORE bundle
            # (archetypes/canonicals/rules/conventions/profile.json), so a
            # valid-JSON-but-wrong-schema core artifact slipped through as
            # "parseable" while the profile is actually unloadable. Catch the two
            # shapes the loader rejects that a bare json.loads does not:
            #   (1) a core artifact that is not a JSON OBJECT (a bare array/scalar);
            #   (2) a generation MISMATCH across the artifacts the LOADER gates on.
            # The unloadable claim mirrors load_profile_dir's own consistency
            # gate exactly: that gate covers profile/archetypes/rules/canonicals
            # only. conventions.json drifting a generation behind never blocks a
            # load, so it is reported as staleness, not unloadability.
            _core_bundle = (
                "archetypes.json",
                "canonicals.json",
                "rules.json",
                "conventions.json",
                "profile.json",
            )
            _loader_gated = {"archetypes.json", "canonicals.json", "rules.json", "profile.json"}
            _generations: set = set()
            _conventions_gen: set = set()
            for art in _core_bundle:
                apath = cwd_profile_dir / art
                if not apath.is_file():
                    continue
                try:
                    _obj = _json.loads(apath.read_text(encoding="utf-8"))
                except Exception:
                    continue  # already reported corrupt above if it was expected
                if not isinstance(_obj, dict):
                    artifact_problems.append(f"{art} is not a JSON object (wrong schema)")
                    continue
                if "generation" in _obj:
                    (_generations if art in _loader_gated else _conventions_gen).add(
                        _obj.get("generation")
                    )
            if len(_generations) > 1:
                artifact_problems.append(
                    f"profile generation mismatch across artifacts ({sorted(map(str, _generations))}); "
                    "the bundle is unloadable"
                )
            elif _conventions_gen and _generations and _conventions_gen != _generations:
                artifact_problems.append(
                    "conventions.json generation lags the loaded bundle "
                    f"({sorted(map(str, _conventions_gen))} vs {sorted(map(str, _generations))}); "
                    "loads fine, but taught-convention data may predate the last refresh"
                )
            if artifact_problems:
                checks.append(
                    {
                        "name": "profile_artifacts",
                        "status": "warn",
                        "detail": (
                            "generated profile artifacts unhealthy: "
                            + "; ".join(artifact_problems)
                            + ". Cross-file facts and duplication checks degrade "
                            "without them; run /chameleon-refresh to regenerate."
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "name": "profile_artifacts",
                        "status": "ok",
                        "detail": (
                            f"{len(expected_artifacts)} generated artifacts present and parseable"
                        ),
                    }
                )
        else:
            checks.append(
                {
                    "name": "profile_artifacts",
                    "status": "ok",
                    "detail": "no profile in the current directory",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "profile_artifacts",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        from chameleon_mcp.profile.trust import plugin_data_dir as _pdd

        judge_detail = "no attested sessions for this repo"
        judge_warn = False
        # The .is_dir() guard keeps doctor read-only here: the attestation
        # reader's path resolution would otherwise CREATE the repo data dir.
        if cwd_repo_id and (_pdd() / cwd_repo_id).is_dir():
            from chameleon_mcp.review_ledger import read_session_attestations

            _hist = read_session_attestations(cwd_repo_id, limit=5)
            _records = _hist.get("records") or []
            # A grounding event (judge_defs_*/judge_transitive_*/judge_facts_*)
            # rides the degraded_spawn channel in pre-2.38.9 attestations but is
            # NOT a spawn failure; counting it warned "reviewer failing to spawn"
            # for a healthy reviewer. Drop those so only genuine failures warn.
            from chameleon_mcp.judge import is_grounding_event as _is_grounding

            # Recognizes BOTH check-event vocabularies so a mixed-version
            # attestation history (a repo whose ledger spans the phase-3
            # cutover) still surfaces a dead reviewer: the pre-cutover
            # "correctness_judge" checks, and the async scheduler's own
            # "review_job" checks (stop/scheduler.py + stop/pipeline.py's
            # _run_review_job). The new vocabulary has no "spawned/completed"
            # equivalent (the detached job runner records no explicit success
            # checkpoint), so a clean launch is inferred as a "review_job"/
            # "spawned" with no launch-failure companion -- across the whole
            # window, a net surplus of spawns over degradations.
            #
            # Both the spawn and the degrade tallies MUST sum the attestation's
            # per-entry `count` (``_build_session_attestation`` collapses
            # repeated identical (check, status, reason) events into ONE entry
            # carrying a count), never len() of the entry list: review_job/
            # degraded always carries the single reason "platform_unavailable",
            # so counting ENTRIES reads any number of failures as exactly one --
            # an 8-spawn/2-fail reviewer would false-warn (2 spawn entries > 1
            # degrade entry is fine, but 1 spawn reason vs 1 degrade reason nets
            # zero) and a 1-spawn/9-fail one would false-OK.
            def _ev_count(e: dict) -> int:
                c = e.get("count")
                return c if isinstance(c, int) and not isinstance(c, bool) and c > 0 else 1

            judge_events: list[dict] = []
            degraded: list[dict] = []
            completed: list[dict] = []
            review_spawned_count = 0
            review_degraded_count = 0
            for rec in _records:
                rec_checks = [e for e in (rec.get("checks") or []) if isinstance(e, dict)]

                old_events = [e for e in rec_checks if e.get("check") == "correctness_judge"]
                judge_events.extend(old_events)
                degraded.extend(
                    e
                    for e in old_events
                    if e.get("status") == "degraded_spawn" and not _is_grounding(e.get("reason"))
                )
                # Every spawn attempt logs "spawned/started"; only
                # "spawned/completed" marks a reviewer that actually ran.
                completed.extend(
                    e
                    for e in old_events
                    if e.get("status") == "spawned" and e.get("reason") == "completed"
                )

                review_events = [e for e in rec_checks if e.get("check") == "review_job"]
                judge_events.extend(review_events)
                review_degraded = [
                    e
                    for e in review_events
                    if e.get("status") in ("degraded", "platform_unavailable")
                ]
                degraded.extend(review_degraded)
                review_spawned_count += sum(
                    _ev_count(e) for e in review_events if e.get("status") == "spawned"
                )
                review_degraded_count += sum(_ev_count(e) for e in review_degraded)
            # A net surplus of review-job spawns over launch failures means at
            # least one clean detach in the window -- the review_job analog to
            # the correctness check's "spawned/completed" success signal.
            if review_spawned_count - review_degraded_count > 0:
                completed.append({"check": "review_job", "status": "spawned"})
            if degraded and not completed:
                judge_warn = True
                _reasons = sorted({str(e.get("reason") or "unknown") for e in degraded})
                _n_sessions = len({r.get("session_id") for r in _records if r.get("session_id")})
                _span = (
                    f"{_n_sessions} recent session(s)"
                    if _n_sessions
                    else f"{len(_records)} recent attestation(s)"
                )
                judge_detail = (
                    f"turn-end reviewer failing to spawn ({', '.join(_reasons)}) across the "
                    f"last {_span}; correctness review is not running. Check the claude "
                    "binary/auth, then verify with a new session."
                )
            elif judge_events:
                judge_detail = "reviewer spawning normally in recent sessions"
        checks.append(
            {
                "name": "judge_spawn_health",
                "status": "warn" if judge_warn else "ok",
                "detail": judge_detail,
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "judge_spawn_health",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        # Rows usually carry file_rel=None (it is only attributed at the block
        # gates); when present, a non-source attribution (README edits) is a
        # normal null-archetype row, so it is excluded rather than counted.
        _source_exts = (
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".rb",
            ".py",
            ".pyi",
        )

        def _keep(row: dict) -> bool:
            if row.get("trust_state") != "trusted":
                return False
            _fr = row.get("file_rel")
            return not (isinstance(_fr, str) and not _fr.endswith(_source_exts))

        recent_rows = _recent_preflight_rows(cwd_repo_id, keep=_keep)
        null_count = sum(1 for r in recent_rows if r.get("archetype") is None)
        if len(recent_rows) >= 5 and null_count == len(recent_rows):
            checks.append(
                {
                    "name": "advisory_emission",
                    "status": "warn",
                    "detail": (
                        f"advisories are not firing: the last {len(recent_rows)} trusted "
                        "edits in this repo resolved no archetype; archetype resolution "
                        "may be broken. Run /chameleon-refresh and /chameleon-status."
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "advisory_emission",
                    "status": "ok",
                    "detail": (
                        "no trusted preflight rows recorded for this repo"
                        if not recent_rows
                        else f"{len(recent_rows) - null_count}/{len(recent_rows)} recent "
                        "trusted edits resolved an archetype"
                    ),
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "advisory_emission",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    # advisory_emission above asks whether archetype resolution works, so it
    # reads trusted rows only. That leaves the deadest install of all reporting
    # green: an untrusted repo injects nothing on every edit, produces zero
    # trusted rows, and lands in that check's empty-window "ok" branch. This is
    # the second diagnosis, with a different remedy -- a grant, not a re-derive.
    try:
        _sup_rows = _recent_preflight_rows(cwd_repo_id)
        # Only states where nothing was actually injected. A deliberate pause is
        # the user's own decision and must not read as breakage, and a STALE
        # grant still delivers the whole advisory block (plus its own banner) --
        # counting it here would report a working install as silent.
        _blocked = [
            r
            for r in _sup_rows
            if r.get("trust_state") == "untrusted"
            or r.get("suppression_reason") == "trust_prompt_dedup"
        ]
        from chameleon_mcp._thresholds import threshold_int

        _min_rows = threshold_int("ADVISORY_SUPPRESSED_MIN_ROWS")
        if len(_sup_rows) >= _min_rows and len(_blocked) > len(_sup_rows) / 2:
            checks.append(
                {
                    "name": "advisory_suppressed",
                    "status": "warn",
                    "detail": (
                        f"{len(_blocked)} of the last {len(_sup_rows)} edits in this repo "
                        "injected nothing because the profile is not trusted here; a moved "
                        "or re-cloned checkout does this, since a grant records an absolute "
                        "repo root. Run /chameleon-trust."
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "advisory_suppressed",
                    "status": "ok",
                    "detail": (
                        "no preflight rows recorded for this repo"
                        if not _sup_rows
                        else f"{len(_sup_rows) - len(_blocked)}/{len(_sup_rows)} recent edits "
                        "ran against a trusted profile"
                    ),
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "advisory_suppressed",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        from chameleon_mcp import index_db as _index_db_mod

        conn = _index_db_mod.init_index_db()
        row = conn.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
        checks.append(
            {
                "name": "index_db",
                "status": "ok",
                "detail": f"schema_version={row[0] if row else 'missing'}",
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "index_db", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    try:
        from chameleon_mcp.bootstrap.transaction import is_committed
        from chameleon_mcp.profile.loader import load_profile_dir

        lp = list_profiles(limit=20)
        profiles = lp.get("data", {}).get("profiles", [])
        repo_states = []
        any_corrupt = False
        # A single path can carry several registry rows -- an older repo_id left
        # behind by a re-bootstrap or an id-scheme change (remote->uuid->path).
        # list_profiles orders most-recent-first, so the FIRST row for a root is
        # the active profile; the rest are stale. Listing the path three times
        # with contradictory trust states (the reported symptom) is noise, so
        # collapse the stale rows into a count on the authoritative entry.
        seen_roots: dict[str, int] = {}
        for r in profiles:
            root = r.get("repo_root")
            if root and root in seen_roots:
                prior = repo_states[seen_roots[root]]
                prior["stale_registry_entries"] = prior.get("stale_registry_entries", 0) + 1
                if r.get("trust_state") != prior.get("trust_state"):
                    prior["note"] = (
                        "older registry rows for this path record a different "
                        "trust state; the most recent profile (shown) governs"
                    )
                continue
            if root and is_committed(Path(root) / ".chameleon"):
                # A committed profile can still be unloadable (corrupt or
                # incomplete JSON); load_profile_dir rejects it on every edit,
                # so report it as corrupt rather than a healthy profile_present.
                try:
                    load_profile_dir(Path(root) / ".chameleon")
                    status = "profile_present"
                except Exception:
                    status = "profile_corrupt"
                    any_corrupt = True
            elif root:
                status = "no_profile"
            else:
                status = "unknown"
            entry = {
                "repo_root": root,
                "profile_status": status,
                "trust_state": r.get("trust_state"),
            }
            # An unresolved git merge leaves conflict markers inside the
            # markdown artifacts (the JSON ones fail their parse and already
            # read as corrupt). Markers in principles/summary inject garbage
            # into sessions and markers in idioms.md hide taught idioms, so
            # surface them here with the resolution.
            if root:
                conflicted = _conflict_marked_artifacts(Path(root) / ".chameleon")
                if conflicted:
                    entry["merge_conflict_markers"] = conflicted
                    entry["resolution"] = (
                        "accept one side of the merge for these files, then "
                        "run /chameleon-refresh to regenerate"
                    )
                    any_corrupt = True
            if root:
                seen_roots[root] = len(repo_states)
            repo_states.append(entry)
        checks.append(
            {
                "name": "known_repos",
                "status": "warn" if any_corrupt else "ok",
                "detail": repo_states,
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "known_repos", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    error_count = sum(1 for c in checks if c["status"] == "error")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    if error_count:
        overall = "error"
    elif warn_count:
        overall = "warn"
    else:
        overall = "ok"

    return _envelope(
        {
            "overall": overall,
            "platform": {"system": platform.system(), "release": platform.release()},
            "chameleon_version": _chameleon_version_or_unknown(),
            "checks": checks,
            "summary": {
                "total": len(checks),
                "ok": len(checks) - error_count - warn_count,
                "warn": warn_count,
                "error": error_count,
            },
        }
    )
