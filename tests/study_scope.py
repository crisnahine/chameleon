"""Back-compat shim: the scoping engine now ships in the package.

It moved to ``chameleon_mcp.commit_scope`` when the headless gate needed the
same arithmetic the studies use. Re-exported here so the study drivers keep
their import, and so the gate and the study can never compute "introduced"
two different ways.
"""

from __future__ import annotations

from chameleon_mcp.commit_scope import (
    PER_EDIT_SUPPRESSED,
    actionable,
    blob_at,
    first_parent,
    net_new,
    violation_key,
)

__all__ = [
    "PER_EDIT_SUPPRESSED",
    "actionable",
    "blob_at",
    "first_parent",
    "net_new",
    "violation_key",
]
