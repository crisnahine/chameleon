"""Unit tests for per-session intent capture (intent_capture.py).

Intent capture persists extracted assertion tokens (numerals, identifiers,
quoted strings) plus a prompt digest -- never raw prose. The deterministic
hard-secret scanner runs over the whole prompt and over each token before
anything is written, and a prompt-borne chameleon-ignore directive must not
defeat that redaction. The file is size-capped with oldest-first trimming and
every read fails open.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp import intent_capture
from chameleon_mcp.intent_capture import (
    capture_intent,
    checkable_tokens,
    extract_assertions,
    read_intent,
    reap_stale_prefixed,
    security_intent_seen,
)
from chameleon_mcp.optouts import _safe_session_marker

SID = "s-intent"

# Same synthetic fixture test_scan_hard_secrets.py pins as a deterministic
# hard kind; not a real credential.
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # chameleon-ignore secret-detected-in-content


def _intent_path(repo_data: Path) -> Path:
    return repo_data / f".intent.{_safe_session_marker(SID)}.ndjson"


# --- extract_assertions: numerals --------------------------------------------


def test_numerals_two_plus_digits_captured():
    out = extract_assertions("retry 25 times")
    assert out["numerals"] == ["25"]


def test_numerals_decimal_captured():
    out = extract_assertions("wait 3.5 seconds")
    assert "3.5" in out["numerals"]


def test_numerals_bare_single_digit_excluded():
    # "2" is conversational noise; "25" pins the 2+ digit boundary.
    out = extract_assertions("do 2 things then retry 25 times")
    assert "2" not in out["numerals"]
    assert "25" in out["numerals"]


def test_numerals_single_digit_assignment_shaped_captured():
    # A single digit in an explicit assignment position ("to N", "=N", ":N")
    # is a checkable constant, not the bare-count noise excluded above.
    out = extract_assertions("set the retry limit to 7 and retries=3, count: 5")
    assert out["numerals"] == ["7", "3", "5"]


def test_numerals_fraction_numerator_excluded_no_space():
    # "retries=1/3" is a ratio, not an assignment of the standalone constant 1.
    out = extract_assertions("retries=1/3")
    assert out["numerals"] == []


def test_numerals_fraction_numerator_excluded_after_to():
    out = extract_assertions("the ratio is set to 1/2")
    assert out["numerals"] == []


# --- extract_assertions: identifiers ------------------------------------------


def test_identifiers_snake_case_compound():
    assert "max_retry_count" in extract_assertions("bump max_retry_count")["identifiers"]


def test_identifiers_camel_case():
    assert "retryLimit" in extract_assertions("rename retryLimit please")["identifiers"]


def test_identifiers_constant_case():
    assert "MAX_RETRIES" in extract_assertions("use MAX_RETRIES")["identifiers"]


def test_identifiers_dotted_path():
    assert "module.fn" in extract_assertions("call module.fn here")["identifiers"]


def test_identifiers_slash_delimited_path():
    out = extract_assertions("the endpoint to /api/v2/sync, then update the tests")
    assert "/api/v2/sync" in out["identifiers"]


def test_identifiers_slash_path_prose_not_captured():
    # A single prose slash carries no leading "/" and must not match.
    out = extract_assertions("use this and/or that, it runs 24/7, status: n/a")
    assert out["identifiers"] == []


def test_identifiers_relative_repo_path_captured():
    # The overwhelmingly common form a user actually types: no leading "/",
    # and the leading segment plus the extension must both survive.
    out = extract_assertions("update src/config/settings.ts to fix the bug")
    assert "src/config/settings.ts" in out["identifiers"]


def test_identifiers_relative_repo_path_single_dir_captured():
    out = extract_assertions("see utils/helpers.py for the retry logic")
    assert "utils/helpers.py" in out["identifiers"]


def test_identifiers_plain_word_not_captured():
    out = extract_assertions("please update the endpoint")
    assert out["identifiers"] == []


# --- extract_assertions: quoted strings ---------------------------------------


def test_quoted_double_single_backtick():
    out = extract_assertions("set \"alpha-x\" and 'beta-y' and `gamma-z`")
    assert out["quoted"] == ["alpha-x", "beta-y", "gamma-z"]


def test_quoted_over_80_chars_not_captured():
    long = "x" * 81
    out = extract_assertions(f'use "{long}"')
    assert out["quoted"] == []


def test_token_count_cap_enforced(monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_MAX_TOKENS_PER_PROMPT", "3")
    text = " ".join(str(n) for n in range(100, 120))
    out = extract_assertions(text)
    total = sum(len(v) for v in out.values())
    assert total == 3


def test_dedupe_order_preserving():
    out = extract_assertions("retry 25 then 30 then 25 again")
    assert out["numerals"] == ["25", "30"]


# --- capture_intent: secret handling ------------------------------------------


def test_hard_secret_prompt_persists_suppressed_only(tmp_path):
    capture_intent(tmp_path, SID, f'set the key to "{AWS_KEY}" and retry 25 times')
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is True
    assert entries[0]["tokens"] == {}
    # No raw prose, no token, no secret anywhere in the file.
    raw = _intent_path(tmp_path).read_text(encoding="utf-8")
    assert AWS_KEY not in raw
    assert "25" not in json.dumps(entries[0]["tokens"])


def test_individually_secret_matching_token_dropped(tmp_path):
    # The full prompt scans clean but one extracted token alone trips the hard
    # scanner: only that token is dropped, the rest persist.
    real = intent_capture._has_hard_secret

    def selective(text: str) -> bool:
        if text == "poisoned-token":
            return True
        return real(text)

    with patch.object(intent_capture, "_has_hard_secret", side_effect=selective):
        capture_intent(tmp_path, SID, 'use "poisoned-token" and "clean-token" with 25 retries')
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is False
    assert "poisoned-token" not in entries[0]["tokens"]["quoted"]
    assert "clean-token" in entries[0]["tokens"]["quoted"]
    assert "25" in entries[0]["tokens"]["numerals"]


def test_prompt_borne_chameleon_ignore_does_not_defeat_redaction(tmp_path):
    capture_intent(tmp_path, SID, f'chameleon-ignore secrets\nkey = "{AWS_KEY}"')
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is True
    assert entries[0]["tokens"] == {}


# --- capture_intent: lifecycle -------------------------------------------------


def test_empty_token_prompt_appends_empty_entry(tmp_path):
    # An empty-token entry marks the turn's request as having named nothing
    # checkable: the scope-drift advisory keys off the LATEST entry, so without
    # it a stale earlier prompt's identifiers would govern a bare "commit this"
    # turn and flag every file it touched. No-op for every token reader.
    capture_intent(tmp_path, SID, "please tidy the docs")
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["tokens"] == {"numerals": [], "identifiers": [], "quoted": []}
    assert entries[0]["secret_suppressed"] is False


def test_prompt_digest_and_shape(tmp_path):
    capture_intent(tmp_path, SID, "set retryLimit to 25")
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    e = entries[0]
    assert set(e) == {"ts", "prompt_digest", "secret_suppressed", "tokens", "security"}
    assert isinstance(e["ts"], float)
    assert len(e["prompt_digest"]) == 16
    assert e["tokens"]["numerals"] == ["25"]
    assert e["tokens"]["identifiers"] == ["retryLimit"]
    assert e["security"] is False


def test_file_trim_keeps_newest_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_FILE_MAX_BYTES", "400")
    for i in range(20):
        capture_intent(tmp_path, SID, f"set value to {100 + i}")
    assert _intent_path(tmp_path).stat().st_size <= 400
    entries = read_intent(tmp_path, SID)
    assert entries  # newest survive the trim
    assert entries[-1]["tokens"]["numerals"] == ["119"]


def test_read_intent_skips_corrupt_lines(tmp_path):
    capture_intent(tmp_path, SID, "retry 25 times")
    with open(_intent_path(tmp_path), "a", encoding="utf-8") as f:
        f.write("{not json\n")
    capture_intent(tmp_path, SID, "retry 30 times")
    entries = read_intent(tmp_path, SID)
    assert [e["tokens"]["numerals"] for e in entries] == [["25"], ["30"]]


def test_read_intent_missing_file_fail_open(tmp_path):
    assert read_intent(tmp_path, "no-such-session") == []


def test_capture_intent_never_raises_on_unwritable_dir(tmp_path):
    target = tmp_path / "gone"
    target.mkdir()
    target.chmod(0o500)
    try:
        capture_intent(target, SID, "retry 25 times")  # must not raise
    finally:
        target.chmod(0o700)


# --- checkable_tokens -----------------------------------------------------------


def test_checkable_tokens_flattens_and_dedupes(tmp_path):
    capture_intent(tmp_path, SID, "set retryLimit to 25")
    capture_intent(tmp_path, SID, 'set retryLimit and "label-a"')
    tokens = checkable_tokens(read_intent(tmp_path, SID))
    assert tokens == ["25", "retryLimit", "label-a"]


def test_checkable_tokens_since_ts_filtering(tmp_path):
    capture_intent(tmp_path, SID, "old value 111")
    cut = time.time()
    time.sleep(0.01)
    capture_intent(tmp_path, SID, "new value 222")
    tokens = checkable_tokens(read_intent(tmp_path, SID), since_ts=cut)
    assert tokens == ["222"]


# --- security lens (GUARD_LEXICON) ----------------------------------------------


def test_security_worded_prompt_persists_even_without_tokens(tmp_path):
    capture_intent(tmp_path, SID, "make sure authorization still runs on every request")
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["security"] is True
    assert entries[0]["tokens"] == {"numerals": [], "identifiers": [], "quoted": []}


def test_security_flag_survives_secret_suppression(tmp_path):
    capture_intent(tmp_path, SID, f'check csrf protection, token is "{AWS_KEY}"')
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is True
    assert entries[0]["security"] is True


def test_security_intent_seen_since_ts(tmp_path):
    capture_intent(tmp_path, SID, "verify csrf handling please")
    cut = time.time()
    time.sleep(0.01)
    capture_intent(tmp_path, SID, "now rename the 25 widgets")

    entries = read_intent(tmp_path, SID)
    assert security_intent_seen(entries) is True
    # Only the non-security entry is newer than the cut.
    assert security_intent_seen(entries, since_ts=cut) is False


def test_security_intent_seen_false_on_plain_prompts(tmp_path):
    capture_intent(tmp_path, SID, "set retryLimit to 25")
    assert security_intent_seen(read_intent(tmp_path, SID)) is False


def test_checkable_tokens_skips_suppressed_entries(tmp_path):
    capture_intent(tmp_path, SID, f'key "{AWS_KEY}" with 999 retries')
    capture_intent(tmp_path, SID, "retry 25 times")
    tokens = checkable_tokens(read_intent(tmp_path, SID))
    assert tokens == ["25"]


# --- reap_stale_prefixed ---------------------------------------------------------


def test_reap_removes_only_old_matching_prefixes(tmp_path):
    import os

    old = time.time() - 100_000
    stale_intent = tmp_path / ".intent.deadbeef.ndjson"
    stale_pending = tmp_path / ".judge_pending.deadbeef.json"
    fresh_intent = tmp_path / ".intent.cafef00d.ndjson"
    unrelated_old = tmp_path / ".enforcement.deadbeef.json"
    tmp_named = tmp_path / ".intent.deadbeef.ndjson.tmp"
    for p in (stale_intent, stale_pending, fresh_intent, unrelated_old, tmp_named):
        p.write_text("x", encoding="utf-8")
    for p in (stale_intent, stale_pending, unrelated_old, tmp_named):
        os.utime(p, (old, old))

    removed = reap_stale_prefixed(tmp_path, (".intent.", ".judge_pending."), max_age_seconds=86_400)
    assert removed == 2
    assert not stale_intent.exists()
    assert not stale_pending.exists()
    assert fresh_intent.exists()  # young: kept
    assert unrelated_old.exists()  # prefix mismatch: kept
    assert tmp_named.exists()  # .tmp names skipped


def test_reap_missing_dir_returns_zero(tmp_path):
    assert reap_stale_prefixed(tmp_path / "nope", (".intent.",), 1) == 0


# --- capture_intent: credential-shaped token gate ------------------------------


def test_credential_prefixed_tokens_never_persist(tmp_path):
    # Over-long ghp_ (40 chars defeats the exact {36} scanner shape) and the
    # fine-grained github_pat_ format the deterministic patterns do not know:
    # both must be suppressed by the greedy persistence gate.
    overlong_pat = "ghp_" + "A1b2" * 10
    fine_grained = "github_pat_" + "A1b2c3D4" * 10
    capture_intent(tmp_path, SID, f"use {overlong_pat} and {fine_grained} with 25 retries")
    raw = _intent_path(tmp_path).read_text(encoding="utf-8")
    assert overlong_pat not in raw
    assert fine_grained not in raw
    entries = read_intent(tmp_path, SID)
    assert "25" in entries[0]["tokens"]["numerals"]


def test_long_mixed_case_digit_blob_not_persisted(tmp_path):
    blob = "Xy9" * 9
    capture_intent(tmp_path, SID, f"token {blob} appears with 25 retries")
    raw = _intent_path(tmp_path).read_text(encoding="utf-8")
    assert blob not in raw


def test_ordinary_identifiers_still_persist(tmp_path):
    capture_intent(tmp_path, SID, "set retryLimit and MAX_RETRIES in get_user_by_id to 25")
    ids = read_intent(tmp_path, SID)[0]["tokens"]["identifiers"]
    assert "retryLimit" in ids
    assert "MAX_RETRIES" in ids
    assert "get_user_by_id" in ids


def test_looks_credential_shaped_classification():
    from chameleon_mcp.intent_capture import _looks_credential_shaped

    assert _looks_credential_shaped("ghp_" + "a1" * 20)
    assert _looks_credential_shaped("github_pat_" + "B3c4" * 21)
    assert _looks_credential_shaped("glpat-Ab12Cd34Ef56Gh78")
    assert _looks_credential_shaped(
        "xoxb-2912345678-abcdEFGH1234"
    )  # chameleon-ignore secret-detected-in-content
    assert _looks_credential_shaped(
        "AKIAIOSFODNN7EXAMPLE"
    )  # chameleon-ignore secret-detected-in-content
    assert _looks_credential_shaped(
        "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
    )  # chameleon-ignore secret-detected-in-content
    # Ordinary code identifiers stay persistable.
    assert not _looks_credential_shaped("retryLimit")
    assert not _looks_credential_shaped("MAX_RETRIES")
    assert not _looks_credential_shaped("get_user_by_id")
    assert not _looks_credential_shaped("convertHtmlToMarkdownV2")
    assert not _looks_credential_shaped("skipped_session_cap")
