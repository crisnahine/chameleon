"""Per-repo block-rule calibration artifact (``.chameleon/enforcement.json``).

A block rule is only allowed to block in a repo if it produces (near) zero
violations against that repo's own committed files. This module persists and
reads that decision; the measurement lives in ``calibrate_block_rules``.
Fail-open: a missing/corrupt artifact means no measured rule is active
(advisory only). The one exception is ``SECURITY_BLOCK_RULES``: calibration
runs no content scans, so those two rules have no measurement to fail open
FROM — they stay active regardless of the artifact (see active_block_rules).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.violation_class import (
    BLANKET_IMMUNE_RULES,
    BLOCK_ELIGIBLE_RULES,
    BLOCK_RULE_LANGUAGES,
)

ARTIFACT = "enforcement.json"

# Rules exempt from override-driven auto-demotion. Derived from the
# blanket-immune deterministic set rather than redefined: the same two rules
# (hard-kind secrets, eval/exec sinks) guard security facts, not
# team-convention preferences, and calibration never runs content scans, so
# their "active" verdict carries no false-positive measurement that override
# pressure could legitimately contradict. A high override rate on one of these
# is always recorded as a proposal for a human to act on, never applied.
SECURITY_BLOCK_RULES: frozenset[str] = BLANKET_IMMUNE_RULES

# Upper bound on the on-disk artifact we will read. enforcement.json is a tiny
# per-rule verdict (a handful of small entries) in normal operation. A committed
# profile is attacker-controlled, so a planted multi-megabyte file must not be
# slurped into memory; over the cap we fail open (no measured rule active; the
# calibration-exempt security rules stay armed).
_MAX_ENFORCEMENT_BYTES = 256 * 1024

# Process-level cache of the parsed block_rules, keyed by resolved profile_dir and
# invalidated by the artifact's mtime+size token. The Stop backstop re-lints every
# candidate file against the same set, so without this each candidate re-read and
# re-parsed enforcement.json from disk. Mirrors the load_profile_dir cache pattern.
_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_CACHE_LOCK = threading.Lock()

# Cache of the profile's recorded language, keyed like the block_rules cache and
# invalidated by profile.json's mtime+size token. active_block_rules sits on the
# PostToolUse and Stop hot paths, so the read-time language gate must not re-read
# and re-parse profile.json per call.
_LANG_CACHE: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}
_MAX_PROFILE_META_BYTES = 256 * 1024


def _clear_block_rules_cache() -> None:
    """Drop the in-process block_rules cache (tests; mutation paths after a write)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _LANG_CACHE.clear()


# Calibration thresholds (CALIBRATION_MAX_FILES, CALIBRATION_MAX_SIBLINGS,
# CALIBRATION_FP_EPSILON) are all read at call time inside the functions that
# use them, never at import, so tests and operators can override via the env
# vars without reloading the module.
#
# The file cap and epsilon move together: with the default cap (1200) below
# 1/epsilon (2000), a single flagged file already exceeds the tolerance, so in
# practice this is a "zero false positives" gate; raise CALIBRATION_FP_EPSILON
# above 1/CALIBRATION_MAX_FILES to allow any slack.


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


def _stored_profile_languages(profile_dir: Path) -> frozenset[str]:
    """Language(s) recorded in the on-disk profile, for read-time rule gating."""
    path = profile_dir / "profile.json"
    token = _cache_token(path)
    if token is None or token[1] > _MAX_PROFILE_META_BYTES:
        return frozenset()
    try:
        key = str(profile_dir.resolve())
    except OSError:
        key = str(profile_dir)

    with _CACHE_LOCK:
        cached = _LANG_CACHE.get(key)
        if cached is not None and cached[0] == token:
            return cached[1]

    langs: frozenset[str] = frozenset()
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        lang = profile.get("language") if isinstance(profile, dict) else None
        if lang in ("typescript", "ruby", "python"):
            langs = frozenset({lang})
    except (OSError, ValueError):
        langs = frozenset()

    with _CACHE_LOCK:
        _LANG_CACHE[key] = (token, langs)
    return langs


def rule_inert_for_language(rule: str, profile_dir: Path) -> bool:
    """True when the rule positively cannot fire for this profile's language.

    Calibration applies this gate when it writes enforcement.json, but a
    profile calibrated by an older engine carries its stale verdict until the
    first refresh recomputes it. Re-applying the gate at read time keeps a
    vacuously-active rule from being reported (or relied on) in the window
    between the engine upgrade and that refresh. Gates only on POSITIVE
    knowledge — an unknown/legacy language keeps the measured behavior.
    """
    supported = BLOCK_RULE_LANGUAGES.get(rule)
    if supported is None:
        return False
    langs = _stored_profile_languages(profile_dir)
    return bool(langs) and not (langs & supported)


