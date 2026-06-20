"""Deterministic caller-contract signature diff (SP4).

The turn-end correctness judge is TOLD to treat the committed-caller facts as a
signature/return/throws contract check, but it derives the delta itself from the
diff. The non-LLM auto-pass router has no per-symbol contract signal at all: it
routes on file-level blast radius, so a signature that NARROWS in a low-importer
file slides under the gate with no deterministic check. This module supplies that
missing deterministic signal.

For a changed file it compares each callable's OLD parameter contract (parsed
from the file's version at a git ref) against its NEW contract (the other ref or
the working tree) and flags only a NARROWING of the POSITIONAL contract -- a new
required positional argument, or a positional argument that flipped
optional->required. That is exactly what breaks an existing positional caller.

Deliberately narrow, to keep the false-positive rate near zero:
- POSITIONAL only. A new required KEYWORD argument (Ruby ``kwarg:``) does not
  break a positional call site, so keyword/keyword_rest params are never counted.
  TS object-destructured params occupy one positional slot and ARE counted; a
  rest/splat param absorbs extra args and is never required.
- NARROWING only. A removed required positional (the widening direction) and a
  param reorder are left to the LLM judge; only an INCREASE in required
  positional count is flagged.
- A name present on only ONE side yields no diff (new code breaks no committed
  caller; a deletion is a different signal).
- Return type and thrown errors are out of scope: neither dump emits them.

Pure comparison here; the git materialization, re-parse, and caller join live in
the tool/Stop-time consumer, which is fail-open and never on a hook hot path.

Surfaced at the auto-pass router (``get_autopass_verdict``: the one non-LLM gate,
which otherwise has no per-symbol contract signal) and in pr-review
(``get_contract_breaks``, a deterministic FIX). A separate turn-end Stop line is
deliberately NOT added: the correctness judge already performs a contract check
on the SAME committed-caller facts via its prompt directive, so a non-PR edit is
covered by that LLM pass; this module fills the deterministic gap where no LLM
runs, which is the auto-pass router.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

# Param kinds that occupy a POSITIONAL call slot. TS: "positional" and
# "destructured" (an object/array-bound positional arg). Ruby: "positional"
# (required) and "optional" (a defaulted positional). A "rest"/"keyword"/
# "keyword_rest" kind is never a required positional, so it is excluded -- this
# is the guard that keeps a Ruby required-keyword addition from false-positiving.
_POSITIONAL_KINDS = frozenset({"positional", "destructured", "optional"})


@dataclass(frozen=True)
class ContractBreak:
    """One callable whose positional contract narrowed between two versions."""

    name: str
    old_required_positional: int
    new_required_positional: int


def _required_positional_count(params) -> int:
    """Number of REQUIRED positional parameters in a dump param-shape list.

    A param counts when its kind is a positional slot AND it is not optional.
    Non-list / malformed input counts as zero (fail-safe: no spurious break).
    """
    if not isinstance(params, list):
        return 0
    n = 0
    for p in params:
        if not isinstance(p, dict):
            continue
        if p.get("kind") in _POSITIONAL_KINDS and not bool(p.get("optional")):
            n += 1
    return n


def diff_file_contracts(
    old_callables: dict[str, list], new_callables: dict[str, list]
) -> list[ContractBreak]:
    """Contract breaks for callables present in BOTH versions of one file.

    ``old_callables`` / ``new_callables`` map an (unambiguous) callable name to
    its raw dump param-shape list. A break is emitted only when the required
    positional count INCREASED -- the narrowing that breaks an existing
    positional caller. Names on only one side are skipped.
    """
    out: list[ContractBreak] = []
    for name, new_params in (new_callables or {}).items():
        if name not in (old_callables or {}):
            continue
        old_req = _required_positional_count(old_callables[name])
        new_req = _required_positional_count(new_params)
        if new_req > old_req:
            out.append(
                ContractBreak(
                    name=name,
                    old_required_positional=old_req,
                    new_required_positional=new_req,
                )
            )
    return out


@dataclass(frozen=True)
class ContractFinding:
    """A narrowed callable whose contract change breaks committed callers."""

    rel: str
    name: str
    old_required_positional: int
    new_required_positional: int
    caller_total: int
    callers: list = field(default_factory=list)


def compute_contract_breaks(
    changed_files,
    *,
    old_params_fn: Callable[[str], dict],
    new_params_fn: Callable[[str], dict],
    callers_fn: Callable[[str, str], dict | None],
) -> list[ContractFinding]:
    """Contract-break findings across changed files, joined to committed callers.

    For each changed file, parse its OLD and NEW callables (via the injected
    ``old_params_fn`` / ``new_params_fn``, each ``rel -> {name: params}``),
    diff the positional contract, and emit a finding ONLY when the narrowed
    callable has committed callers (``callers_fn`` returns a non-empty result).
    A narrowing with no committed caller breaks nothing, so it is suppressed.

    Fail-open: any per-file parse/lookup error drops that file's findings rather
    than raising, so the consumer (autopass router / pr-review / turn-end) never
    crashes on a malformed blob.
    """
    out: list[ContractFinding] = []
    for rel in changed_files or ():
        if not isinstance(rel, str):
            continue
        try:
            old = old_params_fn(rel) or {}
            new = new_params_fn(rel) or {}
            for brk in diff_file_contracts(old, new):
                callers = callers_fn(rel, brk.name)
                if not callers:
                    continue
                total = int(callers.get("total") or 0)
                if total <= 0:
                    continue
                out.append(
                    ContractFinding(
                        rel=rel,
                        name=brk.name,
                        old_required_positional=brk.old_required_positional,
                        new_required_positional=brk.new_required_positional,
                        caller_total=total,
                        callers=list(callers.get("callers") or []),
                    )
                )
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Real parse + git materialization (tool/Stop-time only, never a hook hot path)
# ---------------------------------------------------------------------------


# Extension -> extractor language. The extractor is chosen by the FILE's own
# extension (not a repo-level language detection), so a .rb file in a
# TS-dominant repo -- or a bare test repo with no language markers -- is still
# parsed by the right backend.
_TS_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


def _extractor_for_ext(suffix: str):
    """A fresh extractor instance for a file extension, or None.

    Instantiated directly (no repo-level detection): each backend just spawns
    its own parser subprocess and reads the paths it is given.
    """
    s = suffix.lower()
    try:
        if s == ".rb":
            from chameleon_mcp.extractors.ruby import RubyExtractor

            return RubyExtractor()
        if s in _TS_EXTS:
            from chameleon_mcp.extractors.typescript import TypeScriptExtractor

            return TypeScriptExtractor()
    except Exception:
        return None
    return None


def _callables_of_parsed_file(pf) -> dict[str, list]:
    """One parsed file's ``{name: raw_params}``, intra-file ambiguous names dropped.

    A name appearing more than once in the file (a TS overload set, or two
    same-named methods in different classes) is AMBIGUOUS and dropped: the caller
    index keys on the bare name, so a contract diff for a duplicated name could
    not be attributed reliably -- fail-safe (a possible missed break) over a
    misattributed false positive.
    """
    extras = getattr(pf, "extras", None) or {}
    raw = extras.get("callable_signatures")
    if not isinstance(raw, list):
        return {}
    seen: dict[str, list] = {}
    ambiguous: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        params = entry.get("params")
        if not isinstance(name, str) or not name or not isinstance(params, list):
            continue
        if name in seen:
            ambiguous.add(name)
        else:
            seen[name] = params
    return {n: p for n, p in seen.items() if n not in ambiguous}


def parse_callables(repo_root, abs_path) -> dict[str, list]:
    """Parse one source file into ``{name: raw_params}`` from the extractor dump.

    Uses the RAW ``callable_signatures`` param shapes (each ``{name, optional,
    kind}``) -- NOT the reduced (arity, required) tuple -- because the
    positional-vs-keyword discrimination needs per-param ``kind``. The extractor
    is chosen by the file's extension. Returns {} on any parse failure.
    """
    extractor = _extractor_for_ext(Path(abs_path).suffix)
    if extractor is None:
        return {}
    try:
        parse_result = extractor.parse_repo(Path(repo_root), paths=[Path(abs_path)])
    except Exception:
        return {}
    for pf in getattr(parse_result, "files", None) or ():
        return _callables_of_parsed_file(pf)
    return {}


def _batch_parse(repo_root, abs_paths) -> dict[str, dict[str, list]]:
    """Parse many files, ONE extractor invocation per language, fail-open.

    Returns ``{resolved-abs-path-str: {name: params}}``. Files are grouped by
    extension so a mixed TS+Ruby change set spawns at most two parser
    subprocesses total, not one per file.
    """
    by_lang: dict[str, list[Path]] = {}
    for ap in abs_paths:
        p = Path(ap)
        lang = (
            "rb" if p.suffix.lower() == ".rb" else ("ts" if p.suffix.lower() in _TS_EXTS else None)
        )
        if lang is None:
            continue
        by_lang.setdefault(lang, []).append(p)

    out: dict[str, dict[str, list]] = {}
    for lang, paths in by_lang.items():
        extractor = _extractor_for_ext(".rb" if lang == "rb" else ".ts")
        if extractor is None:
            continue
        try:
            parse_result = extractor.parse_repo(Path(repo_root), paths=list(paths))
        except Exception:
            continue
        for pf in getattr(parse_result, "files", None) or ():
            try:
                key = str(Path(pf.path).resolve())
            except OSError:
                key = str(pf.path)
            out[key] = _callables_of_parsed_file(pf)
    return out


def _materialize_ref(repo_root, rel: str, ref: str, run_git) -> Path | None:
    """``git show <ref>:<rel>`` to a temp file with the file's suffix, or None."""
    res = run_git(["show", f"{ref}:{rel}"], cwd=Path(repo_root))
    if res is None or getattr(res, "returncode", 1) != 0:
        return None
    content = getattr(res, "stdout", "") or ""
    suffix = Path(rel).suffix or ".txt"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            return Path(tmp.name)
    except OSError:
        return None


