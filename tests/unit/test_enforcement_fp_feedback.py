from chameleon_mcp.enforcement_calibration import apply_override_feedback_demotion


def test_override_rates_for_demotion_maps_audit(monkeypatch):
    from chameleon_mcp import review_ledger, tools

    fake_audit = {
        "rules": {
            "import-preference-violation": {
                "overrides": 8,
                "would_blocks": 2,
                "override_rate": 0.8,
            },
            # Below the audit's min-events floor, build_override_audit reports
            # override_rate=None; that rule carries no evidence and is omitted.
            "phantom-import": {"overrides": 1, "would_blocks": 1, "override_rate": None},
        }
    }
    monkeypatch.setattr(
        review_ledger,
        "build_override_audit",
        lambda repo_id, window_days=None: fake_audit,
    )

    rates = tools._override_rates_for_demotion("repo-xyz")

    assert rates == {"import-preference-violation": {"rate": 0.8, "events": 10}}


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
        "import-preference-violation": {"rate": 0.8, "events": 10},
    }

    out = apply_override_feedback_demotion(verdicts, override_rates, threshold=0.5, min_events=5)

    assert out["import-preference-violation"]["active"] is False
    assert out["import-preference-violation"]["demoted_reason"] == "high-override-rate"
    assert out["import-preference-violation"]["override_rate"] == 0.8
    # The calibration verdict passed in must not be mutated in place.
    assert verdicts["import-preference-violation"]["active"] is True


def test_rate_below_threshold_keeps_rule_active():
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"phantom-import": {"rate": 0.3, "events": 50}}

    out = apply_override_feedback_demotion(verdicts, rates, threshold=0.5, min_events=5)

    assert out["phantom-import"]["active"] is True
    assert "demoted_reason" not in out["phantom-import"]


def test_rate_at_threshold_is_not_demoted():
    # Strictly greater-than: a rule sitting exactly on the line stays active.
    verdicts = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"eval-call": {"rate": 0.5, "events": 50}}

    out = apply_override_feedback_demotion(verdicts, rates, threshold=0.5, min_events=5)

    assert out["eval-call"]["active"] is True


def test_insufficient_events_does_not_demote():
    # A high rate over too few fires is noise, not evidence.
    verdicts = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 100}}
    rates = {"eval-call": {"rate": 1.0, "events": 2}}

    out = apply_override_feedback_demotion(verdicts, rates, threshold=0.5, min_events=5)

    assert out["eval-call"]["active"] is True
    assert "demoted_reason" not in out["eval-call"]


def test_already_inactive_rule_untouched():
    # An advisory rule must never gain a demotion marker, and never be resurrected.
    verdicts = {"naming-convention-violation": {"active": False, "fp_rate": 0.02, "sampled": 50}}
    rates = {"naming-convention-violation": {"rate": 0.9, "events": 20}}

    out = apply_override_feedback_demotion(verdicts, rates, threshold=0.5, min_events=5)

    assert out["naming-convention-violation"]["active"] is False
    assert "demoted_reason" not in out["naming-convention-violation"]


def test_rule_absent_from_override_data_is_untouched():
    verdicts = {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100}}

    out = apply_override_feedback_demotion(verdicts, {}, threshold=0.5, min_events=5)

    assert out["phantom-import"]["active"] is True


def test_only_offending_rule_demoted_among_many():
    verdicts = {
        "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100},
        "import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 100},
    }
    rates = {
        "phantom-import": {"rate": 0.1, "events": 30},
        "import-preference-violation": {"rate": 0.7, "events": 30},
    }

    out = apply_override_feedback_demotion(verdicts, rates, threshold=0.5, min_events=5)

    assert out["phantom-import"]["active"] is True
    assert out["import-preference-violation"]["active"] is False


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
    # ...but the team overrides it on most fires.
    monkeypatch.setattr(
        tools,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "import-preference-violation": {"rate": 0.9, "events": 20}
        },
    )

    tools._calibrate_block_rules_for_repo(tmp_path)

    data = ec.load_block_rules(profile_dir)
    assert data["import-preference-violation"]["active"] is False
    assert data["import-preference-violation"]["demoted_reason"] == "high-override-rate"
    # And the read-time gate now excludes it from the active blocking set.
    assert "import-preference-violation" not in ec.active_block_rules(profile_dir)


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
