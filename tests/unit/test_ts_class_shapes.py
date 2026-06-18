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
