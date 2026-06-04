"""Unit tests for the caller-callee reverse index (build / load / query)."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.symbol_index import (
    REVERSE_INDEX_FILENAME,
    SCHEMA_VERSION,
    Importer,
    ReverseIndex,
    build_reverse_index,
    load_reverse_index,
    module_key_for_path,
)


@dataclass
class FakeParsed:
    path: Path
    extras: dict = field(default_factory=dict)


def _write_index(repo: Path, payload: dict) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / REVERSE_INDEX_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def _touch(repo: Path, rel: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("// stub\n", encoding="utf-8")
    return p


class TestBuild:
    def test_resolves_relative_import_to_target_key(self, tmp_path):
        _touch(tmp_path, "src/pricing.ts")
        importer = FakeParsed(
            tmp_path / "src" / "cart.ts",
            {"import_symbols": [{"name": "editPrice", "module": "./pricing", "line": 3}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["schema_version"] == SCHEMA_VERSION
        rows = idx["targets"]["src/pricing.ts"]["editPrice"]
        assert rows == [{"path": "src/cart.ts", "line": 3}]

    def test_bare_package_import_dropped(self, tmp_path):
        importer = FakeParsed(
            tmp_path / "a.ts",
            {"import_symbols": [{"name": "useState", "module": "react", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_unresolvable_relative_dropped(self, tmp_path):
        # The target file does not exist on disk -> no in-repo key.
        importer = FakeParsed(
            tmp_path / "a.ts",
            {"import_symbols": [{"name": "x", "module": "./ghost", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_multiple_importers_sorted_and_deduped(self, tmp_path):
        _touch(tmp_path, "lib/util.ts")
        importers = [
            FakeParsed(
                tmp_path / "b.ts",
                {"import_symbols": [{"name": "fmt", "module": "./lib/util", "line": 2}]},
            ),
            FakeParsed(
                tmp_path / "a.ts",
                {"import_symbols": [{"name": "fmt", "module": "./lib/util", "line": 9}]},
            ),
            # Duplicate of a.ts row collapses.
            FakeParsed(
                tmp_path / "a.ts",
                {"import_symbols": [{"name": "fmt", "module": "./lib/util", "line": 9}]},
            ),
        ]
        idx = build_reverse_index(importers, tmp_path)
        rows = idx["targets"]["lib/util.ts"]["fmt"]
        assert rows == [{"path": "a.ts", "line": 9}, {"path": "b.ts", "line": 2}]

    def test_directory_index_target(self, tmp_path):
        _touch(tmp_path, "widgets/index.ts")
        importer = FakeParsed(
            tmp_path / "page.ts",
            {"import_symbols": [{"name": "Widget", "module": "./widgets", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert "widgets/index.ts" in idx["targets"]

    def test_importer_outside_root_skipped(self, tmp_path):
        _touch(tmp_path, "pricing.ts")
        outside = tmp_path.parent / "out.ts"
        importer = FakeParsed(
            outside,
            {"import_symbols": [{"name": "editPrice", "module": "./pricing", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_malformed_rows_dropped(self, tmp_path):
        _touch(tmp_path, "m.ts")
        importer = FakeParsed(
            tmp_path / "a.ts",
            {
                "import_symbols": [
                    "not-a-dict",
                    {"name": 5, "module": "./m", "line": 1},
                    {"name": "ok", "module": "./m", "line": None},
                ]
            },
        )
        idx = build_reverse_index([importer], tmp_path)
        rows = idx["targets"]["m.ts"]["ok"]
        assert rows == [{"path": "a.ts", "line": None}]


class TestLoad:
    def test_missing_artifact_returns_none(self, tmp_path):
        assert load_reverse_index(tmp_path) is None

    def test_none_root_returns_none(self):
        assert load_reverse_index(None) is None

    def test_roundtrip(self, tmp_path):
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "targets": {
                    "pricing.ts": {"editPrice": [{"path": "cart.ts", "line": 4}]},
                },
            },
        )
        idx = load_reverse_index(tmp_path)
        assert idx is not None
        importers = idx.importers_of("pricing.ts", "editPrice")
        assert importers == [Importer(path="cart.ts", line=4)]
        assert idx.importers_of("pricing.ts", "missing") == []
        assert idx.importers_of("nope.ts", "x") == []

    def test_future_schema_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION + 1, "targets": {}})
        assert load_reverse_index(tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        cham = tmp_path / ".chameleon"
        cham.mkdir(parents=True)
        (cham / REVERSE_INDEX_FILENAME).write_text("{bad", encoding="utf-8")
        assert load_reverse_index(tmp_path) is None

    def test_non_dict_targets_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION, "targets": ["bad"]})
        assert load_reverse_index(tmp_path) is None

    def test_cache_refreshes_on_rewrite(self, tmp_path):
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "targets": {"m.ts": {"a": [{"path": "u.ts", "line": 1}]}},
            },
        )
        first = load_reverse_index(tmp_path)
        assert first.importers_of("m.ts", "a")[0].line == 1
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "targets": {"m.ts": {"a": [{"path": "u.ts", "line": 99}]}},
            },
        )
        second = load_reverse_index(tmp_path)
        assert second.importers_of("m.ts", "a")[0].line == 99


class TestBrokenImporters:
    def _index(self):
        return ReverseIndex(
            {
                "pricing.ts": {
                    "editPrice": [Importer("cart.ts", 3)],
                    "oldName": [Importer("legacy.ts", 7)],
                }
            }
        )

    def test_removed_export_is_broken(self):
        idx = self._index()
        broken = idx.broken_importers("pricing.ts", frozenset({"editPrice"}))
        assert set(broken) == {"oldName"}
        assert broken["oldName"] == [Importer("legacy.ts", 7)]

    def test_all_present_no_break(self):
        idx = self._index()
        broken = idx.broken_importers("pricing.ts", frozenset({"editPrice", "oldName"}))
        assert broken == {}

    def test_unknown_module_no_break(self):
        idx = self._index()
        assert idx.broken_importers("ghost.ts", frozenset()) == {}

    def test_names_for(self):
        idx = self._index()
        names = idx.names_for("pricing.ts")
        assert set(names) == {"editPrice", "oldName"}


class TestModuleKeyForPath:
    def test_relative_posix(self, tmp_path):
        f = _touch(tmp_path, "src/pricing.ts")
        assert module_key_for_path(f, tmp_path) == "src/pricing.ts"

    def test_none_root(self, tmp_path):
        assert module_key_for_path(tmp_path / "a.ts", None) is None

    def test_outside_root_none(self, tmp_path):
        outside = tmp_path.parent / "out.ts"
        assert module_key_for_path(outside, tmp_path) is None
