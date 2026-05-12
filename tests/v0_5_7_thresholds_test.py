"""Verify _thresholds module: defaults match prior literals; env overrides apply."""

import os
import sys

from chameleon_mcp._thresholds import (
    DEFAULTS,
    DOCS,
    threshold,
    threshold_float,
    threshold_int,
)

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Defaults match the values pre-existing in the various modules.
# ---------------------------------------------------------------------------
section("Defaults match prior hardcoded values")

t("WORKSPACE_FANOUT_CAP default = 50", DEFAULTS["WORKSPACE_FANOUT_CAP"] == 50)
t("WARNING_SAMPLE_PATHS default = 3", DEFAULTS["WARNING_SAMPLE_PATHS"] == 3)
t("SPARSE_WARNING_LIMIT default = 50", DEFAULTS["SPARSE_WARNING_LIMIT"] == 50)
t("MAX_EXTENDS_HOPS default = 8", DEFAULTS["MAX_EXTENDS_HOPS"] == 8)
t("EDIT_OBS_HARD_CAP default = 50000", DEFAULTS["EDIT_OBS_HARD_CAP"] == 50_000)
t("EDIT_OBS_SOFT_CAP default = 10000", DEFAULTS["EDIT_OBS_SOFT_CAP"] == 10_000)
t("EDIT_OBS_AGE_DAYS default = 90", DEFAULTS["EDIT_OBS_AGE_DAYS"] == 90)
t("STRUCTURED_TOTAL_CAP default = 50000", DEFAULTS["STRUCTURED_TOTAL_CAP"] == 50_000)
t("SPAWN_WAIT_SECONDS default = 3.0", DEFAULTS["SPAWN_WAIT_SECONDS"] == 3.0)
t("LISTEN_BACKLOG default = 16", DEFAULTS["LISTEN_BACKLOG"] == 16)
t("MAX_CONCAT_FOLDS_PER_FILE default = 1000", DEFAULTS["MAX_CONCAT_FOLDS_PER_FILE"] == 1000)


# ---------------------------------------------------------------------------
# Every default has a one-line doc string.
# ---------------------------------------------------------------------------
section("DOCS covers every default")

missing = [k for k in DEFAULTS if k not in DOCS]
t("every threshold has a DOCS entry", not missing, f"missing={missing}")


# ---------------------------------------------------------------------------
# threshold() returns the default when env is unset.
# ---------------------------------------------------------------------------
section("threshold() unset env -> default")

os.environ.pop("CHAMELEON_WORKSPACE_FANOUT_CAP", None)
t("threshold('WORKSPACE_FANOUT_CAP') == 50", threshold("WORKSPACE_FANOUT_CAP") == 50)


# ---------------------------------------------------------------------------
# threshold() respects env override.
# ---------------------------------------------------------------------------
section("threshold() env override applies")

os.environ["CHAMELEON_WORKSPACE_FANOUT_CAP"] = "200"
try:
    t("env override returns 200", threshold("WORKSPACE_FANOUT_CAP") == 200)
    t("threshold_int returns int", isinstance(threshold_int("WORKSPACE_FANOUT_CAP"), int))
finally:
    del os.environ["CHAMELEON_WORKSPACE_FANOUT_CAP"]


# ---------------------------------------------------------------------------
# Non-numeric env value -> falls back to default.
# ---------------------------------------------------------------------------
section("Non-numeric env value falls back to default")

os.environ["CHAMELEON_WORKSPACE_FANOUT_CAP"] = "not-a-number"
try:
    t("non-numeric env -> default 50", threshold("WORKSPACE_FANOUT_CAP") == 50)
finally:
    del os.environ["CHAMELEON_WORKSPACE_FANOUT_CAP"]


# ---------------------------------------------------------------------------
# Float threshold respects float env.
# ---------------------------------------------------------------------------
section("Float threshold parses float env")

os.environ["CHAMELEON_SPAWN_WAIT_SECONDS"] = "1.5"
try:
    t("float env override applies", threshold_float("SPAWN_WAIT_SECONDS") == 1.5)
finally:
    del os.environ["CHAMELEON_SPAWN_WAIT_SECONDS"]


# ---------------------------------------------------------------------------
# Unknown threshold name raises.
# ---------------------------------------------------------------------------
section("Unknown threshold raises KeyError")

try:
    threshold("NOT_REAL")
    t("unknown name raises", False, "did not raise")
except KeyError:
    t("unknown name raises", True)


print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
