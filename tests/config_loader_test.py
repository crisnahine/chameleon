"""Tests for the v0.6.0 .chameleon/config.json loader."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name

from chameleon_mcp.profile.config import (  # noqa: E402
    AutoRefreshConfig,
    ChameleonConfig,
    ChameleonConfigError,
    TrustConfig,
    load_config,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def expect_raises(name: str, exc_type, fn) -> None:
    try:
        fn()
        t(name, False, "did not raise")
    except exc_type as e:
        t(name, True, str(e)[:80])
    except Exception as e:  # noqa: BLE001
        t(name, False, f"wrong exc: {type(e).__name__}: {e}")


section("Missing file → defaults (v0.5.x compat)")
with tempfile.TemporaryDirectory() as td:
    cfg = load_config(Path(td))
    t("returns ChameleonConfig", isinstance(cfg, ChameleonConfig))
    t("canonical_ref is None", cfg.canonical_ref is None)
    t("branch_pinning_enabled is False", not cfg.branch_pinning_enabled)
    t("auto_refresh.enabled is False", not cfg.auto_refresh.enabled)
    t("trust.auto_preserve_when is None", cfg.trust.auto_preserve_when is None)
    t("auto_rename is True (default ON in v0.6.0)", cfg.auto_rename is True)


section("Full config round-trips")
with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td) / "config.json"
    cfg_path.write_text(
        json.dumps({
            "$schema": "chameleon-config-0.6.0",
            "canonical_ref": "origin/main",
            "auto_refresh": {
                "enabled": True,
                "drift_threshold": 0.15,
                "max_age_hours": 72,
            },
            "trust": {"auto_preserve_when": "pulled_from_remote"},
            "auto_rename": False,
        }),
        encoding="utf-8",
    )
    cfg = load_config(Path(td))
    t("canonical_ref == 'origin/main'", cfg.canonical_ref == "origin/main")
    t("branch_pinning_enabled is True", cfg.branch_pinning_enabled)
    t("auto_refresh.enabled is True", cfg.auto_refresh.enabled)
    t(
        "auto_refresh.drift_threshold == 0.15",
        abs(cfg.auto_refresh.drift_threshold - 0.15) < 1e-9,
    )
    t("auto_refresh.max_age_hours == 72", cfg.auto_refresh.max_age_hours == 72)
    t(
        "trust.auto_preserve_when == 'pulled_from_remote'",
        cfg.trust.auto_preserve_when == "pulled_from_remote",
    )
    t("auto_rename overridden to False", cfg.auto_rename is False)


section("Partial config — missing fields use defaults")
with tempfile.TemporaryDirectory() as td:
    (Path(td) / "config.json").write_text(
        json.dumps({"canonical_ref": "origin/master"}), encoding="utf-8"
    )
    cfg = load_config(Path(td))
    t("canonical_ref set", cfg.canonical_ref == "origin/master")
    t("auto_refresh defaults to disabled", not cfg.auto_refresh.enabled)
    t("trust defaults to None", cfg.trust.auto_preserve_when is None)
    t("auto_rename defaults to True", cfg.auto_rename is True)


section("Validation errors")


def _write(td: Path, contents) -> None:
    (Path(td) / "config.json").write_text(json.dumps(contents), encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"unknown_field": 1})
    expect_raises(
        "unknown top-level key raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"canonical_ref": ""})
    expect_raises(
        "empty canonical_ref raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"canonical_ref": 123})
    expect_raises(
        "non-string canonical_ref raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"auto_refresh": "yes"})
    expect_raises(
        "non-object auto_refresh raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"auto_refresh": {"enabled": True, "drift_threshold": 1.5}})
    expect_raises(
        "out-of-range drift_threshold raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"trust": {"auto_preserve_when": "everything"}})
    expect_raises(
        "unknown auto_preserve_when value raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    _write(Path(td), {"trust": {"unknown_inner": 1}})
    expect_raises(
        "unknown key inside trust raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )

with tempfile.TemporaryDirectory() as td:
    (Path(td) / "config.json").write_text("not valid json{", encoding="utf-8")
    expect_raises(
        "malformed JSON raises",
        ChameleonConfigError,
        lambda: load_config(Path(td)),
    )


section("Dataclass invariants")
t(
    "ChameleonConfig.AutoRefreshConfig is the default factory",
    isinstance(ChameleonConfig().auto_refresh, AutoRefreshConfig),
)
t(
    "ChameleonConfig.TrustConfig is the default factory",
    isinstance(ChameleonConfig().trust, TrustConfig),
)


section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
