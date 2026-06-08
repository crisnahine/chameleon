from chameleon_mcp.profile.config import EnforcementConfig, _coerce_enforcement


def test_default_on_when_no_enforcement_block():
    assert EnforcementConfig().duplication_review is True
    assert _coerce_enforcement(None).duplication_review is True


def test_default_on_when_enforcement_block_omits_key():
    # The correctness_judge off-by-default trap must NOT be repeated.
    cfg = _coerce_enforcement({"mode": "shadow"})
    assert cfg.duplication_review is True


def test_explicit_off():
    assert _coerce_enforcement({"duplication_review": False}).duplication_review is False
