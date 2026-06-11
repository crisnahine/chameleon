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

    def test_aliased_row_keys_on_exported_name(self, tmp_path):
        # import { editPrice as renamed }: the reverse index keys on the
        # exported name (who-imports-editPrice ignores the importer's local
        # alias); the `local` field the calls index consumes is ignored here.
        _touch(tmp_path, "src/pricing.ts")
        importer = FakeParsed(
            tmp_path / "src" / "cart.ts",
            {
                "import_symbols": [
                    {"name": "editPrice", "local": "renamed", "module": "./pricing", "line": 3}
                ]
            },
        )
        idx = build_reverse_index([importer], tmp_path)
        rows = idx["targets"]["src/pricing.ts"]["editPrice"]
        assert rows == [{"path": "src/cart.ts", "line": 3}]
        assert "renamed" not in idx["targets"]["src/pricing.ts"]

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


def _write_tsconfig(
    repo: Path, paths: dict, base_url: str = ".", at: str = "tsconfig.json"
) -> None:
    p = repo / at
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"compilerOptions": {"baseUrl": base_url, "paths": paths}}),
        encoding="utf-8",
    )


class TestBuildAliases:
    """tsconfig path-alias importers resolve into the reverse index.

    Alias-dominant TypeScript repos (most named imports go through `~/*`) were
    blind to their own existence breaks: the builder dropped every non-relative
    specifier. These cover the alias-resolution path now wired into the builder.
    """

    def test_wildcard_alias_resolves_to_target_key(self, tmp_path):
        _write_tsconfig(tmp_path, {"~/*": ["src/*"]})
        _touch(tmp_path, "src/utils/user.ts")
        importer = FakeParsed(
            tmp_path / "src" / "components" / "Card.tsx",
            {"import_symbols": [{"name": "getFullName", "module": "~/utils/user", "line": 7}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        rows = idx["targets"]["src/utils/user.ts"]["getFullName"]
        assert rows == [{"path": "src/components/Card.tsx", "line": 7}]

    def test_exact_alias_resolves(self, tmp_path):
        _write_tsconfig(tmp_path, {"@config": ["src/config/index.ts"]})
        _touch(tmp_path, "src/config/index.ts")
        importer = FakeParsed(
            tmp_path / "src" / "a.ts",
            {"import_symbols": [{"name": "settings", "module": "@config", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"]["src/config/index.ts"]["settings"] == [
            {"path": "src/a.ts", "line": 1}
        ]

    def test_bare_package_still_dropped_with_aliases_present(self, tmp_path):
        # A real package import must NOT be mistaken for an alias even when a
        # tsconfig declares paths; it resolves to no in-repo file.
        _write_tsconfig(tmp_path, {"~/*": ["src/*"]})
        importer = FakeParsed(
            tmp_path / "src" / "a.ts",
            {"import_symbols": [{"name": "useState", "module": "react", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_alias_to_missing_file_dropped(self, tmp_path):
        # An alias that maps to no on-disk file yields no key (the existence
        # break can only be reasoned about for a real in-repo module).
        _write_tsconfig(tmp_path, {"~/*": ["src/*"]})
        importer = FakeParsed(
            tmp_path / "src" / "a.ts",
            {"import_symbols": [{"name": "x", "module": "~/ghost/thing", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_no_tsconfig_drops_alias(self, tmp_path):
        # Without a tsconfig there is no alias map, so a `~/...` specifier is
        # unresolvable and dropped (unchanged from the bare-package behavior).
        _touch(tmp_path, "src/utils/user.ts")
        importer = FakeParsed(
            tmp_path / "src" / "a.ts",
            {"import_symbols": [{"name": "getFullName", "module": "~/utils/user", "line": 1}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"] == {}

    def test_monorepo_nearest_tsconfig_anchors_alias(self, tmp_path):
        # Two apps map `~/*` to their own src; the importer's alias must resolve
        # against its nearest tsconfig, not a sibling app's.
        _write_tsconfig(tmp_path / "apps" / "web", {"~/*": ["src/*"]})
        _touch(tmp_path, "apps/web/src/lib/fmt.ts")
        importer = FakeParsed(
            tmp_path / "apps" / "web" / "src" / "page.tsx",
            {"import_symbols": [{"name": "fmt", "module": "~/lib/fmt", "line": 2}]},
        )
        idx = build_reverse_index([importer], tmp_path)
        assert idx["targets"]["apps/web/src/lib/fmt.ts"]["fmt"] == [
            {"path": "apps/web/src/page.tsx", "line": 2}
        ]


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
