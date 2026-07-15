"""Attestation-gated auto-pass (#7): session-governance signals fold RAISE-ONLY
into the auto-pass verdict.

Pins the attestation->diff attribution (by file overlap), the three governance
signals (verify suppressed / judge degraded / override on a diff file), the
over-fire guard (a normal low-risk judge skip must NOT flag), and the raise-only
contract (no attestation match classifies exactly as before).
"""

from __future__ import annotations

from chameleon_mcp.autopass import (
    build_autopass_verdict,
    classify_change,
    session_coverage_from_attestations,
)


def _att(*, governed=(), ungoverned=(), checks=(), overrides=(), env=None):
    return {
        "governed_files": [{"file": f} for f in governed],
        "ungoverned_files": [{"file": f} for f in ungoverned],
        "checks": list(checks),
        "overrides": list(overrides),
        "env": env or {},
    }


# --- session_coverage_from_attestations --------------------------------------


def test_no_records_all_clear():
    sc = session_coverage_from_attestations([], ["a.ts"])
    assert sc == {
        "verify_suppressed": False,
        "judge_degraded": False,
        "overrides_on_diff": False,
        "matched": False,
    }


def test_no_file_overlap_is_no_attribution():
    # The attestation touched other files; it must NOT attribute to this diff.
    rec = _att(governed=["other.ts"], env={"verify_off": True})
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["matched"] is False
    assert sc["verify_suppressed"] is False


def test_verify_off_env_flags_when_file_overlaps():
    rec = _att(governed=["a.ts"], env={"verify_off": True})
    sc = session_coverage_from_attestations([rec], ["a.ts", "b.ts"])
    assert sc["matched"] is True
    assert sc["verify_suppressed"] is True


def test_whole_session_verify_off_no_files_still_attributes():
    # A session that ran entirely under CHAMELEON_VERIFY=0 records NO touched files
    # (both verify + recorder hooks skip), so its file lists are empty. verify_off
    # is session-global, so this must STILL attribute (the feature's headline
    # scenario) -> route to human, not silently auto-pass.
    rec = _att(governed=[], ungoverned=[], env={"verify_off": True})
    sc = session_coverage_from_attestations([rec], ["src/a.ts"])
    assert sc["matched"] is True
    assert sc["verify_suppressed"] is True


def test_verify_off_with_recorded_files_but_no_overlap_does_not_attribute():
    # A verify-off session that DID record files (so overlap is meaningful) but
    # touched none of the diff must NOT attribute -- only the empty-list
    # (whole-session-suppressed) case gets the session-global fallback.
    rec = _att(governed=["other/x.ts"], env={"verify_off": True})
    sc = session_coverage_from_attestations([rec], ["src/a.ts"])
    assert sc["verify_suppressed"] is False


def test_verify_on_empty_record_buys_nothing():
    # Raise-only: a forged clean (verify_off False) empty record never attributes.
    rec = _att(governed=[], ungoverned=[], env={"verify_off": False})
    sc = session_coverage_from_attestations([rec], ["src/a.ts"])
    assert sc["matched"] is False