def callables_at_ref(repo_root, rel: str, ref: str, run_git) -> dict[str, list]:
    """Parse one file's callables from its committed version at ``ref``, or {}.

    Single-file convenience over :func:`_materialize_ref` + :func:`parse_callables`;
    the batch path in :func:`contract_breaks` is what the consumers use.
    """
    tmp_path = _materialize_ref(repo_root, rel, ref, run_git)
    if tmp_path is None:
        return {}
    try:
        return parse_callables(repo_root, tmp_path)
    finally:
        with suppress(OSError):
            tmp_path.unlink()


def _callables_by_rel_at_ref(repo_root, rels, ref, run_git) -> dict[str, dict[str, list]]:
    """``{rel: {name: params}}`` for each file's version at ``ref`` (batched)."""
    tmp_by_rel: dict[str, Path] = {}
    try:
        for rel in rels:
            tp = _materialize_ref(repo_root, rel, ref, run_git)
            if tp is not None:
                tmp_by_rel[rel] = tp
        parsed = _batch_parse(repo_root, list(tmp_by_rel.values()))
        out: dict[str, dict[str, list]] = {}
        for rel, tp in tmp_by_rel.items():
            try:
                key = str(tp.resolve())
            except OSError:
                key = str(tp)
            out[rel] = parsed.get(key, {})
        return out
    finally:
        for tp in tmp_by_rel.values():
            with suppress(OSError):
                tp.unlink()


