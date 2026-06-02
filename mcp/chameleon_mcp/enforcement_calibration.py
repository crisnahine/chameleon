"""Per-repo block-rule calibration artifact (``.chameleon/enforcement.json``).

A block rule is only allowed to block in a repo if it produces (near) zero
violations against that repo's own committed files. This module persists and
reads that decision; the measurement lives in ``calibrate_block_rules``.
Fail-open: a missing/corrupt artifact means no rule is active (advisory only).
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

ARTIFACT = "enforcement.json"

# Upper bound on sampled witnesses; protects huge repos from scanning every file.
_MAX_FILES_SAMPLED = threshold_int("CALIBRATION_MAX_FILES")
# A rule is demoted if it flags more than this fraction of sampled committed
# files. With the default cap (600) below 1/epsilon (1000), a single hit already
# exceeds the tolerance, so in practice this is a "zero false positives" gate;
# raise CALIBRATION_FP_EPSILON above 1/CALIBRATION_MAX_FILES to allow any slack.
_FP_EPSILON = threshold_float("CALIBRATION_FP_EPSILON")


def write_block_rules(profile_dir: Path, data: dict) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    payload = {"block_rules": data}
    tmp = profile_dir / (ARTIFACT + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.rename(profile_dir / ARTIFACT)


def load_block_rules(profile_dir: Path) -> dict:
    path = profile_dir / ARTIFACT
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rules = raw.get("block_rules")
    return rules if isinstance(rules, dict) else {}


def active_block_rules(profile_dir: Path) -> set[str]:
    out = set()
    for rule, meta in load_block_rules(profile_dir).items():
        if isinstance(meta, dict) and meta.get("active") is True:
            out.add(rule)
    return out


def _sample_files(loaded) -> list[tuple[str, str]]:
    """Repo-relative path + archetype for each witness (deduped, bounded)."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    canon = (getattr(loaded, "canonicals", {}) or {}).get("canonicals", {}) or {}
    for archetype, entries in canon.items():
        for entry in entries or []:
            rel = ((entry or {}).get("witness") or {}).get("path")
            if rel and rel not in seen:
                seen.add(rel)
                out.append((rel, archetype))
            if len(out) >= _MAX_FILES_SAMPLED:
                return out
    return out


def _archetype_baselines(repo_root: Path, loaded) -> dict[str, dict]:
    """Recalibrated ast_query per archetype, derived from its representative witness.

    Mirrors the runtime path in hook_helper: the stored ast_query came from the
    real AST parser, but lint() compares against regex-derived dimensions, so the
    baseline is rebuilt from the first witness's own regex snapshot. Each archetype
    gets ONE baseline; sampled files are then linted against that shared query
    rather than against their own snapshot, which would always match by
    construction and let no structural rule ever fire.
    """
    from chameleon_mcp.lint_engine import (
        detect_language,
        extract_dimensions,
        recalibrate_ast_query,
    )

    canon = (getattr(loaded, "canonicals", {}) or {}).get("canonicals", {}) or {}
    baselines: dict[str, dict] = {}
    for archetype, entries in canon.items():
        first = (entries or [{}])[0] or {}
        stored_query = (first.get("normative_shape") or {}).get("ast_query")
        witness_rel = (first.get("witness") or {}).get("path")
        if not stored_query or not witness_rel:
            continue
        w_full = repo_root / witness_rel
        try:
            w_content = w_full.read_bytes()[:100_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        w_lang = detect_language(witness_rel)
        w_snap = extract_dimensions(w_content, language=w_lang, file_path=witness_rel)
        baselines[archetype] = recalibrate_ast_query(w_snap)
    return baselines


def _violations_for_file(
    repo_root: Path, rel: str, archetype: str, loaded, baseline: dict | None
) -> list[dict]:
    from chameleon_mcp.lint_engine import (
        detect_language,
        extract_dimensions,
        lint,
        lint_conventions,
    )
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    full = repo_root / rel
    try:
        content = full.read_bytes()[:100_000].decode("utf-8", errors="replace")
    except OSError:
        return []
    language = detect_language(rel)
    violations: list[dict] = []

    if baseline:
        snap = extract_dimensions(content, language=language, file_path=rel)
        violations += [v.to_dict() for v in lint(snap, baseline)]

    conv = (getattr(loaded, "conventions", {}) or {}).get("conventions", {}) or {}
    arch_conv: dict = {}
    for key in ("imports", "naming", "inheritance"):
        if conv.get(key, {}).get(archetype):
            arch_conv[key] = conv[key][archetype]
    if arch_conv:
        violations += [
            v.to_dict()
            for v in lint_conventions(content, arch_conv, language=language)
            if v.rule != "secret-detected-in-content"
        ]

    # lint_phantom_imports resolves relative imports off the file's real location,
    # so it requires the absolute path; the dimension/convention scans only use
    # the path for language detection and are fine with the repo-relative form.
    violations += [
        v.to_dict()
        for v in lint_phantom_imports(
            content,
            file_path=str(full),
            repo_root=repo_root,
            language=language,
            rules=getattr(loaded, "rules", {}),
        )
    ]
    return violations


def calibrate_block_rules(repo_root: Path, loaded) -> dict:
    """Measure each block-eligible rule against the repo's own committed files.

    A rule that flags more than _FP_EPSILON of sampled files is marked inactive
    (advisory only) for this repo. The witness corpus is presumed correct.

    Fail-closed on no evidence: with zero sampled witnesses (empty or
    unbootstrapped profile) every block-eligible rule stays inactive rather than
    greenlighting blockers no file vouched for.
    """
    sample = _sample_files(loaded)
    n = len(sample)
    baselines = _archetype_baselines(repo_root, loaded)
    flagged: dict[str, set[str]] = {r: set() for r in BLOCK_ELIGIBLE_RULES}
    for rel, archetype in sample:
        for v in _violations_for_file(repo_root, rel, archetype, loaded, baselines.get(archetype)):
            rule = v.get("rule")
            if rule in flagged:
                # jsx only counts as block-eligible at error severity
                if rule == "jsx-presence-mismatch" and v.get("severity") != "error":
                    continue
                flagged[rule].add(rel)

    result: dict = {}
    for rule in BLOCK_ELIGIBLE_RULES:
        hits = len(flagged[rule])
        fp_rate = (hits / n) if n else 0.0
        result[rule] = {
            "active": n > 0 and fp_rate <= _FP_EPSILON,
            "fp_rate": round(fp_rate, 4),
            "sampled": n,
            "flagged": hits,
        }
    return result
