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
                    {"name": n, "kind": "method", "enclosing_class": cls}
                    for n in ("a", "b", "c", "d", "e")
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert len(out["required_methods"]) == 3


def test_below_threshold_macro_dropped():
    # 'string' in all 10, 'rare' in only 4 (40% < 60%)
    def f(i):
        cls = f"C{i}"
        calls = [{"name": "string", "class": cls}]
        if i < 4:
            calls.append({"name": "rare", "class": cls})
        return _pf(f"a{i}.rb", extras={"class_body_calls": calls, "callable_signatures": []})

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="ruby")
    assert out["dsl_macros"] == ["string"]


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
