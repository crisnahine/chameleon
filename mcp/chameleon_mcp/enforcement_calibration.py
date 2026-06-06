"""Per-repo block-rule calibration artifact (``.chameleon/enforcement.json``).

A block rule is only allowed to block in a repo if it produces (near) zero
violations against that repo's own committed files. This module persists and
reads that decision; the measurement lives in ``calibrate_block_rules``.
Fail-open: a missing/corrupt artifact means no rule is active (advisory only).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES, BLOCK_RULE_LANGUAGES

ARTIFACT = "enforcement.json"

# Upper bound on the on-disk artifact we will read. enforcement.json is a tiny
# per-rule verdict (a handful of small entries) in normal operation. A committed
# profile is attacker-controlled, so a planted multi-megabyte file must not be
# slurped into memory; over the cap we fail open (no rule active = advisory only).
_MAX_ENFORCEMENT_BYTES = 256 * 1024

# Process-level cache of the parsed block_rules, keyed by resolved profile_dir and
# invalidated by the artifact's mtime+size token. The Stop backstop re-lints every
# candidate file against the same set, so without this each candidate re-read and
# re-parsed enforcement.json from disk. Mirrors the load_profile_dir cache pattern.
_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_CACHE_LOCK = threading.Lock()


def _clear_block_rules_cache() -> None:
    """Drop the in-process block_rules cache (tests; mutation paths after a write)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# Upper bound on sampled witnesses; protects huge repos from scanning every file.
_MAX_FILES_SAMPLED = threshold_int("CALIBRATION_MAX_FILES")
# Per-archetype cap on ordinary sibling files added beyond the witnesses. Read at
# call time (not import) so tests and operators can override via the env var.
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
    # Drop any cached parse so the next read reflects the new verdict immediately
    # even if the rename landed within the same mtime granularity as the prior write.
    _clear_block_rules_cache()


