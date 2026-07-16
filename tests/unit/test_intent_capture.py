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
    extract_scope_lines,
    read_intent,
    reap_stale_prefixed,
    recent_excerpts,
    scope_lines,
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
    assert set(e) == {
        "ts",
        "prompt_digest",
        "secret_suppressed",
        "tokens",
        "security",
        "scope_lines",
    }
    assert isinstance(e["ts"], float)
    assert len(e["prompt_digest"]) == 16
    assert e["tokens"]["numerals"] == ["25"]
    assert e["tokens"]["identifiers"] == ["retryLimit"]
    assert e["security"] is False
    assert e["scope_lines"] == []


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


# --- extract_scope_lines: the intent contract's verbatim scope-line channel ----


def test_extract_scope_lines_returns_matching_sentences_verbatim():
    # Split is on [.!?\n] only (no comma), so two scoping directives joined
    # by a period are two independent sentences, each kept byte-for-byte
    # (stripped, never paraphrased or reworded).
    text = "Don't touch the auth module. Only change the retry count."
    assert extract_scope_lines(text) == [
        "Don't touch the auth module",
        "Only change the retry count",
    ]


def test_extract_scope_lines_comma_joined_clauses_are_one_sentence():
    # The same two directives joined by a comma instead of a period have no
    # [.!?\n] delimiter between them, so they split as ONE sentence and
    # persist as one scope line, not two -- the split rule is exactly
    # [.!?\n], never comma-aware.
    text = "don't touch the auth module, only change the retry count"
    assert extract_scope_lines(text) == [text]


def test_extract_scope_lines_no_scoping_phrase_returns_empty():
    assert extract_scope_lines("please tidy up the docs and fix typos") == []


def test_extract_scope_lines_empty_and_non_string_input():
    assert extract_scope_lines("") == []
    assert extract_scope_lines(None) == []  # type: ignore[arg-type]


def test_extract_scope_lines_covers_every_owner_approved_phrase():
    cases = {
        "do not touch the migration files": "do not touch",
        "only modify the docstring": "only modify",
        "only edit the config": "only edit",
        "leave the tests alone": "leave...alone",
        "don't modify the schema": "don't modify",
        "don't edit the lockfile": "don't edit",
        "don't change the public API": "don't change",
        "keep the endpoint as is": "keep...as is",
        "keep the endpoint as-is": "keep...as-is",
        "without changing the schema": "without changing",
        "without touching the tests": "without touching",
        "must not break backward compatibility": "must not",
    }
    for text, label in cases.items():
        assert extract_scope_lines(text) == [text], f"phrase class {label!r} did not match"


def test_extract_scope_lines_case_insensitive():
    assert extract_scope_lines("DO NOT TOUCH the auth module") == ["DO NOT TOUCH the auth module"]


