"""Trust-facing audit surfaces over a repo's chameleon history.

This module holds three audit surfaces that share only their consumer
(the status/lead tooling that reasons about the gate):

1. ``build_override_audit`` -- how often each block rule gets
   ``chameleon-ignore``d (see its own docstring).

2. The persisted PR-review ledger: an append-only, HMAC-signed record of every
   ``/chameleon-pr-review`` verdict, written so a lead can later answer "which
   merged commits passed review, and did we ship any BLOCK over anyway?"

3. The session-attestation ledger (``session_attestations.ndjson``): one
   signed record per top-level Stop, capturing which turn-end checks ran /
   were skipped / degraded, the governed-vs-ungoverned touched-file universe
   with pinned decision snapshots, the session's inline overrides, and any
   observable disable/pause state.

The PR-review skill is chat-only by default and persists nothing, so a merged
commit leaves no trace of whether chameleon ever looked at it. The ledger fills
that hole. Each review run appends one record pinning the commit SHA, the exact
profile that reviewed it (``profile_sha256`` + generation + schema_version), the
trust state at review time, the verdict, a findings-by-severity summary, the
engine version, and the reviewing user.

RAISE-ONLY DOCTRINE (session attestations). The attestation is self-signed and
raise-only: nothing recorded in it may ever lower scrutiny anywhere downstream.
A consumer may use it only to RAISE gate depth (skipped checks, degraded
spawns, ungoverned files, disable windows escalate) and to make post-incident
replay honest. The merge gate's floor is computed from diff facts alone and
trusts none of this without re-verification; a forged-clean attestation
therefore buys nothing.

INTEGRITY SCOPE -- tamper-evident, NOT forgery-proof, NOT a CI gate. Covers the
PR-review ledger AND the session-attestation ledger alike.

The signing key is the same per-user local HMAC key the exec log uses
(``CHAMELEON_HMAC_KEY_PATH``, owner-checked, 0600). That makes a record
tamper-EVIDENT against a *third* local user silently editing it: a changed line
no longer verifies. It does NOT make the record forgery-proof against the
developer being reviewed -- that developer holds the signing key, so they can
re-run the review, hand-write an APPROVE record, and sign it. CI cannot verify
these records either: CI has no copy of the per-user key (it is never shared or
committed by design). So this is an honest, self-attested audit trail, not an
authority that can replace a human merge gate. A real merge gate needs the
verdict posted to the server-side platform of record (a Bitbucket/GitHub status
check) or an asymmetric key the developer does not control; both are out of
scope here. The signature buys integrity against incidental local tampering and
nothing more, and the docstrings/panels say so rather than dressing a soft LLM
judgement up as cryptographically authoritative.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int


def build_override_audit(
    repo_id: str | None,
    window_days: int | None = None,
) -> dict:
    """Per-rule inline-override audit for ``repo_id`` over the lookback window.

    Returns a plain dict (the MCP tool wraps it in the standard envelope):

    - ``window_days`` — the lookback applied.
    - ``total_overrides`` — sum of override events across all rules in the
      window, the headline number ("overridden in N edits").
    - ``rules`` — per rule (sorted): ``overrides``, ``would_blocks`` (shadow
      would-block metrics PLUS real enforce-mode blocks from decision_log, see
      ``_would_block_counts`` — a purely shadow-sourced count reads an
      actively-enforced rule's evidence as zero), ``blanket`` (bare-directive
      overrides), ``distinct_files``, ``distinct_sessions``, ``override_rate``
      (overrides / (overrides + would_blocks), or None below the min-events
      floor), ``high_override_rate`` (rate at or above the threshold over
      enough events), and ``blanket_abuse`` (the override share that came from
      bare directives is high).
    - ``flagged`` — rule names with ``high_override_rate`` or ``blanket_abuse``
      set, the subset a lead should reconcile via refresh/teach.

    Fail-open: a missing drift.db or unreadable metrics log degrades to an empty
    audit ({"rules": {}, "flagged": [], "total_overrides": 0}) rather than
    raising. The caller (the status tool) already swallows exceptions, but the
    audit guards internally too so a partial read still returns a usable shape.
    """
    if window_days is None:
        window_days = threshold_int("OVERRIDE_AUDIT_WINDOW_DAYS")
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        window_days = threshold_int("OVERRIDE_AUDIT_WINDOW_DAYS")
    if window_days <= 0:
        window_days = threshold_int("OVERRIDE_AUDIT_WINDOW_DAYS")

    high_rate = threshold_float("OVERRIDE_RATE_HIGH")
    min_events = threshold_int("OVERRIDE_AUDIT_MIN_EVENTS")
    blanket_high = threshold_float("OVERRIDE_BLANKET_HIGH")

    empty = {
        "repo_id": repo_id,
        "window_days": window_days,
        "total_overrides": 0,
        "rules": {},
        "flagged": [],
    }
    if not repo_id:
        return empty

    overrides = _override_counts(repo_id, window_days)
    would_blocks = _would_block_counts(repo_id, window_days)

    rule_names = set(overrides) | set(would_blocks)
    if not rule_names:
        return empty

    rules_out: dict[str, dict] = {}
    flagged: list[str] = []
    total_overrides = 0
    for rule in sorted(rule_names):
        ov = overrides.get(rule, {})
        ov_count = int(ov.get("overrides", 0))
        blanket = int(ov.get("blanket", 0))
        wb_count = int(would_blocks.get(rule, 0))
        total_overrides += ov_count

        events = ov_count + wb_count
        rate: float | None = None
        high = False
        if events >= min_events:
            rate = round(ov_count / events, 4) if events else 0.0
            high = rate >= high_rate
        blanket_abuse = ov_count > 0 and (blanket / ov_count) >= blanket_high

        rules_out[rule] = {
            "overrides": ov_count,
            "would_blocks": wb_count,
            "blanket": blanket,
            "distinct_files": int(ov.get("distinct_files", 0)),
            "distinct_sessions": int(ov.get("distinct_sessions", 0)),
            "override_rate": rate,
            "high_override_rate": high,
            "blanket_abuse": blanket_abuse,
        }
        if high or blanket_abuse:
            flagged.append(rule)

    return {
        "repo_id": repo_id,
        "window_days": window_days,
        "total_overrides": total_overrides,
        "rules": rules_out,
        "flagged": flagged,
    }


def _override_counts(repo_id: str, window_days: int) -> dict[str, dict]:
    """Per-rule override tallies from drift.db, or empty on any failure."""
    try:
        from chameleon_mcp.drift.observations import override_counts

        return override_counts(repo_id, window_days=window_days) or {}
    except Exception:
        return {}


def _real_block_counts(repo_id: str, window_days: int) -> dict[str, int]:
    """Per-rule real block tallies from decision_log's ``blocked`` outcome rows.

    Shadow mode emits a would_block metric row per rule per violation instance;
    enforce mode never does — it records the real block straight to
    decision_log instead (see ``_record_edit_decision`` call sites in
    hook_helper.py). Reading only the shadow side leaves an actively-enforced
    rule's contribution to the override-rate denominator at zero, so a rule
    correctly blocking most of its triggers with no overrides reads as
    undefined, and one blocking most triggers with a handful of overrides reads
    as a false 100%. This reads the enforce-mode evidence the shadow side
    misses. ``blockable_rules`` is comma-joined and may repeat a rule (one entry
    per violation instance, the same per-instance granularity the shadow
    would_block metric uses), so every occurrence is counted.

    Opens drift.db read-only via the shared hardening helper, the same pattern
    ``index_db.py`` uses for a sibling sqlite store. Fail-open: a missing
    drift.db or any sqlite/OS error returns {}.
    """
    from chameleon_mcp.drift.sqlite_config import open_hardened
    from chameleon_mcp.profile.trust import plugin_data_dir

    db_path = plugin_data_dir() / repo_id / "drift.db"
    if not db_path.is_file():
        return {}
    cutoff = int(time.time()) - window_days * 86_400
    try:
        conn = open_hardened(db_path, read_only=True)
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            "SELECT blockable_rules FROM decision_log WHERE outcome = ? AND observed_at >= ?",
            ("blocked", cutoff),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    counts: dict[str, int] = {}
    for row in rows:
        for rule in (row[0] or "").split(","):
            rule = rule.strip()
            if rule:
                counts[rule] = counts.get(rule, 0) + 1
    return counts


def _would_block_counts(repo_id: str, window_days: int) -> dict[str, int]:
    """Per-rule would-block counts: shadow metrics PLUS real enforce-mode blocks.

    The two sources never double-count the same instance: a rule accrues a
    shadow row only while the repo is in shadow mode, and a decision_log
    ``blocked`` row only while it is in enforce mode, so summing them over the
    same rule/window adds disjoint evidence rather than inflating one event
    twice. See ``_real_block_counts`` for why the enforce-mode half is needed
    at all — without it, an actively-enforced rule's override_rate is computed
    against zero would-blocks regardless of how well it is actually holding.

    Reuses the shadow report's aggregation for the shadow half so that number
    still matches the shadow surface exactly. Empty on any failure.
    """
    counts: dict[str, int] = {}
    try:
        from chameleon_mcp.shadow_report import build_shadow_report

        report = build_shadow_report(repo_id, window_days)
        rules = report.get("rules") or {}
        for rule, meta in rules.items():
            counts[rule] = counts.get(rule, 0) + int(meta.get("would_blocks", 0))
    except Exception:
        pass

    try:
        for rule, n in _real_block_counts(repo_id, window_days).items():
            counts[rule] = counts.get(rule, 0) + n
    except Exception:
        pass

    return counts


# --- PR-review ledger ----------------------------------------------------------
#
# An append-only, HMAC-signed NDJSON file per repo, recording every review run.
# Storage mirrors the exec log's model (per-repo dir under the owner-checked 0700
# plugin-data root) but lives under PLUGIN_DATA, not TMPDIR: a review verdict is
# durable provenance a lead reaches back for, not transient session state.

_LEDGER_FILENAME = "review_ledger.ndjson"

# Verdict vocabulary the skill writes: APPROVE / APPROVE WITH NITS / NEEDS
# CHANGES / BLOCK. Only BLOCK is special-cased (the shipped-over-BLOCK panel).
_BLOCK_VERDICT = "BLOCK"
_KNOWN_VERDICTS = ("APPROVE WITH NITS", "NEEDS CHANGES", "APPROVE", "BLOCK")


def _normalize_verdict(verdict) -> str:
    """Canonicalize a known verdict's case/whitespace at record time.

    The shipped-over-BLOCK audit is the ledger's whole reason to exist, and it
    matched ``verdict == "BLOCK"`` exactly, so a case-variant (``"Block"``) or a
    typo silently dropped a merged-despite-block case from the signal. A verdict
    that matches one of the four known values case-insensitively is stored in its
    canonical form; anything else (including an annotated ``"BLOCK (2 findings)"``)
    is stored verbatim and still caught by the prefix-aware match below.
    """
    s = str(verdict).strip()
    up = s.upper()
    for known in _KNOWN_VERDICTS:
        if up == known:
            return known
    return s


def _is_block_verdict(verdict) -> bool:
    """True when a stored verdict is (or begins with) BLOCK, case-insensitively.

    Catches the canonical ``"BLOCK"`` plus an annotated ``"BLOCK (2 findings)"``,
    so a merged-despite-block case is not missed on a formatting drift.
    """
    up = str(verdict).strip().upper()
    return up == _BLOCK_VERDICT or up.startswith(_BLOCK_VERDICT + " ")


def _ledger_path(repo_id: str) -> Path:
    """Return ``${PLUGIN_DATA}/<repo_id>/review_ledger.ndjson``.

    The per-repo dir is created 0700 and owner-locked via the same helper the
    trust record uses, so the ledger inherits the plugin-data root's owner-only
    traversal guard.
    """
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(repo_id) / _LEDGER_FILENAME


def _sign(record: dict) -> str:
    """HMAC-SHA256 over the canonical JSON of ``record`` (without its signature).

    Reuses the exec log's per-user key. Tamper-evidence only: see the module
    docstring for why this is not forgery-proof against the key holder.
    """
    from chameleon_mcp.exec_log import _ensure_hmac_key

    key = _ensure_hmac_key()
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _reviewer() -> str:
    """Best-effort reviewing-user identity for the audit trail."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")