def _cache_token(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_block_rules(profile_dir: Path) -> dict:
    path = profile_dir / ARTIFACT
    token = _cache_token(path)
    if token is None:
        return {}
    try:
        key = str(profile_dir.resolve())
    except OSError:
        key = str(profile_dir)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == token:
            return cached[1]

    # Bound the read: a tampered, oversized artifact must not be loaded into memory.
    if token[1] > _MAX_ENFORCEMENT_BYTES:
        with _CACHE_LOCK:
            _CACHE[key] = (token, {})
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        rules: dict = {}
    else:
        candidate = raw.get("block_rules")
        rules = candidate if isinstance(candidate, dict) else {}

    with _CACHE_LOCK:
        _CACHE[key] = (token, rules)
    return rules


def active_block_rules(profile_dir: Path) -> set[str]:
    out = set()
    for rule, meta in load_block_rules(profile_dir).items():
        # Only block-eligible rules can ever block; a committed profile that marks
        # some other rule "active" (tampering or schema drift) must not promote it.
        if rule not in BLOCK_ELIGIBLE_RULES:
            continue
        if isinstance(meta, dict) and meta.get("active") is True:
            out.add(rule)
    return out


def _sample_files(repo_root: Path, loaded) -> list[tuple[str, str]]:
    """Repo-relative path + archetype for the calibration corpus (deduped, bounded).

    Includes every archetype's witnesses PLUS a bounded sample of ordinary sibling
    files: real, same-extension files in each witness's own directory, excluding
    the witnesses themselves. Witnesses are the most-canonical files and are the
    least likely to trip a rule; a rule that is clean on witnesses but flags plain
    sibling files would otherwise pass calibration and wrongly block, so siblings
    are sampled to close that hole in the zero-false-positive gate.
    """
    from chameleon_mcp.lint_engine import detect_language

    max_siblings = threshold_int("CALIBRATION_MAX_SIBLINGS")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    canon = (getattr(loaded, "canonicals", {}) or {}).get("canonicals", {}) or {}

    # First pass: every witness, tagged with its archetype. Deduped so a file that
    # is a witness of two archetypes counts once (and is never re-added as a sibling).
    witness_dirs: list[tuple[str, str]] = []  # (witness rel path, archetype)
    for archetype, entries in canon.items():
        for entry in entries or []:
            rel = ((entry or {}).get("witness") or {}).get("path")
            if not rel:
                continue
            # Profile artifacts use forward slashes; fold any backslashes from a
            # Windows-authored or cross-platform-shared profile so the dedup set
            # and the sibling paths below share one separator convention.
            rel = rel.replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            out.append((rel, archetype))
            witness_dirs.append((rel, archetype))
            if len(out) >= _MAX_FILES_SAMPLED:
                return out

    # Second pass: bounded siblings per archetype. A sibling is a real, readable,
    # same-extension file in the witness's directory that is not itself a witness.
    # iterdir() is run once per directory and shared across witnesses that live in
    # the same directory, so a dense witness dir is scanned a single time.
    dir_names_cache: dict[str, list[str] | None] = {}
    for witness_rel, archetype in witness_dirs:
        if len(out) >= _MAX_FILES_SAMPLED:
            break
        # Witness paths may carry backslashes when the profile was authored on
        # Windows or shared cross-platform; fold them so the parent/extension
        # parse and the forward-slash dedup set agree.
        wpath = Path(witness_rel.replace("\\", "/"))
        ext = wpath.suffix
        if not ext:
            continue
        wdir_rel = wpath.parent.as_posix()
        if wdir_rel in dir_names_cache:
            names = dir_names_cache[wdir_rel]
        else:
            wdir_full = repo_root / wpath.parent
            try:
                names = sorted(p.name for p in wdir_full.iterdir() if p.is_file())
            except OSError:
                names = None
            dir_names_cache[wdir_rel] = names
        if names is None:
            continue
        taken = 0
        for name in names:
            if taken >= max_siblings or len(out) >= _MAX_FILES_SAMPLED:
                break
            if not name.endswith(ext):
                continue
            sib_rel = (wpath.parent / name).as_posix()
            if sib_rel in seen:
                continue
            if detect_language(sib_rel) is None:
                continue
            seen.add(sib_rel)
            out.append((sib_rel, archetype))
            taken += 1
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
        # Pass the file's repo-relative path so the file-naming check (gated on a
        # file_path) runs during calibration too. Without it the check is silent
        # here while the runtime lint path supplies the path and runs it, so a
        # file-naming rule that flags the repo's own committed files would
        # measure a 0.0 false-positive rate and ship active -- then hard-block
        # those very files at runtime.
        violations += [
            v.to_dict()
            for v in lint_conventions(content, arch_conv, language=language, file_path=rel)
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


def _profile_languages(loaded) -> set[str]:
    """The language(s) the profile actually analyzed, for rule capability gating."""
    langs: set[str] = set()
    profile = getattr(loaded, "profile", {}) or {}
    lang = profile.get("language")
    if lang in ("typescript", "ruby"):
        langs.add(lang)
    return langs


def calibrate_block_rules(repo_root: Path, loaded) -> dict:
    """Measure each block-eligible rule against the repo's own committed files.

    A rule that flags more than _FP_EPSILON of sampled files is marked inactive
    (advisory only) for this repo. The witness corpus is presumed correct.

    Fail-closed on no evidence: with zero sampled witnesses (empty or
    unbootstrapped profile) every block-eligible rule stays inactive rather than
    greenlighting blockers no file vouched for. Same principle for language
    capability: a rule with no signal source for the profile's language can
    never fire, so its vacuous 0.0 fp_rate must not certify it active —
    silence from a rule that cannot speak is not evidence of safety. Those
    rules are marked inactive with an ``inert_reason`` so /chameleon-status
    can say why.
    """
    sample = _sample_files(repo_root, loaded)
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

    langs = _profile_languages(loaded)
    result: dict = {}
    for rule in BLOCK_ELIGIBLE_RULES:
        hits = len(flagged[rule])
        fp_rate = (hits / n) if n else 0.0
        supported = BLOCK_RULE_LANGUAGES.get(rule)
        # Gate only on POSITIVE knowledge: an unknown/legacy profile language
        # (no `language` key) keeps the measured behavior rather than demoting
        # every language-scoped rule.
        can_fire = supported is None or not langs or bool(langs & supported)
        entry: dict = {
            "active": can_fire and n > 0 and fp_rate <= _FP_EPSILON,
            "fp_rate": round(fp_rate, 4),
            "sampled": n,
            "flagged": hits,
        }
        if not can_fire:
            entry["inert_reason"] = "no-signal-for-language"
        result[rule] = entry
    return result