def test_posttool_verify_skipped_flags():
    rec = _att(
        ungoverned=["a.ts"],
        checks=[{"check": "posttool_verify", "status": "skipped", "reason": "verify_env_off"}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["verify_suppressed"] is True


def test_cooldown_verify_skip_does_NOT_flag():
    # A routine cooldown re-verify skip is normal (the file WAS verified earlier);
    # only the env-off suppression counts, or nearly every diff routes to a human.
    rec = _att(
        governed=["a.ts"],
        checks=[{"check": "posttool_verify", "status": "skipped", "reason": "cooldown"}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["verify_suppressed"] is False


def test_judge_degraded_spawn_flags():
    rec = _att(
        governed=["a.ts"],
        checks=[
            {"check": "correctness_judge", "status": "degraded_spawn", "reason": "spawn_timeout"}
        ],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["judge_degraded"] is True


def test_grounding_reason_degraded_spawn_does_NOT_flag():
    # Pre-2.38.9 attestations misfiled grounding events onto the degraded_spawn
    # channel; `judge_defs_skipped_no_index` means the judge ran HEALTHILY on a
    # repo with no calls index, not a failure. Must not read as degradation.
    for reason in ("judge_defs_skipped_no_index", "judge_facts_none", "judge_transitive_skipped"):
        rec = _att(
            governed=["a.ts"],
            checks=[{"check": "correctness_judge", "status": "degraded_spawn", "reason": reason}],
        )
        sc = session_coverage_from_attestations([rec], ["a.ts"])
        assert sc["judge_degraded"] is False, reason


def test_real_spawn_failure_reasons_still_flag():
    for reason in ("spawn_timeout", "spawn_exec_error", "unparseable_output", "pipeline_error"):
        rec = _att(
            governed=["a.ts"],
            checks=[{"check": "correctness_judge", "status": "degraded_spawn", "reason": reason}],
        )
        sc = session_coverage_from_attestations([rec], ["a.ts"])
        assert sc["judge_degraded"] is True, reason


def test_normal_low_risk_judge_skip_does_NOT_flag():
    # A deliberate low-risk skip is normal, not degradation; flagging it would
    # route nearly every routine diff to a human and defeat auto-pass.
    rec = _att(
        governed=["a.ts"],
        checks=[{"check": "correctness_judge", "status": "skipped", "reason": "low_risk"}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["judge_degraded"] is False


def test_override_on_diff_file_flags():
    # Production shape from session_override_rows: the path is under "file".
    rec = _att(governed=["a.ts"], overrides=[{"file": "a.ts", "rule": "naming", "count": 1}])
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["overrides_on_diff"] is True


# --- post-cutover "review_job" vocabulary (stop/scheduler.py + stop/pipeline.py) ---


def test_review_job_degraded_flags():
    rec = _att(
        governed=["a.ts"],
        checks=[{"check": "review_job", "status": "degraded", "reason": "platform_unavailable"}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["judge_degraded"] is True


def test_review_job_platform_unavailable_status_flags():
    # The literal status the spec names, in case a future caller ever emits it
    # directly rather than riding "degraded"/reason="platform_unavailable".
    rec = _att(
        governed=["a.ts"],
        checks=[{"check": "review_job", "status": "platform_unavailable", "reason": None}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["judge_degraded"] is True


def test_review_job_spawned_without_degraded_does_NOT_flag():
    rec = _att(
        governed=["a.ts"],
        checks=[{"check": "review_job", "status": "spawned", "reason": "first_low_risk"}],
    )
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["judge_degraded"] is False


def test_review_job_routing_skips_do_NOT_flag():
    # routed_skip_low_risk / skipped_session_cap / skipped_digest_dup /
    # multiroot_budget are deliberate skips, not degradations.
    for status, reason in (
        ("routed_skip_low_risk", None),
        ("skipped_session_cap", None),
        ("skipped_digest_dup", None),
        ("skipped", "multiroot_budget"),
    ):
        rec = _att(
            governed=["a.ts"], checks=[{"check": "review_job", "status": status, "reason": reason}]
        )
        sc = session_coverage_from_attestations([rec], ["a.ts"])
        assert sc["judge_degraded"] is False, status


def test_mixed_old_and_new_vocab_either_flags():
    # A ledger spanning the phase-3 cutover carries both vocabularies; either
    # one degrading must still raise the flag (OR, never AND).
    rec = _att(
        governed=["a.ts"],
        checks=[
            {"check": "correctness_judge", "status": "degraded_spawn", "reason": "spawn_timeout"},
        ],
    )
    sc_old = session_coverage_from_attestations([rec], ["a.ts"])
    rec2 = _att(
        governed=["a.ts"],
        checks=[
            {"check": "review_job", "status": "degraded", "reason": "platform_unavailable"},
        ],
    )
    sc_new = session_coverage_from_attestations([rec2], ["a.ts"])
    assert sc_old["judge_degraded"] is True
    assert sc_new["judge_degraded"] is True


def test_override_on_other_file_does_not_flag():
    rec = _att(governed=["a.ts"], overrides=[{"file": "other.ts", "rule": "naming", "count": 1}])
    sc = session_coverage_from_attestations([rec], ["a.ts"])
    assert sc["overrides_on_diff"] is False


def test_malformed_records_never_raise():
    for bad in ("string", 5, None, ["list"], {"governed_files": "not-a-list"}):
        sc = session_coverage_from_attestations([bad], ["a.ts"])
        assert sc["matched"] is False


def test_empty_changed_files_all_clear():
    rec = _att(governed=["a.ts"], env={"verify_off": True})
    sc = session_coverage_from_attestations([rec], [])
    assert sc["matched"] is False


# --- raise-only fold into the verdict ----------------------------------------


def test_coverage_adds_soft_reason_and_elevated_risk():
    facts = {"session_verify_suppressed": 1}
    v = classify_change(facts)
    assert v["auto_pass_eligible"] is False
    assert v["risk"] == "elevated"  # soft governance signal, not a grounded failure
    assert any("verification was suppressed" in r for r in v["reasons"])


def test_all_three_signals_produce_three_reasons():
    facts = {
        "session_verify_suppressed": 1,
        "session_judge_degraded": 1,
        "session_overrides_on_diff": 1,
    }
    v = classify_change(facts)
    joined = " | ".join(v["reasons"])
    assert "verification was suppressed" in joined
    assert "correctness judge spawn degraded" in joined
    assert "chameleon-ignore override fired" in joined


def test_none_session_coverage_is_unchanged_verdict():
    # A clean small diff with no attestation is eligible exactly as before.
    numstat = "1\t0\tsrc/a.ts\n"
    name_status = "M\tsrc/a.ts\n"
    kw = dict(
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )
    base = build_autopass_verdict(numstat, name_status, **kw)
    with_none = build_autopass_verdict(numstat, name_status, session_coverage=None, **kw)
    assert base["auto_pass_eligible"] == with_none["auto_pass_eligible"] is True
    assert with_none["reasons"] == base["reasons"]


def test_session_coverage_demotes_an_otherwise_eligible_diff():
    numstat = "1\t0\tsrc/a.ts\n"
    name_status = "M\tsrc/a.ts\n"
    kw = dict(
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )
    v = build_autopass_verdict(
        numstat, name_status, session_coverage={"judge_degraded": True}, **kw
    )
    assert v["auto_pass_eligible"] is False
    assert any("correctness judge spawn degraded" in r for r in v["reasons"])
