"""End-to-end tests for the cross-file existence BLOCK branch in _stop_gates
(roadmap #10, Step C).

Drives the real ``_stop_gates`` with a committed git repo, a reverse index, a
config, and an EnforcementState, so the whole seam runs: compute breaks ->
_confirmed_crossfile_break_sites (F3 HEAD scope + F2 strict sourcing) -> mode gate
-> block / would_block. Trust is the caller's (``_gate_one_root``) concern, so
these call ``_stop_gates`` directly.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state
from chameleon_mcp.hook_helper import _stop_gates
from chameleon_mcp.symbol_index import REVERSE_INDEX_FILENAME, SCHEMA_VERSION

REPO_ID = "cfblock"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _build(
    tmp_path: Path, *, mode: str, pricing_head: str, pricing_now: str, cart: str, block: bool = True
):
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "config.json").write_text(
        json.dumps(
            {
                "enforcement": {
                    "mode": mode,
                    "stop_backstop": True,
                    "crossfile_existence_block": block,
                }
            }
        ),
        encoding="utf-8",
    )
    (cham / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _write(repo, "src/pricing.ts", pricing_head)
    _write(repo, "src/cart.ts", cart)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    # This turn's edit: remove the export (still on disk, just changed).
    _write(repo, "src/pricing.ts", pricing_now)
    (cham / REVERSE_INDEX_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "targets": {"src/pricing.ts": {"oldName": [{"path": "src/cart.ts", "line": 1}]}},
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data" / REPO_ID
    data.mkdir(parents=True, exist_ok=True)
    st = EnforcementState()
    st.files[str(repo / "src/pricing.ts")] = FileState()
    save_state(st, data, "sess")
    return repo, data


def _drive(tmp_path: Path, repo: Path, data: Path) -> dict:
    payload = {"tool_name": "Edit", "session_id": "sess"}
    with patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data")}, clear=False):
        return _stop_gates(
            payload=payload,
            repo_root=repo,
            repo_id=REPO_ID,
            session_id="sess",
            is_subagent=False,
            repo_data=data,
            allow_model_spawn=False,
        )


_HEAD = "export function oldName() {}\nexport function editPrice() {}\n"
_NOW = "export function editPrice() {}\n"  # oldName removed this turn
_CART = "import { oldName } from './pricing';\noldName();\n"


def test_enforce_blocks_turn_introduced_export_removal(tmp_path):
    repo, data = _build(tmp_path, mode="enforce", pricing_head=_HEAD, pricing_now=_NOW, cart=_CART)
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") == "block"
    assert "oldName" in out.get("reason", "")
    assert "src/cart.ts:1" in out.get("reason", "")


def test_shadow_does_not_block(tmp_path):
    repo, data = _build(tmp_path, mode="shadow", pricing_head=_HEAD, pricing_now=_NOW, cart=_CART)
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"


def test_opt_in_default_off_does_not_block(tmp_path):
    # The deny is opt-in: enforce mode alone does NOT block until the repo sets
    # enforcement.crossfile_existence_block = true.
    repo, data = _build(
        tmp_path, mode="enforce", pricing_head=_HEAD, pricing_now=_NOW, cart=_CART, block=False
    )
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"


def test_off_does_not_block(tmp_path):
    repo, data = _build(tmp_path, mode="off", pricing_head=_HEAD, pricing_now=_NOW, cart=_CART)
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"


def test_pre_existing_head_break_does_not_block(tmp_path):
    # oldName was never exported at HEAD -> the break pre-exists -> F3 suppresses.
    head = "export function editPrice() {}\n"  # no oldName at HEAD
    repo, data = _build(tmp_path, mode="enforce", pricing_head=head, pricing_now=_NOW, cart=_CART)
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"


def test_mid_turn_repoint_to_bare_package_does_not_block(tmp_path):
    # F2: cart repointed oldName to a bare package this turn -> not our break.
    cart = "import { oldName } from '@scope/pricing';\noldName();\n"
    repo, data = _build(tmp_path, mode="enforce", pricing_head=_HEAD, pricing_now=_NOW, cart=cart)
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"


def _build_py(tmp_path: Path, *, mode: str, block: bool = True):
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "config.json").write_text(
        json.dumps(
            {
                "enforcement": {
                    "mode": mode,
                    "stop_backstop": True,
                    "crossfile_existence_block": block,
                }
            }
        ),
        encoding="utf-8",
    )
    (cham / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _write(repo, "pricing.py", "def old_name():\n    pass\n\n\ndef edit_price():\n    pass\n")
    _write(repo, "cart.py", "from pricing import old_name\n\nold_name()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    _write(repo, "pricing.py", "def edit_price():\n    pass\n")  # old_name removed this turn
    (cham / REVERSE_INDEX_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "targets": {"pricing.py": {"old_name": [{"path": "cart.py", "line": 1}]}},
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data" / REPO_ID
    data.mkdir(parents=True, exist_ok=True)
    st = EnforcementState()
    st.files[str(repo / "pricing.py")] = FileState()
    save_state(st, data, "sess")
    return repo, data


def test_python_enforce_blocks_turn_introduced_removal(tmp_path):
    # Language coverage: the deny works for Python (module-level def removed).
    repo, data = _build_py(tmp_path, mode="enforce")
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") == "block"
    assert "old_name" in out.get("reason", "")


def test_python_shadow_does_not_block(tmp_path):
    repo, data = _build_py(tmp_path, mode="shadow")
    out = _drive(tmp_path, repo, data)
    assert out.get("decision") != "block"