def _normalize_findings(findings: dict | None) -> dict[str, int]:
    """Coerce a free-form findings summary into a ``{severity: count}`` map.

    The skill passes counts by severity (BLOCK / FIX / NIT). A non-dict or
    non-integer value is dropped rather than stored, so a malformed argument
    can never corrupt the signed record's shape.
    """
    out: dict[str, int] = {}
    if not isinstance(findings, dict):
        return out
    for sev, count in findings.items():
        if not isinstance(sev, str):
            continue
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out[sev] = n
    return out


def record_review(
    repo_id: str,
    *,
    commit_sha: str | None,
    verdict: str,
    findings: dict | None = None,
    profile_sha256: str | None = None,
    generation: int | None = None,
    schema_version: int | None = None,
    trust_state: str | None = None,
    engine_version: str | None = None,
    pr_id: str | None = None,
    complexity_tier: str | None = None,
) -> dict:
    """Append one signed PR-review record to ``repo_id``'s ledger.

    Records the provenance that pins exactly which knowledge base reviewed the
    code: ``commit_sha`` (the head reviewed), ``profile_sha256`` + ``generation``
    + ``schema_version`` (the profile), ``trust_state`` at review time,
    ``engine_version``, the ``verdict``, and a findings-by-severity summary. The
    reviewing user and a UTC timestamp are stamped here. The whole record is
    HMAC-signed before the line is written.

    Returns the stored record (including ``signed``: whether signing succeeded).
    Signing failure does NOT drop the record -- an unsigned-but-recorded verdict
    is more useful to a lead than a silently lost one -- it is written with
    ``"hmac": null`` and ``signed: False`` so the reader can flag it. Raises only
    if the file write itself fails; the caller (the skill's final step) treats a
    raise as "ledger unavailable, surface the verdict in chat anyway".
    """
    record: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit_sha": str(commit_sha) if commit_sha else None,
        "pr_id": str(pr_id) if pr_id else None,
        "verdict": _normalize_verdict(verdict),
        "findings": _normalize_findings(findings),
        "profile_sha256": str(profile_sha256) if profile_sha256 else None,
        "generation": generation if isinstance(generation, int) else None,
        "schema_version": schema_version if isinstance(schema_version, int) else None,
        "trust_state": str(trust_state) if trust_state else None,
        "engine_version": str(engine_version) if engine_version else None,
        # The change's structural complexity tier (easy / medium / hard /
        # complex) at review time, so per-tier review-clean rates are trackable
        # over the ledger; None for records written before it was captured.
        "complexity_tier": str(complexity_tier) if complexity_tier else None,
        "reviewer": _reviewer(),
    }
    try:
        record["hmac"] = _sign(record)
    except Exception:
        # No key (e.g. /dev/urandom unavailable in a stripped container): keep
        # the verdict, mark it unsigned so the reader does not claim integrity.
        record["hmac"] = None

    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    path = _ledger_path(repo_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    _trim_ledger(path, threshold_int("REVIEW_LEDGER_MAX_RECORDS"))
    return record


def _trim_ledger(path: Path, cap: int) -> None:
    """Keep only the most-recent ``cap`` lines of an NDJSON ledger.

    Ledgers are never wiped by refresh, so without a cap they grow unbounded.
    One record per event keeps them small in practice; this trims by recency
    only when the line count crosses the cap. Best-effort: any read/write error
    leaves the file untouched rather than risking data loss.
    """
    if cap <= 0:
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= cap:
        return
    keep = lines[-cap:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _verify(record: dict) -> bool:
    """Return True iff ``record``'s stored HMAC matches a fresh signature.

    A record written with ``"hmac": null`` (signing was unavailable at write
    time) reads as unverified, never as verified.
    """
    stored = record.get("hmac")
    if not isinstance(stored, str):
        return False
    body = {k: v for k, v in record.items() if k != "hmac"}
    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key

        key = _ensure_hmac_key()
    except Exception:
        return False
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(stored, actual)


def read_review_history(
    repo_id: str | None,
    limit: int | None = None,
) -> dict:
    """Return the most-recent review records for ``repo_id``, newest first.

    Each returned record carries every stored field plus ``verified`` (the HMAC
    re-check) so a reader can tell a tamper-evident-clean record from one that
    no longer verifies (silently edited by another local user, or written
    unsigned). ``unverified`` counts the returned records that failed the check.

    Tamper-evidence only: a verified record proves no THIRD party silently
    edited the line. It does NOT prove the developer being reviewed did not
    re-run and re-sign their own APPROVE -- they hold the key. See the module
    docstring.

    Fail-open: a missing or unreadable ledger returns an empty history rather
    than raising. A single corrupt (non-JSON) line is skipped, not fatal.
    """
    if limit is None:
        limit = threshold_int("REVIEW_HISTORY_DEFAULT_LIMIT")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = threshold_int("REVIEW_HISTORY_DEFAULT_LIMIT")
    if limit <= 0:
        limit = threshold_int("REVIEW_HISTORY_DEFAULT_LIMIT")

    empty = {"repo_id": repo_id, "records": [], "total": 0, "unverified": 0}
    if not repo_id:
        return empty

    path = _ledger_path(repo_id)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except OSError:
        return empty

    parsed: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            parsed.append(record)

    total = len(parsed)
    recent = parsed[-limit:][::-1]
    unverified = 0
    out_records: list[dict] = []
    for record in recent:
        verified = _verify(record)
        if not verified:
            unverified += 1
        enriched = dict(record)
        enriched["verified"] = verified
        out_records.append(enriched)

    return {
        "repo_id": repo_id,
        "records": out_records,
        "total": total,
        "unverified": unverified,
    }


def build_review_ledger_panel(repo_id: str | None) -> dict | None:
    """Compact ledger summary for the status surface, or None when empty.

    Surfaces the one question the ledger exists to answer: did any BLOCK verdict
    ship anyway? Returns:

    - ``total`` -- review records on file for this repo.
    - ``last`` -- the most-recent record's ``{ts, commit_sha, verdict}``.
    - ``shipped_over_block`` -- BLOCK records whose ``commit_sha`` is an ancestor
      of HEAD in the working tree, i.e. a BLOCK verdict that was reviewed and
      then merged. Each entry is ``{commit_sha, ts}``. This is the
      merged-despite-BLOCK list a lead must eyeball.
    - ``unverified`` -- records in the recent window whose HMAC no longer
      verifies (tamper-evident signal; see the module docstring for the scope).

    Fail-open: any read failure returns None so the status call degrades to no
    panel rather than crashing.
    """
    if not repo_id:
        return None
    try:
        history = read_review_history(repo_id, limit=threshold_int("REVIEW_LEDGER_MAX_RECORDS"))
    except Exception:
        return None
    records = history.get("records") or []
    if not records:
        return None

    last = records[0]
    block_shas = [
        r.get("commit_sha")
        for r in records
        if _is_block_verdict(r.get("verdict")) and r.get("commit_sha")
    ]
    shipped = _shas_merged_into_head(repo_id, block_shas)
    shipped_over_block = [
        {"commit_sha": r.get("commit_sha"), "ts": r.get("ts")}
        for r in records
        if _is_block_verdict(r.get("verdict")) and r.get("commit_sha") in shipped
    ]

    return {
        "total": history.get("total", len(records)),
        "last": {
            "ts": last.get("ts"),
            "commit_sha": last.get("commit_sha"),
            "verdict": last.get("verdict"),
        },
        "shipped_over_block": shipped_over_block,
        "unverified": history.get("unverified", 0),
    }


def _shas_merged_into_head(repo_id: str, shas: list) -> set:
    """Return the subset of ``shas`` that are ancestors of the repo's HEAD.

    Resolves the repo path from the trust record's ``repo_root`` (the only
    repo_id -> path mapping this module can reach without importing tools) and
    asks git whether each SHA is reachable from HEAD. A merged-despite-BLOCK
    case is exactly a BLOCK-verdict commit that now sits in HEAD's history.

    Best-effort and read-only: no git available, no repo path, a SHA git does
    not know, or any subprocess error all degrade to "not merged" for that SHA
    (the panel under-reports rather than crashing or false-flagging).
    """
    candidates = [s for s in shas if isinstance(s, str) and s]
    if not candidates:
        return set()

    repo_root = _repo_root_for(repo_id)
    if repo_root is None:
        return set()

    from chameleon_mcp.profile.canonical_loader import _run_git

    merged: set = set()
    for sha in candidates:
        # Ledger records are self-written, but the SHA still came from a file
        # on disk: validate the shape so a corrupted value can never reach git
        # argv as something option-like.
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha or ""):
            continue
        result = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], cwd=repo_root)
        if result is not None and result.returncode == 0:
            merged.add(sha)
    return merged


