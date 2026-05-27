"""Unit tests for chameleon_mcp.conventions — schema, serialization, extraction."""
from __future__ import annotations
import json
from chameleon_mcp.conventions import (
    CONVENTIONS_SCHEMA_VERSION,
    empty_conventions,
    serialize_conventions,
)

class TestConventionsSchema:
    def test_empty_conventions_has_schema_version(self):
        c = empty_conventions(generation=42)
        assert c["schema_version"] == CONVENTIONS_SCHEMA_VERSION
        assert c["generation"] == 42
        assert c["conventions"]["imports"] == {}
        assert c["conventions"]["naming"] == {}

    def test_serialize_round_trip(self):
        c = empty_conventions(generation=1)
        c["conventions"]["imports"]["model"] = {
            "preferred": [{"module": "useCustomQuery", "source": "@/hooks", "frequency": 47, "total": 52}],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        c["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = serialize_conventions(c)
        parsed = json.loads(text)
        assert parsed["conventions"]["imports"]["model"]["preferred"][0]["module"] == "useCustomQuery"
        assert parsed["conventions"]["naming"]["component"]["interface_prefix"]["consistency"] == 0.999
