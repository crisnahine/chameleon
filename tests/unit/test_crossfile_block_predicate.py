"""Tests for the crossfile-existence BLOCK predicate (roadmap #10, Step B).

These drive ``_confirmed_crossfile_break_sites`` and its F3 (git-HEAD-export
scope) + F2 (strict per-importer target-sourcing) helpers against a REAL git
repo -- F3 reads ``git show HEAD:<path>`` to decide whether the removal was
introduced this turn, so the test needs a committed HEAD, not just files on disk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from chameleon_mcp.hook_helper import (
    _confirmed_crossfile_break_sites,
    _module_exports_at_head,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _commit(root: Path, msg: str = "c") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


def _rec(root: Path, name="oldName", target="src/pricing.ts", kind="export", lang="typescript"):
    return {
        "name": name,
        "target_key": target,
        "kind": kind,
        "lang": lang,
        "ws_root": root,
        "importers": [("src/cart.ts", 1)],
    }


def test_turn_introduced_export_removal_is_block_eligible(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    # This turn removes the export (still importer-referenced).
    _write(tmp_path, "src/pricing.ts", "export function newName() {}\n")
    sites = _confirmed_crossfile_break_sites(_rec(tmp_path))
    assert sites == [("src/cart.ts", 1)]


def test_pre_existing_head_break_is_not_block_eligible(tmp_path):
    # oldName was NEVER exported at HEAD; the importer was already broken. Editing
    # pricing this turn must NOT block a defect the turn didn't introduce (F3).
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function keep() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function keep2() {}\n")  # unrelated edit
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_same_turn_repoint_to_bare_package_is_not_block_eligible(tmp_path):
    # F2: the importer repoints oldName to a bare package this turn. The specifier
    # no longer resolves in-repo -> empty keys -> the strict predicate refuses to
    # block (the keep-bias over-block the advisory tolerates but a deny must not).
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function newName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from '@scope/pricing';\noldName();\n")
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_repoint_to_other_inrepo_module_is_not_block_eligible(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/other.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function newName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './other';\noldName();\n")
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_commented_out_stale_import_does_not_block(tmp_path):
    # BLOCK-1: a repoint that left the OLD import commented out must not defeat F2.
    # foo is live-imported from ./other; the commented `./pricing` line must not
    # re-introduce the target key.
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/other.ts", "export function oldName() {}\n")
    _write(
        tmp_path,
        "src/cart.ts",
        "import { oldName } from './other';\n// import { oldName } from './pricing';\noldName();\n",
    )
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function newName() {}\n")  # oldName removed
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_block_comment_stale_import_does_not_block(tmp_path):
    # Same defeat via a /* */ block comment around the stale import.
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/other.ts", "export function oldName() {}\n")
    _write(
        tmp_path,
        "src/cart.ts",
        "import { oldName } from './other';\n"
        "/* import { oldName } from './pricing'; */\n"
        "oldName();\n",
    )
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function newName() {}\n")
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_target_reexports_name_does_not_block(tmp_path):
    # Defense-in-depth: the target now re-exports oldName, so it still provides it.
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export { oldName } from './impl';\n")
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_es_to_cjs_conversion_does_not_block(tmp_path):
    # FIX-2: converting the ES export to CommonJS in one turn must not read as a
    # removal (the name is still provided via module.exports under CJS interop).
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    _write(tmp_path, "src/cart.ts", "import { oldName } from './pricing';\noldName();\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "function oldName() {}\nmodule.exports = { oldName };\n")
    assert _confirmed_crossfile_break_sites(_rec(tmp_path)) == []


def test_ruby_constant_stays_advisory_only(tmp_path):
    # Global constant resolution: cannot cheaply prove no other file defines it at
    # Stop, so a Ruby constant break is advisory, never a block, in v1.
    assert _confirmed_crossfile_break_sites(_rec(tmp_path, kind="constant", lang="ruby")) == []


def test_barrel_stays_advisory_only(tmp_path):
    assert _confirmed_crossfile_break_sites(_rec(tmp_path, kind="barrel")) == []


def test_deleted_module_stays_advisory_only_in_v1(tmp_path):
    # A gone target makes the importer specifier unresolvable, so strict F2 cannot
    # separate "still points at target" from "repointed to a bare package". v1
    # keeps deleted advisory-only (still surfaced, never blocked). Under-block safe.
    _init_repo(tmp_path)
    _write(tmp_path, "src/mod.ts", "export function gone() {}\n")
    _write(tmp_path, "src/cart.ts", "import { gone } from './mod';\ngone();\n")
    _commit(tmp_path)
    (tmp_path / "src/mod.ts").unlink()
    rec = _rec(tmp_path, name="gone", target="src/mod.ts", kind="deleted")
    assert _confirmed_crossfile_break_sites(rec) == []


def test_module_exports_at_head_none_without_git(tmp_path):
    # No git repo -> cannot confirm -> None (fail-safe: caller does not block).
    _write(tmp_path, "src/pricing.ts", "export function oldName() {}\n")
    assert _module_exports_at_head(tmp_path, "src/pricing.ts", "typescript") is None


def test_module_exports_at_head_reads_committed_set(tmp_path):
    _init_repo(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function a() {}\nexport const b = 1;\n")
    _commit(tmp_path)
    _write(tmp_path, "src/pricing.ts", "export function a() {}\n")  # b removed this turn
    got = _module_exports_at_head(tmp_path, "src/pricing.ts", "typescript")
    assert got is not None and "a" in got and "b" in got