# Mirrors the lint engine's consistency gate: a sub-convention below this
# never produces a violation, so it cannot make the rule fire either.
_NAMING_MIN_CONSISTENCY = 0.60

# Mirrors the lint engine's inheritance gate (Ruby and Python paths both
# require dominant_base and frequency >= 0.60 before flagging anything —
# see _python_inheritance_violations and its Ruby counterpart in
# lint_engine.py). Below this floor the rule has no base to compare against.
_INHERITANCE_MIN_FREQUENCY = 0.60

# Signal cache keyed by (rule, resolved profile_dir) so rules with distinct
# conventions.json sub-trees don't collide on one shared token.
_SIGNAL_CACHE: dict[tuple[str, str], tuple[tuple[int, int], bool]] = {}


def _num_or_zero(value: object) -> float:
    """A numeric threshold value, or 0.0 for a missing/null/non-numeric one.

    conventions.json is profile-derived and can be hand-edited or damaged (an
    explicit ``null`` frequency/consistency), so a raw ``>=`` comparison would
    raise TypeError and crash the read path (get_status). Coercing a non-number
    to 0.0 fails open to 'no signal', preserving the docstring's fail-open
    contract. ``bool`` is excluded so a stray ``true`` is not read as 1.0.
    """
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _naming_entry_drives_rule(entry: object) -> bool:
    """True when one archetype's naming map can produce naming-convention-violation."""
    if not isinstance(entry, dict):
        return False
    ip = entry.get("interface_prefix")
    if (
        isinstance(ip, dict)
        and ip.get("pattern")
        and _num_or_zero(ip.get("consistency")) >= _NAMING_MIN_CONSISTENCY
    ):
        return True
    for key, pattern in (
        ("method_casing", "snake_case"),
        ("class_casing", "PascalCase"),
        ("constant_casing", "SCREAMING_SNAKE_CASE"),
    ):
        sub = entry.get(key)
        if (
            isinstance(sub, dict)
            and sub.get("pattern") == pattern
            and _num_or_zero(sub.get("consistency")) >= _NAMING_MIN_CONSISTENCY
        ):
            return True
    return False


def _naming_rule_has_signal_from_conventions(conventions_doc: object) -> bool:
    """Whether any archetype carries a naming sub-convention the rule reads.

    naming-convention-violation fires only off interface_prefix (TS) or the
    casing sub-conventions (Ruby). A profile derived before those existed —
    or whose repo never converged on one — holds only ``file_naming`` (a
    different rule), leaving naming-convention-violation unable to fire.
    """
    if not isinstance(conventions_doc, dict):
        return False
    naming_by_arch = (conventions_doc.get("conventions") or {}).get("naming") or {}
    if not isinstance(naming_by_arch, dict):
        return False
    return any(_naming_entry_drives_rule(entry) for entry in naming_by_arch.values())


def _inheritance_entry_drives_rule(entry: object) -> bool:
    """True when one archetype's inheritance map can produce inheritance-convention-violation."""
    if not isinstance(entry, dict):
        return False
    return (
        bool(entry.get("dominant_base"))
        and _num_or_zero(entry.get("frequency")) >= _INHERITANCE_MIN_FREQUENCY
    )


def _inheritance_rule_has_signal_from_conventions(conventions_doc: object) -> bool:
    """Whether any archetype carries an inheritance convention the rule reads.

    inheritance-convention-violation (Ruby/Python only) fires only when an
    archetype's dominant_base clears the same 60% frequency floor the lint
    engine itself gates on. A repo whose classes never converge on one base
    per archetype — an empty inheritance map, the common case for a small or
    stylistically loose codebase — leaves the rule with nothing to compare
    against.
    """
    if not isinstance(conventions_doc, dict):
        return False
    inheritance_by_arch = (conventions_doc.get("conventions") or {}).get("inheritance") or {}
    if not isinstance(inheritance_by_arch, dict):
        return False
    return any(_inheritance_entry_drives_rule(entry) for entry in inheritance_by_arch.values())


# Rules gated by rule_inert_missing_signal, paired with the check that reads
# their driving sub-convention out of conventions.json. A rule absent from
# this map has no such gate here — its evidentiary floor (if any) lives
# elsewhere, e.g. the witness-count check in calibrate_block_rules.
_SIGNAL_CHECKS: dict[str, Callable[[object], bool]] = {
    "naming-convention-violation": _naming_rule_has_signal_from_conventions,
    "inheritance-convention-violation": _inheritance_rule_has_signal_from_conventions,
}


