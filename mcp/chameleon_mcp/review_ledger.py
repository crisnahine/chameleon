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
    - ``rules`` — per rule (sorted): ``overrides``, ``would_blocks``,
      ``blanket`` (bare-directive overrides), ``distinct_files``,
      ``distinct_sessions``, ``override_rate`` (overrides / (overrides +
      would_blocks), or None below the min-events floor), ``high_override_rate``
      (rate at or above the threshold over enough events), and ``blanket_abuse``
      (the override share that came from bare directives is high).
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


def _would_block_counts(repo_id: str, window_days: int) -> dict[str, int]:
    """Per-rule would-block counts from the shadow metrics log.

    Reuses the shadow report's aggregation so the override rate is measured
    against the same would-block numbers the shadow surface shows. Empty on any
    failure.
    """
    try:
        from chameleon_mcp.shadow_report import build_shadow_report

        report = build_shadow_report(repo_id, window_days)
        rules = report.get("rules") or {}
        return {rule: int(meta.get("would_blocks", 0)) for rule, meta in rules.items()}
    except Exception:
        return {}


# --- PR-review ledger ----------------------------------------------------------
#
# An append-only, HMAC-signed NDJSON file per repo, recording every review run.
# Storage mirrors the exec log's model (per-repo dir under the owner-checked 0700
# plugin-data root) but lives under PLUGIN_DATA, not TMPDIR: a review verdict is
# durable provenance a lead reaches back for, not transient session state.

_LEDGER_FILENAME = "review_ledger.ndjson"

# Verdict vocabulary the skill writes. APPROVE / FIX / BLOCK mirror the review
# severities; anything else a caller passes is stored verbatim but never reads
# as a shipped-over-BLOCK case in the panel.
_BLOCK_VERDICT = "BLOCK"


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
        "verdict": str(verdict),
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
        if r.get("verdict") == _BLOCK_VERDICT and r.get("commit_sha")
    ]
    shipped = _shas_merged_into_head(repo_id, block_shas)
    shipped_over_block = [
        {"commit_sha": r.get("commit_sha"), "ts": r.get("ts")}
        for r in records
        if r.get("verdict") == _BLOCK_VERDICT and r.get("commit_sha") in shipped
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
