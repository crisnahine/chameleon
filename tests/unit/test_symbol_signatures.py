"""Unit tests for the symbol-signature index backing C2.2 definition hydration.

Stores, per file, each named callable's parameter shape, declared param/return
type text (best-effort), and body span, so a turn-end / tool-time consumer can
hydrate the definitions of the symbols an edited file imports. Build-from-parsed
and load-from-artifact share one schema and must round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.symbol_signatures import (
    SYMBOL_SIGNATURES_FILENAME,
    build_symbol_signatures,
    load_symbol_signatures,
)


def _file(name: str, signatures: list[dict]) -> ParsedFile:
    return ParsedFile(
        path=Path(name),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras={"callable_signatures": signatures} if signatures else {},
    )


def _sig(name, params, *, start=1, end=5, return_type=None, **extra):
    base = {
        "name": name,
        "kind": "function",
        "params": params,
        "start_line": start,
        "end_line": end,
    }
    if return_type is not None:
        base["return_type"] = return_type
    base.update(extra)
    return base


def test_build_stores_callable_with_params_and_span(tmp_path):
    f = _file(
        str(tmp_path / "money.ts"),
        [
            _sig(
                "formatCurrency",
                [{"name": "amount", "optional": False, "kind": "positional", "type": "number"}],
                start=10,
                end=14,
                return_type="string",
            )
        ],
    )
    payload = build_symbol_signatures([f], tmp_path)
    entry = payload["files"]["money.ts"]["formatCurrency"]
    assert entry["start_line"] == 10
    assert entry["end_line"] == 14
    assert entry["return_type"] == "string"
    assert entry["params"][0]["type"] == "number"


def test_build_skips_callables_without_spans(tmp_path):
    f = _file(str(tmp_path / "a.ts"), [{"name": "noSpan", "kind": "function", "params": []}])
    payload = build_symbol_signatures([f], tmp_path)
    assert payload["files"] == {}


def test_build_dedups_ambiguous_name_keeping_first(tmp_path):
    f = _file(
        str(tmp_path / "a.ts"),
        [
            _sig("dup", [{"name": "a", "optional": False, "kind": "positional"}], start=1, end=3),
            _sig(
                "dup",
                [{"name": "x", "optional": False, "kind": "positional"}],
                start=5,
                end=7,
            ),
        ],
    )
    payload = build_symbol_signatures([f], tmp_path)
    # One entry per name; the first declaration wins.
    assert payload["files"]["a.ts"]["dup"]["start_line"] == 1


def test_load_round_trips(tmp_path):
    chameleon = tmp_path / ".chameleon"
    chameleon.mkdir()
    f = _file(
        str(tmp_path / "money.ts"),
        [_sig("formatCurrency", [{"name": "n", "optional": False, "kind": "positional"}])],
    )
    payload = build_symbol_signatures([f], tmp_path)
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text(json.dumps(payload))

    index = load_symbol_signatures(tmp_path)
    assert index is not None
    entry = index.lookup("money.ts", "formatCurrency")
    assert entry is not None
    assert entry["start_line"] == 1
    assert index.lookup("money.ts", "nope") is None
    assert index.lookup("other.ts", "formatCurrency") is None


def test_load_missing_or_corrupt_is_none(tmp_path):
    assert load_symbol_signatures(tmp_path) is None  # no .chameleon
    chameleon = tmp_path / ".chameleon"
    chameleon.mkdir()
    assert load_symbol_signatures(tmp_path) is None  # no artifact
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text("{ not json")
    assert load_symbol_signatures(tmp_path) is None  # corrupt
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text(
        json.dumps({"schema_version": 999, "files": {}})
    )
    assert load_symbol_signatures(tmp_path) is None  # future schema


# ---------------------------------------------------------------------------
# wiring: trust surface, txn protocol, bootstrap (integration)
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from chameleon_mcp.bootstrap.transaction import _PROTOCOL_FILES  # noqa: E402
from chameleon_mcp.profile.trust import _HASHED_ARTIFACTS  # noqa: E402


def test_symbol_signatures_in_trust_surface():
    assert "symbol_signatures.json" in _HASHED_ARTIFACTS


def test_symbol_signatures_in_txn_protocol():
    # Drop-on-failure posture (judge-facing index): stale is worse than none.
    assert "symbol_signatures.json" in _PROTOCOL_FILES


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_bootstrap_writes_symbol_signatures(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    services = repo / "src" / "services"
    services.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (services / "money.ts").write_text(
        "export function formatCurrency(amount: number, code: string): string {\n"
        "  return code + amount\n"
        "}\n",
        encoding="utf-8",
    )
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        (services / f"{name}.ts").write_text(
            f"export function {name}(x: string): string {{ return x }}\n", encoding="utf-8"
        )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")

    result = tools.bootstrap_repo(str(repo))
    assert result["data"]["status"] == "success"
    artifact = repo / ".chameleon" / "symbol_signatures.json"
    assert artifact.is_file(), "bootstrap did not write symbol_signatures.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    entry = payload["files"]["src/services/money.ts"]["formatCurrency"]
    assert entry["return_type"] == "string"
    assert entry["params"][0]["type"] == "number"

    # The loader reads it back.
    index = load_symbol_signatures(repo)
    assert index is not None
    assert index.lookup("src/services/money.ts", "formatCurrency") is not None


# ---------------------------------------------------------------------------
# Unit D: render + hydrate imported definitions for the judge
# ---------------------------------------------------------------------------

from chameleon_mcp.symbol_signatures import (  # noqa: E402
    hydrate_imported_definitions,
    render_imported_definition,
)


def test_render_imported_definition_signature():
    entry = {
        "params": [
            {"name": "amount", "optional": False, "kind": "positional", "type": "number"},
            {"name": "code", "optional": True, "kind": "positional", "type": "string"},
        ],
        "return_type": "string",
        "start_line": 1,
        "end_line": 3,
    }
    line = render_imported_definition("formatCurrency", entry, "src/money.ts")
    assert "formatCurrency(" in line
    assert "amount: number" in line
    assert "code?: string" in line
    assert "): string" in line
    assert "src/money.ts" in line


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_hydrate_imported_definitions_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo_hy"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / "money.ts").write_text(
        "export function formatCurrency(amount: number, code: string): string {\n"
        "  return code + amount\n"
        "}\n"
    )
    (sub / "checkout.ts").write_text(
        'import { formatCurrency } from "../money"\n'
        "export function checkout(total: number) { return formatCurrency(total, 'USD') }\n"
    )
    # Build + persist the index from the defining file.
    from chameleon_mcp.extractors.typescript import TypeScriptExtractor

    res = TypeScriptExtractor().parse_repo(repo, paths=[repo / "money.ts"])
    payload = build_symbol_signatures(list(res.files), repo)
    chameleon = repo / ".chameleon"
    chameleon.mkdir()
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text(json.dumps(payload))

    lines = hydrate_imported_definitions(repo, [sub / "checkout.ts"], max_items=10)
    assert any("formatCurrency(" in ln and "money.ts" in ln for ln in lines)


def test_hydrate_no_index_returns_empty(tmp_path):
    # No artifact -> fail-open, no hydration.
    assert hydrate_imported_definitions(tmp_path, [tmp_path / "x.ts"], max_items=10) == []


# ---------------------------------------------------------------------------
# Unit E: judge wiring (build_prompt + imported_definition_facts + config)
# ---------------------------------------------------------------------------


def test_build_prompt_includes_imported_defs_block():
    from chameleon_mcp.judge import FileDiff, build_prompt

    diffs = [FileDiff(rel_path="a.ts", diff_text="+ x", is_whole_file=False, archetype=None)]
    block = "Definitions of symbols this change IMPORTS ...:\n- formatCurrency(n: number): string — money.ts"
    prompt = build_prompt(Path("/r"), Path("/r/.chameleon"), diffs, imported_defs=block)
    assert "formatCurrency(n: number): string" in prompt
    assert "imported contract" in prompt


def test_config_judge_imported_definitions_defaults_true_and_roundtrips():
    from chameleon_mcp.profile.config import EnforcementConfig, _coerce_enforcement

    assert EnforcementConfig().judge_imported_definitions is True
    # An existing config WITHOUT the key defaults to True (back-compat).
    cfg = _coerce_enforcement({})
    assert cfg.judge_imported_definitions is True
    cfg2 = _coerce_enforcement({"judge_imported_definitions": False})
    assert cfg2.judge_imported_definitions is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_imported_definition_facts_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.extractors.typescript import TypeScriptExtractor
    from chameleon_mcp.judge import FileDiff, imported_definition_facts

    repo = tmp_path / "repo_jw"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / "money.ts").write_text(
        "export function formatCurrency(amount: number, code: string): string {\n  return code\n}\n"
    )
    (sub / "checkout.ts").write_text(
        'import { formatCurrency } from "../money"\nexport function checkout(t: number) { return formatCurrency(t) }\n'
    )
    res = TypeScriptExtractor().parse_repo(repo, paths=[repo / "money.ts"])
    payload = build_symbol_signatures(list(res.files), repo)
    chameleon = repo / ".chameleon"
    chameleon.mkdir()
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text(json.dumps(payload))

    diffs = [
        FileDiff(rel_path="sub/checkout.ts", diff_text="+ x", is_whole_file=False, archetype=None)
    ]
    block = imported_definition_facts(repo, diffs)
    assert "formatCurrency(" in block and "money.ts" in block


# ---------------------------------------------------------------------------
# C2.2 review fixes: render kinds, location, size caps
# ---------------------------------------------------------------------------


def test_render_rest_param_uses_spread_not_optional_marker():
    entry = {
        "params": [{"name": "args", "optional": True, "kind": "rest", "type": "string[]"}],
        "start_line": 2,
        "end_line": 4,
    }
    line = render_imported_definition("log", entry, "u.ts")
    assert "...args: string[]" in line
    assert "args?" not in line


def test_render_destructured_param_shows_braces():
    entry = {
        "params": [{"name": "{}", "optional": False, "kind": "destructured", "type": "Options"}],
        "start_line": 1,
        "end_line": 3,
    }
    line = render_imported_definition("make", entry, "u.ts")
    assert ": Options" in line
    assert "{…}" in line or "{...}" in line


def test_render_includes_definition_line():
    entry = {"params": [], "start_line": 42, "end_line": 50}
    line = render_imported_definition("f", entry, "src/u.ts")
    assert "src/u.ts:42" in line


def test_build_truncates_oversized_type_text(tmp_path):
    huge = "{ " + ", ".join(f"f{i}: string" for i in range(200)) + " }"
    f = _file(
        str(tmp_path / "big.ts"),
        [_sig("h", [{"name": "x", "optional": False, "kind": "positional", "type": huge}])],
    )
    payload = build_symbol_signatures([f], tmp_path)
    stored_type = payload["files"]["big.ts"]["h"]["params"][0]["type"]
    assert len(stored_type) < len(huge)
    assert stored_type.endswith("…")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_imported_definition_facts_block_is_char_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_JUDGE_IMPORTED_DEFS_CHAR_CAP", "120")
    from chameleon_mcp.extractors.typescript import TypeScriptExtractor
    from chameleon_mcp.judge import FileDiff, imported_definition_facts

    repo = tmp_path / "repo_cap"
    repo.mkdir()
    # Several defining files, each exporting a long-named symbol the editor imports.
    names = [f"reallyLongFunctionNameNumber{i}" for i in range(8)]
    for i, nm in enumerate(names):
        (repo / f"m{i}.ts").write_text(f"export function {nm}(a: number): string {{ return '' }}\n")
    imp = "\n".join(f'import {{ {nm} }} from "./m{i}"' for i, nm in enumerate(names))
    (repo / "edit.ts").write_text(imp + "\nexport function use() { return 1 }\n")

    files = [repo / f"m{i}.ts" for i in range(8)]
    res = TypeScriptExtractor().parse_repo(repo, paths=files)
    payload = build_symbol_signatures(list(res.files), repo)
    chameleon = repo / ".chameleon"
    chameleon.mkdir()
    (chameleon / SYMBOL_SIGNATURES_FILENAME).write_text(json.dumps(payload))

    diffs = [FileDiff(rel_path="edit.ts", diff_text="+ x", is_whole_file=False, archetype=None)]
    block = imported_definition_facts(repo, diffs)
    # Bounded well under the 8 full lines; truncation is signalled.
    assert len(block) <= 400
    assert "more" in block.lower() or "truncat" in block.lower()
