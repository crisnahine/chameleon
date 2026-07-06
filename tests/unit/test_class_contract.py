from pathlib import Path

from chameleon_mcp.conventions import (
    extract_all_conventions,
    extract_class_contract_conventions,
)
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: str, *, extras: dict) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras=extras,
    )


def _ruby_interaction(i: int) -> ParsedFile:
    cls = f"Foo{i}Interaction"
    return _pf(
        f"app/interactions/foo{i}.rb",
        extras={
            "class_body_calls": [
                {"name": "string", "class": cls},
                {"name": "integer", "class": cls},
                {"name": "object", "class": cls},
                {"name": "private", "class": cls},  # stoplisted
            ],
            "callable_signatures": [
                {
                    "name": "execute",
                    "kind": "method",
                    "enclosing_class": cls,
                    "base_class": "ActiveInteraction::Base",
                },
            ],
        },
    )


def test_ruby_active_interaction_contract():
    files = [_ruby_interaction(i) for i in range(12)]
    out = extract_class_contract_conventions(files, language="ruby")
    assert out["dsl_macros"] == ["integer", "object", "string"]  # sorted, stoplist drops private
    assert out["required_methods"] == ["execute"]
    assert out["base"] == "ActiveInteraction::Base"
    assert out["sample_size"] == 12
    assert "private" not in out["dsl_macros"]


