from chameleon_mcp.enforcement_calibration import apply_override_feedback_demotion


def test_override_rates_for_demotion_maps_audit(monkeypatch):
    from chameleon_mcp import review_ledger, tools

    fake_audit = {
        "rules": {
            "import-preference-violation": {
                "overrides": 8,
                "would_blocks": 2,
                "override_rate": 0.8,
                "distinct_sessions": 4,
            },
            # Below the audit's min-events floor, build_override_audit reports
            # override_rate=None; that rule carries no evidence and is omitted.
            "phantom-import": {
                "overrides": 1,
                "would_blocks": 1,
                "override_rate": None,
                "distinct_sessions": 1,
            },
        }
    }
    monkeypatch.setattr(
        review_ledger,
        "build_override_audit",
        lambda repo_id, window_days=None: fake_audit,
    )

    rates = tools._override_rates_for_demotion("repo-xyz")

    assert rates == {
        "import-preference-violation": {"rate": 0.8, "events": 10, "distinct_sessions": 4}
    }


def test_high_override_rate_demotes_active_rule():
    verdicts = {
        "import-preference-violation": {
            "active": True,
            "fp_rate": 0.0,
            "sampled": 100,
            "flagged": 0,
        },
    }
    override_rates = {
        "import-preference-violation": {"rate": 0.8, "events": 10, "distinct_sessions": 3},
    }

    out = apply_override_feedback_demotion(
        verdicts, override_rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["import-preference-violation"]["active"] is False
    assert out["import-preference-violation"]["demoted_reason"] == "high-override-rate"
    assert out["import-preference-violation"]["override_rate"] == 0.8
    # The demoted entry must carry the multi-session evidence that authorized it.
    assert out["import-preference-violation"]["override_distinct_sessions"] == 3
    # The calibration verdict passed in must not be mutated in place.
    assert verdicts["import-preference-violation"]["active"] is True


def test_rate_below_threshold_keeps_rule_active():
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"phantom-import": {"rate": 0.3, "events": 50, "distinct_sessions": 5}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["phantom-import"]["active"] is True
    assert "demoted_reason" not in out["phantom-import"]


def test_rate_at_threshold_is_not_demoted():
    # Strictly greater-than: a rule sitting exactly on the line stays active.
    verdicts = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"eval-call": {"rate": 0.5, "events": 50, "distinct_sessions": 5}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["eval-call"]["active"] is True


def test_insufficient_events_does_not_demote():
    # A high rate over too few fires is noise, not evidence.
    verdicts = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"eval-call": {"rate": 1.0, "events": 2, "distinct_sessions": 2}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["eval-call"]["active"] is True
    assert "demoted_reason" not in out["eval-call"]


def test_already_inactive_rule_untouched():
    # An advisory rule must never gain a demotion marker, and never be resurrected.
    verdicts = {"naming-convention-violation": {"active": False, "fp_rate": 0.02, "sampled": 50}}
    rates = {"naming-convention-violation": {"rate": 0.9, "events": 20, "distinct_sessions": 5}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["naming-convention-violation"]["active"] is False
    assert "demoted_reason" not in out["naming-convention-violation"]
    assert "demotion_proposed" not in out["naming-convention-violation"]


def test_rule_absent_from_override_data_is_untouched():
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}

    out = apply_override_feedback_demotion(
        verdicts, {}, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["phantom-import"]["active"] is True


def test_only_offending_rule_demoted_among_many():
    verdicts = {
        "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100},
        "import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 100},
    }
    rates = {
        "phantom-import": {"rate": 0.1, "events": 30, "distinct_sessions": 4},
        "import-preference-violation": {"rate": 0.7, "events": 30, "distinct_sessions": 4},
    }

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["phantom-import"]["active"] is True
    assert out["import-preference-violation"]["active"] is False


def test_single_session_evidence_proposes_instead_of_demoting():
    # Override evidence is author-generated: one session's overrides must never
    # silence a calibrated block rule on their own. Below the session floor the
    # demotion is recorded as a proposal and the rule keeps blocking.
    verdicts = {
        "import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 100},
    }
    rates = {
        "import-preference-violation": {"rate": 0.9, "events": 10, "distinct_sessions": 1},
    }

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    entry = out["import-preference-violation"]
    assert entry["active"] is True
    assert "demoted_reason" not in entry
    assert entry["demotion_proposed"] == {
        "reason": "high-override-rate",
        "override_rate": 0.9,
        "events": 10,
        "distinct_sessions": 1,
        "security_rule": False,
    }
    # The calibration verdict passed in must not be mutated in place.
    assert "demotion_proposed" not in verdicts["import-preference-violation"]


def test_missing_distinct_sessions_reads_as_zero():
    # Absent evidence must never weaken the gate: a rates dict without the
    # session count proposes, never demotes.
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"phantom-import": {"rate": 0.9, "events": 10}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    entry = out["phantom-import"]
    assert entry["active"] is True
    assert "demoted_reason" not in entry
    assert entry["demotion_proposed"]["distinct_sessions"] == 0


def test_distinct_sessions_at_floor_demotes():
    # The floor is inclusive: evidence spanning exactly min_distinct_sessions applies.
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"phantom-import": {"rate": 0.9, "events": 10, "distinct_sessions": 2}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["phantom-import"]["active"] is False
    assert out["phantom-import"]["demoted_reason"] == "high-override-rate"
    assert out["phantom-import"]["override_distinct_sessions"] == 2


def test_security_rule_never_auto_demoted():
    # eval-call and secret-detected-in-content guard security facts and carry no
    # calibration measurement behind their active verdict; override pressure can
    # only ever propose their demotion, regardless of session spread.
    for rule in ("eval-call", "secret-detected-in-content"):
        verdicts = {rule: {"active": True, "fp_rate": 0.0, "sampled": 100}}
        rates = {rule: {"rate": 1.0, "events": 20, "distinct_sessions": 5}}

        out = apply_override_feedback_demotion(
            verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
        )

        assert out[rule]["active"] is True
        assert "demoted_reason" not in out[rule]
        assert out[rule]["demotion_proposed"]["security_rule"] is True
        assert out[rule]["demotion_proposed"]["distinct_sessions"] == 5


def test_security_rules_derive_from_blanket_immune_set():
    # The demotion-exempt set must be the same object as violation_class's
    # blanket-immune deterministic set: a single source of truth, never a
    # parallel literal that could drift.
    from chameleon_mcp.enforcement_calibration import SECURITY_BLOCK_RULES
    from chameleon_mcp.violation_class import BLANKET_IMMUNE_RULES

    assert SECURITY_BLOCK_RULES is BLANKET_IMMUNE_RULES


def test_security_rule_below_threshold_gains_no_proposal():
    # The proposal path only opens once the demotion bar is crossed.
    verdicts = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"eval-call": {"rate": 0.2, "events": 20, "distinct_sessions": 5}}

    out = apply_override_feedback_demotion(
        verdicts, rates, threshold=0.5, min_events=5, min_distinct_sessions=2
    )

    assert out["eval-call"]["active"] is True
    assert "demotion_proposed" not in out["eval-call"]
    assert "demoted_reason" not in out["eval-call"]


def test_status_partition_surfaces_override_demotion():
    # /chameleon-status must tell a lead WHY a rule is advisory: an override-driven
    # demotion carries its own reason and the measured rate, distinct from the
    # capability inert_reasons.
    from chameleon_mcp.tools import _partition_block_rules

    rules = {
        "phantom-import": {"active": True, "fp_rate": 0.0},
        "import-preference-violation": {
            "active": False,
            "fp_rate": 0.0,
            "demoted_reason": "high-override-rate",
            "override_rate": 0.8,
        },
    }

    active, demoted = _partition_block_rules(
        rules, lang_inert=lambda r: False, signal_inert=lambda r: False
    )

    assert active == ["phantom-import"]
    assert demoted == [
        {
            "rule": "import-preference-violation",
            "fp_rate": 0.0,
            "demoted_reason": "high-override-rate",
            "override_rate": 0.8,
        }
    ]


def test_status_surfaces_proposed_demotions():
    # A pending proposal is reported with its rule name attached, and the rule
    # stays in the active (still blocking) partition.
    from chameleon_mcp.tools import _collect_demotion_proposals, _partition_block_rules

    rules = {
        "phantom-import": {"active": True, "fp_rate": 0.0},
        "import-preference-violation": {
            "active": True,
            "fp_rate": 0.0,
            "demotion_proposed": {
                "reason": "high-override-rate",
                "override_rate": 0.9,
                "events": 12,
                "distinct_sessions": 1,
                "security_rule": False,
            },
        },
    }

    proposals = _collect_demotion_proposals(rules)

    assert proposals == [
        {
            "rule": "import-preference-violation",
            "reason": "high-override-rate",
            "override_rate": 0.9,
            "events": 12,
            "distinct_sessions": 1,
            "security_rule": False,
        }
    ]

    active, _demoted = _partition_block_rules(
        rules, lang_inert=lambda r: False, signal_inert=lambda r: False
    )
    assert "import-preference-violation" in active


def test_refresh_wires_demotion_into_enforcement_json(tmp_path, monkeypatch):
    # End-to-end: the refresh calibration path must persist a demotion to the
    # trust-hashed artifact when the override stream shows the team fighting a rule.
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools
    from chameleon_mcp.profile import loader as loader_mod

    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir()

    # Avoid a real bootstrap: calibration certifies the rule active on committed files.
    monkeypatch.setattr(loader_mod, "load_profile_dir", lambda pd: object())
    monkeypatch.setattr(
        ec,
        "calibrate_block_rules",
        lambda repo_root, loaded: {
            "import-preference-violation": {
                "active": True,
                "fp_rate": 0.0,
                "sampled": 100,
                "flagged": 0,
            },
        },
    )
    # ...but the team overrides it on most fires, across multiple sessions.
    monkeypatch.setattr(
        tools,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "import-preference-violation": {"rate": 0.9, "events": 20, "distinct_sessions": 3}
        },
    )

    tools._calibrate_block_rules_for_repo(tmp_path)

    data = ec.load_block_rules(profile_dir)
    assert data["import-preference-violation"]["active"] is False
    assert data["import-preference-violation"]["demoted_reason"] == "high-override-rate"
    # And the read-time gate now excludes it from the active blocking set.
    assert "import-preference-violation" not in ec.active_block_rules(profile_dir)


