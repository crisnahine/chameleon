"""Drift detection module for chameleon.

drift.db (per-repo, in ${PLUGIN_DATA}/<repo_id>/) tracks:
- mtime + sha_hint (xxhash64) per tracked file
- cached cluster signatures
- per-edit confidence observations (for drift-driven nags)

See docs/architecture.md "SQLite schemas" → "drift.db" subsection for full DDL.
"""