def test_ts_nest_contract():
    def f(i):
        cls = f"Foo{i}Service"
        return _pf(
            f"src/foo{i}.service.ts",
            extras={
                "class_shapes": [
                    {
                        "name": cls,
                        "decorators": ["Injectable"],
                        "extends": "BaseService",
                        "implements": [],
                    },
                ],
                "callable_signatures": [
                    {"name": "execute", "kind": "method", "enclosing_class": cls},
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="typescript")
    assert out["decorators"] == ["Injectable"]
    assert out["required_methods"] == ["execute"]
    assert out["base"] == "BaseService"
    assert "dsl_macros" not in out  # TS has no DSL-macro key


def test_minority_rich_anchor_does_not_beat_dominant_cohort():
    # Flask-shaped: many classes per file. A niche decorator carried by 4 of 12
    # classes clears the file-count anchor gate (4 >= 0.6 * 5 files) and yields
    # a richer contract than the dominant base, but its within-cohort
    # frequencies are NOT the archetype's contract — the dominant cohort must
    # win, and the niche decorator must not be projected onto every class.
    files = []
    rich = [f"Rich{j}View" for j in range(4)]
    files.append(
        _pf(
            "app/user/views.py",
            extras={
                "class_shapes": [
                    {"name": c, "bases": ["MethodView"], "decorators": ["attr.s"]} for c in rich
                ],
                "callable_signatures": [
                    {"name": m, "kind": "method", "enclosing_class": c}
                    for c in rich
                    for m in ("get", "post", "redirect")
                ],
            },
        )
    )
    for i in range(1, 5):
        classes = [f"Plain{i}AView", f"Plain{i}BView"]
        files.append(
            _pf(
                f"app/mod{i}/views.py",
                extras={
                    "class_shapes": [
                        {"name": c, "bases": ["MethodView"], "decorators": []} for c in classes
                    ],
                    "callable_signatures": [
                        {"name": "get", "kind": "method", "enclosing_class": c} for c in classes
                    ],
                },
            )
        )
    out = extract_class_contract_conventions(files, language="python")
    assert out["base"] == "MethodView"
    assert out["sample_size"] == 12
    assert "attr.s" not in out.get("decorators", [])
    assert "redirect" not in out.get("required_methods", [])
    assert "get" in out.get("required_methods", [])


def test_below_sample_size_returns_empty():
    files = [_ruby_interaction(i) for i in range(9)]
    assert extract_class_contract_conventions(files, language="ruby") == {}


def test_no_contract_returns_empty():
    files = [
        _pf(f"app/x{i}.rb", extras={"class_body_calls": [], "callable_signatures": []})
        for i in range(12)
    ]
    assert extract_class_contract_conventions(files, language="ruby") == {}


def test_required_methods_capped_top_3():
    def f(i):
        cls = f"C{i}"
        return _pf(
            f"a{i}.rb",
            extras={
                "class_body_calls": [],
                "callable_signatures": [
                    {"name": n, "kind": "method", "enclosing_class": cls, "base_class": "Base"}
                    for n in ("a", "b", "c", "d", "e")
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert len(out["required_methods"]) == 3


def test_below_threshold_macro_dropped():
    # 'string' in all 10, 'rare' in only 4 (40% < 60%); base anchors the contract
    def f(i):
        cls = f"C{i}"
        calls = [{"name": "string", "class": cls}]
        if i < 4:
            calls.append({"name": "rare", "class": cls})
        return _pf(
            f"a{i}.rb",
            extras={
                "class_body_calls": calls,
                "callable_signatures": [
                    {"name": "run", "kind": "method", "enclosing_class": cls, "base_class": "Base"}
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert out["dsl_macros"] == ["string"]


def test_nested_helper_class_does_not_dilute_or_pollute():
    # Each file has the interaction class PLUS a co-located error class with its own
    # method. The error class must neither collapse the contract nor leak `message`.
    def f(i):
        main = f"Create{i}"
        err = f"Error{i}"
        return _pf(
            f"app/interactions/c{i}.rb",
            extras={
                "class_body_calls": [
                    {"name": "string", "class": main},
                    {"name": "integer", "class": main},
                ],
                "callable_signatures": [
                    {
                        "name": "execute",
                        "kind": "method",
                        "enclosing_class": main,
                        "base_class": "ActiveInteraction::Base",
                    },
                    {
                        "name": "message",
                        "kind": "method",
                        "enclosing_class": err,
                        "base_class": "StandardError",
                    },
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert out["base"] == "ActiveInteraction::Base"
    assert "string" in out["dsl_macros"]
    assert out["required_methods"] == ["execute"]
    assert "message" not in out["required_methods"]


def test_initialize_and_operators_excluded_from_required_methods():
    def f(i):
        cls = f"Svc{i}"
        return _pf(
            f"app/services/s{i}.rb",
            extras={
                "class_body_calls": [],
                "callable_signatures": [
                    {"name": n, "kind": "method", "enclosing_class": cls, "base_class": "Base"}
                    for n in ("initialize", "to_s", "==", "call")
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert out["required_methods"] == ["call"]


def test_allowlisted_rails_dsl_excluded_from_macros():
    # validates/belongs_to are generic (Common DSL covers them); only the novel
    # macro `monetize` is the contract's value-add.
    def f(i):
        cls = f"Model{i}"
        return _pf(
            f"app/models/m{i}.rb",
            extras={
                "class_body_calls": [
                    {"name": "validates", "class": cls},
                    {"name": "belongs_to", "class": cls},
                    {"name": "monetize", "class": cls},
                ],
                "callable_signatures": [
                    {
                        "name": "x",
                        "kind": "method",
                        "enclosing_class": cls,
                        "base_class": "ApplicationRecord",
                    }
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert out["dsl_macros"] == ["monetize"]


def test_no_structural_anchor_returns_empty():
    # Classes share a method name but have no base and no decorator -> not a contract.
    def f(i):
        cls = f"P{i}"
        return _pf(
            f"a{i}.rb",
            extras={
                "class_body_calls": [],
                "callable_signatures": [{"name": "run", "kind": "method", "enclosing_class": cls}],
            },
        )

    assert extract_class_contract_conventions([f(i) for i in range(10)], language="ruby") == {}


def test_wired_into_extract_all_ruby():
    files = [_ruby_interaction(i) for i in range(12)]
    conv = extract_all_conventions(
        files_by_archetype={"interaction": files},
        declarations_by_archetype={},
        generation=0,
        language="ruby",
    )
    cc = conv["conventions"]["class_contract"]["interaction"]
    assert cc["required_methods"] == ["execute"]
    assert "string" in cc["dsl_macros"]
