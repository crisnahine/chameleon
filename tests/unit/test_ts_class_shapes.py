import shutil
from pathlib import Path

import pytest

from chameleon_mcp.extractors.typescript import TypeScriptExtractor, _extras_from_record


def test_class_shapes_lifted_from_record():
    record = {
        "class_shapes": [
            {
                "name": "FooService",
                "decorators": ["Injectable"],
                "extends": "BaseService",
                "implements": ["OnInit"],
            },
        ],
        "callable_signatures": [],
        "function_scopes": [],
        "call_sites": [],
    }
    extras = _extras_from_record(record)
    assert extras["class_shapes"][0]["decorators"] == ["Injectable"]
    assert extras["class_shapes"][0]["extends"] == "BaseService"


def test_class_shapes_absent_defaults_empty():
    extras = _extras_from_record({"callable_signatures": []})
    assert extras.get("class_shapes", []) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ts_dump_emits_class_shapes(tmp_path: Path):
    (tmp_path / "tsconfig.json").write_text("{}")
    src = tmp_path / "foo.service.ts"
    src.write_text(
        "@Injectable()\n"
        "export class FooService extends BaseService implements OnInit {\n"
        "  async execute(input: Dto): Promise<Result> { return null as any; }\n"
        "}\n"
    )
    result = TypeScriptExtractor().parse_repo(tmp_path)
    pf = next(f for f in result.files if f.path.name == "foo.service.ts")
    shapes = pf.extras.get("class_shapes", [])
    fs = next(s for s in shapes if s["name"] == "FooService")
    assert fs["decorators"] == ["Injectable"]
    assert fs["extends"] == "BaseService"
    assert "OnInit" in fs["implements"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ts_dump_method_signature_carries_decorators_base_and_path(tmp_path: Path):
    (tmp_path / "tsconfig.json").write_text("{}")
    src = tmp_path / "foo.controller.ts"
    src.write_text(
        "export namespace Api {\n"
        "  export class FooController extends BaseController {\n"
        "    @Get()\n"
        "    findAll(): string { return 'x'; }\n"
        "  }\n"
        "}\n"
    )
    result = TypeScriptExtractor().parse_repo(tmp_path)
    pf = next(f for f in result.files if f.path.name == "foo.controller.ts")
    sigs = pf.extras.get("callable_signatures", [])
    m = next(s for s in sigs if s["name"] == "findAll")
    assert m["enclosing_class"] == "FooController"
    assert m["enclosing_class_path"] == "Api.FooController"
    assert m["base_class"] == "BaseController"
    assert "Get" in m.get("decorators", [])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ts_dump_plain_function_has_no_class_fields(tmp_path: Path):
    (tmp_path / "tsconfig.json").write_text("{}")
    src = tmp_path / "util.ts"
    src.write_text("export function helper(x: number): number { return x; }\n")
    result = TypeScriptExtractor().parse_repo(tmp_path)
    pf = next(f for f in result.files if f.path.name == "util.ts")
    sigs = pf.extras.get("callable_signatures", [])
    m = next(s for s in sigs if s["name"] == "helper")
    assert m.get("enclosing_class") is None
    assert m.get("enclosing_class_path") is None  # omitted for plain functions
    assert m.get("base_class") is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ts_dump_top_level_class_method_path_is_bare_name(tmp_path: Path):
    (tmp_path / "tsconfig.json").write_text("{}")
    src = tmp_path / "svc.ts"
    src.write_text(
        "export class Svc {\n  run(): void {}\n}\n",
    )
    result = TypeScriptExtractor().parse_repo(tmp_path)
    pf = next(f for f in result.files if f.path.name == "svc.ts")
    sigs = pf.extras.get("callable_signatures", [])
    m = next(s for s in sigs if s["name"] == "run")
    assert m["enclosing_class"] == "Svc"
    assert m["enclosing_class_path"] == "Svc"
    assert m.get("base_class") is None  # no extends -> omitted