def rule_inert_missing_signal(rule: str, profile_dir: Path) -> bool:
    """True when the rule's driving convention data is absent from the profile.

    Same stale-verdict window as the language gate one level deeper: a profile
    whose conventions lack every sub-convention a rule reads leaves the rule
    active-but-inert until a refresh derives them, so /chameleon-status would
    advertise a guarantee that cannot fire. Gates only on POSITIVE knowledge —
    an unreadable conventions.json keeps the measured behavior.
    """
    signal_check = _SIGNAL_CHECKS.get(rule)
    if signal_check is None:
        return False
    path = profile_dir / "conventions.json"
    token = _cache_token(path)
    if token is None:
        return False
    try:
        key = str(profile_dir.resolve())
    except OSError:
        key = str(profile_dir)
    cache_key = (rule, key)

    with _CACHE_LOCK:
        cached = _SIGNAL_CACHE.get(cache_key)
        if cached is not None and cached[0] == token:
            return not cached[1]

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    has_signal = signal_check(doc)

    with _CACHE_LOCK:
        _SIGNAL_CACHE[cache_key] = (token, has_signal)
    return not has_signal


def active_block_rules(profile_dir: Path) -> set[str]:
    # The deterministic security rules (hard-kind credentials, error-severity
    # eval/exec) are active unconditionally, not by persisted verdict.
    # Calibration never measures them — the committed-corpus pass runs no
    # content scans — so their stored "active" flag carried only the witness
    # floor (n > 0), which disarmed the credential/eval deny exactly where
    # exposure is highest (fresh, small, or sparse profiles) and let a planted
    # zero-witness or torn enforcement.json switch the deny off. Trust, the
    # enforcement mode (config.json's enforcement.mode stays the deliberate,
    # status-surfaced operator control — off/shadow still disables blocking),
    # and the rule-named chameleon-ignore override all gate the actual block
    # downstream; this only fixes which rules are considered calibrated to
    # speak.
    out = set(SECURITY_BLOCK_RULES)
    for rule, meta in load_block_rules(profile_dir).items():
        # Only block-eligible rules can ever block; a committed profile that marks
        # some other rule "active" (tampering or schema drift) must not promote it.
        if rule not in BLOCK_ELIGIBLE_RULES:
            continue
        if rule_inert_for_language(rule, profile_dir):
            continue
        if rule_inert_missing_signal(rule, profile_dir):
            continue
        if isinstance(meta, dict) and meta.get("active") is True:
            out.add(rule)
    return out


def fp_demoted_rules(profile_dir: Path) -> frozenset[str]:
    """Rules calibration demoted because they flagged CONFORMING committed code.

    A rule with ``active: False`` AND ``flagged > 0`` fired on files that are
    conforming by definition (committed = the convention), so those firings are
    measured false positives (e.g. inheritance-convention on a DRF
    ``FlexFieldsModelSerializer``). The per-edit render uses this to present such
    a rule as advisory rather than an imperative "Fix these." -- the calibration
    layer already knows the rule is FP-prone, so its individual edit-time firings
    should not carry the conformance-failure tone. A rule with ``flagged == 0``
    (never mis-fired, or inert for the language) is NOT demoted. Fail-open to
    empty. This is presentation only; block eligibility already excludes these.
    """
    out: set[str] = set()
    for rule, meta in load_block_rules(profile_dir).items():
        if not isinstance(meta, dict) or rule in SECURITY_BLOCK_RULES:
            continue  # a hard-class security rule is never tone-demoted
        try:
            flagged = int(meta.get("flagged") or 0)
        except (TypeError, ValueError):
            flagged = 0
        if meta.get("active") is False and flagged > 0:
            out.add(rule)
    return frozenset(out)


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
    max_files = threshold_int("CALIBRATION_MAX_FILES")
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
            if len(out) >= max_files:
                return out

    # Second pass: bounded siblings per archetype. A sibling is a real, readable,
    # same-extension file in the witness's directory that is not itself a witness.
    # iterdir() is run once per directory and shared across witnesses that live in
    # the same directory, so a dense witness dir is scanned a single time.
    dir_names_cache: dict[str, list[str] | None] = {}
    for witness_rel, archetype in witness_dirs:
        if len(out) >= max_files:
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
            if taken >= max_siblings or len(out) >= max_files:
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
        violations += [v.to_dict() for v in lint(snap, baseline, language=language)]

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
    if lang in ("typescript", "ruby", "python"):
        langs.add(lang)
    return langs


