"""Verify /chameleon-init from zero state.

All prior tests assumed .chameleon/ already existed. This test fakes
a brand-new repo (no .chameleon/, no .git, no plugin data) and
exercises the cold-start path:

Round 1 — direct bootstrap_repo on synthetic TS + Ruby repos with
          fresh state. Verify .chameleon/ created, all 5 artifacts
          present, COMMITTED sentinel, archetypes detected.
Round 2 — real Claude Code session on a fresh temp repo where Claude
          has to invoke bootstrap_repo via the /chameleon-init skill
          without prior trust or profile.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import bootstrap_repo


def make_synthetic_ts_repo(root: Path, n_files: int = 30) -> None:
    """Create a TS repo with multiple structurally similar files so clusters form."""
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "queries").mkdir(parents=True)
    (root / "src" / "utils").mkdir(parents=True)
    (root / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"strict": True, "target": "ESNext"}}, indent=2)
    )
    (root / "package.json").write_text(
        json.dumps({"name": "synthetic", "dependencies": {"typescript": "5.0.0"}})
    )
    # Six clusters of >=5 similar files (cluster threshold is 5 by default).
    for i in range(n_files // 3):
        (root / "src" / "components" / f"Component{i}.tsx").write_text(
            f"import React from 'react';\nexport const Component{i} = () => <div>{i}</div>;\n"
        )
    for i in range(n_files // 3):
        (root / "src" / "queries" / f"useQuery{i}.ts").write_text(
            f"import {{ useQuery }} from 'react-query';\nexport const useQuery{i} = () => useQuery('q{i}', async () => {i});\n"
        )
    for i in range(n_files // 3):
        (root / "src" / "utils" / f"util{i}.ts").write_text(
            f"export const util{i} = (x: number) => x + {i};\n"
        )


def make_synthetic_ruby_repo(root: Path, n_files: int = 30) -> None:
    """Create a Rails-style repo with structurally similar files."""
    (root / "app" / "models").mkdir(parents=True)
    (root / "app" / "controllers" / "api" / "v1").mkdir(parents=True)
    (root / "app" / "services").mkdir(parents=True)
    (root / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    (root / "config").mkdir()
    (root / "config" / "application.rb").write_text(
        "require 'rails'\nmodule App\n  class Application < Rails::Application\n  end\nend\n"
    )
    for i in range(n_files // 3):
        (root / "app" / "models" / f"model_{i}.rb").write_text(
            f"class Model{i} < ApplicationRecord\n  validates :name, presence: true\nend\n"
        )
    for i in range(n_files // 3):
        (root / "app" / "controllers" / "api" / "v1" / f"resource_{i}_controller.rb").write_text(
            f"class Api::V1::Resource{i}Controller < ApplicationController\n  def index\n    head :ok\n  end\nend\n"
        )
    for i in range(n_files // 3):
        (root / "app" / "services" / f"service_{i}.rb").write_text(
            f"class Service{i}\n  def call\n    {i}\n  end\nend\n"
        )


# ---------------------------------------------------------------------------
# Round 1 — synthetic TS repo cold start
# ---------------------------------------------------------------------------
section("Round 1 — cold-start bootstrap on synthetic TS repo")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "fresh_ts"
    repo.mkdir()
    make_synthetic_ts_repo(repo)

    t(".chameleon/ does NOT exist before init", not (repo / ".chameleon").exists())

    r = bootstrap_repo(str(repo))["data"]
    t(f"Bootstrap status=success ({r['status']})", r["status"] == "success")
    t(
        f"Archetypes detected (got {r['archetypes_detected']})",
        r["archetypes_detected"] >= 2,
    )
    t(f"Files processed > 0 (got {r['files_processed']})", r["files_processed"] > 0)

    # All 5 artifacts present + COMMITTED
    chameleon_dir = repo / ".chameleon"
    t("profile.json exists", (chameleon_dir / "profile.json").is_file())
    t("archetypes.json exists", (chameleon_dir / "archetypes.json").is_file())
    t("rules.json exists", (chameleon_dir / "rules.json").is_file())
    t("canonicals.json exists", (chameleon_dir / "canonicals.json").is_file())
    t("idioms.md exists", (chameleon_dir / "idioms.md").is_file())
    t("COMMITTED sentinel exists", (chameleon_dir / "COMMITTED").is_file())
    t(
        "profile.summary.md exists",
        (chameleon_dir / "profile.summary.md").is_file(),
    )


# ---------------------------------------------------------------------------
# Round 1 — synthetic Ruby repo cold start
# ---------------------------------------------------------------------------
section("Round 1 — cold-start bootstrap on synthetic Ruby repo")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "fresh_ruby"
    repo.mkdir()
    make_synthetic_ruby_repo(repo)

    t(".chameleon/ does NOT exist before init", not (repo / ".chameleon").exists())

    r = bootstrap_repo(str(repo))["data"]
    t(f"Ruby bootstrap status=success ({r['status']})", r["status"] == "success")
    t(
        f"Ruby archetypes detected (got {r['archetypes_detected']})",
        r["archetypes_detected"] >= 2,
    )

    chameleon_dir = repo / ".chameleon"
    profile = json.loads((chameleon_dir / "profile.json").read_text())
    t(
        f"profile.json language=ruby (got {profile.get('language')})",
        profile.get("language") == "ruby",
    )


# ---------------------------------------------------------------------------
# Round 2 — Claude Code /chameleon-init on fresh temp repo
# ---------------------------------------------------------------------------
section("Round 2 — real Claude Code /chameleon-init on fresh repo")

if shutil.which("claude") is None:
    print("  SKIP: claude CLI not on PATH")
else:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "claude_fresh"
        repo.mkdir()
        make_synthetic_ts_repo(repo, n_files=30)

        t(".chameleon/ absent before Claude session", not (repo / ".chameleon").exists())

        proc = subprocess.run(
            [
                "claude", "-p",
                f"/chameleon:chameleon-init\n\nThe repo I want to bootstrap is the current working directory: {repo}. Please bootstrap it by calling chameleon-mcp's bootstrap_repo tool. Stop after the call returns success.",
                "--plugin-dir", str(PLUGIN_ROOT),
                "--output-format", "stream-json",
                "--max-turns", "10",
                "--verbose",
                "--allowedTools",
                "Bash Read mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=240,
        )

        events = []
        for line in proc.stdout.splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Verify Claude called bootstrap_repo
        called_bootstrap = False
        for e in events:
            msg = e.get("message", {})
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_use"
                    and item.get("name") == "mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo"
                ):
                    called_bootstrap = True

        t(
            "Claude invoked bootstrap_repo via /chameleon-init",
            called_bootstrap,
        )
        t(
            ".chameleon/profile.json exists after Claude session",
            (repo / ".chameleon" / "profile.json").is_file(),
        )
        t(
            "COMMITTED sentinel exists after Claude session",
            (repo / ".chameleon" / "COMMITTED").is_file(),
        )


# ---------------------------------------------------------------------------
# Round 2 — re-init on existing profile (skill should suggest refresh)
# ---------------------------------------------------------------------------
section("Round 2 — bootstrap on already-bootstrapped repo is idempotent")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "double_init"
    repo.mkdir()
    make_synthetic_ts_repo(repo)

    r1 = bootstrap_repo(str(repo))["data"]
    t("First bootstrap success", r1["status"] == "success")

    # Re-bootstrap on same repo — should still succeed (idempotent)
    r2 = bootstrap_repo(str(repo))["data"]
    t("Second bootstrap success (idempotent)", r2["status"] == "success")
    t(
        "Same archetype count across runs",
        r1["archetypes_detected"] == r2["archetypes_detected"],
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
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
