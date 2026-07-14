"""The stop/ package extraction contract: aliases stay patchable, seams late-bind."""

from __future__ import annotations

from unittest.mock import patch


def test_advisory_builders_are_hook_helper_attributes():
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.stop import advisories

    for name in (
        "_stale_test_advisory_lines",
        "_changeset_completeness_lines",
        "_cochange_history_advisory_lines",
        "_crossfile_existence_advisory_lines",
        "_crossworkspace_existence_advisory_lines",
        "_scope_drift_advisory_lines",
        "_test_integrity_advisory_lines",
    ):
        assert getattr(hh, name) is getattr(advisories, name)


def test_patching_hook_helper_attribute_is_honored():
    # The Pattern-A harnesses patch by string path into hook_helper; the
    # extraction must keep that working even for calls made from stop/ code.
    from chameleon_mcp import hook_helper as hh

    with patch("chameleon_mcp.hook_helper._stale_test_advisory_lines", return_value=["x"]):
        assert hh._stale_test_advisory_lines() == ["x"]


def test_gate_seams_are_hook_helper_attributes():
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.stop import gates

    for name in (
        "_stop_file_still_blockable",
        "_ledger_recheck_and_resurface",
        "_ledger_persist",
        "_confirmed_crossfile_break_sites",
        "_effective_stop_blocks",
        "_stop_block_scope",
    ):
        assert getattr(hh, name) is getattr(gates, name)


def test_stop_gates_shim_signature_and_patchability():
    import inspect

    from chameleon_mcp import hook_helper as hh

    params = list(inspect.signature(hh._stop_gates).parameters)
    assert params == [
        "payload",
        "repo_root",
        "repo_id",
        "session_id",
        "is_subagent",
        "repo_data",
        "daemon_state",
        "only_files",
        "allow_model_spawn",
    ]
    with patch.object(hh, "_correctness_judge_route") as route:
        route.return_value = None
        # patched attribute must be what pipeline code resolves at call time
        assert hh._correctness_judge_route is route
