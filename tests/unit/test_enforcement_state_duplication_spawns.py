from chameleon_mcp.enforcement import EnforcementState, _merge_states


def test_duplication_spawns_roundtrip_and_default():
    s = EnforcementState()
    assert s.duplication_spawns == 0
    s.duplication_spawns = 2
    restored = EnforcementState.from_dict(s.to_dict())
    assert restored.duplication_spawns == 2


def test_duplication_spawns_max_merged():
    disk = EnforcementState()
    disk.duplication_spawns = 2
    mem = EnforcementState()
    mem.duplication_spawns = 1
    assert _merge_states(disk, mem).duplication_spawns == 2


def test_from_dict_defaults_missing_key_to_zero():
    assert EnforcementState.from_dict({}).duplication_spawns == 0
