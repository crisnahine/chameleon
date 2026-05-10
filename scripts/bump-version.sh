#!/usr/bin/env bash
# bump-version.sh — keep .claude-plugin/plugin.json, package.json, and
# mcp/pyproject.toml versions in sync.
#
# Usage:
#   scripts/bump-version.sh patch    # 0.1.0 → 0.1.1
#   scripts/bump-version.sh minor    # 0.1.0 → 0.2.0
#   scripts/bump-version.sh major    # 0.1.0 → 1.0.0
#   scripts/bump-version.sh 0.5.0    # explicit version
#
# Phase 1C placeholder. Phase 7 will implement the real version bump logic.

set -euo pipefail

echo "scripts/bump-version.sh: Phase 1C placeholder."
echo ""
echo "Phase 7 implementation will:"
echo "  1. Read current version from .claude-plugin/plugin.json"
echo "  2. Compute new version based on argument (patch/minor/major or explicit)"
echo "  3. Update .claude-plugin/plugin.json"
echo "  4. Update package.json"
echo "  5. Update mcp/pyproject.toml"
echo "  6. Print confirmation; user reviews before committing"
echo ""
echo "Usage will be: $0 [patch|minor|major|<x.y.z>]"
exit 1
