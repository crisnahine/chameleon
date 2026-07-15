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
    with patch.object(hh, "_scheduler_route") as route:
        route.return_value = None
        # patched attribute must be what pipeline code resolves at call time
        assert hh._scheduler_route is route


def test_multiroot_shims_signature_and_patchability():
    # hook_helper._discover_stop_roots / _gate_one_root / _write_session_attestation
    # are tiny wrapper FUNCTIONS (not `is`-identical aliases) that defer-import
    # stop.pipeline and delegate -- pipeline.py must not be imported at
    # hook_helper's top, so an `is`-identity assertion (the gates.py/advisories.py
    # shape) does not apply here. The contract this pins instead: the frozen
    # signature every existing call site depends on, AND that patching the
    # hook_helper attribute is what a caller resolving it by module-global name
    # (stop_backstop's per-root loop) actually sees.
    import inspect

    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.stop import pipeline

    assert list(inspect.signature(hh._discover_stop_roots).parameters) == list(
        inspect.signature(pipeline.discover_stop_roots).parameters
    )
    assert list(inspect.signature(hh._gate_one_root).parameters) == list(
        inspect.signature(pipeline.gate_one_root).parameters
    )
    assert list(inspect.signature(hh._write_session_attestation).parameters) == list(
        inspect.signature(pipeline.write_session_attestation).parameters
    )

    # Patching hook_helper._gate_one_root by name intercepts stop_backstop's
    # per-root loop, since it calls the module-global (not a bound closure).
    with patch.object(hh, "_gate_one_root") as gate_mock:
        gate_mock.return_value = {
            "output": {},
            "attest": False,
            "gated": False,
            "suppressed_reason": None,
        }
        with (
            patch.object(hh, "_discover_stop_roots", return_value=[{"repo_id": "r"}]),
            patch("sys.stdin"),
            patch("chameleon_mcp.hook_helper._read_payload_dict", return_value={}),
            patch("chameleon_mcp.hook_helper._emit"),
        ):
            hh.stop_backstop()
        gate_mock.assert_called_once()
