"""Deterministic turn-end Stop advisory builders.

Each builder returns ``list[str]`` of pre-sanitized lines, or ``[]`` when
nothing applies. The Stop pipeline (``hook_helper._run_advisories``) wraps
each non-empty list in ``"<chameleon-context>\\n" + "\\n".join(lines) +
"\\n</chameleon-context>"`` and joins blocks with ``"\\n\\n"`` into
``hookSpecificOutput.additionalContext``. Every builder fails open to ``[]``
via a whole-body ``try/except``.

Extracted verbatim from ``hook_helper.py``. Symbols still owned by
``hook_helper`` (shared with the block-gate pipeline or other callers) are
resolved late-bound via a deferred ``from chameleon_mcp import hook_helper as
hh`` import inside each function that needs one, so this module's own
top-level imports stay stdlib-only -- mirroring hook_helper's own pattern of
deferring every non-stdlib import to call time.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def _stale_test_advisory_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    preloaded,
    daemon_state: dict | None,
) -> list[str]:
    """Build the turn-end stale-test advisory lines, or [] when nothing applies.

    A source file edited this turn whose archetype's siblings overwhelmingly ship
    a paired test, but whose existing paired test went untouched, is at risk of
    going stale. For each such file, name the test path and the exports the edit
    may have moved out from under it. Advisory only: the pairing floor admits a
    sizable fraction of legitimately untested files, so this is a coverage nudge,
    never a block. Returns sanitized lines ready to fold into the Stop context;
    fails open to [] on any error.

    ``preloaded`` is the once-per-pass profile the backstop already loaded, so
    this reuses it rather than re-reading conventions.json. ``daemon_state`` is
    the shared liveness flag the backstop threads through archetype resolution.
    """
    try:
        if cfg.mode == "off" or not cfg.stale_test_advisory:
            return []
        if preloaded is None or not hasattr(preloaded, "conventions"):
            return []
        conv = preloaded.conventions.get("conventions", {}) or {}
        test_pairing = conv.get("test_pairing") or {}
        if not test_pairing:
            return []
        key_exports = conv.get("key_exports") or {}

        from chameleon_mcp.violation_class import ignored_rules

        # Edited files that still exist and are not opted out via an inline
        # `chameleon-ignore tests` (or bare ignore) directive in the touched file.
        edited_abs: set[str] = set()
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "tests" in ign or "stale-test" in ign:
                continue
            edited_abs.add(path)
        if not edited_abs:
            return []

        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.cochange import stale_test_items
        from chameleon_mcp.lint_engine import detect_language

        resolver = hh._archetype_resolver(repo_root, daemon_state or {"available": True})

        def _read_content(abs_path: str) -> str | None:
            try:
                return Path(abs_path).read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                return None

        items = stale_test_items(
            repo_root=repo_root,
            test_pairing=test_pairing,
            key_exports=key_exports,
            edited_abs=edited_abs,
            archetype_of=resolver,
            language_of=detect_language,
            read_content=_read_content,
        )
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        max_files = threshold_int("STALE_TEST_ADVISORY_MAX_FILES")
        shown = items[:max_files]
        extra = len(items) - len(shown)

        lines = [
            f"[🦎 chameleon: {len(items)} edited source file"
            f"{'s' if len(items) != 1 else ''} may have left a paired test stale]",
            "You changed these files but did not touch their paired tests. Confirm "
            "the test still covers the new behavior; this is advisory, not a block.",
        ]
        for it in shown:
            src = sanitize_for_chameleon_context(it.source_rel)
            test = sanitize_for_chameleon_context(it.test_rel)
            line = f"- {src} -> test {test} (unchanged)"
            if it.exports:
                names = ", ".join(sanitize_for_chameleon_context(n) for n in it.exports)
                line += f"; changed exports: {names}"
            lines.append(line)
        if extra > 0:
            lines.append(f"- ...and {extra} more.")
        lines.append(
            "To silence this for a file, add `// chameleon-ignore tests` "
            "(`# chameleon-ignore tests` in Ruby) in the source you touched."
        )
        return lines
    except Exception:
        return []


def _new_files_in_changeset(repo_root: Path, edited_abs: set[str]) -> set[str]:
    """Subset of ``edited_abs`` that did not exist at HEAD (created this turn).

    A change-set-completeness rule only fires on a brand-new file: editing a
    method on an existing model must not demand a fresh migration. "New" is
    decided by git, which is the only turn-end signal available (the enforcement
    state does not record creation). A path is new when ``git ls-files`` does not
    track it AND ``git cat-file`` finds no blob for it at HEAD, so a file the user
    created and `git add`-ed this turn still counts as new.

    Fails safe to the empty set: when git is unavailable or the work tree cannot
    be probed, no file is treated as new and the change-set check stays silent
    rather than guess a creation it cannot confirm.
    """
    try:
        from chameleon_mcp.judge import _git_available, _run_git

        if not edited_abs or not _git_available(repo_root):
            return set()
        new_abs: set[str] = set()
        for ap in edited_abs:
            try:
                rel = Path(ap).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            tracked = _run_git(["ls-files", "--error-unmatch", "--", rel], cwd=repo_root)
            if tracked is not None and tracked.returncode == 0:
                # Tracked in the index; not a file this turn created.
                continue
            at_head = _run_git(["cat-file", "-e", f"HEAD:{rel}"], cwd=repo_root)
            if at_head is not None and at_head.returncode == 0:
                # Exists in the HEAD tree; an existing file, not a creation.
                continue
            new_abs.add(ap)
        return new_abs
    except Exception:
        return set()


def _cochange_history_advisory_lines(
    *, repo_root: Path, repo_id: str | None, state, cfg, persist=None
) -> list[str]:
    """Turn-end historical co-change advisory, or [] (F7).

    When this turn edited a file whose git history shows a strong co-change partner
    (edited together in >= min_ratio of the file's commits) that the turn did NOT
    touch, nudge to consider it. Framework-agnostic: mined from the repo's OWN
    history at bootstrap (cochange_history.py) and read from the plugin data dir --
    the complement to the hand-curated framework pairs of the sibling advisory.
    Advisory only, never a block (a partial edit may defer its partner to a
    follow-up commit). Once per session per (source, partner). Fails open to [].
    """
    try:
        if cfg.mode == "off" or os.environ.get("CHAMELEON_COCHANGE_HISTORY") == "0" or not repo_id:
            return []
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.cochange_history import (
            COCHANGE_HISTORY_FILENAME,
            load_cochange_history,
            missing_partners,
        )

        index = load_cochange_history(hh._plugin_data_dir() / repo_id / COCHANGE_HISTORY_FILENAME)
        if index is None:
            return []
        # Keys are relative to the work-tree top recorded at build time; relativize
        # the turn's edited files to that SAME top so they match a monorepo's shared
        # global index -- without spawning git on the Stop hot path.
        top_str = index.get("root")
        if not isinstance(top_str, str) or not top_str:
            return []
        git_top = Path(top_str)

        from chameleon_mcp.violation_class import ignored_rules

        changed_rels: set[str] = set()
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "cochange" in ign:  # honor the same inline opt-out
                continue
            try:
                changed_rels.add(p.resolve().relative_to(git_top).as_posix())
            except (ValueError, OSError):
                continue
        if not changed_rels:
            return []

        # Re-check each partner still exists in the tree at consume time: the index
        # is only rebuilt at bootstrap/refresh, so a partner deleted since would
        # otherwise be surfaced as an un-actionable false omission (a nudge to edit
        # a file that no longer exists). CONTAIN the partner path first: the
        # plugin-data index is off the trust surface (not HMAC-signed), so a third
        # local user tampering it could inject a ``../`` traversal partner, turning
        # the existence re-check into an out-of-repo existence oracle. A legit miner
        # only ever writes top-relative paths, so containment drops only tampered
        # ones -- the same guard the sibling _pending_findings_block applies.
        from chameleon_mcp.stop_verify import _contained_rel

        items = []
        for it in missing_partners(index, changed_rels):
            partner = it.get("partner")
            safe = _contained_rel(git_top, partner) if isinstance(partner, str) else None
            if safe is not None and (git_top / safe).is_file():
                items.append(it)

        # Once per session per (source -> partner); reuse the sibling advisory's
        # dedup set with a namespaced key so the same pairing does not re-render
        # on every consecutive Stop. Distinct key format, no collision.
        def _key(it) -> str:
            return f"cochist:{it['source']}->{it['partner']}"

        items = [it for it in items if _key(it) not in state.cochange_shown]
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        shown = items[: threshold_int("COCHANGE_ADVISORY_MAX_ITEMS")]
        state.cochange_shown.update(_key(it) for it in shown)
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

        lines = [
            "[🦎 chameleon: co-change]",
            "This turn edited files whose git history shows a usual partner left "
            "untouched (advisory; a follow-up commit may be intended):",
        ]
        for it in shown:
            src = sanitize_for_chameleon_context(str(it["source"]))
            partner = sanitize_for_chameleon_context(str(it["partner"]))
            pct = int(round((it.get("ratio") or 0) * 100))
            lines.append(
                f"- {src} usually changes with {partner} "
                f"({pct}% of {src}'s {it.get('of')} commits); {partner} is untouched."
            )
        return lines
    except Exception:
        return []


def _changeset_completeness_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    daemon_state: dict | None,
    persist=None,
) -> list[str]:
    """Build the turn-end change-set-completeness advisory lines, or [].

    When a turn creates a NEW file that conventionally cannot stand alone (a Rails
    model needs a migration, a new controller a route) but the change-set carries
    no matching companion, surface a nudge to add it. Driven by a hand-curated
    framework pair table; each rule is silenced for a repo whose own committed
    files already break the pairing often enough that firing it would nag.
    Advisory only, never a block: a partial edit may legitimately defer its
    companion to a follow-up commit. Returns sanitized lines ready to fold into
    the Stop context; fails open to [] on any error.

    ``daemon_state`` is unused here (the check is path-pattern only and needs no
    archetype resolution) but kept in the signature to mirror the sibling
    advisory builders the backstop calls.
    """
    try:
        if cfg.mode == "off" or not cfg.changeset_completeness:
            return []

        from chameleon_mcp.violation_class import ignored_rules

        # Edited files that still exist and are not opted out via an inline
        # `chameleon-ignore cochange` (or bare ignore) directive in the file.
        edited_abs: set[str] = set()
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "cochange" in ign:
                continue
            edited_abs.add(path)
        if not edited_abs:
            return []

        new_files_abs = _new_files_in_changeset(repo_root, edited_abs)
        if not new_files_abs:
            return []

        from chameleon_mcp.cochange import changeset_completeness_items, cochange_rule_disabled
        from chameleon_mcp.lint_engine import detect_language

        # Cache the per-rule repo-applicability verdict across the turn's new
        # files so the bounded committed-file walk runs at most once per rule.
        disabled_cache: dict[str, bool] = {}

        def _rule_enabled(rule) -> bool:
            verdict = disabled_cache.get(rule.rule_id)
            if verdict is None:
                verdict = cochange_rule_disabled(rule, repo_root)
                disabled_cache[rule.rule_id] = verdict
            return not verdict

        items = changeset_completeness_items(
            repo_root=repo_root,
            new_files_abs=new_files_abs,
            edited_abs=edited_abs,
            language_of=detect_language,
            rule_enabled=_rule_enabled,
        )
        # Once per session per (file, rule): the same still-unresolved pairing
        # would otherwise re-render verbatim on every consecutive Stop. The
        # caller persists `state` after the gates run, so the marker survives
        # the turn. Only items actually DISPLAYED are recorded: one that fell
        # past the display cap (surfaced only as "...and N more") has not been
        # shown and may resurface on a later Stop.
        items = [it for it in items if f"{it.source_rel}::{it.rule_id}" not in state.cochange_shown]
        if not items:
            return []

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        max_items = threshold_int("COCHANGE_ADVISORY_MAX_ITEMS")
        shown = items[:max_items]
        extra = len(items) - len(shown)
        state.cochange_shown.update(f"{it.source_rel}::{it.rule_id}" for it in shown)
        # The advisory path has no downstream save_state (only the block paths
        # save), so without persisting here the marker dies with this process
        # and the same advisory re-renders on every consecutive Stop. The
        # caller supplies the writer; a persist failure must not cost the
        # advisory itself.
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

        lines = [
            f"[🦎 chameleon: {len(items)} new file"
            f"{'s' if len(items) != 1 else ''} may be missing a companion change]",
            "You created these files but the change-set has no matching companion. "
            "Add it or confirm it is a separate commit; this is advisory, not a block.",
        ]
        for it in shown:
            src = sanitize_for_chameleon_context(it.source_rel)
            msg = sanitize_for_chameleon_context(it.message)
            lines.append(f"- {src}: {msg}")
        if extra > 0:
            lines.append(f"- ...and {extra} more.")
        # The comment token depends on the flagged file's language: `#` for
        # Ruby/Python, `//` for TS/JS. The Django model->migration rule fires
        # EXCLUSIVELY on Python, where `//` is a syntax error, so a blind `//`
        # hint was actively wrong. Scope the token to the languages actually shown.
        _langs = {detect_language(it.source_rel) for it in shown}
        if _langs and _langs <= {"ruby", "python"}:
            _ign = "`# chameleon-ignore cochange`"
        elif _langs and _langs <= {"typescript", "javascript"}:
            _ign = "`// chameleon-ignore cochange`"
        else:
            _ign = (
                "`# chameleon-ignore cochange` (Ruby/Python) or "
                "`// chameleon-ignore cochange` (TS/JS)"
            )
        lines.append(f"To silence this for a file, add {_ign} in the file you touched.")
        return lines
    except Exception:
        return []


# Any quoted string literal (both kinds), used only for offset-preserving
# DETECTION passes where every string must disappear regardless of interpolation.
_QUOTED_ANY_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
# Ruby %-literals with a bracket delimiter. The captured letter selects the
# interpolation rule: lowercase w/i/q/s never interpolate; W/I/Q/r/x and the
# bare `%(` do. Non-nesting single-delimiter match -- the common Rails style.
_RUBY_PERCENT_RE = re.compile(
    r"%([wWiIqQrsx]?)\[[^\]]*\]"
    r"|%([wWiIqQrsx]?)\([^)]*\)"
    r"|%([wWiIqQrsx]?)\{[^}]*\}"
    r"|%([wWiIqQrsx]?)<[^>]*>"
)
_RUBY_PERCENT_NONINTERP = frozenset({"w", "i", "q", "s"})
# A heredoc: `<<`, optional `-`/`~`, an optionally-quoted tag, arbitrary opener
# tail, then the body up to a line that is only the tag. Single-quoted tag =
# non-interpolating.
_RUBY_HEREDOC_RE = re.compile(
    r"<<[-~]?(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n.*?^[ \t]*\2\b",
    re.DOTALL | re.MULTILINE,
)


def _blank_keep_newlines(s: str) -> str:
    """Replace every non-newline char with a space (equal length, newlines kept)
    so downstream offset/line math over the blanked copy stays aligned."""
    return re.sub(r"[^\n]", " ", s)


def _blank_ruby_noncode(text: str) -> str:
    """Blank Ruby comments, %-literals, and heredocs that carry a constant name
    only as inert text after a rename, WHILE preserving `#{Const}` interpolation
    (a real reference). Keep-biased: an unparseable form stays counted (a
    false-positive over-fire) rather than dropped (a false-negative that hides a
    real break). Offset/length-preserving throughout.
    """
    try:
        # Heredocs first: a heredoc body can hold `#` chars that would otherwise
        # look like comment starts. Blank a non-interpolating heredoc (single-quoted
        # tag), or an interpolating one with no `#{` in its body.
        def _heredoc_sub(m: re.Match) -> str:
            body = m.group(0)
            quoted = m.group(1) == "'"
            if quoted or "#{" not in body:
                return _blank_keep_newlines(body)
            return body

        s = _RUBY_HEREDOC_RE.sub(_heredoc_sub, text)

        # %-literals: non-interpolating letters always blank; interpolating ones
        # only when they carry no `#{`.
        def _percent_sub(m: re.Match) -> str:
            lit = m.group(0)
            letter = next((g for g in m.groups() if g is not None), "")
            if letter in _RUBY_PERCENT_NONINTERP or "#{" not in lit:
                return _blank_keep_newlines(lit)
            return lit

        s = _RUBY_PERCENT_RE.sub(_percent_sub, s)

        # Comments: detect on a copy with ALL quoted strings blanked, so a `#`
        # inside a string (including a literal `#` before a `#{Const}` in an
        # interpolating string) is never mistaken for a comment start. `#{` is
        # excluded so interpolation openers are never treated as comments. Blank
        # the detected [start, EOL) ranges in `s` (offsets align: string blanking
        # is equal-length).
        detect = _QUOTED_ANY_RE.sub(lambda mm: _blank_keep_newlines(mm.group(0)), s)
        chars = list(s)
        for cm in re.finditer(r"#(?!\{)", detect):
            start = cm.start()
            eol = detect.find("\n", start)
            if eol == -1:
                eol = len(chars)
            for j in range(start, eol):
                chars[j] = " "
        s = "".join(chars)

        # Finally the non-interpolating quoted strings (the round-1 behaviour):
        # a plain "..." / '...' with no `#{` is inert text.
        def _quoted_sub(m: re.Match) -> str:
            lit = m.group(0)
            return _blank_keep_newlines(lit) if "#{" not in lit else lit

        return _QUOTED_ANY_RE.sub(_quoted_sub, s)
    except Exception:
        # Any regex failure falls back to the plain-string-only blanking so the
        # check never crashes; keep-biased means it just over-fires at worst.
        return _QUOTED_ANY_RE.sub(
            lambda m: _blank_keep_newlines(m.group(0)) if "#{" not in m.group(0) else m.group(0),
            text,
        )


# `export * from '<spec>'` -- captures the re-export source spec so a barrel's
# star sources can be expanded (the plain `export * from` regex in
# phantom_imports only detects presence, not the spec).
_TS_EXPORT_STAR_FROM_RE = re.compile(r"\bexport\s*\*\s*from\s*['\"]([^'\"]+)['\"]")


def _barrel_reexports_stem(barrel_content: str, stem: str) -> bool:
    """True if ``barrel_content`` re-exports the sibling module ``stem`` -- either
    ``export * from './stem'`` or ``export { ... } from './stem'`` (with or without
    an extension). Used to decide whether an edited origin file's removed export
    flows through this barrel."""
    esc = re.escape(stem)
    pat = re.compile(
        r"\bexport\s*(?:\*|\{[^}]*\})\s*from\s*['\"]\.[^'\"]*/?" + esc + r"(?:\.[cm]?[jt]sx?)?['\"]"
    )
    return bool(pat.search(barrel_content))


def _barrel_effective_exports(barrel_path: Path, ws_root: Path, resolver):
    """Compute the set of names a TS barrel currently exports, expanding its
    ``export * from`` sources ONE level, or signal it cannot be trusted.

    Returns ``(names, resolvable)``. ``resolvable`` is False -- and the caller
    must then NOT report a break -- when any star source cannot be read or is
    itself a star barrel (a deeper chain we do not expand): the removed name might
    still be provided there, so firing would be a false positive. Fail-safe by
    construction: uncertainty suppresses the finding.
    """
    from chameleon_mcp.phantom_imports import _current_export_names

    try:
        content = barrel_path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
    except OSError:
        return frozenset(), False
    names: set[str] = set()
    # The barrel's own direct exports: strip the `export * from` lines first so
    # _current_export_names sees a closed set (those stars are expanded below).
    without_stars = _TS_EXPORT_STAR_FROM_RE.sub(" ", content)
    direct, direct_open = _current_export_names(without_stars)
    if direct_open:
        return frozenset(), False  # a non-star open shape we can't enumerate
    names |= set(direct)
    for m in _TS_EXPORT_STAR_FROM_RE.finditer(content):
        spec = m.group(1)
        src_key = resolver(spec, barrel_path.parent)
        if not src_key:
            return frozenset(), False  # unresolvable source -> can't confirm
        src_path = ws_root / src_key
        try:
            src_content = src_path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        except OSError:
            return frozenset(), False
        src_names, src_open = _current_export_names(src_content)
        if src_open:
            return frozenset(), False  # nested star barrel -> not expanded, bail
        names |= set(src_names)
    return frozenset(names), True


def _crossfile_existence_advisory_lines(
    *,
    repo_root: Path,
    state,
    cfg,
    deleted_paths: list[str] | None = None,
    out_breaks: list | None = None,
    for_block: bool = False,
) -> list[str]:
    """Build the turn-end cross-file existence-break advisory lines, or [].

    For each TypeScript source the turn touched, recompute its current export set
    (a regex read, no parser) and consult the prebuilt reverse index for names an
    indexed importer still references that the file NO LONGER exports. Each such
    removed/renamed export is a call site this turn broke; name it and its still-
    referencing importers. Advisory only, never a block: a mid-rename turn may
    legitimately leave a site for a follow-up edit. Bounded -- the index is read
    once, each touched file's exports come from a single regex pass, and the
    importer presence check is a word-boundary scan, so no caller is parsed at
    Stop. Returns sanitized lines ready to fold into the Stop context; fails open
    to [] on any error.
    """
    try:
        # for_block: the deny caller collects out_breaks under its own feature flag,
        # so it bypasses the advisory nudge flag here (mode==off still short-circuits
        # both -- an enforcement-off repo runs neither the advisory nor the block).
        if cfg.mode == "off" or (not for_block and not cfg.crossfile_existence_advisory):
            return []

        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.constant_index import load_constant_index
        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.symbol_index import (
            load_reverse_index,
            make_module_resolver,
            module_key_for_path,
        )
        from chameleon_mcp.violation_class import ignored_rules

        # Per-edited-file WORKSPACE resolution. In a monorepo the cwd's repo_root
        # is the git top-level, whose reverse/constant index covers only the root
        # workspace; each edited file's OWN workspace (nearest ancestor `.chameleon`)
        # owns the index that records its importers -- the same per-file resolution
        # posttool-verify uses (find_repo_root(file)). Resolving one index for
        # repo_root left every non-root workspace's breaks silent at Stop. Cache the
        # workspace root + its indexes so repeated files in one workspace load once.
        _ws_root_cache: dict[str, Path] = {}
        _ws_index_cache: dict[str, object] = {}
        _ws_cidx_cache: dict[str, object] = {}

        def _ws_root_for(abs_path: Path) -> Path:
            key = str(abs_path)
            r = _ws_root_cache.get(key)
            if r is None:
                try:
                    r = find_repo_root(Path(abs_path)) or repo_root
                except Exception:
                    r = repo_root
                _ws_root_cache[key] = r
            return r

        def _ws_index_for(ws_root: Path):
            key = str(ws_root)
            if key not in _ws_index_cache:
                try:
                    _ws_index_cache[key] = load_reverse_index(ws_root)
                except Exception:
                    _ws_index_cache[key] = None
            return _ws_index_cache[key]

        def _ws_cidx_for(ws_root: Path):
            key = str(ws_root)
            if key not in _ws_cidx_cache:
                try:
                    _ws_cidx_cache[key] = load_constant_index(ws_root)
                except Exception:
                    _ws_cidx_cache[key] = None
            return _ws_cidx_cache[key]

        # Cheap early-out: if the cwd workspace AND no child workspace can hold an
        # index (neither reverse nor constant at repo_root, and repo_root has no
        # nested .chameleon), there is nothing to check. A monorepo root with child
        # workspaces still proceeds -- the per-file resolution finds their indexes.
        _has_child_ws = False
        try:
            from chameleon_mcp.tools import _iter_workspace_chameleon_dirs

            _has_child_ws = next(_iter_workspace_chameleon_dirs(Path(repo_root)), None) is not None
        except Exception:
            _has_child_ws = False
        if (
            load_reverse_index(repo_root) is None
            and load_constant_index(repo_root) is None
            and not _has_child_ws
        ):
            return []

        from chameleon_mcp._thresholds import threshold_int

        max_files = threshold_int("CROSSFILE_STOP_ADVISORY_MAX_FILES")
        max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")

        # One specifier resolver per (language, workspace root), built lazily: it
        # joins a relative/aliased import onto the importer's dir exactly the way
        # that workspace's reverse index did at build, so a key it returns is
        # comparable to that workspace's target keys. Used to drop a REPOINTED
        # import (the name now sourced from a different module) from the finding.
        _resolver_cache: dict[str, object] = {}

        def _resolver_for(language: str, ws_root: Path):
            key = f"{language}\x00{ws_root}"
            r = _resolver_cache.get(key)
            if r is None:
                try:
                    resolved = Path(ws_root).resolve()
                except OSError:
                    resolved = Path(ws_root)
                r = make_module_resolver(resolved, language)
                _resolver_cache[key] = r
            return r

        def _live_break(
            importer_rel: str,
            name: str,
            line: int | None,
            target_key: str,
            language: str,
            ws_root: Path,
        ) -> bool:
            # One read of the importer answers both questions a real break needs:
            # (1) does it still reference ``name`` (word-boundary bareword), and
            # (2) is that reference still sourced from the TARGET module. A
            # move-and-reimport refactor leaves the bareword present but repoints
            # the import to a NEW module; the reverse index is a bootstrap snapshot
            # that still attributes the import to the OLD module, so the bareword
            # check alone would fire a phantom "you broke a call site" finding every
            # turn until the next refresh -- the recurring "stale index" complaint.
            # Suppress only when ``name`` IS imported but NONE of those imports
            # resolve to the target; an unresolved/absent binding falls back to the
            # bareword result so a parse miss never hides a genuine break. The
            # importer path is relative to the file's OWN workspace root, so join
            # to ws_root (the monorepo root would miss a nested-workspace importer).
            ip = ws_root / importer_rel
            try:
                text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                return False
            # (1) still references ``name`` as CODE (not only inside the import path
            # string OR a stale comment), and (2) the reference is still sourced
            # from the TARGET module.
            if not hh._reference_present(text, name, line, language):
                return False
            keys = hh._imported_source_keys(
                text, name, ip.parent, language, _resolver_for(language, ws_root)
            )
            if keys and target_key not in keys:
                return False  # repointed to a different module -> not broken
            return True

        def _name_present(importer_rel: str, name: str, line: int | None, ws_root: Path) -> bool:
            # Cheap presence check: the index is a bootstrap snapshot, so confirm
            # the importer still names the binding (the rename may have reached it
            # too) before claiming its call site is broken. No parse -- a
            # word-boundary scan over the importer bytes. A Ruby constant has no
            # import binding to anchor a code-only check, so a bareword surviving
            # only as INERT TEXT (a `# comment`, a plain "..."/'...' string, a
            # %w/%i/%q/%Q/%() literal, or a heredoc body) after a completed rename
            # is NOT a live reference and must not fire the advisory. `_blank_ruby_
            # noncode` neutralizes all of those while KEEPING `#{Const}`
            # interpolation (a real reference). Keep-biased: an unparseable form
            # stays counted (a harmless over-fire) rather than dropped. The
            # importer path is relative to the file's own workspace root.
            ip = ws_root / importer_rel
            try:
                text = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                return False
            needle = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")

            blanked = _blank_ruby_noncode(text)
            text_lines = blanked.splitlines()
            if line is not None and 1 <= line <= len(text_lines):
                if needle.search(text_lines[line - 1]):
                    return True
            return bool(needle.search(blanked))

        # Ruby move-in-one-turn suppression: the class/module names each edited
        # Ruby file currently defines, computed once and memoized. A class moved to
        # another file edited the SAME turn is not a broken reference -- Ruby
        # constants resolve globally (Zeitwerk/autoload/require), so a still-loaded
        # redefinition elsewhere keeps every referencer valid. This is the Ruby
        # analog of the TS/Python repoint suppression; it covers the dominant
        # same-turn refactor (a cross-turn move would need a full-repo scan, too
        # costly at Stop, and falls back to the bareword behavior).
        _ruby_defs_cache: dict | None = None

        def _ruby_defs_by_file() -> dict:
            nonlocal _ruby_defs_cache
            if _ruby_defs_cache is None:
                _ruby_defs_cache = {}
                for sp in state.files:
                    spp = Path(sp)
                    if not spp.is_file() or detect_language(str(spp)) != "ruby":
                        continue
                    try:
                        txt = spp.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    try:
                        srel = spp.resolve().relative_to(Path(repo_root).resolve()).as_posix()
                    except (ValueError, OSError):
                        continue
                    _ruby_defs_cache[srel] = set(
                        re.findall(r"(?m)^[ \t]*(?:class|module)\s+([A-Z]\w*)", txt)
                    )
            return _ruby_defs_cache

        def _const_redefined_elsewhere(const: str, current_rrel: str) -> bool:
            return any(
                const in names for rel, names in _ruby_defs_by_file().items() if rel != current_rrel
            )

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        def _safe_ref_field(s: str) -> str:
            # A symbol/module/site field rendered on a SINGLE advisory line. Strip
            # line-splitting and other control bytes FIRST (a crafted filename with
            # a newline would otherwise break the one-line format or forge a marker
            # on its own line), THEN run the standard context sanitizer (neutralize
            # forged [chameleon-untrusted-data:]/[chameleon:] markers and residual
            # escapes). sanitize_for_chameleon_context alone preserves newlines (it
            # is built for multi-line prose), so a path-typed field needs both.
            return sanitize_for_chameleon_context(re.sub(r"[\x00-\x1f\x7f]", "", s))

        # (symbol, module, sites, kind) where kind is "export" (TS/Python named
        # export) or "constant" (Ruby class/module), in touch order so the
        # advisory lists the breaks this turn introduced.
        breaks: list[tuple[str, str, list[str], str]] = []
        seen_files = 0
        for path in state.files:
            if seen_files >= max_files:
                break
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            lang = detect_language(str(p))
            # This file's own workspace root (nearest .chameleon), so the indexes,
            # module keys, and importer paths below are workspace-correct in a
            # monorepo instead of all keyed on the cwd's git-top-level root.
            ws_root = _ws_root_for(p)
            if lang == "ruby":
                # Ruby uses the constant graph, not a named-export reverse index:
                # a class/module the index records as defined here that the file
                # no longer defines, while referencers still name it, is the Ruby
                # existence break. High-confidence only: one defining file, bare
                # top-level name.
                from chameleon_mcp.constant_index import referencing_files

                cidx = _ws_cidx_for(ws_root)
                if cidx is None:
                    continue
                ign = ignored_rules(content, file_path=path) or set()
                if "" in ign or "removed-export-breaks-importers" in ign:
                    continue
                try:
                    rrel = p.resolve().relative_to(Path(ws_root).resolve()).as_posix()
                except (ValueError, OSError):
                    continue
                seen_files += 1
                for const, entry in sorted((cidx.get("constants") or {}).items()):
                    dl = entry.get("defined_in") or []
                    if "::" in const or dl != [rrel] or not entry.get("referenced_by"):
                        continue
                    if re.search(r"(?m)^(?:class|module)\s+" + re.escape(const) + r"\b", content):
                        continue
                    # The class was removed from THIS file but redefined in another
                    # file edited the same turn -> a move, not a break. Ruby resolves
                    # the constant globally, so every referencer is still valid.
                    if _const_redefined_elsewhere(const, rrel):
                        continue
                    rlive = [
                        r
                        for r in referencing_files(cidx, const)
                        if _name_present(r, const, None, ws_root)
                    ]
                    if not rlive:
                        continue
                    rsites = [_safe_ref_field(r) for r in sorted(rlive)[:max_sites]]
                    breaks.append(
                        (_safe_ref_field(const), _safe_ref_field(rrel), rsites, "constant")
                    )
                    if out_breaks is not None:
                        # Raw (unsanitized) fields the Stop block branch needs to
                        # re-verify with its stricter predicate + HEAD check.
                        out_breaks.append(
                            {
                                "name": const,
                                "target_key": rrel,
                                "kind": "constant",
                                "lang": "ruby",
                                "ws_root": ws_root,
                                "importers": [(r, None) for r in sorted(rlive)],
                            }
                        )
                continue
            # The reverse index spans the TS and Python module graphs, so both
            # languages are checked here; anything else is skipped. A Ruby-only
            # workspace has no reverse index, so a stray TS/Python file is skipped.
            ws_index = _ws_index_for(ws_root)
            if lang not in ("typescript", "python") or ws_index is None:
                continue
            ign = ignored_rules(content, file_path=path) or set()
            if "" in ign or "removed-export-breaks-importers" in ign:
                continue
            target_key = module_key_for_path(p, ws_root)
            if target_key is None:
                continue
            # Read each module's live export set with its own language reader: the
            # TS regex finds zero exports in a Python module, which would misreport
            # every Python importer as a broken reference. Pass the path so the
            # Python reader can add an __init__.py package's sibling re-exports.
            if lang == "python":
                current, open_set = _python_current_export_names(content, p)
            else:
                current, open_set = _current_export_names(content)
            if open_set:
                # `export * from` re-exports an unknown set, so a name absent from
                # the visible set may still be exported -- skip, matching the
                # edit-time and tool stance.
                continue
            seen_files += 1
            broken = ws_index.broken_importers(target_key, current)
            if not broken:
                continue
            for name in sorted(broken):
                importers = broken[name]
                live = [
                    imp
                    for imp in importers
                    if _live_break(imp.path, name, imp.line, target_key, lang, ws_root)
                ]
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
                )
                sites = [
                    _safe_ref_field(f"{imp.path}:{imp.line}" if imp.line is not None else imp.path)
                    for imp in live_sorted[:max_sites]
                ]
                breaks.append((_safe_ref_field(name), _safe_ref_field(target_key), sites, "export"))
                if out_breaks is not None:
                    out_breaks.append(
                        {
                            "name": name,
                            "target_key": target_key,
                            "kind": "export",
                            "lang": lang,
                            "ws_root": ws_root,
                            "importers": [(imp.path, imp.line) for imp in live_sorted],
                        }
                    )

        # Deleted TS/Python modules: a file the turn edited then removed exports
        # nothing now, so every importer the reverse index still attributes to it
        # is a genuine break -- the strongest existence break there is, and the one
        # shape the loop above never sees (the file is gone from state.files). A
        # deleted module has a CLOSED empty export set, so broken_importers(key,
        # {}) returns every still-referencing importer, and the per-site _live_break
        # re-check keeps only importers that still name it and still resolve here (a
        # same-turn move that redefined the module elsewhere and repointed the
        # importer drops out). No inline-ignore is possible (the source is gone), so
        # this is advisory-only, like the tool-level path (_module_file_missing).
        for path in deleted_paths or []:
            if seen_files >= max_files:
                break
            if detect_language(str(Path(path))) not in ("typescript", "python"):
                continue
            # A deleted file still resolves its workspace: find_repo_root walks up
            # from the (still-present) parent dir to the nearest .chameleon.
            del_ws_root = _ws_root_for(Path(path))
            del_index = _ws_index_for(del_ws_root)
            if del_index is None:
                continue
            target_key = module_key_for_path(Path(path), del_ws_root)
            if target_key is None:
                continue
            # Only a genuinely-gone file counts; a path that merely failed a
            # read for another reason must not fabricate a deleted-module break.
            try:
                if (del_ws_root / target_key).exists():
                    continue
            except OSError:
                continue
            seen_files += 1
            broken = del_index.broken_importers(target_key, frozenset())
            if not broken:
                continue
            for name in sorted(broken):
                importers = broken[name]
                lang = detect_language(str(Path(path)))
                live = [
                    imp
                    for imp in importers
                    if _live_break(imp.path, name, imp.line, target_key, lang, del_ws_root)
                ]
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
                )
                sites = [
                    _safe_ref_field(f"{imp.path}:{imp.line}" if imp.line is not None else imp.path)
                    for imp in live_sorted[:max_sites]
                ]
                # target_key (the deleted module PATH) is the ONLY break field
                # rendered from the module itself -- and it is attacker-influenced
                # (a crafted deleted filename). Sanitize it like name/sites so a
                # forged marker / ANSI / newline in the path cannot reach the
                # advisory verb below.
                breaks.append(
                    (_safe_ref_field(name), _safe_ref_field(target_key), sites, "deleted")
                )
                if out_breaks is not None:
                    out_breaks.append(
                        {
                            "name": name,
                            "target_key": target_key,
                            "kind": "deleted",
                            "lang": lang,
                            "ws_root": del_ws_root,
                            "importers": [(imp.path, imp.line) for imp in live_sorted],
                        }
                    )

        # Barrel (star re-export) breaks: an edited TS ORIGIN file whose exports
        # flow to importers through a sibling `index.*` barrel (`export * from
        # './origin'`). A name removed from the origin leaves the barrel's
        # importers broken, but the origin's OWN module key has no reverse-index
        # entry (importers import from the barrel, not the origin), so the loop
        # above never sees it. For each edited origin, find sibling barrels that
        # re-export it, recompute the barrel's effective export set (expanding its
        # stars ONE level), and report the barrel's broken importers. Fail-safe:
        # `_barrel_effective_exports` returns resolvable=False on any unreadable /
        # nested-star source, and we then skip -- an uncertain barrel never fires,
        # so this can only ADD a genuine finding, never a false positive from a
        # name still provided by another star.
        try:
            barrel_seen: set = set()
            already = {(b[0], b[1]) for b in breaks}  # (name, module) dedup vs direct path
            for path in list(state.files):
                if seen_files >= max_files:
                    break
                p = Path(path)
                if not p.is_file() or detect_language(str(p)) != "typescript":
                    continue
                ws_root = _ws_root_for(p)
                ws_index = _ws_index_for(ws_root)
                if ws_index is None:
                    continue
                stem = p.stem
                for bx in ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs"):
                    barrel = p.parent / bx
                    if barrel == p or not barrel.is_file():
                        continue
                    bkey = module_key_for_path(barrel, ws_root)
                    if bkey is None or bkey in barrel_seen:
                        continue
                    try:
                        bcontent = barrel.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    if not _barrel_reexports_stem(bcontent, stem):
                        continue
                    ign = ignored_rules(bcontent, file_path=str(barrel)) or set()
                    if "" in ign or "removed-export-breaks-importers" in ign:
                        continue
                    eff, resolvable = _barrel_effective_exports(
                        barrel, ws_root, _resolver_for("typescript", ws_root)
                    )
                    if not resolvable:
                        continue
                    broken = ws_index.broken_importers(bkey, eff)
                    if not broken:
                        continue
                    barrel_seen.add(bkey)
                    seen_files += 1
                    for name in sorted(broken):
                        if (name, bkey) in already:
                            continue
                        live = [
                            imp
                            for imp in broken[name]
                            if _live_break(imp.path, name, imp.line, bkey, "typescript", ws_root)
                        ]
                        if not live:
                            continue
                        live_sorted = sorted(
                            live,
                            key=lambda imp: (imp.path, imp.line if imp.line is not None else -1),
                        )
                        bsites = [
                            _safe_ref_field(
                                f"{imp.path}:{imp.line}" if imp.line is not None else imp.path
                            )
                            for imp in live_sorted[:max_sites]
                        ]
                        breaks.append(
                            (_safe_ref_field(name), _safe_ref_field(bkey), bsites, "barrel")
                        )
                        if out_breaks is not None:
                            out_breaks.append(
                                {
                                    "name": name,
                                    "target_key": bkey,
                                    "kind": "barrel",
                                    "lang": "typescript",
                                    "ws_root": ws_root,
                                    "importers": [(imp.path, imp.line) for imp in live_sorted],
                                }
                            )
        except Exception:
            pass

        if not breaks:
            return []

        plural = len(breaks) != 1
        lines = [
            f"[🦎 chameleon: {len(breaks)} definition{'s' if plural else ''} you "
            f"removed still ha{'ve' if plural else 's'} live call sites]",
            "These names are gone from the files you edited but other files still "
            "reference them; their call sites are now broken. Restore the "
            "definition or update the references. This is advisory, not a block.",
        ]
        for name, module, sites, kind in breaks:
            shown = ", ".join(sites)
            more = " ..." if len(sites) >= max_sites else ""
            if kind == "constant":
                verb = "defined; still referenced by"
            elif kind == "deleted":
                verb = f"exported ({module} was deleted); still imported by"
            elif kind == "barrel":
                verb = f"re-exported (via {module}); still imported by"
            else:
                verb = "exported; still imported by"
            lines.append(f"- '{name}' no longer {verb} {shown}{more}")
        # Infer the ignore-comment token from the turn's touched files. Include
        # deleted_paths: on a pure-deletion turn the deleted .py/.rb was pruned
        # from state.files, leaving an EMPTY set that falls through to the `//`
        # default -- a syntax error a Python/Ruby author would paste. The deleted
        # path's extension still tells us the language for the surviving-importer
        # source the author edits.
        hint_paths = [path for path in state.files] + list(deleted_paths or [])
        lines.append(
            "To silence this for a file, add "
            + hh._ignore_hint(hint_paths, "removed-export-breaks-importers")
            + " in the source you touched."
        )
        return lines
    except Exception:
        return []


def _owning_package(mono_key: str, packages: dict) -> str | None:
    """The package NAME whose monorepo-relative dir owns ``mono_key`` (longest
    prefix wins), or None. ``packages`` is the cross index's name -> mono-dir map."""
    best: str | None = None
    best_len = -1
    for pname, pdir in (packages or {}).items():
        if not isinstance(pname, str) or not isinstance(pdir, str):
            continue
        d = pdir.rstrip("/")
        if (mono_key == d or mono_key.startswith(d + "/")) and len(d) > best_len:
            best, best_len = pname, len(d)
    return best


def _importer_cleanly_repointed(itext: str, name: str, owning_pkg: str, packages: dict) -> bool:
    """True (=> SUPPRESS the cross-workspace break) ONLY when the importer now
    imports ``name`` from a DIFFERENT KNOWN workspace package and no longer from
    ``owning_pkg`` (the package the removed export lived in).

    The cross-package analog of :func:`_live_break`'s repoint suppression, but
    fail-SAFE toward keeping the advisory: suppression requires a POSITIVE match
    to another package in the cross index's ``packages`` map. Anything ambiguous
    -- a relative or tsconfig-alias specifier that may still resolve INTO
    ``owning_pkg``, an external/unmapped bare package, a re-export, an import
    still from ``owning_pkg``, or an unparseable form -- returns False so the
    advisory is KEPT. A name-prefix-only check would wrongly suppress a still-
    broken relative import like ``../a/src/foo`` (it doesn't start with the
    package name yet targets the package), i.e. MISS a genuine break; that is the
    one direction this must never take. TypeScript/JS only (the v1 consumer scope).
    """
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.phantom_imports import _TS_IMPORT_SPEC_RE, _named_specifiers

        known_others = {p for p in (packages or {}) if isinstance(p, str) and p and p != owning_pkg}
        scan = hh._blank_strings_comments(itext, "typescript", keep_strings=True)
        from_owning = False
        from_other_known = False
        for m in _TS_IMPORT_SPEC_RE.finditer(scan):
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            spec = raw.split("?", 1)[0].split("#", 1)[0]
            if not spec:
                continue
            names = _named_specifiers(m.group(0))
            if not names or name not in names:
                continue
            if spec == owning_pkg or spec.startswith(owning_pkg + "/"):
                from_owning = True
            elif any(spec == k or spec.startswith(k + "/") for k in known_others):
                from_other_known = True
            # A relative / alias / external-bare spec is left AMBIGUOUS on purpose
            # (it may still target owning_pkg), so it never drives suppression.
        return from_other_known and not from_owning
    except Exception:
        return False


def _crossworkspace_existence_advisory_lines(*, repo_root: Path, state, cfg) -> list[str]:
    """Turn-end advisory for cross-WORKSPACE existence breaks (WP-C5): a monorepo
    workspace file that removed a named export a SIBLING workspace still imports.

    The counterpart to :func:`_crossfile_existence_advisory_lines` (which is
    scoped to one workspace's own reverse index): this consults the coordinator
    cross index in the plugin data dir, resolved per edited file via its workspace
    profile's parent repo_id. Advisory ONLY, never a block; a repoint or a stale
    row is tolerable noise. Each break is confirmed by a live presence re-check on
    the importer's CURRENT content (importer paths are monorepo-root-relative, so
    they join against the coordinator root, not a workspace root). Fail-open to [];
    gated by CHAMELEON_CROSSWS_INDEX and cfg.mode != off.
    """
    try:
        if cfg.mode == "off" or os.environ.get("CHAMELEON_CROSSWS_INDEX") == "0":
            return []
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.lint_engine import detect_language
        from chameleon_mcp.phantom_imports import _current_export_names
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        def _s(v: str) -> str:
            return sanitize_for_chameleon_context(re.sub(r"[\x00-\x1f\x7f]", "", v))

        max_files = threshold_int("CROSSFILE_STOP_ADVISORY_MAX_FILES")
        max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")
        coord_cache: dict[str, tuple] = {}
        # (symbol, edited-module mono-key, [importer sites])
        # (symbol, edited-module mono-key, [importer sites], truncated?)
        breaks: list[tuple[str, str, list[str], bool]] = []
        seen = 0
        for path in state.files:
            if seen >= max_files:
                break
            p = Path(path)
            if not p.is_file():
                continue
            lang = detect_language(str(p))
            # v1 resolves cross-package specifiers for TypeScript/JS only (the
            # coordinator JOIN probes JS extensions + package.json main); Python
            # cross-package resolution is a documented follow-up, so no Python
            # cross-workspace edge is ever produced and checking a .py edit here
            # would only ever no-op. Scope to TS so the code matches the pipeline.
            if lang != "typescript":
                continue
            ws_root = find_repo_root(p)
            if ws_root is None:
                continue
            key = str(ws_root)
            if key not in coord_cache:
                coord_cache[key] = hh._resolve_coordinator_cross_index(ws_root)
            mono_root, res = coord_cache[key]
            if mono_root is None or res is None:
                continue
            ri, _packages = res
            try:
                mono_key = p.resolve().relative_to(mono_root).as_posix()
            except (ValueError, OSError):
                continue
            try:
                content = p.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            current, open_set = _current_export_names(content)
            if open_set:
                continue
            seen += 1
            broken = ri.broken_importers(mono_key, current)
            if not broken:
                continue
            # The package that owns the edited/removed-from file, so a same-turn
            # repoint away from it can be suppressed (parity with _live_break).
            owning_pkg = _owning_package(mono_key, _packages)
            for name in sorted(broken):
                live = []
                for imp in broken[name]:
                    ip = mono_root / imp.path
                    try:
                        itext = ip.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    if not hh._reference_present(itext, name, imp.line, detect_language(str(ip))):
                        continue
                    # Repoint suppression: if the importer cleanly repointed the
                    # name to a DIFFERENT known workspace package, this
                    # cross-workspace break is stale (the removal no longer affects
                    # it). Ambiguous specifiers keep the advisory (never miss).
                    if owning_pkg is not None and _importer_cleanly_repointed(
                        itext, name, owning_pkg, _packages
                    ):
                        continue
                    live.append(imp)
                if not live:
                    continue
                live_sorted = sorted(
                    live, key=lambda i: (i.path, i.line if i.line is not None else -1)
                )
                sites = [
                    _s(f"{i.path}:{i.line}" if i.line is not None else i.path)
                    for i in live_sorted[:max_sites]
                ]
                breaks.append((_s(name), _s(mono_key), sites, len(live_sorted) > max_sites))

        if not breaks:
            return []
        plural = len(breaks) != 1
        lines = [
            f"[🦎 chameleon: {len(breaks)} export{'s' if plural else ''} you removed "
            f"{'are' if plural else 'is'} still imported by ANOTHER workspace]",
            "These names are gone from the workspace file you edited but a sibling "
            "workspace still imports them across the package boundary; their call "
            "sites are now broken. Restore the export or update the importers. "
            "This is advisory, not a block.",
        ]
        for name, module, sites, truncated in breaks:
            shown = ", ".join(sites)
            more = " ..." if truncated else ""
            lines.append(
                f"- '{name}' no longer exported by {module}; still imported by {shown}{more}"
            )
        return lines
    except Exception:
        return []


def _test_integrity_advisory_lines(
    *,
    repo_root: Path,
    repo_id: str | None = None,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
) -> list[str]:
    """Turn-end test-integrity advisory, or [].

    When the turn changed live source AND weakened tests (added skip markers,
    dropped assertions, net test deletion -- the deterministic signals the
    auto-pass router already computes), name what was weakened so the author
    restores coverage before the PR. Deterministic, zero model spawn. Deduped per
    (session, diff-digest) so an unchanged weakening does not re-nag every later
    turn that touches the file. Advisory only; fails open to [].
    """
    try:
        from chameleon_mcp import hook_helper as hh

        if not getattr(cfg, "test_integrity_review", True):
            hh._emit_check_event(
                repo_id, session_id, "test_integrity_review", "skipped", "feature_disabled"
            )
            return []
        if cfg.mode == "off":
            hh._emit_check_event(
                repo_id, session_id, "test_integrity_review", "skipped", "mode_off"
            )
            return []

        from chameleon_mcp import test_integrity as ti

        edited = list(state.files)
        if not edited:
            return []
        diff_text = ti.build_turn_diff(repo_root, edited)
        assessment = ti.assess_test_weakening(diff_text, edited)
        if not assessment:
            return []

        # Dedup per (session, diff-digest): the same weakening left in place must
        # not re-fire on every later turn. Reuses the duplication gate's marker
        # helpers under a distinct namespace so the two judged-sets never collide.
        digest = hashlib.sha256((diff_text or "").encode()).hexdigest()[:16]
        from chameleon_mcp import duplication_review as dr

        if dr.already_judged(
            repo_data, session_id or "", "testint", digest, prefix=".testint_judged."
        ):
            hh._emit_check_event(
                repo_id, session_id, "test_integrity_review", "skipped", "digest_already_emitted"
            )
            return []
        dr.mark_judged(repo_data, session_id or "", "testint", digest, prefix=".testint_judged.")
        hh._emit_check_event(repo_id, session_id, "test_integrity_review", "ran")
        return ti.format_test_integrity_advisory(assessment)
    except Exception:
        return []


def _scope_drift_advisory_lines(
    *,
    repo_root: Path,
    repo_data: Path,
    session_id: str | None,
    state,
    cfg,
) -> list[str]:
    """Turn-end advisory naming changed files that look unrequested, or [].

    The turn's captured request -- the LATEST prompt, not the whole session --
    named specific identifiers (symbol / file / module names). A changed file
    whose path shares no word with any of them is a candidate unrequested
    change. Scoping to the latest prompt is what keeps a bare "commit this"
    turn silent: whole-session aggregation let a stale first prompt's
    identifiers govern every later turn, flagging the same files on every
    Stop. Stays silent unless that request named enough identifiers AND at
    least one changed file matched. Advisory only, never a block.
    Privacy-preserving: reads only the stored identifier tokens, never prompt
    prose. Fails open to [].
    """
    try:
        if cfg.mode == "off" or not getattr(cfg, "intent_scope_advisory", True):
            return []
        from chameleon_mcp import intent_capture

        entries = intent_capture.read_intent(repo_data, session_id)
        idents = intent_capture.latest_request_identifiers(entries)
        if not idents:
            return []
        root = repo_root.resolve()
        rel_paths: list[str] = []
        for path in state.files:
            if not isinstance(path, str):
                continue
            try:
                rel_paths.append(str(Path(path).resolve().relative_to(root)))
            except (ValueError, OSError):
                rel_paths.append(Path(path).name)
        drifted = intent_capture.scope_drift_files(idents, rel_paths)
        if not drifted:
            return []
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        listed = ", ".join(sanitize_for_chameleon_context(p) for p in drifted)
        return [
            f"[🦎 chameleon: possible scope drift — {len(drifted)} changed file(s) "
            f"share nothing with what the request named ({listed}). Confirm they are "
            "intended, not unrequested changes.]"
        ]
    except Exception:  # noqa: BLE001
        return []


def _test_run_reminder_lines(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
) -> list[str]:
    """Turn-end nudge: real source edited, no passing test run seen this session.

    Extracted from the legacy idiom-review gate, where it rode the SAME
    once-per-session block the idiom/principle CONTENT did (the block is
    gone: the idiom review is now a scoped detector lens, spec section 5.2).
    Standalone, this builder follows every sibling in this module instead of
    the deleted gate's guaranteed-once shape: it fires independently of
    ``enforcement.idiom_review`` and re-checks THIS turn's edits on every
    qualifying Stop, so it keeps nudging until a passing run is observed
    rather than nagging exactly once per session. Advisory only; fails open
    to [] on any error.
    """
    try:
        if cfg.mode == "off":
            return []
        if not session_id:
            return []

        from chameleon_mcp.violation_class import ignored_rules

        # An edited file that still exists and is not opted out via an inline
        # bare `chameleon-ignore` directive in the touched file.
        edited: list[str] = []
        for path in state.files:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            if "" in (ignored_rules(content, file_path=path) or set()):
                continue
            edited.append(path)
        if not edited:
            return []

        from chameleon_mcp.lint_engine import detect_language

        def _governed_language(path: str) -> str | None:
            # A notebook cell is Python source (detect_language('.ipynb') is
            # None), same special case the idiom lens uses.
            if path.lower().endswith(".ipynb"):
                return "python"
            return detect_language(path)

        governed = [p for p in edited if _governed_language(p) is not None]
        if not governed:
            return []

        from chameleon_mcp import hook_helper as hh

        edited_source = False
        for path in governed:
            try:
                rel = os.path.relpath(path, str(repo_root))
            except ValueError:
                rel = Path(path).name
            if hh._is_source_for_test_signal(rel, language=_governed_language(path)):
                edited_source = True
                break
        if not edited_source:
            return []

        from chameleon_mcp.exec_log import session_test_run_seen

        if session_test_run_seen(repo_id, session_id):
            return []

        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        names = ", ".join(sanitize_for_chameleon_context(Path(p).name) for p in governed[:5])

        try:
            from chameleon_mcp.metrics import emit_hook_metric

            # Own metric name (unchanged from the legacy gate) so the nudge's
            # real frequency stays independently measurable, decoupled from
            # whatever the idiom/correctness surfaces report.
            emit_hook_metric(
                "stop-test-run-signal",
                elapsed_ms=0,
                repo_id=repo_id,
                advisory_emitted=True,
                would_block=False,
            )
        except Exception:
            pass

        return [
            "[🦎 chameleon: no passing test run this turn]",
            f"You edited {names} with no recorded passing test run. Run the suite "
            "to confirm your changes pass before ending (skip only if a watch "
            "process or CI is already running them). Advisory; the turn ends "
            "normally.",
        ]
    except Exception:  # noqa: BLE001
        return []