def test_extract_scope_lines_over_long_line_truncated(monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_SCOPE_LINE_MAX_CHARS", "20")
    text = "don't touch the entire legacy authentication subsystem end to end"
    out = extract_scope_lines(text)
    assert len(out) == 1
    assert len(out[0]) == 20
    # Truncation keeps a verbatim PREFIX -- never rewritten, just shortened.
    assert text.startswith(out[0])


def test_extract_scope_lines_over_count_capped(monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_SCOPE_LINES_MAX", "3")
    text = ". ".join(f"must not break rule {n}" for n in range(10)) + "."
    out = extract_scope_lines(text)
    assert len(out) == 3
    assert out == ["must not break rule 0", "must not break rule 1", "must not break rule 2"]


def test_extract_scope_lines_hard_secret_line_dropped(tmp_path):
    text = f'don\'t touch the deploy key "{AWS_KEY}"'
    out = extract_scope_lines(text)
    assert out == []


def test_extract_scope_lines_credential_shaped_line_dropped(monkeypatch):
    # Isolate the credential-SHAPE gate from the hard-secret scanner: force
    # _has_hard_secret to False so only _looks_credential_shaped can drop
    # this line, proving the extractor really runs both gates independently.
    monkeypatch.setattr(intent_capture, "_has_hard_secret", lambda text: False)
    overlong_pat = "ghp_" + "A1b2" * 10
    text = f"{overlong_pat} must not leak into logs"
    out = extract_scope_lines(text)
    assert out == []


def test_extract_scope_lines_disabled_via_intent_contract_env(monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_CONTRACT", "0")
    assert extract_scope_lines("don't touch the auth module, only change the retry count") == []


def test_extract_scope_lines_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CHAMELEON_INTENT_CONTRACT", raising=False)
    assert extract_scope_lines("don't touch the auth module") == ["don't touch the auth module"]


# --- extract_scope_lines: clause tightening around a run-on ---------------------
#
# The regex still matches against the FULL sentence (so a phrase span that
# crosses commas, like "leave X alone", still matches whole) -- but the
# PERSISTED text is now a bounded clause around the match, not the whole
# sentence, so unrelated prose glued on by a punctuation-free run-on no
# longer rides along.


def test_extract_scope_lines_run_on_tightens_to_clause_dropping_unrelated_prose():
    text = (
        "please help me refactor the payment flow and while you are at it "
        "do not touch the legacy billing reconciliation job because it is "
        "fragile and has been in production for six years"
    )
    out = extract_scope_lines(text)
    assert out == ["while you are at it do not touch the legacy billing reconciliation job"]
    assert "refactor the payment flow" not in out[0]
    assert "because it is fragile" not in out[0]


def test_extract_scope_lines_plain_no_conjunction_prompt_unchanged():
    # No conjunction and no reason marker means both bounds default to the
    # sentence edges -- output is identical to the pre-tightening behavior.
    text = "please do not touch the auth module"
    assert extract_scope_lines(text) == [text]


# --- extract_scope_lines: the object-preservation safety invariant -------------
#
# For every phrase form the regex matches, the scope OBJECT must survive the
# new clause-narrowing -- this is the property a reviewer would hunt for a
# regression in.


def test_extract_scope_lines_object_after_phrase_kept():
    # object AFTER the phrase, no reason marker -> right bound is the
    # sentence end, so the object rides along untouched.
    text = "do not touch the payments module"
    out = extract_scope_lines(text)
    assert out == [text]
    assert "the payments module" in out[0]


def test_extract_scope_lines_object_within_phrase_span_kept():
    # object WITHIN the phrase's own match span ("leave ... alone",
    # "keep ... as is") -> the whole match span is never a cut candidate.
    assert extract_scope_lines("leave the payments module alone") == [
        "leave the payments module alone"
    ]
    assert extract_scope_lines("keep the retry count as is") == ["keep the retry count as is"]


def test_extract_scope_lines_object_before_phrase_via_comma_kept():
    # object BEFORE the phrase, joined by a comma -> a comma is never a left
    # boundary, so the preceding object is not cut.
    text = "the auth module, leave it alone"
    out = extract_scope_lines(text)
    assert out == [text]
    assert "the auth module" in out[0]


def test_extract_scope_lines_compound_object_kept():
    # compound object joined by "and" -> "and"/"or"/"but" are never right
    # boundaries, so neither half of the compound object is dropped.
    text = "don't touch billing and the retry logic"
    out = extract_scope_lines(text)
    assert out == [text]
    assert "billing and the retry logic" in out[0]


def test_extract_scope_lines_multi_comma_phrase_kept():
    # the phrase's own match span crosses two commas -> the whole span,
    # commas included, is never split.
    text = "keep the retry count, which is 3, as is"
    assert extract_scope_lines(text) == [text]


def test_extract_scope_lines_run_on_secret_still_dropped():
    # A secret inside the NARROWED clause (not the discarded leading prose)
    # must still trip the hard-secret gate and be dropped.
    text = f'refactor the flow and don\'t touch the deploy key "{AWS_KEY}" because it is sensitive'
    assert extract_scope_lines(text) == []


def test_extract_scope_lines_char_cap_applies_after_clause_narrowing(monkeypatch):
    # The char cap must truncate the NARROWED clause, not the raw sentence:
    # the dropped leading prose must never reappear via the cap's prefix.
    monkeypatch.setenv("CHAMELEON_INTENT_SCOPE_LINE_MAX_CHARS", "20")
    text = (
        "please refactor the payment flow and don't touch the entire legacy "
        "authentication subsystem end to end because it is fragile"
    )
    out = extract_scope_lines(text)
    assert len(out) == 1
    assert len(out[0]) == 20
    assert "refactor the payment flow" not in out[0]
    clause = "don't touch the entire legacy authentication subsystem end to end"
    assert clause.startswith(out[0])


def test_extract_scope_lines_multiple_matches_in_one_sentence_dedup():
    # Two distinct matches in one sentence, each bounded by the other's
    # conjunction, both widen back out to the same full clause and dedup to
    # a single entry (order-preserving).
    text = "don't touch the auth module, only change the retry count"
    assert extract_scope_lines(text) == [text]


def test_extract_scope_lines_pathological_input_completes_quickly():
    # Thousands of left-boundary words before the match and thousands of
    # right-boundary words after it -- both scans must stay linear, not
    # blow up on an adversarial input.
    text = ("and " * 5000) + "don't touch the module" + (" because" * 5000)
    start = time.monotonic()
    out = extract_scope_lines(text)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0
    assert out == ["don't touch the module"]


# --- capture_intent: scope_lines persistence ------------------------------------


def test_capture_intent_persists_scope_lines(tmp_path):
    capture_intent(tmp_path, SID, "Don't touch the auth module. Please tidy the docs.")
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["scope_lines"] == ["Don't touch the auth module"]


def test_capture_intent_no_scoping_phrase_persists_empty_scope_lines(tmp_path):
    capture_intent(tmp_path, SID, "please tidy the docs")
    entries = read_intent(tmp_path, SID)
    assert entries[0]["scope_lines"] == []


def test_capture_intent_suppressed_prompt_persists_zero_scope_lines(tmp_path):
    # The prompt names an explicit scoping phrase ("don't touch") AND carries
    # a hard secret. The suppressed branch must win completely: zero tokens
    # AND zero scope lines, never a partial persist of the scoping sentence.
    capture_intent(tmp_path, SID, f'don\'t touch the deploy key "{AWS_KEY}"')
    entries = read_intent(tmp_path, SID)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is True
    assert entries[0]["tokens"] == {}
    assert entries[0]["scope_lines"] == []
    raw = _intent_path(tmp_path).read_text(encoding="utf-8")
    assert AWS_KEY not in raw


def test_capture_intent_disabled_intent_contract_persists_empty_scope_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_INTENT_CONTRACT", "0")
    capture_intent(tmp_path, SID, "don't touch the auth module")
    entries = read_intent(tmp_path, SID)
    assert entries[0]["scope_lines"] == []


# --- scope_lines / recent_excerpts readers --------------------------------------


def test_scope_lines_reader_flattens_and_dedupes(tmp_path):
    capture_intent(tmp_path, SID, "don't touch the auth module")
    capture_intent(tmp_path, SID, "don't touch the auth module. only change the retry count.")
    out = scope_lines(read_intent(tmp_path, SID))
    assert out == ["don't touch the auth module", "only change the retry count"]


def test_scope_lines_reader_since_ts_filtering(tmp_path):
    capture_intent(tmp_path, SID, "don't touch the old module")
    cut = time.time()
    time.sleep(0.01)
    capture_intent(tmp_path, SID, "don't touch the new module")
    out = scope_lines(read_intent(tmp_path, SID), since_ts=cut)
    assert out == ["don't touch the new module"]


def test_scope_lines_reader_skips_suppressed_entries(tmp_path):
    capture_intent(tmp_path, SID, f'don\'t touch the deploy key "{AWS_KEY}"')
    capture_intent(tmp_path, SID, "don't touch the auth module")
    out = scope_lines(read_intent(tmp_path, SID))
    assert out == ["don't touch the auth module"]


def test_scope_lines_reader_missing_field_fails_open(tmp_path):
    # An entry captured before this field existed has no "scope_lines" key at
    # all; the reader must skip it silently, never raise.
    path = _intent_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": time.time(),
                    "prompt_digest": "0" * 16,
                    "secret_suppressed": False,
                    "tokens": {"numerals": [], "identifiers": [], "quoted": []},
                    "security": False,
                }
            )
            + "\n"
        )
    assert scope_lines(read_intent(tmp_path, SID)) == []


