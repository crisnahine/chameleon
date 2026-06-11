"""Unit tests for the committed calls index (build / load / query).

The builder inverts the dumpers' raw call_sites into callee-first caller
edges with exactly three deterministic grades; everything name-only is
deliberately absent. The loader mirrors the symbol-index loaders: fail-open
None on any ambiguity, mtime+size cache token, schema check.
"""

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.calls_index import (
    CALLS_INDEX_FILENAME,
    SCHEMA_VERSION,
    build_calls_index,
    load_calls_index,
)


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


def _sig(name, enclosing_class=None):
    return {"name": name, "kind": "function", "enclosing_class": enclosing_class}


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
                    _sig("initialize", enclosing_class="User"),
                    _sig("find_by_slug", enclosing_class="User"),
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
            {"callable_signatures": [_sig("find_by_slug", enclosing_class="User")]},
        )
        b = FakeParsed(
            tmp_path / "lib" / "legacy" / "user.rb",
            {"callable_signatures": [_sig("find_by_slug", enclosing_class="User")]},
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
            {"callable_signatures": [_sig("find_by_slug", enclosing_class="User")]},
        )
        caller = FakeParsed(
            tmp_path / "app" / "x.rb",
            {"call_sites": [_site("new", "User", "constant", 8, "create")]},
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
                    _sig("find_by_slug", enclosing_class="User"),
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
                            {"path": "src/page.ts", "caller": "<module>", "line": 9, "grade": "import"}
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
