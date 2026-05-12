"""Verification of BUG-023 (continued): detect_repo surfaces unsupported schema.

Pre-v0.5.7 the profile-loader correctly refused schema_version > MAX_SUPPORTED,
but detect_repo only opened profile.json to test parseability. A v99
profile reported profile_status: "profile_present" and the only signal of the
mismatch was the later load failure. Now detect_repo peeks schema_version
and returns profile_status: "profile_unsupported_schema_version".
"""

import json
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import detect_repo

section("schema_version=99 profile flagged as unsupported")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "future-profile"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5"}}')
    (repo / "tsconfig.json").write_text("{}")
    cham = repo / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(json.dumps({
        "schema_version": 99,
        "language": "typescript",
        "engine_min_version": "1.0.0",
    }))
    (cham / "COMMITTED").touch()
    src = repo / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export const x = 1;")

    resp = detect_repo(str(src))
    status = resp["data"]["profile_status"]
    trust = resp["data"]["trust_state"]
    t("profile_status is 'profile_unsupported_schema_version'",
      status == "profile_unsupported_schema_version",
      f"got {status}")
    t("trust_state is 'n/a' for unsupported schema",
      trust == "n/a",
      f"got {trust}")

section("Normal v7 profile still reports profile_present")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "normal-profile"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5"}}')
    (repo / "tsconfig.json").write_text("{}")
    cham = repo / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(json.dumps({
        "schema_version": 7,
        "language": "typescript",
    }))
    (cham / "COMMITTED").touch()
    src = repo / "src" / "x.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export const x = 1;")

    resp = detect_repo(str(src))
    status = resp["data"]["profile_status"]
    t("v7 profile still profile_present", status == "profile_present", f"got {status}")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} — {info}")
    sys.exit(1)
