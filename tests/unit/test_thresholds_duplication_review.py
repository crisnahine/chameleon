import os
from unittest.mock import patch

from chameleon_mcp._thresholds import threshold_int


def test_duplication_review_defaults():
    assert threshold_int("DUPLICATION_REVIEW_MAX_FILES") == 12
    assert threshold_int("DUPLICATION_REVIEW_MAX_FINDINGS") == 8
    assert threshold_int("DUPLICATION_REVIEW_MAX_PROMPT_BYTES") == 60_000
    assert threshold_int("DUPLICATION_REVIEW_MAX_SPAWNS_PER_SESSION") == 2


def test_duplication_review_env_override_at_call_time():
    with patch.dict(os.environ, {"CHAMELEON_DUPLICATION_REVIEW_MAX_FILES": "3"}):
        assert threshold_int("DUPLICATION_REVIEW_MAX_FILES") == 3
