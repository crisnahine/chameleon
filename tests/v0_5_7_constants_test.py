"""Verify _constants module pins enum values that consumers depend on."""

import sys

from chameleon_mcp._constants import (
    BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED,
    BOOTSTRAP_STATUS_FAILED,
    BOOTSTRAP_STATUS_FAILED_UNSUPPORTED_LANGUAGE,
    BOOTSTRAP_STATUS_SUCCESS,
    BOOTSTRAP_STATUS_VALUES,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_VALUES,
    CONTENT_SIGNAL_NONE,
    CONTENT_SIGNAL_STRONG,
    CONTENT_SIGNAL_VALUES,
    CONTENT_SIGNAL_WEAK,
    PROFILE_STATUS_NO_PROFILE,
    PROFILE_STATUS_NO_REPO,
    PROFILE_STATUS_PROFILE_CORRUPTED,
    PROFILE_STATUS_PROFILE_PRESENT,
    PROFILE_STATUS_PROFILE_UNSUPPORTED_SCHEMA,
    PROFILE_STATUS_VALUES,
    TRUST_STATE_NA,
    TRUST_STATE_STALE,
    TRUST_STATE_TRUSTED,
    TRUST_STATE_UNTRUSTED,
    TRUST_STATE_VALUES,
)

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Pin the literal values consumers (slash command skills, external scripts) rely on.
# ---------------------------------------------------------------------------
section("profile_status values")

t("'no_repo'", PROFILE_STATUS_NO_REPO == "no_repo")
t("'no_profile'", PROFILE_STATUS_NO_PROFILE == "no_profile")
t("'profile_present'", PROFILE_STATUS_PROFILE_PRESENT == "profile_present")
t("'profile_corrupted'", PROFILE_STATUS_PROFILE_CORRUPTED == "profile_corrupted")
t(
    "'profile_unsupported_schema_version'",
    PROFILE_STATUS_PROFILE_UNSUPPORTED_SCHEMA == "profile_unsupported_schema_version",
)
t("PROFILE_STATUS_VALUES has 5 unique entries", len(set(PROFILE_STATUS_VALUES)) == 5)


section("trust_state values")
t("'n/a'", TRUST_STATE_NA == "n/a")
t("'untrusted'", TRUST_STATE_UNTRUSTED == "untrusted")
t("'trusted'", TRUST_STATE_TRUSTED == "trusted")
t("'stale'", TRUST_STATE_STALE == "stale")
t("TRUST_STATE_VALUES has 4 unique entries", len(set(TRUST_STATE_VALUES)) == 4)


section("bootstrap_repo status values")
t("'success'", BOOTSTRAP_STATUS_SUCCESS == "success")
t("'failed'", BOOTSTRAP_STATUS_FAILED == "failed")
t(
    "'failed_unsupported_language'",
    BOOTSTRAP_STATUS_FAILED_UNSUPPORTED_LANGUAGE == "failed_unsupported_language",
)
t("'already_bootstrapped'", BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED == "already_bootstrapped")
t("BOOTSTRAP_STATUS_VALUES has 6 entries", len(set(BOOTSTRAP_STATUS_VALUES)) == 6)


section("confidence_band values")
t("'high'", CONFIDENCE_HIGH == "high")
t("'medium'", CONFIDENCE_MEDIUM == "medium")
t("'low'", CONFIDENCE_LOW == "low")
t("CONFIDENCE_VALUES has 3 entries", len(set(CONFIDENCE_VALUES)) == 3)


section("content_signal_match values")
t("'strong'", CONTENT_SIGNAL_STRONG == "strong")
t("'weak'", CONTENT_SIGNAL_WEAK == "weak")
t("'none'", CONTENT_SIGNAL_NONE == "none")
t("CONTENT_SIGNAL_VALUES has 3 entries", len(set(CONTENT_SIGNAL_VALUES)) == 3)


# ---------------------------------------------------------------------------
# Cross-check the constants against real call paths.
# detect_repo on a no-profile fixture should return PROFILE_STATUS_NO_PROFILE
# and TRUST_STATE_NA.
# ---------------------------------------------------------------------------
section("detect_repo wire values match constants")

import tempfile
from pathlib import Path

from chameleon_mcp.tools import detect_repo

with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    (repo / "package.json").write_text('{"dependencies": {"typescript": "5"}}')
    (repo / "tsconfig.json").write_text("{}")
    (repo / "src").mkdir()
    src = repo / "src" / "x.ts"
    src.write_text("")

    resp = detect_repo(str(src))
    data = resp["data"]
    t(
        "no-profile fixture returns PROFILE_STATUS_NO_PROFILE",
        data["profile_status"] == PROFILE_STATUS_NO_PROFILE,
        f"got {data['profile_status']}",
    )
    t(
        "no-profile fixture returns TRUST_STATE_NA",
        data["trust_state"] == TRUST_STATE_NA,
        f"got {data['trust_state']}",
    )


print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
