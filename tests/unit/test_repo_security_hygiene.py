"""Repo-hygiene guards: a security policy and a dependabot config must exist.

Locks in the C10 floor fix so the files are not silently dropped, and checks the
dependabot config is valid YAML covering the ecosystems the repo actually uses
(github-actions, npm, pip).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_security_md_exists_and_has_reporting_section():
    sec = REPO_ROOT / "SECURITY.md"
    assert sec.is_file(), "SECURITY.md is missing"
    text = sec.read_text(encoding="utf-8").lower()
    assert "report" in text and "vulnerab" in text


def test_dependabot_config_is_valid_and_covers_ecosystems():
    cfg_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert cfg_path.is_file(), ".github/dependabot.yml is missing"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("version") == 2
    ecosystems = {u["package-ecosystem"] for u in cfg.get("updates", [])}
    assert {"github-actions", "npm", "pip"} <= ecosystems
