"""Stop turn-end block-gate machinery: the lint re-check, the finding->fix
ledger, and the crossfile-existence deny predicate.

``_stop_file_still_blockable`` is the live re-lint the Stop backstop runs on
every candidate file whose per-edit verify armed a blockable flag; a hard
violation still enforceable on the live content is what actually refuses the
turn. ``_ledger_persist`` / ``_ledger_recheck_and_resurface`` are the
finding->fix loop: findings a reviewer surfaced this turn are persisted with a
content-digest anchor, and the NEXT Stop re-checks each open one before this
turn's findings persist, re-surfacing an unaddressed high-severity finding
exactly once. ``_confirmed_crossfile_break_sites`` is the strict F2/F3
predicate that decides whether a removed export is deny-eligible (never the
advisory's keep-biased check). ``_stop_block_scope`` /
``_effective_stop_blocks`` are the shared per-workspace anti-loop block-cap
accounting both the lint backstop and the idiom-review gate charge against.

Extracted verbatim from ``hook_helper.py``. Symbols still owned by
``hook_helper`` (shared with the advisory pipeline or other callers) are
resolved late-bound via a deferred ``from chameleon_mcp import hook_helper as
hh`` import inside each function that needs one, so this module's own
top-level imports stay stdlib-only -- mirroring hook_helper's own pattern of
deferring every non-stdlib import to call time.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

_FINDING_HIGH_CONFIDENCE = 0.7


def _finding_fingerprint(lens: str, rel: str | None, line, message: str | None) -> str:
    """Stable per-(lens, file, locus) dedup key so the same finding across turns
    is one logical ledger row. Uses a message PREFIX (wording is stable enough at
    80 chars; the full message drifts less than the line does under edits)."""
    loc = line if isinstance(line, int) else ""
    key = f"{lens}|{rel or ''}|{loc}|{(message or '')[:80]}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def _finding_severity(f: dict) -> str | None:
    """Normalize a finding's severity across lens shapes: an explicit ``severity``
    string wins; a correctness finding carries only ``confidence`` (0..1), mapped
    to high at/above the confidence floor; a multi-lens finding surfaced by TWO
    lenses independently agreeing reads high; else medium/unknown."""
    sev = f.get("severity")
    if isinstance(sev, str) and sev:
        return sev
    conf = f.get("confidence")
    if isinstance(conf, (int, float)):
        return "high" if conf >= _FINDING_HIGH_CONFIDENCE else "medium"
    lenses = f.get("lenses")
    if isinstance(lenses, list) and len(lenses) >= 2:
        return "high"
    return None


def _finding_message(f: dict) -> str | None:
    """The finding's human message across shapes: correctness uses ``message``,
    the multi-lens synthesis uses ``claim``."""
    m = f.get("message") or f.get("claim")
    return m if isinstance(m, str) else None


def _finding_is_high(severity: str | None) -> bool:
    return isinstance(severity, str) and severity.strip().lower() in ("high", "block", "critical")


def _ledger_persist(repo_id, session_id, repo_root: Path, lens: str, findings) -> None:
    """Persist surfaced findings to the judge_findings ledger (the finding->fix
    loop). ``findings`` is a list of ``{file, line, message, confidence?/severity?}``.
    Records the reviewed file's content digest as the addressed/ignored anchor.
    Gated by CHAMELEON_FINDING_LEDGER, fail-open, off the per-edit hot path."""
    if os.environ.get("CHAMELEON_FINDING_LEDGER") == "0" or not repo_id or not findings:
        return
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.drift.observations import record_judge_finding

        for f in findings:
            if not isinstance(f, dict):
                continue
            rel = f.get("file")
            rel = rel if isinstance(rel, str) else None
            line = f.get("line")
            anchor = None
            if rel:
                try:
                    anchor = hh._content_digest_16(
                        (repo_root / rel).read_bytes()[:100_000].decode("utf-8", errors="replace")
                    )
                except OSError:
                    anchor = None
            record_judge_finding(
                repo_id,
                lens=lens,
                fingerprint=_finding_fingerprint(lens, rel, line, _finding_message(f)),
                severity=_finding_severity(f),
                rel_path=rel,
                line=line if isinstance(line, int) else None,
                anchor_digest=anchor,
                ws_root=str(repo_root),
                session_id=session_id,
            )
    except Exception:
        return


def _ledger_recheck_and_resurface(repo_id, session_id, repo_root: Path) -> list[str]:
    """At Stop, BEFORE this turn's findings persist: re-check every open ledger
    finding against the reviewed file's CURRENT digest -- changed or gone since
    review => addressed (mark + drop) -- and re-surface an unaddressed
    high-severity finding ONCE (mark resurfaced, emit one advisory line). A
    finding already resurfaced and still unchanged is left alone (no re-nag).
    Gated, fail-open to []."""
    if os.environ.get("CHAMELEON_FINDING_LEDGER") == "0" or not repo_id:
        return []
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.drift.observations import mark_judge_finding, open_judge_findings
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        now = int(time.time())
        resurfaced: list[dict] = []
        # Scope to THIS workspace: in a shared-repo_id monorepo several workspaces
        # share one drift.db, and a finding's rel_path is relative to the root
        # that persisted it, so an unscoped re-check would mis-resolve a sibling
        # workspace's findings against this root (file-not-here read as addressed).
        for row in open_judge_findings(repo_id, ws_root=str(repo_root)):
            fid = row.get("id")
            rel = row.get("rel_path")
            anchor = row.get("anchor_digest")
            has_path = isinstance(rel, str)
            current = None
            if has_path:
                try:
                    current = hh._content_digest_16(
                        (repo_root / rel).read_bytes()[:100_000].decode("utf-8", errors="replace")
                    )
                except OSError:
                    current = None  # file gone since review -> treat as addressed
            # Changed or gone => the cited content moved => addressed (a proxy;
            # aggregate telemetry, never per-row enforcement). The digest proxy
            # only applies to a finding that CITED a file: a file-less finding
            # (rel_path=None -- a whole-diff or lens finding with no anchor) has no
            # content to compare, so it must NOT be auto-addressed here (that
            # silently loses a real high-severity finding); it falls through to the
            # one-shot high-severity resurface below and otherwise stays open.
            if has_path and (current is None or (anchor is not None and current != anchor)):
                mark_judge_finding(repo_id, fid, status="addressed", resolved_at=now)
                continue
            # A file-less finding (no digest proxy) that is NOT high never
            # resurfaces, so leaving it open would clog the recheck window forever.
            # Resolve it here (its pre-fix fate); only a file-less HIGH finding
            # escapes to the one-shot resurface below.
            if not has_path and not _finding_is_high(row.get("severity")):
                mark_judge_finding(repo_id, fid, status="addressed", resolved_at=now)
                continue
            # Unchanged and still 'open' and high-severity => one re-surface.
            if row.get("status") == "open" and _finding_is_high(row.get("severity")):
                mark_judge_finding(repo_id, fid, status="resurfaced")
                resurfaced.append(row)
        if not resurfaced:
            return []
        lines = [
            f"[🦎 chameleon: {len(resurfaced)} unaddressed high-severity finding(s) "
            "from a previous turn's review, surfaced once more]",
            "Advisory; verify each before acting -- they may be wrong, or already handled.",
        ]
        for row in resurfaced[:8]:
            rel = row.get("rel_path")
            loc = sanitize_for_chameleon_context(str(rel)) if rel else "?"
            ln = row.get("line")
            if isinstance(ln, int):
                loc += f":{ln}"
            lines.append(f"- {loc} ({sanitize_for_chameleon_context(str(row.get('lens') or '?'))})")
        return lines
    except Exception:
        return []


def _stop_block_scope(repo_root: Path) -> str:
    """Stable per-workspace key for the anti-loop block budget.

    Both the lint backstop and the idiom-review gate charge and read the block
    count under this key, in BOTH single- and multi-root mode, so a workspace
    stays capped across a mid-session single<->multi cardinality change (the
    scalar and the per-workspace map are otherwise disjoint counters, letting a
    persistent violation exceed the cap after a mode flip).
    """
    try:
        return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    except OSError:
        return hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:12]


def _effective_stop_blocks(state, scope: str) -> int:
    """The workspace's block count so far, reconciling the legacy scalar with the
    per-workspace map.

    New blocks always charge the per-workspace map, but a pre-fix single-root
    session (or an old committed state file) recorded them on the scalar. Taking
    the max means a workspace capped under either representation stays capped,
    with no migration step and no way for a mode flip to re-arm a spent cap.
    """
    try:
        scalar = int(state.stop_hook_blocks or 0)
    except (TypeError, ValueError):
        scalar = 0
    try:
        per_root = int(state.stop_hook_blocks_by_root.get(scope, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        per_root = 0
    return max(scalar, per_root)


def _stop_file_still_blockable(
    repo_root: Path,
    file_path: str,
    loaded=None,
    active=None,
    daemon_state=None,
    out_rules=None,
    level: int = 2,  # LEVEL_L2; a module-level literal so the default binds at import time
) -> bool | None:
    """Re-lint a candidate file live and report whether an enforceable hard-class
    violation still stands.

    The Stop backstop reads a cached blockable_unresolved flag that the per-edit
    verify set. That flag can go stale: a phantom import that armed it may have
    been resolved by a later edit to a *different* file (the import target was
    created), so the importing file itself was never re-verified. This cold-path
    re-check (only candidate files reach it) reads the live file, re-resolves the
    archetype, and re-runs the same lints, so a resolved violation no longer
    blocks the turn.

    A hard violation counts only if it is enforceable on the live file: an
    archetype-independent rule (a deterministic content/filesystem fact -- a
    leaked credential or a phantom import) always counts at any escalation level,
    because nothing about a wrong archetype guess makes it spurious; an
    archetype-dependent rule (naming/inheritance/file-naming) counts only when the
    archetype is AST-confirmed at high or medium confidence AND the file has
    escalated to L2, matching the per-edit block gate's ladder so a single
    wrong-archetype match cannot trap the turn. ``level`` is the file's current
    escalation level; archetype-independent rules ignore it. Inline-ignored rules
    never count. Returns False on any error (fail-open: a re-check that can't run
    does not block).

    ``loaded`` is the once-per-pass profile the backstop preloads so the per-file
    re-lint does not re-read the profile for every candidate. ``active`` is the
    once-per-pass set of active block rules; when None it is read from disk (the
    per-edit callers pass nothing). ``daemon_state`` is a shared ``{"available":
    bool}`` flag the backstop threads through the candidate loop: once a daemon
    call comes back empty, every later file skips the daemon and goes straight to
    the in-process archetype resolve, so a hung daemon cannot stack per-file
    timeouts past the hook's hard deadline.

    ``out_rules``, when a list is passed, is appended with the enforceable hard
    rule names that still stand on this file, so the shadow would_block row can
    attribute the backstop block to a specific rule. Collecting them here reuses
    the re-lint this function already performs; the bool return is unchanged.
    """
    try:
        from chameleon_mcp import hook_helper as hh

        p = Path(file_path)
        if not p.is_file():
            return False
        try:
            _sb_raw = p.read_bytes()
            _sb_truncated = len(_sb_raw) > 100_000
            content = _sb_raw[:100_000].decode("utf-8", errors="replace")
        except OSError:
            # The file exists but could not be read this turn (a permissions flip,
            # a network-FS hiccup, an editor lock). "Couldn't check" is NOT
            # "resolved": returning False here would let the caller clear the
            # armed flag and permanently disarm the backstop for a violation still
            # on disk. Signal unknown so the caller keeps the file armed and
            # re-checks it next Stop, without blocking this (unverifiable) turn.
            return None

        archetype_name: str | None = None
        confidence_band: str | None = None
        match_quality: str | None = None
        daemon_usable = daemon_state is None or daemon_state.get("available", True)
        if daemon_usable:
            try:
                from chameleon_mcp import daemon_client

                arch_result = daemon_client.call(
                    "get_archetype", {"repo": str(repo_root), "file_path": file_path}
                )
                if arch_result:
                    data = arch_result.get("data") or {}
                    archetype_name = data.get("archetype")
                    confidence_band = data.get("confidence_band")
                    match_quality = data.get("match_quality")
                elif daemon_state is not None:
                    # Daemon unreachable or timed out: skip it for the rest of the
                    # pass so the remaining files don't each eat a full timeout.
                    daemon_state["available"] = False
            except Exception:
                if daemon_state is not None:
                    daemon_state["available"] = False
        if not archetype_name:
            from chameleon_mcp.tools import get_archetype

            data = (get_archetype(str(repo_root), file_path).get("data")) or {}
            archetype_name = data.get("archetype")
            confidence_band = data.get("confidence_band")
            match_quality = data.get("match_quality")

        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.violation_class import (
            block_eligible_on_file,
            build_ignore_index,
            hard_class_violations,
            is_archetype_independent,
            is_violation_ignored,
        )

        if not archetype_name:
            # No archetype: only archetype-independent content facts (phantom
            # imports, deterministic secrets) can still stand. An eval-call is
            # scanned but filtered by the archetype-independent check below,
            # because it stays gated on an archetype match. posttool_verify
            # recorded this file under the synthetic no-archetype label
            # precisely so the credential blocks the turn here.
            indep = hh._scan_archetype_independent(
                content, file_path, hh._load_rules_for_style(repo_root), repo_root=repo_root
            )
            if not indep:
                return False
            if active is None:
                from chameleon_mcp.enforcement_calibration import active_block_rules

                active = active_block_rules(hh._enf_profile_dir(repo_root))
            # A non-code file (detect_language None) cannot hard-block on an
            # archetype-independent rule: a credential-shaped token in doc/config
            # prose stays advisory but never turn-traps (it has no inline-ignore
            # escape). Matches the posttool arming gate so the two paths agree.
            hard = block_eligible_on_file(
                hard_class_violations(indep, active), language=detect_language(file_path)
            )
            idx = build_ignore_index(content, file_path=file_path)
            if idx is not None:
                hard = [v for v in hard if not is_violation_ignored(v, idx)]
            enforceable = [v for v in hard if is_archetype_independent(v.get("rule"))]
            if isinstance(out_rules, list):
                out_rules.extend(v.get("rule") for v in enforceable if v.get("rule"))
            return bool(enforceable)

        violations = hh._lint_file_in_process(
            repo_root,
            archetype_name,
            content,
            file_path,
            loaded=loaded,
            content_truncated=_sb_truncated,
        )
        if not violations:
            return False

        if active is None:
            from chameleon_mcp.enforcement_calibration import active_block_rules

            active = active_block_rules(hh._enf_profile_dir(repo_root))
        # A non-code file (detect_language None) can resolve to an archetype via a
        # legacy extension-blind paths_pattern; it still has no inline
        # chameleon-ignore escape, so archetype-independent rules (eval/secret)
        # stay advisory and never turn-trap here. Mirrors the no-archetype branch.
        hard = block_eligible_on_file(
            hard_class_violations(violations, active), language=detect_language(file_path)
        )
        idx = build_ignore_index(content, file_path=file_path)
        if idx is not None:
            hard = [v for v in hard if not is_violation_ignored(v, idx)]
        gate_ok = (match_quality == "ast") and (confidence_band in ("high", "medium"))
        # Archetype-dependent rules honor the per-edit escalation ladder: they
        # only refuse the turn once the file has reached L2, so a single
        # wrong-archetype match cannot trap a turn. Archetype-independent facts
        # (secrets, phantom imports) ignore the level entirely.
        from chameleon_mcp.enforcement import LEVEL_L2

        dep_ok = gate_ok and level >= LEVEL_L2
        enforceable = [v for v in hard if is_archetype_independent(v.get("rule")) or dep_ok]
        if isinstance(out_rules, list):
            out_rules.extend(v.get("rule") for v in enforceable if v.get("rule"))
        return bool(enforceable)
    except Exception:
        return False


def _module_exports_at_head(ws_root: Path, target_key: str, lang: str) -> set[str] | None:
    """The named-export set of ``target_key`` at git HEAD, or None if unknowable.

    F3 scope check for the crossfile BLOCK: a break is deny-eligible only when the
    turn INTRODUCED it -- the name was exported at HEAD and is gone now. A name
    already absent at HEAD (a pre-existing broken import surfaced only because the
    module was edited this turn for an unrelated reason) must NOT block. Returns
    None -- read as "cannot confirm, do not block" -- when git is unavailable, the
    blob is not in HEAD (a file created this session), or the export set is open
    (`export *`, an unexpandable star). TS/Python only; the reverse index records
    import intent, not export reality, so only HEAD tells us the name was real.
    """
    try:
        from chameleon_mcp.judge import _run_git
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )
        from chameleon_mcp.production_ref import git_toplevel

        top = git_toplevel(ws_root)
        if top is None:
            return None
        try:
            git_rel = (ws_root / target_key).resolve().relative_to(top).as_posix()
        except (ValueError, OSError):
            return None
        res = _run_git(["show", f"HEAD:{git_rel}"], cwd=ws_root)
        if res is None or res.returncode != 0:
            return None
        content = res.stdout or ""
        if lang == "python":
            # Absolute path so the __init__.py sibling-listing resolves against the
            # module's real directory, not the process cwd (the content is HEAD's,
            # but the dirname must still be the package dir).
            names, open_set = _python_current_export_names(content, ws_root / target_key)
        elif lang == "typescript":
            names, open_set = _current_export_names(content)
        else:
            return None
        if open_set:
            return None
        return set(names)
    except Exception:
        return None


def _importer_confirms_crossfile_break(
    ws_root: Path, importer_rel: str, name: str, line, target_key: str, lang: str
) -> bool:
    """STRICT per-importer block confirmation (F2): the importer still references
    ``name`` AND that reference still POSITIVELY resolves to ``target_key``.

    Stricter than the advisory's ``_live_break``, which keep-biases to "broken"
    when the import specifier does not resolve (empty keys). For a DENY the
    keep-bias is an over-block vector: a same-turn repoint of ``name`` to a
    bare-package / out-of-repo module yields empty keys, and blocking on that is a
    false positive. So the block requires keys NON-EMPTY and containing the target;
    anything less is "cannot confirm still-sourced-from-target" -> advisory only.
    """
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.symbol_index import make_module_resolver

        ip = ws_root / importer_rel
        text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        if not hh._reference_present(text, name, line, lang):
            return False
        try:
            resolver = make_module_resolver(Path(ws_root).resolve(), lang)
        except Exception:
            return False
        keys = hh._imported_source_keys(text, name, ip.parent, lang, resolver)
        return bool(keys) and target_key in keys
    except Exception:
        return False


def _target_still_provides(ws_root: Path, target_key: str, name: str, lang: str) -> bool:
    """True if the target module's CURRENT content still provides ``name``.

    Defense-in-depth so the deny is self-contained: the advisory only emits a break
    for a currently-removed name, but the block predicate must not TRUST that
    invariant. A name the target still provides -- re-added this turn, re-exported
    (`export { name } from './impl'`), behind an open `export *`, or converted from
    an ES export to a CommonJS one (`module.exports` / `exports.name`, which the
    ES-only export scan reads as "removed") -- must never reach a hard block. Fails
    open to False (cannot confirm it provides) so a genuine break is not suppressed
    by a read error; F2/F3 then decide.
    """
    try:
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )

        abs_target = Path(ws_root) / target_key
        content = abs_target.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        if lang == "python":
            names, open_set = _python_current_export_names(content, abs_target)
        else:
            names, open_set = _current_export_names(content)
        if open_set or name in names:
            return True
        if lang == "typescript" and (
            re.search(r"\bmodule\.exports\b", content)
            or re.search(r"(?<![A-Za-z0-9_$])exports\." + re.escape(name) + r"\b", content)
        ):
            return True
    except Exception:
        return False
    return False


def _confirmed_crossfile_break_sites(rec: dict) -> list[tuple[str, int | None]]:
    """Deny-eligible importer sites for one structured break, or [] if none.

    Applies F3 (turn-introduced: name exported by the module at HEAD) then F2 (each
    importer strictly re-confirmed to still source ``name`` from the target). Block
    scope for v1: TS/Python ``export`` (a named export removed from an existing
    module) only. ``deleted`` is advisory-only for now -- a gone target makes the
    importer specifier unresolvable, so the strict F2 sourcing check (which
    separates "still points at the target" from "repointed to a bare package")
    cannot run without raw-specifier comparison; keep-biasing instead would
    reintroduce the very over-block F2 exists to stop. Ruby ``constant`` (global
    resolution -- cannot cheaply prove no other file defines it at Stop) and
    ``barrel`` (star-expansion at HEAD too costly) are advisory-only too.
    Under-block is the safe direction; a deny must be FP-free.
    """
    kind = rec.get("kind")
    lang = rec.get("lang")
    if kind != "export" or lang not in ("typescript", "python"):
        return []
    ws_root = rec.get("ws_root")
    name = rec.get("name")
    target_key = rec.get("target_key")
    if not (ws_root and isinstance(name, str) and isinstance(target_key, str)):
        return []
    head_exports = _module_exports_at_head(ws_root, target_key, lang)
    if head_exports is None or name not in head_exports:
        return []  # cannot confirm the removal was introduced this turn -> advisory only
    if _target_still_provides(ws_root, target_key, name, lang):
        return []  # target still provides it (re-added / re-exported / CJS) -> not a break
    confirmed: list[tuple[str, int | None]] = []
    for imp_rel, line in rec.get("importers") or []:
        if _importer_confirms_crossfile_break(ws_root, imp_rel, name, line, target_key, lang):
            confirmed.append((imp_rel, line))
    return confirmed