def _repo_root_for(repo_id: str) -> Path | None:
    """Resolve ``repo_id`` to an on-disk repo root via the trust record.

    The trust record stamps ``repo_root`` on the first grant; that is enough to
    run git against the working tree. Returns None when there is no trust record
    or the path no longer exists.
    """
    try:
        from chameleon_mcp.profile.trust import trust_state_for

        record = trust_state_for(repo_id)
    except Exception:
        return None
    if record is None or not record.repo_root:
        return None
    root = Path(record.repo_root)
    try:
        return root if root.is_dir() else None
    except OSError:
        return None


# --- Finding fates ---------------------------------------------------------------
#
# A per-finding adjudication ledger: one signed record per human decision on a
# review finding (accepted / declined / converted-to-check), across every
# adjudication surface (pr-review verdicts, receiving AGREE/PUSH BACK, deep-work
# declines). A SEPARATE per-repo NDJSON in the same data dir, reusing the review
# ledger's signing + trim machinery. It stores NO finding prose, only a 16-hex
# digest of it (privacy posture: like intent_capture, digests not raw text), plus
# the lens that raised it and the confidence at emit -- the raw material a later,
# outcome-calibrated lens-weighting step consumes. Aggregation is precision only
# and advisory: nothing here gates or blocks.

_FATES_FILENAME = "finding_fates.ndjson"
_FATE_VOCAB = ("accepted", "declined", "converted")
# Synonyms the three skills may pass, mapped to the canonical vocabulary. The
# receiving skill speaks AGREE / PUSH BACK; pr-review speaks accept/decline; a
# runtime-state finding converts to an executable check.
_FATE_ALIASES = {
    "accept": "accepted",
    "agree": "accepted",
    "agreed": "accepted",
    "decline": "declined",
    "reject": "declined",
    "rejected": "declined",
    "push back": "declined",
    "pushback": "declined",
    "convert": "converted",
    "converted to check": "converted",
    "unrun check": "converted",
}