def calibrate_block_rules(repo_root: Path, loaded) -> dict:
    """Measure each block-eligible rule against the repo's own committed files.

    A rule that flags more than CALIBRATION_FP_EPSILON of sampled files is
    marked inactive (advisory only) for this repo. The witness corpus is
    presumed correct.

    Fail-closed on no evidence: with zero sampled witnesses (empty or
    unbootstrapped profile) every block-eligible rule stays inactive rather than
    greenlighting blockers no file vouched for. Same principle for language
    capability: a rule with no signal source for the profile's language can
    never fire, so its vacuous 0.0 fp_rate must not certify it active —
    silence from a rule that cannot speak is not evidence of safety. Those
    rules are marked inactive with an ``inert_reason`` so /chameleon-status
    can say why.
    """
    fp_epsilon = threshold_float("CALIBRATION_FP_EPSILON")
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
    conventions_doc = getattr(loaded, "conventions", {}) or {}
    rule_has_signal = {rule: check(conventions_doc) for rule, check in _SIGNAL_CHECKS.items()}
    result: dict = {}
    for rule in BLOCK_ELIGIBLE_RULES:
        hits = len(flagged[rule])
        fp_rate = (hits / n) if n else 0.0
        supported = BLOCK_RULE_LANGUAGES.get(rule)
        # Gate only on POSITIVE knowledge: an unknown/legacy profile language
        # (no `language` key) keeps the measured behavior rather than demoting
        # every language-scoped rule.
        lang_ok = supported is None or not langs or bool(langs & supported)
        # Same principle one level deeper: a rule whose driving convention data
        # is absent measures a vacuous 0.0 fp_rate (it cannot flag anything),
        # which must not certify it active.
        signal_ok = rule not in _SIGNAL_CHECKS or rule_has_signal[rule]
        if rule in SECURITY_BLOCK_RULES:
            # The security rules are exempt from the witness floor and the
            # fp gate: this pass runs no content scans, so n and fp_rate say
            # nothing about them, and "fail-closed on no evidence" would read
            # a fresh or sparse profile (zero witnesses) as a reason to disarm
            # the credential/eval deny. active_block_rules applies the same
            # exemption at read time; the entry here keeps the artifact's
            # provenance honest for /chameleon-status and /chameleon-explain.
            entry = {
                "active": lang_ok,
                "fp_rate": round(fp_rate, 4),
                "sampled": n,
                "flagged": hits,
                "exempt_reason": "security-rule",
            }
            result[rule] = entry
            continue
        entry: dict = {
            "active": lang_ok and signal_ok and n > 0 and fp_rate <= fp_epsilon,
            "fp_rate": round(fp_rate, 4),
            "sampled": n,
            "flagged": hits,
        }
        if not lang_ok:
            entry["inert_reason"] = "no-signal-for-language"
        elif not signal_ok:
            entry["inert_reason"] = "missing-convention-data"
        result[rule] = entry
    return result


def apply_override_feedback_demotion(
    verdicts: dict,
    override_rates: dict,
    *,
    threshold: float,
    min_events: int,
    min_distinct_sessions: int,
) -> dict:
    """Demote a calibrated-active rule the team keeps overriding in practice.

    Calibration certifies a rule against the repo's *committed* files; it cannot
    see that, once enforcing, the rule fires on code the team deliberately
    overrides. A rule overridden in more than ``threshold`` of its fires over at
    least ``min_events`` is fighting the team, not catching bugs, so it drops to
    advisory here. The volume floor stops one override out of one fire from
    nuking a rule.

    Override evidence is author-generated, so a single session must not hold a
    kill switch over a calibrated block rule: the demotion auto-applies only
    when the supporting overrides span at least ``min_distinct_sessions``
    distinct sessions (a missing or zero session count reads as zero — absent
    evidence never weakens the gate). Below that floor, and always for
    ``SECURITY_BLOCK_RULES``, the demotion is instead recorded as a
    ``demotion_proposed`` field on the entry: the rule keeps blocking and
    /chameleon-status surfaces the proposal for a human decision. A demoted
    entry carries ``override_distinct_sessions`` so the multi-session evidence
    that authorized it stays on record.

    This runs at refresh time, before the trust hash is taken, so demotions and
    proposals live in the trust-hashed artifact and are never a runtime
    mutation of it. ``verdicts`` is not mutated in place. Proposals need no
    explicit clearing: calibrate_block_rules rebuilds every entry fresh each
    refresh, so a proposal that loses its evidence disappears on the next
    refresh.
    """
    out: dict = {}
    for rule, meta in verdicts.items():
        entry = dict(meta) if isinstance(meta, dict) else meta
        stats = override_rates.get(rule)
        if (
            isinstance(entry, dict)
            and entry.get("active") is True
            and isinstance(stats, dict)
            and stats.get("events", 0) >= min_events
            and stats.get("rate", 0.0) > threshold
        ):
            ds = int(stats.get("distinct_sessions", 0) or 0)
            if rule in SECURITY_BLOCK_RULES or ds < min_distinct_sessions:
                entry["demotion_proposed"] = {
                    "reason": "high-override-rate",
                    "override_rate": stats["rate"],
                    "events": stats["events"],
                    "distinct_sessions": ds,
                    "security_rule": rule in SECURITY_BLOCK_RULES,
                }
            else:
                entry["active"] = False
                entry["demoted_reason"] = "high-override-rate"
                entry["override_rate"] = stats["rate"]
                entry["override_distinct_sessions"] = ds
        out[rule] = entry
    return out
