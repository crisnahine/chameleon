from chameleon_mcp.conventions import format_conventions_echo, format_conventions_for_session


def _conv(cc: dict) -> dict:
    return {"conventions": {"class_contract": cc}}


def test_echo_includes_contract_ruby():
    conv = _conv(
        {
            "interaction": {
                "dsl_macros": ["integer", "object", "string"],
                "required_methods": ["execute"],
                "base": "ActiveInteraction::Base",
                "sample_size": 12,
            }
        }
    )
    echo = format_conventions_echo(conv, archetype="interaction")
    assert "Contract:" in echo
    assert "execute" in echo
    assert "string" in echo


def test_echo_includes_contract_ts():
    conv = _conv(
        {
            "service": {
                "decorators": ["Injectable"],
                "required_methods": ["execute"],
                "base": "BaseService",
                "sample_size": 10,
            }
        }
    )
    echo = format_conventions_echo(conv, archetype="service")
    assert "Contract:" in echo
    assert "Injectable" in echo
    assert "BaseService" in echo


def test_echo_no_contract_no_line():
    echo = format_conventions_echo({"conventions": {}}, archetype="x")
    assert "Contract:" not in echo


def test_render_fails_open_on_malformed_class_contract():
    # A corrupt conventions.json must never crash the SessionStart/PreToolUse hook.
    for bad in ("garbage", 123, ["a"], None, {"arch": "not-a-dict"}, {"arch": 99}):
        conv = {"conventions": {"class_contract": bad}}
        echo = format_conventions_echo(conv, archetype="arch")
        block = format_conventions_for_session(conv)
        assert "Contract:" not in echo
        assert "CONTRACT:" not in block


def test_session_includes_contract_section():
    conv = _conv(
        {
            "interaction": {
                "dsl_macros": ["string"],
                "required_methods": ["execute"],
                "base": "ActiveInteraction::Base",
                "sample_size": 12,
            }
        }
    )
    block = format_conventions_for_session(conv)
    assert "CONTRACT:" in block
    assert "execute" in block
