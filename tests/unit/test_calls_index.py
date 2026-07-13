"""Unit tests for the committed calls index (build / load / query).

The builder inverts the dumpers' raw call_sites into callee-first caller
edges with exactly three deterministic grades; everything name-only is
deliberately absent. The loader mirrors the symbol-index loaders: fail-open
None on any ambiguity, mtime+size cache token, schema check.
"""

import itertools
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chameleon_mcp.calls_index import (
    CALLS_INDEX_FILENAME,
    SCHEMA_VERSION,
    build_calls_index,
    load_calls_index,
)

_NODE_MODULES = (
    Path(__file__).resolve().parents[2] / "plugin" / "mcp" / "node_modules" / "typescript"
)
_HAVE_TS = shutil.which("node") is not None and _NODE_MODULES.is_dir()


def _have_prism() -> bool:
    import subprocess

    if not shutil.which("ruby"):
        return False
    try:
        return (
            subprocess.run(
                ["ruby", "-e", "require 'prism'"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class FakeParsed:
    path: Path
    extras: dict = field(default_factory=dict)


def _touch(repo: Path, rel: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("// stub\n", encoding="utf-8")
    return p


def _write_index(repo: Path, payload) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    (cham / CALLS_INDEX_FILENAME).write_text(body, encoding="utf-8")


def _sig(name, enclosing_class=None, kind="function", enclosing_class_path=None):
    row = {"name": name, "kind": kind, "enclosing_class": enclosing_class}
    if enclosing_class_path is not None:
        row["enclosing_class_path"] = enclosing_class_path
    return row


def _site(name, receiver, kind, line, caller):
    return {
        "name": name,
        "receiver": receiver,
        "kind": kind,
        "line": line,
        "caller": caller,
    }


class TestSameFile:
    def test_bare_call_to_file_local_callable(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [
                    _sig("helper"),
                    _sig("run", enclosing_class="Svc"),
                ],
                "call_sites": [_site("helper", None, "bare", 10, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        assert idx["schema_version"] == SCHEMA_VERSION
        entry = idx["callees"]["src/svc.ts"]["helper"]
        assert entry["callers"] == [
            {"path": "src/svc.ts", "caller": "run", "line": 10, "grade": "same_file"}
        ]
        assert entry["total"] == 1
        assert entry["truncated"] is False

    def test_this_call_to_same_file_class_member(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [
                    _sig("save", enclosing_class="Svc"),
                    _sig("run", enclosing_class="Svc"),
                ],
                "call_sites": [_site("save", "this", "this", 5, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/svc.ts"]["save"]
        assert entry["callers"] == [
            {"path": "src/svc.ts", "caller": "run", "line": 5, "grade": "same_file"}
        ]

    def test_this_call_to_unknown_member_yields_no_edge(self, tmp_path):
        # `this.persist()` where no class in THIS file defines persist: the
        # method may live on a base class in another file, which v1 does not
        # chase, so nothing is asserted.
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [_sig("run", enclosing_class="Svc")],
                "call_sites": [_site("persist", "this", "this", 5, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_this_call_ambiguous_across_two_classes_yields_no_edge(self, tmp_path):
        # Two DISTINCT classes in one file each define `process`, each called
        # via `this.process()` from an unrelated method. This/self call sites
        # carry no enclosing-class field, so there is no way to tell doA's
        # call from doB's -- asserting either would fabricate a merged edge
        # claiming doB calls A's process (or vice versa). Fails open instead.
        pf = FakeParsed(
            tmp_path / "src" / "handlers.ts",
            {
                "callable_signatures": [
                    _sig("process", enclosing_class="A"),
                    _sig("doA", enclosing_class="A"),
                    _sig("process", enclosing_class="B"),
                    _sig("doB", enclosing_class="B"),
                ],
                "call_sites": [
                    _site("process", "this", "this", 5, "doA"),
                    _site("process", "this", "this", 50, "doB"),
                ],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_bare_call_to_module_level_name_beside_ambiguous_classes(self, tmp_path):
        # A module-level `helper` coexists with two DIFFERENT classes that
        # also each define `helper`. The module-level definition is never
        # ambiguous (it isn't tied to any class), so the bare call still
        # resolves to the same-file edge despite the class-name collision.
        pf = FakeParsed(
            tmp_path / "src" / "mix.ts",
            {
                "callable_signatures": [
                    _sig("helper"),
                    _sig("helper", enclosing_class="A"),
                    _sig("helper", enclosing_class="B"),
                ],
                "call_sites": [_site("helper", None, "bare", 10, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/mix.ts"]["helper"]
        assert entry["callers"] == [
            {"path": "src/mix.ts", "caller": "run", "line": 10, "grade": "same_file"}
        ]

    def test_ruby_self_call_to_same_file_member(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("slug", enclosing_class="User"),
                    _sig("save_slug", enclosing_class="User"),
                ],
                "call_sites": [_site("slug", "self", "self", 7, "save_slug")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "ruby")
        entry = idx["callees"]["app/models/user.rb"]["slug"]
        assert entry["callers"][0]["grade"] == "same_file"

    def test_bare_call_to_unknown_name_yields_no_edge(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "a.ts",
            {
                "callable_signatures": [_sig("run")],
                "call_sites": [_site("ghost", None, "bare", 2, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        assert idx["callees"] == {}


class TestImportGrade:
    def _target(self, tmp_path, names, open_set=False):
        _touch(tmp_path, "src/api.ts")
        return FakeParsed(
            tmp_path / "src" / "api.ts",
            {"named_export_names": names, "export_set_open": open_set},
        )

    def test_named_import_bare_call(self, tmp_path):
        target = self._target(tmp_path, ["fetchUser"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 9, "<module>")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        entry = idx["callees"]["src/api.ts"]["fetchUser"]
        assert entry["callers"] == [
            {"path": "src/page.ts", "caller": "<module>", "line": 9, "grade": "import"}
        ]

    def test_new_call_of_named_import_keys_on_exported_name(self, tmp_path):
        target = self._target(tmp_path, ["ApiClient"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "ApiClient", "module": "./api", "line": 1}],
                "call_sites": [_site("ApiClient", None, "new", 4, "boot")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        entry = idx["callees"]["src/api.ts"]["ApiClient"]
        assert entry["callers"][0]["grade"] == "import"

    def test_new_with_receiver_does_not_resolve_via_named_imports(self, tmp_path):
        # `new winston.Logger()` constructs a property of `winston`; the
        # property name coinciding with `import { Logger } from './logger'`
        # proves nothing about the receiver, so no edge is asserted.
        _touch(tmp_path, "src/logger.ts")
        target = FakeParsed(
            tmp_path / "src" / "logger.ts",
            {"named_export_names": ["Logger"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "Logger", "module": "./logger", "line": 1}],
                "call_sites": [_site("Logger", "winston", "new", 4, "boot")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_open_export_set_yields_no_edge(self, tmp_path):
        # A barrel target (`export * from`) has a non-authoritative export set;
        # the edge cannot be asserted deterministically, so it is skipped.
        target = self._target(tmp_path, ["fetchUser"], open_set=True)
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 9, "<module>")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_name_absent_from_closed_set_yields_no_edge(self, tmp_path):
        target = self._target(tmp_path, ["getUser"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 9, "<module>")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_aliased_import_collision_keys_on_local_binding(self, tmp_path):
        # import { x } from './a'; import { x as y } from './b': the call x()
        # must edge to a.ts ONLY, and y() must edge to b.ts under the exported
        # name x. Keying the import map on the exported name collided the two
        # rows (x() edged to whichever import came last) and dropped y()
        # entirely, because call-site identifiers are LOCAL binding names.
        _touch(tmp_path, "src/a.ts")
        _touch(tmp_path, "src/b.ts")
        a = FakeParsed(
            tmp_path / "src" / "a.ts",
            {"named_export_names": ["x"], "export_set_open": False},
        )
        b = FakeParsed(
            tmp_path / "src" / "b.ts",
            {"named_export_names": ["x"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [
                    {"name": "x", "local": "x", "module": "./a", "line": 1},
                    {"name": "x", "local": "y", "module": "./b", "line": 2},
                ],
                "call_sites": [
                    _site("x", None, "bare", 5, "go"),
                    _site("y", None, "bare", 6, "go"),
                ],
            },
        )
        idx = build_calls_index([a, b, caller], tmp_path, "typescript")
        assert idx["callees"]["src/a.ts"]["x"]["callers"] == [
            {"path": "src/page.ts", "caller": "go", "line": 5, "grade": "import"}
        ]
        assert idx["callees"]["src/b.ts"]["x"]["callers"] == [
            {"path": "src/page.ts", "caller": "go", "line": 6, "grade": "import"}
        ]

    def test_aliased_new_records_under_exported_name(self, tmp_path):
        # new Alias() for import { Klass as Alias }: the local alias resolves,
        # and the edge records under the exported name the target declares.
        target = self._target(tmp_path, ["Klass"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [
                    {"name": "Klass", "local": "Alias", "module": "./api", "line": 1}
                ],
                "call_sites": [_site("Alias", None, "new", 4, "boot")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        entry = idx["callees"]["src/api.ts"]["Klass"]
        assert entry["callers"] == [
            {"path": "src/page.ts", "caller": "boot", "line": 4, "grade": "import"}
        ]
        assert "Alias" not in idx["callees"]["src/api.ts"]

    def test_aliased_call_on_exported_name_yields_no_edge(self, tmp_path):
        # import { x as y } binds ONLY y; a bare x() in the same file is some
        # other (unknown) name, never the aliased import.
        target = self._target(tmp_path, ["x"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "x", "local": "y", "module": "./api", "line": 1}],
                "call_sites": [_site("x", None, "bare", 5, "go")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_rows_without_local_fall_back_to_exported_name(self, tmp_path):
        # Old dumps carry no `local`; the exported name doubles as the binding.
        target = self._target(tmp_path, ["fetchUser"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 9, "<module>")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"]["src/api.ts"]["fetchUser"]["callers"][0]["grade"] == "import"

    def test_local_definition_wins_over_import(self, tmp_path):
        # A name both defined in-file and present in the import map grades as
        # same_file: a module-scope local declaration shadows nothing real (TS
        # forbids the duplicate), but if the dump carries both, the local
        # definition is the deterministic anchor.
        target = self._target(tmp_path, ["fetchUser"])
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "callable_signatures": [_sig("fetchUser")],
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 9, "<module>")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"]["src/page.ts"]["fetchUser"]["callers"][0]["grade"] == "same_file"
        assert "src/api.ts" not in idx["callees"]


class TestTypedPropertyGrade:
    """`this.<prop>.<method>()` resolved through a class's declared property
    types -- the TS dependency-injection / typed-field call shape."""

    def _service(self, tmp_path):
        _touch(tmp_path, "src/svc.ts")
        return FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "named_export_names": ["Svc"],
                "export_set_open": False,
                "callable_signatures": [
                    _sig("run", enclosing_class="Svc", kind="method"),
                ],
            },
        )

    def _prop_site(self, method, receiver, enclosing, caller, line=4):
        # A `this.<receiver>.<method>()` site carrying its enclosing class, as
        # ts_dump emits it. The enclosing class scopes the receiver's type lookup.
        row = _site(method, receiver, "this_prop", line, caller)
        row["enclosing_class"] = enclosing
        return row

    def _consumer(self, tmp_path, props, sites, cls="Ctrl"):
        return FakeParsed(
            tmp_path / "src" / "ctrl.ts",
            {
                "import_symbols": [{"name": "Svc", "module": "./svc", "line": 1}],
                "class_property_types": [{"class": cls, "props": props}],
                "call_sites": sites,
                "callable_signatures": [_sig("go", enclosing_class=cls, kind="method")],
            },
        )

    def test_constructor_injected_property_resolves(self, tmp_path):
        target = self._service(tmp_path)
        ctrl = self._consumer(
            tmp_path, {"svc": "Svc"}, [self._prop_site("run", "svc", "Ctrl", "go")]
        )
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        entry = idx["callees"]["src/svc.ts"]["run"]
        assert entry["callers"] == [
            {"path": "src/ctrl.ts", "caller": "go", "line": 4, "grade": "typed_property"}
        ]

    def test_unknown_property_type_yields_no_edge(self, tmp_path):
        target = self._service(tmp_path)
        # Property `svc` has no recorded type -> receiver unresolved -> no edge.
        ctrl = self._consumer(tmp_path, {}, [self._prop_site("run", "svc", "Ctrl", "go")])
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert "src/svc.ts" not in idx["callees"]

    def test_missing_enclosing_class_yields_no_edge(self, tmp_path):
        target = self._service(tmp_path)
        # A this_prop site with no enclosing class (module-level `this`, or an
        # older dump) must not resolve via a file-scoped guess.
        ctrl = self._consumer(tmp_path, {"svc": "Svc"}, [_site("run", "svc", "this_prop", 4, "go")])
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert "src/svc.ts" not in idx["callees"]

    def test_method_not_on_target_class_yields_no_edge(self, tmp_path):
        target = self._service(tmp_path)
        # `missing` is not a member of Svc -> no fabricated edge.
        ctrl = self._consumer(
            tmp_path, {"svc": "Svc"}, [self._prop_site("missing", "svc", "Ctrl", "go")]
        )
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert "src/svc.ts" not in idx["callees"]

    def test_open_export_set_target_yields_no_edge(self, tmp_path):
        _touch(tmp_path, "src/svc.ts")
        target = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                # A barrel (`export * from`) can't be enumerated -> no edge.
                "export_set_open": True,
                "callable_signatures": [_sig("run", enclosing_class="Svc", kind="method")],
            },
        )
        ctrl = self._consumer(
            tmp_path, {"svc": "Svc"}, [self._prop_site("run", "svc", "Ctrl", "go")]
        )
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert "src/svc.ts" not in idx["callees"]

    def test_sibling_class_untyped_property_does_not_leak(self, tmp_path):
        # THE regression pin (P1): class A declares `svc: Svc` and resolves; a
        # sibling class B in the same file uses `this.svc.run()` with svc NOT
        # typed. B's site must NOT inherit A's type -- only A.goA gets the edge.
        target = self._service(tmp_path)
        ctrl = FakeParsed(
            tmp_path / "src" / "ctrl.ts",
            {
                "import_symbols": [{"name": "Svc", "module": "./svc", "line": 1}],
                "class_property_types": [{"class": "A", "props": {"svc": "Svc"}}],
                "call_sites": [
                    self._prop_site("run", "svc", "A", "goA", line=4),
                    self._prop_site("run", "svc", "B", "goB", line=9),
                ],
                "callable_signatures": [
                    _sig("goA", enclosing_class="A", kind="method"),
                    _sig("goB", enclosing_class="B", kind="method"),
                ],
            },
        )
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert idx["callees"]["src/svc.ts"]["run"]["callers"] == [
            {"path": "src/ctrl.ts", "caller": "goA", "line": 4, "grade": "typed_property"}
        ]

    def test_two_classes_distinct_types_each_resolve_per_class(self, tmp_path):
        # Two classes in one file declare `svc` with DIFFERENT types. Per-class
        # scoping resolves each to its own type (not ambiguous): A.svc:Svc ->
        # Svc.run, B.svc:Other -> Other.run. Both real edges.
        target = self._service(tmp_path)
        _touch(tmp_path, "src/other.ts")
        other = FakeParsed(
            tmp_path / "src" / "other.ts",
            {
                "named_export_names": ["Other"],
                "export_set_open": False,
                "callable_signatures": [_sig("ping", enclosing_class="Other", kind="method")],
            },
        )
        ctrl = FakeParsed(
            tmp_path / "src" / "ctrl.ts",
            {
                "import_symbols": [
                    {"name": "Svc", "module": "./svc", "line": 1},
                    {"name": "Other", "module": "./other", "line": 1},
                ],
                "class_property_types": [
                    {"class": "A", "props": {"svc": "Svc"}},
                    {"class": "B", "props": {"svc": "Other"}},
                ],
                "call_sites": [
                    self._prop_site("run", "svc", "A", "goA", line=4),
                    self._prop_site("ping", "svc", "B", "goB", line=9),
                ],
                "callable_signatures": [
                    _sig("goA", enclosing_class="A", kind="method"),
                    _sig("goB", enclosing_class="B", kind="method"),
                ],
            },
        )
        idx = build_calls_index([target, other, ctrl], tmp_path, "typescript")
        assert idx["callees"]["src/svc.ts"]["run"]["callers"][0]["caller"] == "goA"
        assert idx["callees"]["src/other.ts"]["ping"]["callers"][0]["caller"] == "goB"

    def test_intra_class_conflicting_types_poison_the_property(self, tmp_path):
        # One class declares `svc` twice with different types (param-property +
        # field). Poisoned -> no edge.
        target = self._service(tmp_path)
        ctrl = self._consumer(
            tmp_path,
            {"svc": "Svc"},
            [self._prop_site("run", "svc", "Ctrl", "go")],
        )
        # Simulate a second, conflicting declaration by appending another row for
        # the SAME class with a different type for the same property.
        ctrl.extras["class_property_types"].append({"class": "Ctrl", "props": {"svc": "Nope"}})
        idx = build_calls_index([target, ctrl], tmp_path, "typescript")
        assert "src/svc.ts" not in idx["callees"]

    def test_ruby_this_prop_site_is_ignored(self, tmp_path):
        # typed_property is TS-only; a stray this_prop site on a Ruby build is a
        # no-op (Ruby has no static property types to resolve through).
        target = self._service(tmp_path)
        ctrl = self._consumer(
            tmp_path, {"svc": "Svc"}, [self._prop_site("run", "svc", "Ctrl", "go")]
        )
        idx = build_calls_index([target, ctrl], tmp_path, "ruby")
        assert "src/svc.ts" not in idx["callees"]


class TestNamespaceImport:
    def test_member_call_via_namespace_alias(self, tmp_path):
        _touch(tmp_path, "src/utils.ts")
        target = FakeParsed(
            tmp_path / "src" / "utils.ts",
            {"named_export_names": ["fmtDate"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "namespace_imports": [{"alias": "utils", "module": "./utils", "line": 1}],
                "call_sites": [_site("fmtDate", "utils", "member", 4, "render")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        entry = idx["callees"]["src/utils.ts"]["fmtDate"]
        assert entry["callers"] == [
            {"path": "src/page.ts", "caller": "render", "line": 4, "grade": "import"}
        ]

    def test_member_call_with_non_alias_receiver_yields_no_edge(self, tmp_path):
        _touch(tmp_path, "src/utils.ts")
        target = FakeParsed(
            tmp_path / "src" / "utils.ts",
            {"named_export_names": ["fmtDate"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "namespace_imports": [{"alias": "utils", "module": "./utils", "line": 1}],
                "call_sites": [_site("fmtDate", "other", "member", 4, "render")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}

    def test_new_via_namespace_alias_resolves_against_alias_target(self, tmp_path):
        _touch(tmp_path, "src/svc.ts")
        target = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {"named_export_names": ["Client"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "namespace_imports": [{"alias": "ns", "module": "./svc", "line": 1}],
                "call_sites": [_site("Client", "ns", "new", 4, "boot")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        entry = idx["callees"]["src/svc.ts"]["Client"]
        assert entry["callers"] == [
            {"path": "src/page.ts", "caller": "boot", "line": 4, "grade": "import"}
        ]

    def test_new_via_namespace_alias_absent_name_yields_no_edge(self, tmp_path):
        _touch(tmp_path, "src/svc.ts")
        target = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {"named_export_names": ["Client"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "namespace_imports": [{"alias": "ns", "module": "./svc", "line": 1}],
                "call_sites": [_site("Ghost", "ns", "new", 4, "boot")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}


class TestConstantReceiver:
    def test_constant_method_and_new_to_initialize(self, tmp_path):
        target = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("initialize", enclosing_class="User", kind="method"),
                    _sig("find_by_slug", enclosing_class="User", kind="singleton_method"),
                ],
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "controllers" / "users_controller.rb",
            {
                "call_sites": [
                    _site("find_by_slug", "User", "constant", 3, "show"),
                    _site("new", "User", "constant", 8, "create"),
                ],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        callee = idx["callees"]["app/models/user.rb"]
        assert callee["find_by_slug"]["callers"][0]["grade"] == "constant_receiver"
        # Const.new resolves to the target's own initialize, never a synthetic
        # "new" entry.
        assert callee["initialize"]["callers"] == [
            {
                "path": "app/controllers/users_controller.rb",
                "caller": "create",
                "line": 8,
                "grade": "constant_receiver",
            }
        ]
        assert "new" not in callee

    def test_ambiguous_constant_yields_no_edge(self, tmp_path):
        # Two files define class User: the receiver does not name exactly one
        # definition, so no edge is asserted for either.
        a = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("find_by_slug", enclosing_class="User", kind="singleton_method")
                ]
            },
        )
        b = FakeParsed(
            tmp_path / "lib" / "legacy" / "user.rb",
            {
                "callable_signatures": [
                    _sig("find_by_slug", enclosing_class="User", kind="singleton_method")
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("find_by_slug", "User", "constant", 3, "show")]},
        )
        idx = build_calls_index([a, b, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_new_without_initialize_yields_no_edge(self, tmp_path):
        target = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("find_by_slug", enclosing_class="User", kind="singleton_method")
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("new", "User", "constant", 8, "create")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_constant_call_to_instance_method_yields_no_edge(self, tmp_path):
        # Mailer.deliver can only dispatch to a class-level method; an
        # instance `def deliver` is unreachable from a constant receiver, so
        # matching it would fabricate an edge.
        target = FakeParsed(
            tmp_path / "app" / "mailers" / "mailer.rb",
            {"callable_signatures": [_sig("deliver", enclosing_class="Mailer", kind="method")]},
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("deliver", "Mailer", "constant", 4, "notify")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_constant_call_to_singleton_method_records_edge(self, tmp_path):
        target = FakeParsed(
            tmp_path / "app" / "mailers" / "mailer.rb",
            {
                "callable_signatures": [
                    _sig("deliver", enclosing_class="Mailer", kind="singleton_method")
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("deliver", "Mailer", "constant", 4, "notify")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        entry = idx["callees"]["app/mailers/mailer.rb"]["deliver"]
        assert entry["callers"][0]["grade"] == "constant_receiver"

    def test_new_requires_instance_initialize(self, tmp_path):
        # Const.new dispatches to the INSTANCE initialize; a (pathological)
        # singleton-only initialize proves nothing about construction.
        target = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("initialize", enclosing_class="User", kind="singleton_method")
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("new", "User", "constant", 8, "create")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_bare_receiver_does_not_match_namespaced_key(self, tmp_path):
        # `Settings.get` from some other namespace cannot lexically reach
        # A::Settings even when the short name is globally unique; matching it
        # would fabricate an edge (runtime would NameError unless a same-named
        # reachable constant exists).
        target = FakeParsed(
            tmp_path / "app" / "lib" / "namespace_a.rb",
            {
                "callable_signatures": [
                    _sig(
                        "get",
                        enclosing_class="Settings",
                        kind="singleton_method",
                        enclosing_class_path="A::Settings",
                    )
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("get", "Settings", "constant", 4, "run")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_qualified_receiver_matches_namespaced_key(self, tmp_path):
        target = FakeParsed(
            tmp_path / "app" / "lib" / "namespace_a.rb",
            {
                "callable_signatures": [
                    _sig(
                        "get",
                        enclosing_class="Settings",
                        kind="singleton_method",
                        enclosing_class_path="A::Settings",
                    )
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("get", "A::Settings", "constant", 4, "run")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        entry = idx["callees"]["app/lib/namespace_a.rb"]["get"]
        assert entry["callers"][0]["grade"] == "constant_receiver"

    def test_rows_without_path_fall_back_to_enclosing_class(self, tmp_path):
        # Old dumps carry no enclosing_class_path; the lexical class name is
        # still the key so existing profiles keep their top-level edges.
        target = FakeParsed(
            tmp_path / "app" / "models" / "billing.rb",
            {
                "callable_signatures": [
                    _sig("charge", enclosing_class="Billing", kind="singleton_method")
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("charge", "Billing", "constant", 4, "pay")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        entry = idx["callees"]["app/models/billing.rb"]["charge"]
        assert entry["callers"][0]["grade"] == "constant_receiver"

    def test_self_new_override_suppresses_initialize_map(self, tmp_path):
        # A `def self.new` override owns construction: whether and how it
        # reaches initialize is not provable, so Three.new records no edge at
        # all (neither to initialize nor to the override).
        target = FakeParsed(
            tmp_path / "app" / "models" / "three.rb",
            {
                "callable_signatures": [
                    _sig("initialize", enclosing_class="Three", kind="method"),
                    _sig("new", enclosing_class="Three", kind="singleton_method"),
                ]
            },
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("new", "Three", "constant", 8, "create")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "ruby")
        assert idx["callees"] == {}

    def test_constant_grade_is_ruby_only(self, tmp_path):
        target = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {"callable_signatures": [_sig("run", enclosing_class="Svc")]},
        )
        caller = FakeParsed(
            tmp_path / "src" / "x.ts",
            {"call_sites": [_site("run", "Svc", "constant", 3, "boot")]},
        )
        idx = build_calls_index([target, caller], tmp_path, "typescript")
        assert idx["callees"] == {}


class TestCaps:
    def test_per_callee_cap_keeps_true_total(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_CALLS_INDEX_MAX_CALLERS_PER_CALLEE", "2")
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [_sig("helper")],
                "call_sites": [_site("helper", None, "bare", n, "run") for n in range(1, 6)],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/svc.ts"]["helper"]
        assert len(entry["callers"]) == 2
        assert entry["total"] == 5
        assert entry["truncated"] is True
        # The kept rows are the first in sorted (path, line) order.
        assert [r["line"] for r in entry["callers"]] == [1, 2]

    def test_global_edge_cap_truncates_later_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_CALLS_INDEX_MAX_TOTAL_EDGES", "1")
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [_sig("alpha"), _sig("beta")],
                "call_sites": [
                    _site("alpha", None, "bare", 1, "run"),
                    _site("beta", None, "bare", 2, "run"),
                ],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        callee = idx["callees"]["src/svc.ts"]
        assert len(callee["alpha"]["callers"]) == 1
        assert callee["beta"]["callers"] == []
        assert callee["beta"]["total"] == 1
        assert callee["beta"]["truncated"] is True

    def test_global_cap_partial_slice_second_entry(self, tmp_path, monkeypatch):
        # Global cap 3 with two callees of 2 rows each: alpha keeps 2, beta
        # keeps 1 (the partial slice), total stored 3, beta truncated True.
        monkeypatch.setenv("CHAMELEON_CALLS_INDEX_MAX_TOTAL_EDGES", "3")
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [_sig("alpha"), _sig("beta")],
                "call_sites": [
                    _site("alpha", None, "bare", 1, "run"),
                    _site("alpha", None, "bare", 2, "run"),
                    _site("beta", None, "bare", 3, "run"),
                    _site("beta", None, "bare", 4, "run"),
                ],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        callee = idx["callees"]["src/svc.ts"]
        assert len(callee["alpha"]["callers"]) == 2
        assert callee["alpha"]["truncated"] is False
        assert len(callee["beta"]["callers"]) == 1
        assert callee["beta"]["total"] == 2
        assert callee["beta"]["truncated"] is True


class TestDeterminism:
    def _files(self, tmp_path):
        _touch(tmp_path, "src/api.ts")
        target = FakeParsed(
            tmp_path / "src" / "api.ts",
            {"named_export_names": ["fetchUser"], "export_set_open": False},
        )
        a = FakeParsed(
            tmp_path / "src" / "a.ts",
            {
                "callable_signatures": [_sig("go")],
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [
                    _site("go", None, "bare", 9, "<module>"),
                    _site("fetchUser", None, "bare", 3, "go"),
                ],
            },
        )
        b = FakeParsed(
            tmp_path / "src" / "b.ts",
            {
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [_site("fetchUser", None, "bare", 5, "<module>")],
            },
        )
        return target, a, b

    def test_same_inputs_yield_byte_identical_payloads(self, tmp_path):
        target, a, b = self._files(tmp_path)
        first = build_calls_index([target, a, b], tmp_path, "typescript")
        second = build_calls_index([target, a, b], tmp_path, "typescript")
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_input_order_does_not_change_payload(self, tmp_path):
        target, a, b = self._files(tmp_path)
        first = build_calls_index([target, a, b], tmp_path, "typescript")
        second = build_calls_index([b, target, a], tmp_path, "typescript")
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_all_permutations_byte_identical(self, tmp_path):
        # All 6 orderings of the 3-file fixture must produce the same payload.
        files = list(self._files(tmp_path))
        canonical = json.dumps(build_calls_index(files, tmp_path, "typescript"), sort_keys=True)
        for perm in itertools.permutations(files):
            result = json.dumps(
                build_calls_index(list(perm), tmp_path, "typescript"), sort_keys=True
            )
            assert result == canonical, f"ordering {[f.path.name for f in perm]} diverged"

    def test_duplicate_sites_deduped(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "src" / "svc.ts",
            {
                "callable_signatures": [_sig("helper")],
                "call_sites": [
                    _site("helper", None, "bare", 10, "run"),
                    _site("helper", None, "bare", 10, "run"),
                ],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/svc.ts"]["helper"]
        assert entry["total"] == 1
        assert len(entry["callers"]) == 1


class TestMalformedInputs:
    def test_malformed_rows_and_files_skipped(self, tmp_path):
        outside = FakeParsed(
            tmp_path.parent / "out.ts",
            {
                "callable_signatures": [_sig("x")],
                "call_sites": [_site("x", None, "bare", 1, "run")],
            },
        )
        garbage = FakeParsed(
            tmp_path / "g.ts",
            {
                "callable_signatures": ["not-a-dict", {"name": 5}],
                "call_sites": ["nope", {"name": None, "kind": "bare"}, {}],
            },
        )
        idx = build_calls_index([outside, garbage, None], tmp_path, "typescript")
        assert idx["callees"] == {}


class TestLoad:
    def _payload(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "callees": {
                "src/api.ts": {
                    "fetchUser": {
                        "callers": [
                            {
                                "path": "src/page.ts",
                                "caller": "<module>",
                                "line": 9,
                                "grade": "import",
                            }
                        ],
                        "total": 1,
                        "truncated": False,
                    }
                }
            },
        }

    def test_missing_artifact_returns_none(self, tmp_path):
        assert load_calls_index(tmp_path) is None

    def test_none_root_returns_none(self):
        assert load_calls_index(None) is None

    def test_roundtrip(self, tmp_path):
        _write_index(tmp_path, self._payload())
        idx = load_calls_index(tmp_path)
        assert idx is not None
        assert len(idx) == 1
        entry = idx.callers_of("src/api.ts", "fetchUser")
        assert entry == {
            "callers": [
                {
                    "path": "src/page.ts",
                    "caller": "<module>",
                    "line": 9,
                    "grade": "import",
                }
            ],
            "total": 1,
            "truncated": False,
        }
        assert idx.callers_of("src/api.ts", "missing") is None
        assert idx.callers_of("nope.ts", "fetchUser") is None

    def test_corrupt_json_returns_none(self, tmp_path):
        _write_index(tmp_path, "{bad")
        assert load_calls_index(tmp_path) is None

    def test_future_schema_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION + 1, "callees": {}})
        assert load_calls_index(tmp_path) is None

    def test_non_dict_callees_rejected(self, tmp_path):
        _write_index(tmp_path, {"schema_version": SCHEMA_VERSION, "callees": ["bad"]})
        assert load_calls_index(tmp_path) is None

    def test_oversize_artifact_returns_none(self, tmp_path):
        cham = tmp_path / ".chameleon"
        cham.mkdir(parents=True)
        (cham / CALLS_INDEX_FILENAME).write_bytes(b" " * (16_000_001))
        assert load_calls_index(tmp_path) is None

    def test_cache_refreshes_on_rewrite(self, tmp_path):
        _write_index(tmp_path, self._payload())
        first = load_calls_index(tmp_path)
        assert first.callers_of("src/api.ts", "fetchUser")["callers"][0]["line"] == 9
        rewritten = self._payload()
        rewritten["callees"]["src/api.ts"]["fetchUser"]["callers"][0]["line"] = 99
        _write_index(tmp_path, rewritten)
        second = load_calls_index(tmp_path)
        assert second.callers_of("src/api.ts", "fetchUser")["callers"][0]["line"] == 99

    def test_malformed_caller_rows_skipped(self, tmp_path):
        payload = self._payload()
        payload["callees"]["src/api.ts"]["fetchUser"]["callers"].extend(
            ["not-a-dict", {"path": 5, "line": 1}]
        )
        _write_index(tmp_path, payload)
        idx = load_calls_index(tmp_path)
        entry = idx.callers_of("src/api.ts", "fetchUser")
        assert len(entry["callers"]) == 1

    def test_unknown_grade_rows_skipped(self, tmp_path):
        # The grade set is closed: a row carrying anything outside
        # same_file/import/constant_receiver is malformed, not a new tier.
        payload = self._payload()
        payload["callees"]["src/api.ts"]["fetchUser"]["callers"].append(
            {"path": "src/other.ts", "caller": "run", "line": 3, "grade": "name_only"}
        )
        _write_index(tmp_path, payload)
        idx = load_calls_index(tmp_path)
        entry = idx.callers_of("src/api.ts", "fetchUser")
        assert len(entry["callers"]) == 1
        assert entry["callers"][0]["grade"] == "import"

    def test_all_grades_roundtrip(self, tmp_path):
        # Build a payload that exercises all three grades, write it to a real
        # .chameleon dir, and verify every built row survives the load intact.
        _touch(tmp_path, "src/api.ts")
        _touch(tmp_path, "app/models/user.rb")
        target_ts = FakeParsed(
            tmp_path / "src" / "api.ts",
            {"named_export_names": ["fetchUser"], "export_set_open": False},
        )
        caller_ts = FakeParsed(
            tmp_path / "src" / "page.ts",
            {
                "callable_signatures": [_sig("helper")],
                "import_symbols": [{"name": "fetchUser", "module": "./api", "line": 1}],
                "call_sites": [
                    _site("helper", None, "bare", 1, "<module>"),
                    _site("fetchUser", None, "bare", 2, "<module>"),
                ],
            },
        )
        target_rb = FakeParsed(
            tmp_path / "app" / "models" / "user.rb",
            {
                "callable_signatures": [
                    _sig("find_by_slug", enclosing_class="User", kind="singleton_method"),
                ],
            },
        )
        caller_rb = FakeParsed(
            tmp_path / "app" / "controllers" / "users_controller.rb",
            {
                "call_sites": [_site("find_by_slug", "User", "constant", 3, "show")],
            },
        )
        payload = build_calls_index([target_ts, caller_ts, target_rb, caller_rb], tmp_path, "ruby")
        # ruby language: same_file + constant_receiver grades only (no import grade)
        cham = tmp_path / ".chameleon"
        cham.mkdir(parents=True, exist_ok=True)
        (cham / CALLS_INDEX_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        idx = load_calls_index(tmp_path)
        assert idx is not None

        same_file_entry = idx.callers_of("src/page.ts", "helper")
        assert same_file_entry is not None
        assert same_file_entry["callers"][0]["grade"] == "same_file"

        const_entry = idx.callers_of("app/models/user.rb", "find_by_slug")
        assert const_entry is not None
        assert const_entry["callers"][0]["grade"] == "constant_receiver"

    def test_all_three_grades_roundtrip_typescript(self, tmp_path):
        # Build a payload with all three grades present in the artifact, then
        # write it to a real .chameleon dir and verify load preserves each row.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "callees": {
                "src/svc.ts": {
                    "helper": {
                        "callers": [
                            {"path": "src/svc.ts", "caller": "run", "line": 1, "grade": "same_file"}
                        ],
                        "total": 1,
                        "truncated": False,
                    }
                },
                "src/api.ts": {
                    "fetchUser": {
                        "callers": [
                            {
                                "path": "src/page.ts",
                                "caller": "<module>",
                                "line": 9,
                                "grade": "import",
                            }
                        ],
                        "total": 1,
                        "truncated": False,
                    }
                },
                "app/models/user.rb": {
                    "find_by_slug": {
                        "callers": [
                            {
                                "path": "app/controllers/users_controller.rb",
                                "caller": "show",
                                "line": 3,
                                "grade": "constant_receiver",
                            }
                        ],
                        "total": 1,
                        "truncated": False,
                    }
                },
            },
        }
        _write_index(tmp_path, payload)
        idx = load_calls_index(tmp_path)
        assert idx is not None

        sf = idx.callers_of("src/svc.ts", "helper")
        assert sf is not None and sf["callers"][0]["grade"] == "same_file"

        imp = idx.callers_of("src/api.ts", "fetchUser")
        assert imp is not None and imp["callers"][0]["grade"] == "import"

        cr = idx.callers_of("app/models/user.rb", "find_by_slug")
        assert cr is not None and cr["callers"][0]["grade"] == "constant_receiver"


class TestRealDumperAliasedImports:
    @pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
    def test_aliased_import_edges_via_real_dump(self, tmp_path):
        # End-to-end through ts_dump.mjs and the extractor passthrough: the
        # dump must carry the local binding so the builder can separate the
        # colliding exported names.
        from chameleon_mcp.extractors.typescript import TypeScriptExtractor

        (tmp_path / "a.ts").write_text("export function x() { return 1 }\n", encoding="utf-8")
        (tmp_path / "b.ts").write_text("export function x() { return 2 }\n", encoding="utf-8")
        (tmp_path / "page.ts").write_text(
            "import { x } from './a';\n"
            "import { x as y } from './b';\n"
            "export function go() {\n"
            "  x();\n"
            "  y();\n"
            "}\n",
            encoding="utf-8",
        )
        pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.ts")
        idx = build_calls_index(pr.files, tmp_path, "typescript")
        assert idx["callees"]["a.ts"]["x"]["callers"] == [
            {"path": "page.ts", "caller": "go", "line": 4, "grade": "import"}
        ]
        assert idx["callees"]["b.ts"]["x"]["callers"] == [
            {"path": "page.ts", "caller": "go", "line": 5, "grade": "import"}
        ]
        assert "y" not in idx["callees"].get("b.ts", {})


class TestRealDumperMemberChains:
    @pytest.mark.skipif(not _HAVE_TS, reason="node + typescript node_modules not available")
    def test_member_chain_through_namespace_yields_no_edge(self, tmp_path):
        # api.utils.helper() is not api.helper(): the call dispatches through
        # a property of the namespace, which the export set proves nothing
        # about (runtime: api.utils is undefined here). Only the depth-1 form
        # may edge to the namespace target.
        from chameleon_mcp.extractors.typescript import TypeScriptExtractor

        (tmp_path / "m.ts").write_text("export function helper() { return 1 }\n", encoding="utf-8")
        (tmp_path / "chaintrap.ts").write_text(
            "import * as api from './m';\n"
            "export function chainTrap() {\n"
            "  api.helper();\n"
            "  api.utils.helper();\n"
            "}\n",
            encoding="utf-8",
        )
        pr = TypeScriptExtractor().parse_repo(repo_root=tmp_path, glob="**/*.ts")
        idx = build_calls_index(pr.files, tmp_path, "typescript")
        # Exactly one edge: the depth-1 call on line 3. The chained call on
        # line 4 must not be collapsed into a second (fabricated) edge.
        assert idx["callees"]["m.ts"]["helper"]["callers"] == [
            {"path": "chaintrap.ts", "caller": "chainTrap", "line": 3, "grade": "import"}
        ]


class TestRealDumperRubyNamespaces:
    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_short_constant_cross_namespace_yields_no_edge(self, tmp_path):
        # Settings.get inside module B resolves lexically from B, which never
        # reaches A::Settings; the short-name match was a fabrication.
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "namespace_a.rb").write_text(
            "module A\n  class Settings\n    def self.get\n      1\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "namespace_b.rb").write_text(
            "module B\n  class Consumer\n    def run\n      Settings.get\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert "namespace_a.rb" not in idx["callees"]

    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_qualified_constant_receiver_records_edge(self, tmp_path):
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "namespace_a.rb").write_text(
            "module A\n  class Settings\n    def self.get\n      1\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "caller.rb").write_text(
            "class Caller\n  def run\n    A::Settings.get\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert idx["callees"]["namespace_a.rb"]["get"]["callers"] == [
            {"path": "caller.rb", "caller": "run", "line": 3, "grade": "constant_receiver"}
        ]

    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_bare_receiver_still_matches_top_level_class(self, tmp_path):
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "billing.rb").write_text(
            "class Billing\n  def self.charge\n    1\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "caller.rb").write_text(
            "class Caller\n  def run\n    Billing.charge\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert idx["callees"]["billing.rb"]["charge"]["callers"] == [
            {"path": "caller.rb", "caller": "run", "line": 3, "grade": "constant_receiver"}
        ]

    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_bare_receiver_inside_same_module_yields_no_edge(self, tmp_path):
        # Settings.get from inside ANOTHER file's `module A` context WOULD
        # lexically resolve to A::Settings, but call sites carry no lexical
        # context, so the edge is unprovable. Pinned as accepted undercoverage.
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "namespace_a.rb").write_text(
            "module A\n  class Settings\n    def self.get\n      1\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "other_a.rb").write_text(
            "module A\n  class Helper\n    def run\n      Settings.get\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert "namespace_a.rb" not in idx["callees"]

    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_compact_path_class_matches_qualified_receiver(self, tmp_path):
        # `class Utils::Helper` keys as "Utils::Helper" (constant_path already
        # qualified, empty nesting stack), so the qualified receiver matches.
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "utils_helper.rb").write_text(
            "class Utils::Helper\n  def self.assist\n    1\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "caller.rb").write_text(
            "class Caller\n  def run\n    Utils::Helper.assist\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert idx["callees"]["utils_helper.rb"]["assist"]["callers"] == [
            {"path": "caller.rb", "caller": "run", "line": 3, "grade": "constant_receiver"}
        ]

    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_self_new_override_yields_no_edge_via_real_dump(self, tmp_path):
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "three.rb").write_text(
            "class Three\n"
            "  def self.new\n"
            "    42\n"
            "  end\n"
            "\n"
            "  def initialize\n"
            "    @x = 1\n"
            "  end\n"
            "end\n",
            encoding="utf-8",
        )
        (tmp_path / "caller.rb").write_text(
            "class Caller\n  def run\n    Three.new\n  end\nend\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert "three.rb" not in idx["callees"]


class TestRealDumperRubySingletonScope:
    @pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
    def test_constant_edges_respect_member_kinds_via_real_dump(self, tmp_path):
        # End-to-end through prism_dump.rb: a `class << self` def must grade
        # a constant-receiver edge, an instance def with the same name must
        # not, and Const.new must still resolve to the instance initialize.
        from chameleon_mcp.extractors.ruby import RubyExtractor

        (tmp_path / "mailer.rb").write_text(
            "class Mailer\n  class << self\n    def deliver(msg)\n      msg\n    end\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "notifier.rb").write_text(
            "class Notifier\n  def deliver(msg)\n    msg\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "alpha_service.rb").write_text(
            "class AlphaService\n  def initialize(x)\n    @x = x\n  end\nend\n",
            encoding="utf-8",
        )
        (tmp_path / "caller.rb").write_text(
            "class Caller\n"
            "  def run\n"
            "    Mailer.deliver(1)\n"
            "    Notifier.deliver(2)\n"
            "    AlphaService.new(3)\n"
            "  end\n"
            "end\n",
            encoding="utf-8",
        )
        pr = RubyExtractor().parse_repo(repo_root=tmp_path, glob="**/*.rb")
        idx = build_calls_index(pr.files, tmp_path, "ruby")
        assert idx["callees"]["mailer.rb"]["deliver"]["callers"] == [
            {"path": "caller.rb", "caller": "run", "line": 3, "grade": "constant_receiver"}
        ]
        # Notifier#deliver is instance-only: Notifier.deliver cannot dispatch
        # to it, so no edge may be recorded.
        assert "notifier.rb" not in idx["callees"]
        assert idx["callees"]["alpha_service.rb"]["initialize"]["callers"] == [
            {"path": "caller.rb", "caller": "run", "line": 5, "grade": "constant_receiver"}
        ]


class TestDumpTimeTruncation:
    def test_dump_capped_file_marks_contributed_entries_truncated(self, tmp_path):
        # A file with call_sites_truncated True in its extras signals that the
        # dumper capped its site list; every callee entry it contributed to
        # must be marked truncated (the recorded sites are a lower bound).
        pf = FakeParsed(
            tmp_path / "src" / "big.ts",
            {
                "callable_signatures": [_sig("helper")],
                "call_sites": [_site("helper", None, "bare", 1, "run")],
                "call_sites_truncated": True,
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/big.ts"]["helper"]
        assert entry["truncated"] is True

    def test_non_capped_file_does_not_set_truncated(self, tmp_path):
        pf = FakeParsed(
            tmp_path / "src" / "small.ts",
            {
                "callable_signatures": [_sig("helper")],
                "call_sites": [_site("helper", None, "bare", 1, "run")],
            },
        )
        idx = build_calls_index([pf], tmp_path, "typescript")
        entry = idx["callees"]["src/small.ts"]["helper"]
        assert entry["truncated"] is False


class TestPythonSrcLayout:
    """Regression: absolute Python imports resolve against a PyPA src-layout
    src/ package root, not only the repo root. Resolving only the root left
    every absolute-import edge unresolved, building an empty calls index for
    the entire class of src-layout repos."""

    def test_absolute_import_resolves_under_src_root(self, tmp_path):
        _touch(tmp_path, "src/pkg/models/record.py")
        _touch(tmp_path, "src/pkg/readers/csv_reader.py")
        target = FakeParsed(
            tmp_path / "src" / "pkg" / "models" / "record.py",
            {"named_export_names": ["Record"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "src" / "pkg" / "readers" / "csv_reader.py",
            {
                # absolute import (pkg.sub) — the package lives under src/
                "import_symbols": [{"name": "Record", "module": "pkg.models.record", "line": 1}],
                "call_sites": [_site("Record", None, "bare", 11, "read_csv")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "python")
        entry = idx["callees"]["src/pkg/models/record.py"]["Record"]
        assert entry["callers"] == [
            {
                "path": "src/pkg/readers/csv_reader.py",
                "caller": "read_csv",
                "line": 11,
                "grade": "import",
            }
        ]

    def test_flat_layout_absolute_import_still_resolves_at_root(self, tmp_path):
        # Root is probed first, so a flat-layout repo is unchanged.
        _touch(tmp_path, "pkg/models/record.py")
        _touch(tmp_path, "pkg/readers/csv_reader.py")
        target = FakeParsed(
            tmp_path / "pkg" / "models" / "record.py",
            {"named_export_names": ["Record"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "pkg" / "readers" / "csv_reader.py",
            {
                "import_symbols": [{"name": "Record", "module": "pkg.models.record", "line": 1}],
                "call_sites": [_site("Record", None, "bare", 11, "read_csv")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "python")
        assert idx["callees"]["pkg/models/record.py"]["Record"]["callers"][0]["grade"] == "import"

    def test_absolute_import_resolves_under_non_package_source_root(self, tmp_path):
        # A `backend/`-rooted layout (e.g. the FastAPI template): the package
        # `app` lives under backend/, imported as `app.sub`. backend/ is a source
        # root because it is not itself a package but contains one.
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/models/record.py")
        _touch(tmp_path, "backend/app/readers/csv_reader.py")
        target = FakeParsed(
            tmp_path / "backend" / "app" / "models" / "record.py",
            {"named_export_names": ["Record"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "backend" / "app" / "readers" / "csv_reader.py",
            {
                "import_symbols": [{"name": "Record", "module": "app.models.record", "line": 1}],
                "call_sites": [_site("Record", None, "bare", 11, "read_csv")],
            },
        )
        idx = build_calls_index([target, caller], tmp_path, "python")
        entry = idx["callees"]["backend/app/models/record.py"]["Record"]
        assert entry["callers"][0]["grade"] == "import"
        assert entry["callers"][0]["path"] == "backend/app/readers/csv_reader.py"

    def test_package_at_root_is_not_treated_as_source_root(self, tmp_path):
        # A child that is itself a package (has __init__) must NOT be added as a
        # source root, or a flat-layout `pkg.sub` could double-resolve. Here the
        # only valid resolution is under the repo root.
        from chameleon_mcp.symbol_index import _python_source_roots

        _touch(tmp_path, "pkg/__init__.py")
        _touch(tmp_path, "pkg/sub/__init__.py")
        roots = _python_source_roots(tmp_path.resolve())
        assert roots == [tmp_path.resolve()]


class TestModuleAttribute:
    """Python `from pkg import mod; mod.func()` submodule-attribute edges: the
    ``module_attribute`` grade (the Python analog of TS ``typed_property`` /
    Ruby ``constant_receiver``)."""

    def _repo(self, tmp_path, sites, crud_sig="create_user"):
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/crud.py")
        _touch(tmp_path, "backend/app/routes.py")
        crud = FakeParsed(
            tmp_path / "backend" / "app" / "crud.py",
            {"callable_signatures": [_sig(crud_sig)]},
        )
        # The package __init__ lists the submodule `crud` in named_export_names,
        # exactly as the dumper does for a package -- the case a naive
        # export-set "shadow" guard wrongly skipped, defeating the whole grade.
        init = FakeParsed(
            tmp_path / "backend" / "app" / "__init__.py",
            {"named_export_names": ["crud"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "backend" / "app" / "routes.py",
            {
                "import_symbols": [{"name": "crud", "local": "crud", "module": "app", "line": 1}],
                "call_sites": sites,
                "callable_signatures": [_sig("handler")],
            },
        )
        return [crud, init, caller]

    def test_from_import_submodule_member_resolves(self, tmp_path):
        files = self._repo(tmp_path, [_site("create_user", "crud", "member", 5, "handler")])
        idx = build_calls_index(files, tmp_path, "python")
        entry = idx["callees"]["backend/app/crud.py"]["create_user"]
        assert entry["callers"] == [
            {
                "path": "backend/app/routes.py",
                "caller": "handler",
                "line": 5,
                "grade": "module_attribute",
            }
        ]

    def test_member_not_defined_in_submodule_yields_no_edge(self, tmp_path):
        files = self._repo(tmp_path, [_site("no_such_fn", "crud", "member", 5, "handler")])
        idx = build_calls_index(files, tmp_path, "python")
        assert "backend/app/crud.py" not in idx["callees"]

    def test_relative_from_import_resolves_to_current_package(self, tmp_path):
        # `from . import views` (module=".") must resolve to the CURRENT package's
        # views (`.views`), not the parent's (`..views`). A naive dot-join bumped
        # the level, pointing a real call at the parent-level same-named module --
        # a false edge on the standard Django/DRF layout. The parent `views.py`
        # here (same method name) would be the WRONG target under that bug.
        _touch(tmp_path, "views.py")
        _touch(tmp_path, "pkg/__init__.py")
        _touch(tmp_path, "pkg/views.py")
        _touch(tmp_path, "pkg/urls.py")
        parent_views = FakeParsed(tmp_path / "views.py", {"callable_signatures": [_sig("render")]})
        pkg_views = FakeParsed(
            tmp_path / "pkg" / "views.py", {"callable_signatures": [_sig("render")]}
        )
        urls = FakeParsed(
            tmp_path / "pkg" / "urls.py",
            {
                "import_symbols": [{"name": "views", "local": "views", "module": ".", "line": 1}],
                "call_sites": [_site("render", "views", "member", 3, "route")],
                "callable_signatures": [_sig("route")],
            },
        )
        idx = build_calls_index([parent_views, pkg_views, urls], tmp_path, "python")
        # The edge points at pkg/views.py, and the parent views.py gets NONE.
        assert idx["callees"]["pkg/views.py"]["render"]["callers"] == [
            {"path": "pkg/urls.py", "caller": "route", "line": 3, "grade": "module_attribute"}
        ]
        assert "render" not in idx["callees"].get("views.py", {})

    def test_receiver_not_a_submodule_file_yields_no_edge(self, tmp_path):
        # `from app import helper` where app/helper.py does NOT exist (helper is a
        # name from app/__init__, not a submodule) resolves to no module file.
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/routes.py")
        caller = FakeParsed(
            tmp_path / "backend" / "app" / "routes.py",
            {
                "import_symbols": [
                    {"name": "helper", "local": "helper", "module": "app", "line": 1}
                ],
                "call_sites": [_site("do", "helper", "member", 5, "handler")],
                "callable_signatures": [_sig("handler")],
            },
        )
        idx = build_calls_index([caller], tmp_path, "python")
        assert idx["callees"] == {}

    def test_class_member_only_name_yields_no_edge(self, tmp_path):
        # `mod.method()` where `method` exists only INSIDE a class in mod: the
        # module object has no such attribute (AttributeError at runtime), so
        # asserting an edge would be a false positive.
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/klass.py")
        _touch(tmp_path, "backend/app/routes.py")
        klass = FakeParsed(
            tmp_path / "backend" / "app" / "klass.py",
            {"callable_signatures": [_sig("method", enclosing_class="SomeClass", kind="method")]},
        )
        init = FakeParsed(
            tmp_path / "backend" / "app" / "__init__.py",
            {"named_export_names": ["klass"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "backend" / "app" / "routes.py",
            {
                "import_symbols": [{"name": "klass", "local": "klass", "module": "app", "line": 1}],
                "call_sites": [_site("method", "klass", "member", 5, "handler")],
                "callable_signatures": [_sig("handler")],
            },
        )
        idx = build_calls_index([klass, init, caller], tmp_path, "python")
        assert "backend/app/klass.py" not in idx["callees"]

    def test_module_level_def_beside_class_member_keeps_edge(self, tmp_path):
        # A module-level def is a real module attribute even when a class in
        # the same file defines a member with the same name; the edge stands.
        _touch(tmp_path, "backend/app/__init__.py")
        _touch(tmp_path, "backend/app/crud.py")
        _touch(tmp_path, "backend/app/routes.py")
        crud = FakeParsed(
            tmp_path / "backend" / "app" / "crud.py",
            {
                "callable_signatures": [
                    _sig("run"),
                    _sig("run", enclosing_class="Runner", kind="method"),
                ]
            },
        )
        init = FakeParsed(
            tmp_path / "backend" / "app" / "__init__.py",
            {"named_export_names": ["crud"], "export_set_open": False},
        )
        caller = FakeParsed(
            tmp_path / "backend" / "app" / "routes.py",
            {
                "import_symbols": [{"name": "crud", "local": "crud", "module": "app", "line": 1}],
                "call_sites": [_site("run", "crud", "member", 5, "handler")],
                "callable_signatures": [_sig("handler")],
            },
        )
        idx = build_calls_index([crud, init, caller], tmp_path, "python")
        assert idx["callees"]["backend/app/crud.py"]["run"]["callers"] == [
            {
                "path": "backend/app/routes.py",
                "caller": "handler",
                "line": 5,
                "grade": "module_attribute",
            }
        ]

    def test_grade_is_python_only(self, tmp_path):
        # A TS member call on a from-import must never produce a module_attribute
        # edge -- the grade is gated to Python.
        _touch(tmp_path, "src/crud.ts")
        _touch(tmp_path, "src/routes.ts")
        crud = FakeParsed(
            tmp_path / "src" / "crud.ts",
            {
                "named_export_names": ["createUser"],
                "export_set_open": False,
                "callable_signatures": [_sig("createUser")],
            },
        )
        caller = FakeParsed(
            tmp_path / "src" / "routes.ts",
            {
                "import_symbols": [{"name": "crud", "module": "./crud", "line": 1}],
                "call_sites": [_site("createUser", "crud", "member", 5, "handler")],
            },
        )
        idx = build_calls_index([crud, caller], tmp_path, "typescript")
        grades = {
            c["grade"] for bn in idx["callees"].values() for e in bn.values() for c in e["callers"]
        }
        assert "module_attribute" not in grades


class TestLoadReadCap:
    """Regression: the read ceiling derives from the edge cap so the two cannot
    drift. A fixed 16MB cap rejected a legitimately-built large index, silently
    zeroing get_callers / get_blast_radius / get_callees on big repos."""

    def _big_payload(self, n):
        callees = {}
        for i in range(n):
            callees[f"src/mod{i}.ts"] = {
                "fn": {
                    "callers": [
                        {"path": f"src/c{i}.ts", "caller": "go", "line": 1, "grade": "import"}
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        return {"schema_version": SCHEMA_VERSION, "callees": callees}

    def test_large_valid_index_loads_under_default_cap(self, tmp_path):
        _write_index(tmp_path, self._big_payload(300))
        size = (tmp_path / ".chameleon" / CALLS_INDEX_FILENAME).stat().st_size
        assert size > 7000
        assert load_calls_index(tmp_path) is not None

    def test_read_cap_tracks_edge_threshold(self, tmp_path, monkeypatch):
        _write_index(tmp_path, self._big_payload(300))
        size = (tmp_path / ".chameleon" / CALLS_INDEX_FILENAME).stat().st_size
        # Derived ceiling is edge_cap * 700; shrink edge_cap so it falls below
        # the file size and the loader must reject (cap can never drift below
        # build output without this firing).
        monkeypatch.setenv("CHAMELEON_CALLS_INDEX_MAX_TOTAL_EDGES", str(max(1, size // 700 - 1)))
        assert load_calls_index(tmp_path) is None
