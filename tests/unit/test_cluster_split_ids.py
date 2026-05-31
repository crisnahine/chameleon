"""Split-by-sub-bucket children must get DISTINCT cluster ids.

Regression guard for the BLOCK where _split_by_sub_bucket emitted children that
all inherited the parent ClusterKey, so _hash_cluster_key collided them and one
archetype was silently dropped from the profile (reproduced on mastodon's
app/models + app/models/concerns layout).
"""

from __future__ import annotations

import hashlib
import json


def _make_key():
    from chameleon_mcp.signatures import ClusterKey

    return ClusterKey(
        path_pattern_bucket="app/models",
        content_signal_match="model",
        top_level_node_kinds=("class",),
        default_export_kind=None,
        named_export_count_bucket="0",
        import_module_set_hash="abc123",
        jsx_present=False,
    )


def test_split_children_get_distinct_cluster_ids():
    from chameleon_mcp.bootstrap.canonical import _hash_cluster_key
    from chameleon_mcp.bootstrap.clustering import Cluster
    from chameleon_mcp.tools import _hash_cluster_key_for

    key = _make_key()
    base = Cluster(key=key, split_tag="")
    concerns = Cluster(key=key, split_tag="concerns")

    h_base = _hash_cluster_key(base)
    h_concerns = _hash_cluster_key(concerns)

    # The BLOCK: these used to be identical -> one archetype overwrote the other.
    assert h_base != h_concerns

    # Writer (canonical) and reader (tools) must agree byte-for-byte, or the
    # file_clusters map won't line up with archetypes.json.
    assert _hash_cluster_key_for(key, "") == h_base
    assert _hash_cluster_key_for(key, "concerns") == h_concerns


def test_unsplit_cluster_id_is_backward_compatible():
    from chameleon_mcp.bootstrap.canonical import _hash_cluster_key
    from chameleon_mcp.bootstrap.clustering import Cluster
    from chameleon_mcp.tools import _hash_cluster_key_for

    key = _make_key()
    # split_tag="" must keep the legacy key-only hash so existing profiles'
    # cluster_ids stay stable across this change.
    legacy = hashlib.sha256(
        json.dumps(key.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]

    assert _hash_cluster_key(Cluster(key=key)) == legacy
    assert _hash_cluster_key_for(key) == legacy