def test_refresh_persists_single_session_proposal(tmp_path, monkeypatch):
    # End-to-end: single-session override pressure persists a proposal into the
    # trust-hashed artifact while the rule keeps blocking.
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools
    from chameleon_mcp.profile import loader as loader_mod

    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir()

    monkeypatch.setattr(loader_mod, "load_profile_dir", lambda pd: object())
    monkeypatch.setattr(
        ec,
        "calibrate_block_rules",
        lambda repo_root, loaded: {
            "import-preference-violation": {
                "active": True,
                "fp_rate": 0.0,
                "sampled": 100,
                "flagged": 0,
            },
        },
    )
    monkeypatch.setattr(
        tools,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "import-preference-violation": {"rate": 0.9, "events": 20, "distinct_sessions": 1}
        },
    )

    tools._calibrate_block_rules_for_repo(tmp_path)

    data = ec.load_block_rules(profile_dir)
    entry = data["import-preference-violation"]
    assert entry["active"] is True
    assert entry["demotion_proposed"]["reason"] == "high-override-rate"
    assert entry["demotion_proposed"]["distinct_sessions"] == 1
    assert entry["demotion_proposed"]["security_rule"] is False
    assert "import-preference-violation" in ec.active_block_rules(profile_dir)


def test_refresh_persists_security_proposal_and_keeps_blocking(tmp_path, monkeypatch):
    # End-to-end: a security rule never auto-demotes even on multi-session
    # evidence; the proposal is persisted and the rule stays in the blocking set.
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools
    from chameleon_mcp.profile import loader as loader_mod

    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir()

    monkeypatch.setattr(loader_mod, "load_profile_dir", lambda pd: object())
    monkeypatch.setattr(
        ec,
        "calibrate_block_rules",
        lambda repo_root, loaded: {
            "eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100, "flagged": 0},
        },
    )
    monkeypatch.setattr(
        tools,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "eval-call": {"rate": 0.9, "events": 20, "distinct_sessions": 4}
        },
    )

    tools._calibrate_block_rules_for_repo(tmp_path)

    data = ec.load_block_rules(profile_dir)
    entry = data["eval-call"]
    assert entry["active"] is True
    assert "demoted_reason" not in entry
    assert entry["demotion_proposed"]["security_rule"] is True
    assert entry["demotion_proposed"]["distinct_sessions"] == 4
    assert "eval-call" in ec.active_block_rules(profile_dir)


def test_refresh_without_override_data_preserves_calibration(tmp_path, monkeypatch):
    # Fail-safe: an empty/unreadable override stream must leave the structural
    # calibration untouched. The gate must never weaken itself on missing telemetry.
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools
    from chameleon_mcp.profile import loader as loader_mod

    profile_dir = tmp_path / ".chameleon"
    profile_dir.mkdir()

    monkeypatch.setattr(loader_mod, "load_profile_dir", lambda pd: object())
    monkeypatch.setattr(
        ec,
        "calibrate_block_rules",
        lambda repo_root, loaded: {
            "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100, "flagged": 0},
        },
    )
    monkeypatch.setattr(tools, "_override_rates_for_demotion", lambda repo_id, window_days=None: {})

    tools._calibrate_block_rules_for_repo(tmp_path)

    data = ec.load_block_rules(profile_dir)
    assert data["phantom-import"]["active"] is True
    assert "demoted_reason" not in data["phantom-import"]