def _fates_path(repo_id: str) -> Path:
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(repo_id) / _FATES_FILENAME


def finding_digest(message, file, line) -> str:
    """16-hex digest of a finding's identity (normalized message + file + line).

    The message is lowercased and whitespace-collapsed so a re-render with
    different spacing hashes the same. No finding prose is ever persisted -- only
    this digest -- so the ledger carries the finding's identity without its text.
    """
    norm_msg = " ".join(str(message or "").lower().split())
    norm_file = str(file or "")
    try:
        norm_line = str(int(line)) if line is not None else ""
    except (TypeError, ValueError):
        norm_line = ""
    payload = f"{norm_msg}\x00{norm_file}\x00{norm_line}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _normalize_fate(fate) -> str | None:
    """Canonicalize a fate to accepted / declined / converted, or None if unknown.

    Separators are folded (``push-back`` / ``push_back`` / ``push back`` /
    ``pushback`` all map) so a caller spelling the tool docstring's synonyms with a
    hyphen or underscore is not silently rejected.
    """
    s = " ".join(str(fate or "").strip().lower().replace("-", " ").replace("_", " ").split())
    if s in _FATE_VOCAB:
        return s
    return _FATE_ALIASES.get(s)


def _normalize_confidence(value) -> float | None:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    return c if 0.0 <= c <= 1.0 else None


def record_finding_fate(
    repo_id: str,
    *,
    message,
    file=None,
    line=None,
    lens: str | None = None,
    confidence_at_emit=None,
    fate: str,
    surface: str | None = None,
) -> dict:
    """Append one signed finding-fate record to ``repo_id``'s fate ledger.

    Records how a human adjudicated one review finding: its 16-hex ``finding_digest``
    (no prose), the ``lens``/rubric that raised it, the ``confidence_at_emit``, the
    ``fate`` (accepted / declined / converted), and the ``surface`` (pr-review /
    receiving / deep-work). HMAC-signed like the review ledger; written unsigned
    (``hmac: null``) rather than dropped on a key failure. Returns the stored record.

    Raises ValueError on an unknown fate so a caller mistake surfaces loudly
    instead of silently writing a garbage row; raises only if the file write
    itself fails (the caller treats that as "ledger unavailable, carry on").
    """
    canon_fate = _normalize_fate(fate)
    if canon_fate is None:
        raise ValueError(f"unknown fate {fate!r}; expected one of {_FATE_VOCAB} or an alias")
    record: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "finding_digest": finding_digest(message, file, line),
        "lens": str(lens) if lens else None,
        "confidence_at_emit": _normalize_confidence(confidence_at_emit),
        "fate": canon_fate,
        "surface": str(surface) if surface else None,
        "reviewer": _reviewer(),
    }
    try:
        record["hmac"] = _sign(record)
    except Exception:
        record["hmac"] = None

    line_out = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    path = _fates_path(repo_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line_out)
    _trim_ledger(path, threshold_int("FINDING_FATES_MAX_RECORDS"))
    return record


def read_finding_fates(repo_id: str | None, limit: int | None = None) -> dict:
    """Return recent finding-fate records for ``repo_id``, newest first.

    Each record carries ``verified`` (the HMAC re-check). Fail-open: a missing or
    unreadable ledger returns an empty result; a corrupt line is skipped.
    """
    if limit is None:
        limit = threshold_int("FINDING_FATES_MAX_RECORDS")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = threshold_int("FINDING_FATES_MAX_RECORDS")
    if limit <= 0:
        limit = threshold_int("FINDING_FATES_MAX_RECORDS")

    empty = {"repo_id": repo_id, "records": [], "total": 0, "unverified": 0}
    if not repo_id:
        return empty

    path = _fates_path(repo_id)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except OSError:
        return empty

    parsed: list[dict] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            parsed.append(record)

    total = len(parsed)
    recent = parsed[-limit:][::-1]
    unverified = 0
    out_records: list[dict] = []
    for record in recent:
        verified = _verify(record)
        if not verified:
            unverified += 1
        enriched = dict(record)
        enriched["verified"] = verified
        out_records.append(enriched)

    return {"repo_id": repo_id, "records": out_records, "total": total, "unverified": unverified}


def _empty_fate_bucket() -> dict:
    return {"accepted": 0, "declined": 0, "converted": 0, "total": 0, "precision": None}


