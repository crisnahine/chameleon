"""Unit tests for the exported-symbol index (build / load / key resolution)."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.symbol_index import (
    EXPORTS_INDEX_FILENAME,
    SCHEMA_VERSION,
    build_exports_index,
    load_exports_index,
    resolve_index_key,
)


@dataclass
class FakeParsed:
    path: Path
    extras: dict = field(default_factory=dict)


def _write_index(repo: Path, payload: dict) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / EXPORTS_INDEX_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


class TestBuild:
    def test_named_and_open_files_recorded_relative(self, tmp_path):
        files = [
            FakeParsed(tmp_path / "a" / "api.ts", {"named_export_names": ["getUser", "save"]}),
            FakeParsed(tmp_path / "b" / "barrel.ts", {"export_set_open": True}),
        ]
        idx = build_exports_index(files, tmp_path)
        assert idx["schema_version"] == SCHEMA_VERSION
        assert idx["files"]["a/api.ts"] == {"names": ["getUser", "save"], "open": False}
        assert idx["files"]["b/barrel.ts"] == {"names": [], "open": True}

    def test_file_with_no_exports_is_omitted(self, tmp_path):
        files = [FakeParsed(tmp_path / "empty.ts", {})]
        idx = build_exports_index(files, tmp_path)
        assert idx["files"] == {}

    def test_names_deduped_and_sorted(self, tmp_path):
        files = [FakeParsed(tmp_path / "x.ts", {"named_export_names": ["b", "a", "b"]})]
        idx = build_exports_index(files, tmp_path)
        assert idx["files"]["x.ts"]["names"] == ["a", "b"]

    def test_file_outside_root_skipped(self, tmp_path):
        outside = tmp_path.parent / "outside.ts"
        files = [FakeParsed(outside, {"named_export_names": ["z"]})]
        idx = build_exports_index(files, tmp_path)
        assert idx["files"] == {}


class TestLoad:
    def test_missing_artifact_returns_none(self, tmp_path):
        assert load_exports_index(tmp_path) is None

    def test_none_root_returns_none(self):
        assert load_exports_index(None) is None

    def test_roundtrip(self, tmp_path):
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "files": {"api.ts": {"names": ["getUser"], "open": False}},
            },
        )
        idx = load_exports_index(tmp_path)
        assert idx is not None
        entry = idx.lookup("api.ts")
        assert entry is not None
        assert entry.names == frozenset({"getUser"})
        assert entry.open is False
        assert idx.lookup("nope.ts") is None

    def test_open_entry(self, tmp_path):
        _write_index(
            tmp_path,
            {"schema_version": SCHEMA_VERSION, "files": {"b.ts": {"names": [], "open": True}}},
        )
        idx = load_exports_index(tmp_path)
        assert idx.lookup("b.ts").open is True

    def test_future_schema_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION + 1, "files": {}})
        assert load_exports_index(tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        cham = tmp_path / ".chameleon"
        cham.mkdir(parents=True)
        (cham / EXPORTS_INDEX_FILENAME).write_text("{not json", encoding="utf-8")
        assert load_exports_index(tmp_path) is None

    def test_non_dict_files_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION, "files": ["bad"]})
        assert load_exports_index(tmp_path) is None

    def test_cache_refreshes_on_rewrite(self, tmp_path):
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "files": {"a.ts": {"names": ["one"], "open": False}},
            },
        )
        first = load_exports_index(tmp_path)
        assert first.lookup("a.ts").names == frozenset({"one"})
        # Rewrite with new content; mtime/size change invalidates the cache entry.
        _write_index(
            tmp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "files": {"a.ts": {"names": ["one", "two"], "open": False}},
            },
        )
        second = load_exports_index(tmp_path)
        assert second.lookup("a.ts").names == frozenset({"one", "two"})


class TestResolveIndexKey:
    def test_sibling_ts(self, tmp_path):
        (tmp_path / "api.ts").write_text("x", encoding="utf-8")
        key = resolve_index_key(tmp_path / "api", tmp_path)
        assert key == "api.ts"

    def test_directory_index(self, tmp_path):
        (tmp_path / "widgets").mkdir()
        (tmp_path / "widgets" / "index.tsx").write_text("x", encoding="utf-8")
        key = resolve_index_key(tmp_path / "widgets", tmp_path)
        assert key == "widgets/index.tsx"

    def test_js_specifier_maps_to_ts_source(self, tmp_path):
        (tmp_path / "mod.ts").write_text("x", encoding="utf-8")
        key = resolve_index_key(tmp_path / "mod.js", tmp_path)
        assert key == "mod.ts"

    def test_unresolved_returns_none(self, tmp_path):
        assert resolve_index_key(tmp_path / "ghost", tmp_path) is None
