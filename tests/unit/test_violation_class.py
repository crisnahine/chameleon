from chameleon_mcp.violation_class import (
    BLOCK_ELIGIBLE_RULES,
    hard_class_violations,
    is_archetype_independent,
    is_hard_class,
    tag_secret_hardness,
)


def v(rule, severity="warning"):
    return {"rule": rule, "severity": severity, "message": "m", "expected": "", "actual": ""}


def secret(kind, location="line 3"):
    """A secret violation dict shaped like scan_secrets().to_dict() output."""
    return {
        "rule": "secret-detected-in-content",
        "severity": "error",
        "message": f"detect-secrets flagged a {kind} at {location}.",
        "expected": "<no secret>",
        "actual": f"{kind} at {location}",
    }


def test_block_eligible_rules_contents():
    assert BLOCK_ELIGIBLE_RULES == frozenset(
        {
            "phantom-import",
            "import-preference-violation",
            "jsx-presence-mismatch",
            "naming-convention-violation",
            "inheritance-convention-violation",
            "file-naming-convention-violation",
            "secret-detected-in-content",
            "eval-call",
        }
    )


def test_advisory_sink_rules_never_block_eligible():
    # weak-hash, insecure-random, and SQL string interpolation are advisory-only
    # security nags: their precision cannot survive the zero-FP calibration gate
    # on real repos, so they must never be promoted to a block. Only eval-call (a
    # deterministic dangerous sink) is block-eligible.
    for rule in ("weak-hash", "insecure-random", "sql-string-interpolation"):
        assert rule not in BLOCK_ELIGIBLE_RULES
        assert is_hard_class(v(rule)) is False


def test_phantom_is_hard_and_independent():
    assert is_hard_class(v("phantom-import"))
    assert is_archetype_independent("phantom-import")


def test_banned_import_is_hard_but_dependent():
    assert is_hard_class(v("import-preference-violation"))
    assert not is_archetype_independent("import-preference-violation")


def test_jsx_error_is_hard_warning_is_not():
    assert is_hard_class(v("jsx-presence-mismatch", "error"))
    assert not is_hard_class(v("jsx-presence-mismatch", "warning"))


def test_naming_and_inheritance_are_hard_but_dependent():
    # Both rules are always emitted at "warning" severity; is_hard_class must
    # qualify them despite that, since they are not jsx-presence-mismatch.
    assert is_hard_class(v("naming-convention-violation", "warning"))
    assert is_hard_class(v("inheritance-convention-violation", "warning"))
    assert not is_archetype_independent("naming-convention-violation")
    assert not is_archetype_independent("inheritance-convention-violation")


def test_soft_rules_never_hard():
    for r in (
        "default-export-kind-mismatch",
        "top-level-node-kinds-mismatch",
        "content-signal-mismatch",
        "named-export-count-bucket-mismatch",
    ):
        assert not is_hard_class(v(r))


def test_hard_class_violations_filters_by_active_set():
    vs = [
        v("phantom-import"),
        v("naming-convention-violation"),
        v("import-preference-violation"),
    ]
    out = hard_class_violations(vs, active_rules={"phantom-import"})
    assert [x["rule"] for x in out] == ["phantom-import"]


def test_file_naming_is_hard_but_dependent():
    # Always emitted at warning; must qualify as hard (it is not jsx) yet stay
    # archetype-dependent so a wrong match cannot make it spurious at block time.
    assert is_hard_class(v("file-naming-convention-violation", "warning"))
    assert not is_archetype_independent("file-naming-convention-violation")


def test_secret_rule_is_independent():
    assert is_archetype_independent("secret-detected-in-content")


def test_deterministic_secret_kinds_hard_block():
    for kind in (
        "aws_access_key",
        "github_token",
        "ai_api_key",
        "stripe_live_key",
        "stripe_key",
        "slack_token",
        "google_api_key",
        "gcp_service_account",
        "azure_account_key",
        "private_key",
    ):
        s = secret(kind)
        tag_secret_hardness([s])
        assert s["secret_kind"] == kind
        assert s["secret_hard"] is True
        assert is_hard_class(s), kind


def test_noisy_secret_kinds_stay_advisory():
    # Entropy/broad-fallback kinds and the FP-prone JWT/userinfo shapes are
    # detected but never hard-block: their precision can't be calibrated.
    for kind in (
        "possible_aws_secret",
        "high_entropy_hex",
        "password_assignment",
        "Base64 High Entropy String",
        "jwt_token",
        "url_userinfo_credentials",
    ):
        s = secret(kind)
        tag_secret_hardness([s])
        assert s["secret_hard"] is False
        assert not is_hard_class(s), kind


def test_untagged_secret_defaults_advisory():
    # A secret hit that never passed through the tagger must not hard-block.
    assert not is_hard_class(secret("aws_access_key"))


def test_secret_cap_summary_never_hard_blocks():
    cap = {
        "rule": "secret-detected-in-content",
        "severity": "error",
        "message": "file contains 99 potential secrets; reporting the first 50.",
        "expected": "<no secrets beyond the cap>",
        "actual": "+49 more (capped at 50)",
    }
    tag_secret_hardness([cap])
    assert cap["secret_hard"] is False
    assert not is_hard_class(cap)


def test_tag_secret_hardness_ignores_non_secret_violations():
    vs = [v("phantom-import"), v("naming-convention-violation")]
    tag_secret_hardness(vs)
    for x in vs:
        assert "secret_hard" not in x
        assert "secret_kind" not in x


def test_fold_suffix_kind_parsed():
    # scan_secrets appends " [after string-concat fold]" to actual; the kind is
    # still the leading token before " at ", so de-obfuscated hits hard-block too.
    s = secret("github_token", location="position 12")
    s["actual"] = "github_token at position 12 [after string-concat fold]"
    tag_secret_hardness([s])
    assert s["secret_kind"] == "github_token"
    assert s["secret_hard"] is True
