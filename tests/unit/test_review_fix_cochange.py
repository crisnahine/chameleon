"""Regression tests for the Python branch of the co-change stale-test advisory.

`changed_exports_in_content` previously had only typescript/ruby arms; Python
fell through to an empty list, so a Python stale-test advisory never carried the
"changed exports" clause that TS/Ruby files got. These lock in the Python export
surface and confirm the TS/Ruby behavior is untouched.
"""

from chameleon_mcp.cochange import changed_exports_in_content


def test_python_returns_top_level_public_def_and_class_names():
    src = (
        "import os\n"
        "\n"
        "CONST = 1\n"
        "\n"
        "def public_fn():\n"
        "    pass\n"
        "\n"
        "class PublicClass:\n"
        "    def method_should_not_count(self):\n"
        "        pass\n"
        "\n"
        "async def fetch_data():\n"
        "    pass\n"
    )
    assert changed_exports_in_content(src, language="python") == [
        "public_fn",
        "PublicClass",
        "fetch_data",
    ]


def test_python_excludes_underscore_prefixed_and_single_char_names():
    src = "def _private_fn():\n    pass\n\nclass _Hidden:\n    pass\n\ndef x():\n    pass\n"
    assert changed_exports_in_content(src, language="python") == []


def test_python_excludes_nested_methods():
    # Only the top-level class is the module's public surface; its indented
    # methods are not importable names.
    src = "class Service:\n    def run(self):\n        pass\n    def stop(self):\n        pass\n"
    assert changed_exports_in_content(src, language="python") == ["Service"]


def test_python_ignores_def_class_inside_docstrings_and_strings():
    src = (
        "def real_one():\n"
        '    """def fake_in_doc(): class FakeClass:"""\n'
        "    pass\n"
        "\n"
        'NOTE = "class StringClass: def string_fn():"\n'
    )
    assert changed_exports_in_content(src, language="python") == ["real_one"]


def test_python_dedupes_repeated_names():
    src = "def handler():\n    pass\n\ndef handler():\n    pass\n"
    assert changed_exports_in_content(src, language="python") == ["handler"]


def test_python_drops_export_name_skip_tokens():
    # `class` appears in _EXPORT_NAME_SKIP; a real declaration named `Base`
    # (also skipped) must not surface, matching TS/Ruby skip behavior.
    src = "class Base:\n    pass\n\ndef widget():\n    pass\n"
    assert changed_exports_in_content(src, language="python") == ["widget"]


def test_typescript_branch_unchanged():
    src = "export function foo() {}\nexport const bar = 1;\n"
    assert changed_exports_in_content(src, language="typescript") == ["foo", "bar"]


def test_ruby_branch_unchanged():
    src = "class Widget\nend\n\nmodule Helpers\nend\n"
    assert changed_exports_in_content(src, language="ruby") == ["Widget", "Helpers"]


def test_unknown_language_returns_empty():
    assert changed_exports_in_content("def foo(): pass", language="go") == []
