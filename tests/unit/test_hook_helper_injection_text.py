"""Injected-guidance text fixes for hook_helper.py.

These guard the static strings chameleon ships into the chameleon-context
block, where a wrong word costs a real tool call or a confusing banner:

  - the Tier-2 rules pointer must not tell the model to call get_rules with an
    archetype name (get_rules is repo/source-scoped, not per-archetype)
  - the stale-trust banner must be cause-agnostic (refresh, teach, OR a manual
    edit can de-trust) and must not pin the blame on profile.json alone
  - the violation header must pluralize so a single violation reads "1 violation"
"""

from __future__ import annotations

import inspect
import re

from chameleon_mcp import hook_helper


def _preflight_source() -> str:
    return inspect.getsource(hook_helper.preflight_and_advise)


def _posttool_verify_source() -> str:
    return inspect.getsource(hook_helper.posttool_verify)


# --- BUG-F1: rules pointer must not call get_rules(archetype_name) ----------


def test_rules_pointer_does_not_call_get_rules_with_archetype():
    src = _preflight_source()
    # The invalid form passed the archetype name var as the first (repo) arg.
    assert "get_rules(archetype_name" not in src
    assert "get_rules({archetype_name" not in src


# --- BUG-T14a: stale-trust banner is cause-agnostic -------------------------


def test_stale_trust_banner_is_cause_agnostic():
    src = _preflight_source()
    # Pull the staleness banner string literal.
    assert "Trust is stale" in src
    banner_region = src[src.index("Trust is stale") :]
    banner_region = banner_region[: banner_region.index("\n\n")]
    lowered = banner_region.lower()
    # A teach also de-trusts (idioms.md is hashed), so the wording must admit it.
    assert "teach" in lowered
    # It must not pin the cause to profile.json alone.
    assert "`.chameleon/profile.json`" not in banner_region


# --- BUG-F4: violation header pluralizes ------------------------------------


def test_violation_header_pluralizes():
    src = _posttool_verify_source()
    # The hardcoded plural form must be gone.
    assert "{len(violations)} violations]" not in src
    # The header must select singular/plural the same way the statusline does.
    assert re.search(r"violation\{'s' if .* != 1 else ''\}\]", src)
