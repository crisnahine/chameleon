import shutil
from pathlib import Path

import pytest

from chameleon_mcp.extractors.ruby import RubyExtractor, _extras_from_record


def test_class_body_calls_lifted_from_record():
    record = {
        "class_body_calls": [
            {"name": "string", "class": "FooInteraction"},
            {"name": "integer", "class": "FooInteraction"},
        ],
        "callable_signatures": [],
        "function_scopes": [],
        "call_sites": [],
    }
    extras = _extras_from_record(record)
    assert extras["class_body_calls"] == [
        {"name": "string", "class": "FooInteraction"},
        {"name": "integer", "class": "FooInteraction"},
    ]


def test_class_body_calls_absent_defaults_empty():
    extras = _extras_from_record({"callable_signatures": []})
    assert extras.get("class_body_calls", []) == []


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby not installed")
def test_prism_dump_emits_class_body_calls(tmp_path: Path):
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    src = tmp_path / "foo_interaction.rb"
    src.write_text(
        "class FooInteraction < ActiveInteraction::Base\n"
        "  string :name\n"
        "  integer :count, default: 0\n"
        "  private\n"
        "  def execute\n"
        "    helper_call\n"  # inside a method -> must NOT be a class_body_call
        "  end\n"
        "end\n"
    )
    result = RubyExtractor().parse_repo(tmp_path)
    pf = next(f for f in result.files if f.path.name == "foo_interaction.rb")
    calls = pf.extras.get("class_body_calls", [])
    names = {c["name"] for c in calls}
    assert "string" in names and "integer" in names
    assert "helper_call" not in names  # method-body call excluded
    assert {c["class"] for c in calls} == {"FooInteraction"}