def _finalize_fate_bucket(bucket: dict) -> dict:
    # precision = accepted / (accepted + declined); a converted finding is neither
    # a confirmation nor a refutation yet, so it is excluded from the denominator.
    # No decisions -> null, never a fabricated 0.0.
    denom = bucket["accepted"] + bucket["declined"]
    bucket["precision"] = (bucket["accepted"] / denom) if denom else None
    return bucket


def per_lens_precision(repo_id: str | None) -> dict:
    """Aggregate the fate ledger into per-SURFACE, per-lens precision. Aggregation
    ONLY -- no calibration, no gating; this is a read-back surface for a human
    (and, later, for an outcome-calibrated weighting step).

    Broken down by ``surface`` because ``accepted`` means different things at each
    one and pooling them is incoherent: at ``pr-review`` accepted = a finding that
    survived RECALL + the refuter; at ``deep-work`` accepted = a chameleon-reviewer
    finding the author applied (vs declined); at ``receiving`` accepted = AGREE with
    an external reviewer comment. Only WITHIN a surface is a lens's precision a
    single, interpretable number, so there is deliberately no cross-surface
    ``overall``. Each surface carries its own ``overall`` and per-lens breakdown.

    HMAC-unverified rows (a line edited by a third local user since it was signed)
    are EXCLUDED from the math and counted under ``unverified`` -- the tamper
    evidence every sibling reader surfaces must not silently skew an aggregate.
    """
    history = read_finding_fates(repo_id)
    surfaces: dict[str, dict] = {}
    unverified = 0
    for record in history.get("records") or []:
        if not record.get("verified"):
            unverified += 1
            continue
        fate = record.get("fate")
        if fate not in _FATE_VOCAB:
            continue
        surface = record.get("surface") or "(unlabeled)"
        lens = record.get("lens") or "(unlabeled)"
        s = surfaces.setdefault(surface, {"lenses": {}, "overall": _empty_fate_bucket()})
        bucket = s["lenses"].setdefault(lens, _empty_fate_bucket())
        for target in (bucket, s["overall"]):
            target[fate] += 1
            target["total"] += 1

    for s in surfaces.values():
        for bucket in s["lenses"].values():
            _finalize_fate_bucket(bucket)
        _finalize_fate_bucket(s["overall"])

    return {"repo_id": repo_id, "unverified": unverified, "surfaces": surfaces}


# --- Session attestations --------------------------------------------------------
#
# A SEPARATE per-repo NDJSON in the same data dir, sharing the review ledger's
# signing and trim machinery. Keeping attestations out of review_ledger.ndjson
# keeps the review surface byte-stable: read_review_history and
# build_review_ledger_panel never see attestation rows, and the attestation
# ledger trims independently.

_ATTESTATION_FILENAME = "session_attestations.ndjson"
_ATTESTATION_SCHEMA = 1

# Top-level scalar fields of an attestation payload, by coercion. A value that
# does not match its expected type is dropped to the field's neutral value
# rather than stored raw, so a malformed payload can never corrupt the signed
# shape (same stance as _normalize_findings).
_ATTESTATION_STR_KEYS = (
    "session_id",
    "engine_version",
    "profile_sha256",
    "trust_state",
    "enforcement_mode",
)
_ATTESTATION_OPT_INT_KEYS = ("generation", "schema_version")
_ATTESTATION_COUNT_KEYS = (
    "check_events_unverified",
    "governed_truncated",
    "ungoverned_truncated",
    "overrides_truncated",
    "stop_hook_blocks",
    "duplication_spawns",
)


def _attestation_path(repo_id: str) -> Path:
    """Return ``${PLUGIN_DATA}/<repo_id>/session_attestations.ndjson``."""
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(repo_id) / _ATTESTATION_FILENAME


