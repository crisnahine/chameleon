"""Opt-in dependency / supply-chain audit helper.

Runs ``npm audit --json`` and/or ``bundler-audit check`` in the repo root,
whichever manifests are present, and returns a structured advisory summary. This
is the one part of supply-chain review that needs the network, so it is gated and
fails open:

  - Gated behind ``CHAMELEON_ALLOW_DEP_AUDIT=1``. Without it the tool refuses with
    a clear message rather than spawning a network process behind the user's back.
  - Hard wall-clock timeout per auditor; a hung registry can never trap the caller.
  - Fails open to a structured ``unavailable`` result when the binary is missing,
    the network is down, or the auditor errors. An unavailable audit is never an
    error the caller must handle; it is just "no signal".

Advisory only. Nothing here blocks an edit or a turn; the manifest/lockfile
diff-parse checks (no network) live in the pr-review skill and run regardless.
This helper does NOT reuse chameleon's private provisioned-node staging dir
(which installs chameleon's own dep with ``--no-audit`` and never touches the
user's repo); it shells the user's own ``npm`` / ``bundler-audit`` against their
repo root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

ALLOW_ENV = "CHAMELEON_ALLOW_DEP_AUDIT"


def is_enabled() -> bool:
    """True when the operator opted into the network audit for this invocation."""
    return os.environ.get(ALLOW_ENV) == "1"


def _unavailable(tool: str, reason: str) -> dict:
    """A structured "no signal" result: the auditor could not produce findings."""
    return {"tool": tool, "status": "unavailable", "reason": reason, "findings": []}


def _run(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess | None:
    """Run an auditor with a hard timeout; None on timeout or any spawn error."""
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _audit_npm(repo_root: Path, *, timeout: int) -> dict:
    """Run ``npm audit --json`` and summarize the vulnerability counts.

    npm exits non-zero when vulnerabilities are found, so a non-zero exit with
    parseable JSON is still a successful audit, not a failure. Only an unparseable
    body or a missing binary degrades to unavailable.
    """
    if shutil.which("npm") is None:
        return _unavailable("npm-audit", "npm not on PATH")
    proc = _run(["npm", "audit", "--json"], cwd=repo_root, timeout=timeout)
    if proc is None:
        return _unavailable("npm-audit", "npm audit timed out or could not spawn")
    raw = proc.stdout or ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _unavailable("npm-audit", "npm audit returned no parseable JSON (offline?)")

    # npm v7+ shape: {"vulnerabilities": {<name>: {...}},
    #   "metadata": {"vulnerabilities": {"info":N,"low":N,...,"total":N}}}.
    meta = data.get("metadata") if isinstance(data, dict) else None
    severities: dict = {}
    total = 0
    if isinstance(meta, dict):
        sev = meta.get("vulnerabilities")
        if isinstance(sev, dict):
            severities = {
                k: v
                for k, v in sev.items()
                if k in ("info", "low", "moderate", "high", "critical") and isinstance(v, int)
            }
            t = sev.get("total")
            total = t if isinstance(t, int) else sum(severities.values())
    findings: list[dict] = []
    vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
    if isinstance(vulns, dict):
        for name, info in vulns.items():
            if not isinstance(info, dict):
                continue
            findings.append(
                {
                    "package": str(name),
                    "severity": str(info.get("severity") or "unknown"),
                    "via": _npm_via_titles(info.get("via")),
                }
            )
    return {
        "tool": "npm-audit",
        "status": "ok",
        "total": total,
        "severities": severities,
        "findings": findings,
    }


def _npm_via_titles(via) -> list[str]:
    """Pull advisory titles out of an npm ``via`` list, dropping dep-name strings."""
    out: list[str] = []
    if isinstance(via, list):
        for item in via:
            if isinstance(item, dict):
                title = item.get("title")
                if isinstance(title, str) and title:
                    out.append(title)
    return out


def _audit_bundler(repo_root: Path, *, timeout: int) -> dict:
    """Run ``bundler-audit check`` and summarize advisory lines.

    bundler-audit prints human-readable text (not JSON) and exits non-zero when
    advisories are found, so the exit code alone is not the signal; the presence
    of ``Name:`` advisory blocks in stdout is. A missing binary degrades to
    unavailable.
    """
    binary = shutil.which("bundler-audit") or shutil.which("bundle-audit")
    if binary is None:
        return _unavailable("bundler-audit", "bundler-audit not on PATH")
    proc = _run([binary, "check"], cwd=repo_root, timeout=timeout)
    if proc is None:
        return _unavailable("bundler-audit", "bundler-audit timed out or could not spawn")
    out = proc.stdout or ""
    advisories: list[dict] = []
    current: dict = {}
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            if current:
                advisories.append(current)
            current = {"package": stripped.split(":", 1)[1].strip()}
        elif stripped.startswith("Advisory:"):
            current["advisory"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Criticality:"):
            current["severity"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Title:"):
            current["title"] = stripped.split(":", 1)[1].strip()
    if current:
        advisories.append(current)
    return {
        "tool": "bundler-audit",
        "status": "ok",
        "total": len(advisories),
        "findings": advisories,
    }


def run_dep_audit(repo_root: Path) -> dict:
    """Audit whichever ecosystems have a manifest in ``repo_root``.

    Returns a dict ``{"audits": [<per-tool result>], "ran": [...], "skipped": ...}``.
    Each per-tool result is either an ``ok`` summary or an ``unavailable`` no-signal
    result. Never raises; an auditor that blows up contributes an ``unavailable``
    entry so the caller always gets a structured advisory.
    """
    timeout = threshold_int("DEP_AUDIT_TIMEOUT_SECONDS")
    audits: list[dict] = []
    ran: list[str] = []
    skipped: list[str] = []

    has_npm_manifest = (repo_root / "package.json").is_file()
    has_ruby_manifest = (repo_root / "Gemfile.lock").is_file() or (repo_root / "Gemfile").is_file()

    if has_npm_manifest:
        ran.append("npm-audit")
        try:
            audits.append(_audit_npm(repo_root, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            audits.append(_unavailable("npm-audit", f"{type(exc).__name__}: {exc}"))
    else:
        skipped.append("npm-audit (no package.json)")

    if has_ruby_manifest:
        ran.append("bundler-audit")
        try:
            audits.append(_audit_bundler(repo_root, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            audits.append(_unavailable("bundler-audit", f"{type(exc).__name__}: {exc}"))
    else:
        skipped.append("bundler-audit (no Gemfile)")

    return {"audits": audits, "ran": ran, "skipped": skipped}
