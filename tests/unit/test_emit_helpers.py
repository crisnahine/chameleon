import json
from unittest.mock import patch


def _capture(fn, *args):
    cap = []
    with patch("sys.stdout") as out:
        out.write = cap.append
        fn(*args)
    return json.loads("".join(cap).strip())


def test_pretool_deny_shape():
    from chameleon_mcp.hook_helper import _emit_pretool_deny

    d = _capture(_emit_pretool_deny, "banned import: lodash")
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "lodash" in hso["permissionDecisionReason"]


def test_posttool_block_shape():
    from chameleon_mcp.hook_helper import _emit_posttool_block

    d = _capture(_emit_posttool_block, "fix it", "<chameleon-context>\nx\n</chameleon-context>")
    assert d["decision"] == "block"
    assert d["reason"] == "fix it"
    assert d["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "x" in d["hookSpecificOutput"]["additionalContext"]