def _opt_str(value) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def _scalar_str(value) -> str | None:
    """Scalar timestamps (ISO string or epoch number) normalized to a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _norm_entries(value, required: dict, optional: dict) -> list[dict]:
    """Coerce a list of payload entries into a fixed field shape.

    ``required`` fields must coerce to a non-None value or the entry is
    dropped; ``optional`` fields fall back to their coercer's neutral value.
    Non-dict entries are dropped.
    """
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, dict):
            continue
        coerced: dict = {}
        ok = True
        for field, coerce in required.items():
            v = coerce(entry.get(field))
            if v is None:
                ok = False
                break
            coerced[field] = v
        if not ok:
            continue
        for field, coerce in optional.items():
            coerced[field] = coerce(entry.get(field))
        out.append(coerced)
    return out


def _normalize_attestation_payload(payload: dict) -> dict:
    """Coerce a free-form attestation payload into the signed record shape.

    Mirrors ``_normalize_findings``: malformed values are dropped or coerced,
    never trusted to be well-shaped, so a bad argument cannot corrupt the
    signed record.
    """
    src = payload if isinstance(payload, dict) else {}
    out: dict = {}
    for key in _ATTESTATION_STR_KEYS:
        out[key] = _opt_str(src.get(key))
    for key in _ATTESTATION_OPT_INT_KEYS:
        out[key] = _opt_int(src.get(key))
    for key in _ATTESTATION_COUNT_KEYS:
        out[key] = _count(src.get(key))

    env = src.get("env") if isinstance(src.get("env"), dict) else {}
    out["env"] = {
        "verify_off": bool(env.get("verify_off")),
        "enforce_off": bool(env.get("enforce_off")),
    }

    sup = src.get("suppression") if isinstance(src.get("suppression"), dict) else {}
    out["suppression"] = {
        "reason": _opt_str(sup.get("reason")),
        "session_disabled_at": _scalar_str(sup.get("session_disabled_at")),
        "pause_until": _opt_str(sup.get("pause_until")),
    }

    out["checks"] = _norm_entries(
        src.get("checks"),
        required={"check": _opt_str, "status": _opt_str},
        optional={"reason": _opt_str, "count": _count},
    )
    out["governed_files"] = _norm_entries(
        src.get("governed_files"),
        required={"file": _opt_str},
        optional={
            "content_digest": _opt_str,
            "decision_log_id": _opt_int,
            "archetype": _opt_str,
            "match_quality": _opt_str,
            "outcome": _opt_str,
            "observed_at": _opt_int,
        },
    )
    out["ungoverned_files"] = _norm_entries(
        src.get("ungoverned_files"),
        required={"file": _opt_str},
        optional={"content_digest": _opt_str},
    )
    out["overrides"] = _norm_entries(
        src.get("overrides"),
        required={"rule": _opt_str},
        optional={"file": _opt_str, "blanket": lambda v: bool(v), "count": _count},
    )
    return out


def _attestation_digest(body: dict) -> str:
    """Payload digest for the per-session dedup marker.

    Canonical JSON over the normalized body plus the schema stamp -- i.e.
    everything except ``ts``, ``hmac``, and ``record_type`` -- so an unchanged
    session state hashes identically across consecutive Stops.

    Check-event COUNTS are excluded from the digest basis: the Stop relint
    gate records one "ran" event per Stop, so counts grow even on an idle
    session and would defeat the dedup entirely. A session reads as changed
    when a new (check, status, reason) combination appears, not when an
    existing one repeats; the appended record itself keeps the true counts.
    """
    src = dict(body)
    checks = src.get("checks")
    if isinstance(checks, list):
        src["checks"] = [
            {k: v for k, v in entry.items() if k != "count"} if isinstance(entry, dict) else entry
            for entry in checks
        ]
    src["attestation_schema"] = _ATTESTATION_SCHEMA
    canonical = json.dumps(src, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def record_session_attestation(repo_id: str, payload: dict) -> dict:
    """Append one signed session attestation to ``repo_id``'s attestation ledger.

    The attestation is self-signed and raise-only: nothing recorded in it may
    ever lower scrutiny anywhere downstream. A consumer may use it only to
    RAISE gate depth (skipped checks, degraded spawns, ungoverned files,
    disable windows escalate) and to make post-incident replay honest. The
    merge gate's floor is computed from diff facts alone and trusts none of
    this without re-verification; a forged-clean attestation therefore buys
    nothing.

    The payload is defensively normalized before signing. Consecutive identical
    payloads for the same session are deduped through a sidecar digest marker,
    so an idle multi-Stop session writes one row and the NEWEST row per session
    is authoritative by construction. The ledger trims to
    ``ATTESTATION_LEDGER_MAX_RECORDS`` by recency. Signing failure does not
    drop the record: it is written with ``"hmac": null`` so the reader flags it
    (same stance as ``record_review``).

    Returns ``{"appended": bool, "digest": str, "record": dict | None}``; on a
    dedup skip ``record`` is None.
    """
    body = _normalize_attestation_payload(payload)
    digest = _attestation_digest(body)

    marker: Path | None = None
    try:
        from chameleon_mcp.optouts import _safe_session_marker
        from chameleon_mcp.profile.trust import repo_data_dir

        marker = repo_data_dir(repo_id) / (
            f".attestation_last.{_safe_session_marker(body.get('session_id'))}"
        )
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
            return {"appended": False, "digest": digest, "record": None}
    except Exception:
        marker = None

    record: dict = {
        "record_type": "session_attestation",
        "attestation_schema": _ATTESTATION_SCHEMA,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **body,
    }
    try:
        record["hmac"] = _sign(record)
    except Exception:
        record["hmac"] = None

    path = _attestation_path(repo_id)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    if marker is not None:
        try:
            marker.write_text(digest, encoding="utf-8")
        except OSError:
            pass
    _trim_ledger(path, threshold_int("ATTESTATION_LEDGER_MAX_RECORDS"))
    return {"appended": True, "digest": digest, "record": record}


def read_session_attestations(
    repo_id: str | None,
    *,
    session_id: str | None = None,
    limit: int = 10,
) -> dict:
    """Most-recent session attestations for ``repo_id``, newest first.

    Optionally filtered to one ``session_id`` (the newest matching row is that
    session's authoritative attestation). Each record carries ``verified`` (the
    HMAC re-check) and ``unverified`` counts the returned records that failed
    it -- the same tamper-evidence scope as the review ledger, see the module
    docstring. Raise-only applies to every consumer: a verified-clean record
    may never lower scrutiny; only skipped/degraded/ungoverned evidence in it
    may raise it.

    Fail-open: a missing or unreadable ledger returns an empty history; a
    corrupt line is skipped, not fatal.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    if limit <= 0:
        limit = 10

    empty = {"repo_id": repo_id, "records": [], "total": 0, "unverified": 0}
    if not repo_id:
        return empty

    path = _attestation_path(repo_id)
    try:
        with open(path, encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        return empty

    parsed: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if session_id is not None and record.get("session_id") != session_id:
            continue
        parsed.append(record)

    total = len(parsed)
    recent = parsed[-limit:][::-1]
    unverified = 0
    out_records: list[dict] = []
    for record in recent:
        verified = _verify(record)
        if not verified:
            unverified += 1
        enriched = dict(record)
        enriched["verified"] = verified
        out_records.append(enriched)

    return {
        "repo_id": repo_id,
        "records": out_records,
        "total": total,
        "unverified": unverified,
    }


# --- Finding-lifecycle ledger (canonical core.finding.Finding rows) --------------
#
# The single store for review findings across their lifecycle (core/finding.py:
# pending -> delivered -> addressed | resurfaced (HIGH, once) -> expired, with a
# below-bar shelved branch). One JSON file per repo -- NOT an append-only NDJSON
# log like the audit surfaces above, since a row here is mutated in place as a
# finding's status advances -- keyed by match_key so the same claim recurring
# across sessions is one logical row, not a growing pile of duplicates. Writes
# use the same flock + atomic-write discipline as core/idiom_store.py: a
# concurrent detached job and a live Stop must never interleave a partial file.
#
# This store is wired into the live Stop pipeline (stop/pipeline.py calls
# recheck_and_resurface directly). It superseded the pre-existing per-event
# judge_findings table in drift.db (record_judge_finding / open_judge_findings
# / mark_judge_finding in drift/observations.py, and stop/gates.py's
# _ledger_persist / _ledger_recheck_and_resurface) -- that older store's write
# side lost its only caller in the async-first cutover, and its read/resurface
# side has since been retired too. Any HIGH finding it was still holding open
# from before the cutover is not migrated (a documented, low-impact gap --
# unlike the .judge_pending.<session>.json queue, which IS migrated via
# migrate_pending_queue below).

_FINDINGS_LEDGER_FILENAME = "findings_ledger.json"
_RESURFACE_MAX_LINES = 8
_OPEN_STATUSES = ("pending", "delivered", "resurfaced")
_HIGH_SEVERITIES = ("blocker", "high")


def _findings_ledger_path(repo_id: str) -> Path:
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(repo_id) / _FINDINGS_LEDGER_FILENAME


def _read_findings_rows(repo_id: str) -> dict:
    """Lock-free snapshot of the raw ``{match_key: row}`` map; fails open to {}."""
    try:
        raw = json.loads(_findings_ledger_path(repo_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    rows = raw.get("rows") if isinstance(raw, dict) else None
    return rows if isinstance(rows, dict) else {}


def _write_findings_rows(repo_id: str, rows: dict) -> None:
    path = _findings_ledger_path(repo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"rows": rows}, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _update_findings_rows(repo_id: str, mutate) -> None:
    """Load-mutate-save the whole rows map under one flock, mirroring
    core/idiom_store.py's per-write discipline: the read, the mutation, and
    the atomic replace all happen while the lock is held, so a concurrent
    writer never observes (or clobbers) a half-applied batch."""
    from chameleon_mcp.locks import acquire_advisory_lock

    path = _findings_ledger_path(repo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
        rows = _read_findings_rows(repo_id)
        mutate(rows)
        _write_findings_rows(repo_id, rows)


def _passes_surface_bar(finding) -> bool:
    """Built-in default surface bar (spec section 7.1, hardcoded this phase):
    medium and above surface even unverified; low surfaces only once
    independently confirmed."""
    if finding.severity == "low":
        return finding.verified == "confirmed"
    return True


def _record_findings_check_event(repo_id: str, status: str, *, reason: str | None = None) -> None:
    try:
        from chameleon_mcp.exec_log import append_check_event

        append_check_event(
            repo_id, session_id="", check="findings_ledger", status=status, reason=reason
        )
    except Exception:
        pass


def record_findings(repo_id: str, ws_root, findings) -> None:
    """Persist canonical Finding rows, applying the surface bar at write time.

    Each finding is stored keyed by its ``match_key`` (a later finding
    sharing the same match_key overwrites the earlier row -- the
    cross-session recurrence identity ``core/finding.py`` defines). A
    finding below the surface bar (see ``_passes_surface_bar``) is stored
    ``shelved`` instead of whatever status it arrived with, and the batch's
    shelf count is recorded as a check event so the shelf stays visible
    without ever reaching a Stop surface. ``ws_root`` is stamped on every
    row so a later scoped read (``undelivered_findings``,
    ``recheck_and_resurface``) never crosses workspace boundaries in a
    shared-repo_id monorepo. No-op on an empty repo_id or an empty/invalid
    finding list.
    """
    items = [f for f in (findings or []) if getattr(f, "match_key", None)]
    if not repo_id or not items:
        return
    root = str(ws_root) if ws_root else ""
    shelved = 0

    def _mutate(rows: dict) -> None:
        nonlocal shelved
        for f in items:
            row = f.to_dict()
            if not _passes_surface_bar(f):
                row["status"] = "shelved"
                shelved += 1
            row["ws_root"] = root
            rows[f.match_key] = row

    _update_findings_rows(repo_id, _mutate)
    if shelved:
        _record_findings_check_event(repo_id, "shelved", reason=f"count={shelved}")


def undelivered_findings(repo_id: str, *, ws_roots) -> list:
    """Pending rows scoped to ``ws_roots``, oldest first.

    ``resurfaced`` rows are deliberately EXCLUDED: a resurfaced finding
    already got its one-shot inline re-nag from ``recheck_and_resurface``,
    and that emission is meant to be its SOLE re-appearance. If this
    function returned resurfaced rows too, an ordinary delivery pass
    (UserPromptSubmit's ``deliver_pending_findings``, SessionStart's
    ``deliver_dead_session_findings``, or the job's own cached-payload
    render) would hand the row's match_key to ``mark_delivered``, flipping
    it back to ``delivered`` -- and the NEXT Stop's ``recheck_and_resurface``
    treats ``delivered`` as open-and-eligible, so it would resurface the
    same finding again, forever. Excluding it here (paired with
    ``mark_delivered`` refusing a resurfaced row as a source state) makes
    ``resurfaced`` a true terminal status for ordinary delivery.

    Scoping mirrors the pre-existing judge_findings ledger's ws_root
    discipline (drift/observations.py): a shared repo_id can span several
    monorepo workspaces, and a finding's file is relative to the root that
    persisted it, so an unscoped read would hand one workspace's findings
    to another's delivery pass. An empty/falsy ``ws_roots`` disables
    scoping (every workspace's rows are returned) -- callers that know
    their scope always pass it.
    """
    from chameleon_mcp.core.finding import Finding

    if not repo_id:
        return []
    roots = {str(r) for r in (ws_roots or []) if r}
    out = []
    for row in _read_findings_rows(repo_id).values():
        if not isinstance(row, dict) or row.get("status") != "pending":
            continue
        if roots and str(row.get("ws_root") or "") not in roots:
            continue
        try:
            out.append(Finding.from_dict(row))
        except Exception:
            continue
    out.sort(key=lambda f: f.created_at)
    return out


def _transition_status(repo_id: str, match_keys, allowed_from, new_status: str) -> int:
    """Move each row in ``match_keys`` currently in ``allowed_from`` (any
    status, when ``allowed_from`` is None) to ``new_status``. Unknown or
    already-transitioned keys are skipped silently -- a stale or
    double-delivered key is not an error. Returns the count actually moved.
    """
    keys = {k for k in (match_keys or []) if isinstance(k, str) and k}
    if not repo_id or not keys:
        return 0
    moved = 0

    def _mutate(rows: dict) -> None:
        nonlocal moved
        for mk in keys:
            row = rows.get(mk)
            if not isinstance(row, dict):
                continue
            if allowed_from is not None and row.get("status") not in allowed_from:
                continue
            row["status"] = new_status
            moved += 1

    _update_findings_rows(repo_id, _mutate)
    return moved


def mark_delivered(repo_id: str, match_keys) -> None:
    """pending -> delivered.

    ``resurfaced`` is deliberately NOT an accepted source state: it is a
    terminal status for ordinary delivery (see ``undelivered_findings``), so
    a resurfaced row's match_key reaching this function (it should not, since
    ``undelivered_findings`` never returns one -- this is defense in depth)
    is a no-op rather than a transition back to ``delivered``, which would
    re-arm the row for another resurface next Stop.

    Advances the repo-keyed delivery cursor (spec section 3.5: cursors are
    keyed by repo_id, not session) whenever at least one row actually
    transitioned, so a later reader can tell delivery happened and when.
    """
    moved = _transition_status(repo_id, match_keys, {"pending"}, "delivered")
    if moved:
        try:
            from chameleon_mcp.core.session_state import update_delivery_cursor

            update_delivery_cursor(repo_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        except Exception:
            pass


def mark_addressed(repo_id: str, match_keys) -> None:
    """Any open status -> addressed. Idempotent: a row already addressed or
    expired is left alone by the allowed-from set."""
    _transition_status(repo_id, match_keys, set(_OPEN_STATUSES), "addressed")


def mark_resurfaced(repo_id: str, match_keys) -> None:
    """pending|delivered -> resurfaced -- the one-shot HIGH re-nag."""
    _transition_status(repo_id, match_keys, {"pending", "delivered"}, "resurfaced")


def recheck_and_resurface(repo_id: str, ws_root) -> list:
    """Canonical-row successor to the retired ``stop/gates.py``
    ``_ledger_recheck_and_resurface`` (drift.db's judge_findings table). This
    was written as a NEW function rather than an in-place port of the legacy
    one, since the two read disjoint stores (drift.db there, this module's
    findings_ledger.json here); ``stop/pipeline.py``'s ``stop_gates`` calls
    this function directly (the legacy one and its backing store were
    retired once the switchover landed).

    A resurfaced row is TERMINAL for ordinary delivery: ``undelivered_findings``
    never returns a ``resurfaced`` row again, and ``mark_delivered`` refuses
    one as a source state, so the ``<chameleon-context>`` block this function
    returns is the finding's SOLE re-appearance -- it cannot loop back through
    UserPromptSubmit/SessionStart delivery and re-arm itself for another nag.

    At Stop, before this turn's own findings persist: every open
    (pending/delivered/resurfaced) row scoped to ``ws_root`` is re-checked.
    A row whose pinned excerpt no longer matches the file's current content
    at that location -- or whose file is gone -- is marked addressed (the
    reviewed content moved, so re-nagging is noise); a row with no pinned
    excerpt is left as-is (staleness is never fabricated from data
    absence). A fileless row that is not high/blocker severity is
    addressed too (it has no anchor to ever re-check against, so leaving it
    open would clog the recheck window forever). An unaddressed
    high/blocker-severity row still open resurfaces exactly once
    (pending/delivered -> resurfaced); a row already resurfaced and still
    unchanged is left alone -- no second nag. Returns the advisory lines,
    or [] when nothing resurfaces. Fail-open to [].
    """
    if not repo_id:
        return []
    try:
        from chameleon_mcp.judge import _excerpt_sha_stale
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context
        from chameleon_mcp.stop.verify import _contained_rel, _excerpt_window

        root = str(ws_root)
        root_path = Path(root)
        resurfaced_rows: list[dict] = []

        def _mutate(rows: dict) -> None:
            for row in rows.values():
                if not isinstance(row, dict) or row.get("ws_root") != root:
                    continue
                status = row.get("status")
                if status not in _OPEN_STATUSES:
                    continue
                file_ = row.get("file") or ""
                has_file = bool(file_)
                if has_file:
                    try:
                        contained = _contained_rel(root_path, file_)
                        exists = contained is not None and (root_path / contained).is_file()
                    except OSError:
                        exists = False
                    if not exists:
                        row["status"] = "addressed"
                        continue
                    pinned_sha = row.get("excerpt_sha") or ""
                    if pinned_sha:
                        span = row.get("span") or [0, 0]
                        line = (
                            span[0]
                            if isinstance(span, list)
                            and span
                            and isinstance(span[0], int)
                            and span[0] > 0
                            else None
                        )
                        current = _excerpt_window(root_path, file_, line)
                        if _excerpt_sha_stale(pinned_sha, current):
                            row["status"] = "addressed"
                            continue
                severity = row.get("severity")
                is_high = severity in _HIGH_SEVERITIES
                if not has_file and not is_high:
                    row["status"] = "addressed"
                    continue
                if status in ("pending", "delivered") and is_high:
                    row["status"] = "resurfaced"
                    resurfaced_rows.append(row)

        _update_findings_rows(repo_id, _mutate)
        if not resurfaced_rows:
            return []
        lines = [
            f"[🦎 chameleon: {len(resurfaced_rows)} unaddressed high-severity finding(s) "
            "from a previous turn's review, surfaced once more]",
            "Advisory; verify each before acting -- they may be wrong, or already handled.",
        ]
        for row in resurfaced_rows[:_RESURFACE_MAX_LINES]:
            file_ = row.get("file") or ""
            loc = sanitize_for_chameleon_context(str(file_)) if file_ else "?"
            span = row.get("span") or [0, 0]
            if isinstance(span, list) and span and isinstance(span[0], int) and span[0] > 0:
                loc += f":{span[0]}"
            lens = row.get("source_lens") or "?"
            lines.append(f"- {loc} ({sanitize_for_chameleon_context(str(lens))})")
        return lines
    except Exception:
        return []


def _finding_from_legacy_pending(raw: dict):
    """Adapt one legacy ``.judge_pending.<sid>.json`` finding entry (the
    old next-turn delivery payload's shape -- file/line/message/
    confidence/verify/excerpt_sha/suggested_fix/evidence_cmds) into a
    canonical Finding, or None when the entry has no usable message. Every
    such entry was correctness-lens output (the only lens that ever wrote
    that file), so ``kind``/``source_lens`` are fixed accordingly; severity
    reuses the legacy confidence->high/medium split, matching what
    ``stop/gates.py``'s pre-canonical ledger used for the same data.
    """
    from chameleon_mcp.core.finding import Finding, compute_match_key

    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    file_ = raw.get("file")
    file_ = file_ if isinstance(file_, str) else ""
    line = raw.get("line")
    line = line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else 0
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    severity = "high" if confidence >= 0.7 else "medium"
    verify_raw = raw.get("verify")
    verified = verify_raw if verify_raw in ("confirmed", "unverified", "refuted") else "unverified"
    excerpt_sha = raw.get("excerpt_sha")
    excerpt_sha = excerpt_sha if isinstance(excerpt_sha, str) else ""
    try:
        return Finding(
            # Pin id to the match_key (the convention every other Finding
            # adapter follows), so a migrated finding's identity is stable and
            # equal to what __post_init__ derives -- not a random uuid.
            id=compute_match_key(message, file_, "correctness"),
            kind="correctness",
            severity=severity,
            confidence=confidence,
            file=file_,
            span=(line, line),
            claim=message,
            evidence="",
            excerpt_sha=excerpt_sha,
            excerpt="",
            source_lens="correctness",
            status="pending",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            verified=verified,
        )
    except ValueError:
        return None


def migrate_pending_queue(repo_id: str, ws_root) -> dict:
    """One-time migration of the legacy async-judge pending queue
    (``.judge_pending.<session-marker>.json``, the old next-turn
    correctness-finding delivery staging file) into canonical ledger rows
    (spec section 9). Each surviving finding enters the ledger as
    ``pending``: the file's whole reason to exist was that the user had not
    seen these findings yet, so "pending" (not yet delivered) -- not
    "delivered" -- is what makes them reachable by a later
    ``undelivered_findings`` read. Every legacy file found is deleted
    whether or not it parsed, matching the file's original
    one-shot-consumption contract at its old read site. Findings still go
    through ``record_findings``'s surface bar, so a stale/low-confidence
    legacy row shelves exactly like a freshly-produced one.

    Returns ``{"files": int, "findings": int}``; both 0 on any failure or
    when no legacy files exist. Fail-open, never raises.
    """
    if not repo_id:
        return {"files": 0, "findings": 0}
    try:
        from chameleon_mcp.profile.trust import repo_data_dir

        paths = sorted(repo_data_dir(repo_id).glob(".judge_pending.*.json"))
    except OSError:
        return {"files": 0, "findings": 0}
    if not paths:
        return {"files": 0, "findings": 0}

    files_done = 0
    total_findings = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            payload = None
        findings = []
        if isinstance(payload, dict):
            for raw in payload.get("findings") or []:
                if isinstance(raw, dict):
                    f = _finding_from_legacy_pending(raw)
                    if f is not None:
                        findings.append(f)
        if findings:
            record_findings(repo_id, ws_root, findings)
            total_findings += len(findings)
        try:
            path.unlink()
        except OSError:
            pass
        files_done += 1
    return {"files": files_done, "findings": total_findings}