def test_recent_excerpts_returns_same_verbatim_lines_as_scope_lines(tmp_path):
    capture_intent(tmp_path, SID, "don't touch the auth module")
    entries = read_intent(tmp_path, SID)
    assert recent_excerpts(entries) == scope_lines(entries) == ["don't touch the auth module"]


# --- extract_scope_lines: identifier-internal dots are not sentence boundaries -


def test_extract_scope_lines_keeps_dotted_filename_object():
    # A period inside a filename/module/version is part of the scope object,
    # not a sentence boundary -- splitting there would drop the extension.
    assert extract_scope_lines("don't touch config.json") == ["don't touch config.json"]
    assert extract_scope_lines("leave package.json alone") == ["leave package.json alone"]
    assert extract_scope_lines("do not touch app.py or db.py") == ["do not touch app.py or db.py"]
    assert extract_scope_lines("keep v1.2.3 as is") == ["keep v1.2.3 as is"]
    assert extract_scope_lines("do not modify foo.bar.baz") == ["do not modify foo.bar.baz"]


def test_extract_scope_lines_real_sentence_period_still_splits():
    # A period that is a genuine sentence terminator (flanked by a space or the
    # end, not two word chars) still splits, so unrelated prose in a separate
    # sentence is dropped and a dotted object in the scope sentence survives.
    assert extract_scope_lines("please fix the login bug. do not touch config.json") == [
        "do not touch config.json"
    ]
    assert extract_scope_lines("don't touch the auth module. it is fragile") == [
        "don't touch the auth module"
    ]
    # A filename ending the sentence keeps its extension (the trailing period
    # is a terminator; the identifier-internal one is not).
    assert extract_scope_lines("do not touch config.json.") == ["do not touch config.json"]