def _callables_by_rel_worktree(repo_root, rels) -> dict[str, dict[str, list]]:
    """``{rel: {name: params}}`` for each file's WORKING-TREE version (batched)."""
    abs_by_rel = {rel: Path(repo_root) / rel for rel in rels}
    parsed = _batch_parse(repo_root, list(abs_by_rel.values()))
    out: dict[str, dict[str, list]] = {}
    for rel, ap in abs_by_rel.items():
        try:
            key = str(ap.resolve())
        except OSError:
            key = str(ap)
        out[rel] = parsed.get(key, {})
    return out


def contract_breaks(
    repo_root,
    changed_files,
    *,
    old_ref: str,
    new_ref: str | None,
    callers_fn: Callable[[str, str], dict | None],
    run_git,
) -> list[ContractFinding]:
    """Deterministic caller-contract breaks for a change, joined to callers.

    ``old_ref`` is the committed baseline (the merge-base for the auto-pass
    router, ``HEAD`` for the turn-end / pr-review uncommitted path). ``new_ref``
    is the other committed ref, or None to parse the WORKING-TREE file (the
    uncommitted path). ``callers_fn`` is ``calls_index.callers_of``. Both sides
    are parsed in at most one extractor invocation per language. Tool/Stop-time
    only; fail-open throughout.
    """
    rels = [r for r in (changed_files or ()) if isinstance(r, str)]
    try:
        old_map = _callables_by_rel_at_ref(repo_root, rels, old_ref, run_git)
        new_map = (
            _callables_by_rel_worktree(repo_root, rels)
            if new_ref is None
            else _callables_by_rel_at_ref(repo_root, rels, new_ref, run_git)
        )
    except Exception:
        return []

    return compute_contract_breaks(
        rels,
        old_params_fn=lambda rel: old_map.get(rel, {}),
        new_params_fn=lambda rel: new_map.get(rel, {}),
        callers_fn=callers_fn,
    )


def format_contract_advisory(findings: list[ContractFinding], max_sites: int = 3) -> list[str]:
    """Sanitized advisory lines for the turn-end / pr-review surface.

    Names the narrowed callable, the required-arg delta, and a bounded sample of
    affected committed call sites. Returns [] for no findings.
    """
    if not findings:
        return []
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    lines: list[str] = []
    for f in findings:
        sites = []
        for c in f.callers[:max_sites]:
            path = c.get("path") if isinstance(c, dict) else None
            line = c.get("line") if isinstance(c, dict) else None
            if isinstance(path, str):
                sites.append(f"{path}:{line}" if isinstance(line, int) else path)
        more = f.caller_total - len(sites)
        tail = f" (+{more} more)" if more > 0 else ""
        sites_text = ", ".join(sites) + tail if sites else f"{f.caller_total} caller(s)"
        lines.append(
            sanitize_for_chameleon_context(
                f"{f.name}() in {f.rel}: required positional args "
                f"{f.old_required_positional} -> {f.new_required_positional}; "
                f"{f.caller_total} committed caller(s) may now mis-call it: {sites_text}"
            )
        )
    return lines
