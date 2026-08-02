"""The in-process tree-sitter extractor's safety and selection contracts.

Field-level fidelity is measured by tests/differential_treesitter.py against the
real dump scripts; duplicating that here would only restate a corpus diff. What
this file guards is everything the differential CANNOT see: the invariants that
only matter because parsing moved in-process, and the two contract facts a
failing suite already caught once.

The recursion guard is the load-bearing one. Tree-sitter parses a 20,000-deep
expression happily, and a recursive walk over the result raises RecursionError
where the iterative walk returns -- in a subprocess that killed a child, but
in-process it takes the MCP server down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.extractors.registry import select_extractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.treesitter.extractor import TreeSitterExtractor, parse_file
from chameleon_mcp.extractors.treesitter.grammars import (
    TreeSitterUnavailableError,
    grammar_for_path,
    language_for_path,
    probe,
)


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


# --- grammars -------------------------------------------------------------


def test_every_grammar_loads_under_the_pinned_abi():
    """A grammar wheel and the core float independently on PyPI.

    The 0.25.x grammars compile to Language ABI 15, which the core that
    tree-sitter-typescript pins (0.23.x) rejects outright, so an unpinned
    install fails at Language() construction rather than at import.
    """
    assert all(status == "ok" for status in probe().values()), probe()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.ts", "typescript"),
        ("a.tsx", "typescript"),
        ("a.mjs", "typescript"),
        ("a.rb", "ruby"),
        ("a.py", "python"),
        ("a.pyi", "python"),
        # The spec-driven languages. `.go` asserted None while Go was
        # unsupported; it now has a spec, a grammar and an extractor.
        ("a.go", "go"),
        ("a.rs", "rust"),
        ("a.java", "java"),
        ("a.cs", "csharp"),
        ("a.php", "php"),
        # Still genuinely unsupported: no spec ships for it.
        ("a.zig", None),
    ],
)
def test_language_for_path_maps_extensions(name: str, expected: str | None):
    assert language_for_path(name) == expected


def test_unsupported_extension_raises_unavailable_not_keyerror():
    """The bootstrap orchestrator catches ExtractorUnavailableError.

    A raw KeyError from a dependency would escape to the MCP boundary instead
    of degrading into a clean failed report.
    """
    with pytest.raises(TreeSitterUnavailableError):
        grammar_for_path("nope.zig")


# --- selection ------------------------------------------------------------


def test_selection_preserves_the_detected_language(tmp_path: Path):
    """`.language` is read downstream to learn what the repo is written in.

    Archetype naming, the framework-aware layers, and the language-gated lint
    rules all branch on it, so reporting a generic backend name silently
    strips every language-specific behavior from the repo.
    """
    repo = tmp_path / "rb"
    repo.mkdir()
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    (repo / "a.rb").write_text("class A; end\n", encoding="utf-8")

    extractor = select_extractor(repo)
    assert isinstance(extractor, TreeSitterExtractor)
    assert extractor.language == "ruby"


def test_kill_switch_falls_back_to_the_dump_script(tmp_path: Path, monkeypatch):
    repo = tmp_path / "rb"
    repo.mkdir()
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    (repo / "a.rb").write_text("class A; end\n", encoding="utf-8")

    monkeypatch.setenv("CHAMELEON_TREE_SITTER", "0")
    extractor = select_extractor(repo)
    assert isinstance(extractor, RubyExtractor)
    assert extractor.language == "ruby"


def test_extractor_refuses_a_language_it_has_no_tables_for():
    """Go used to be the example here. It now has a spec, so the assertion needs
    a language that genuinely ships no tables, or it proves nothing."""
    with pytest.raises(ValueError):
        TreeSitterExtractor("zig")
    # And a spec-driven language IS accepted, so the refusal above is about
    # missing tables rather than about anything outside the original three.
    assert TreeSitterExtractor("go").language == "go"


def test_parse_repo_honors_the_paths_argument(tmp_path: Path):
    """The orchestrator passes an already-filtered candidate list.

    Discovery, exclusion, and workspace scoping all run before that call, so
    globbing instead would re-include everything discovery ruled out.
    """
    repo = tmp_path / "rb"
    (repo / "sub").mkdir(parents=True)
    wanted = repo / "sub" / "keep.rb"
    wanted.write_text("class Keep; end\n", encoding="utf-8")
    (repo / "sub" / "skip.rb").write_text("class Skip; end\n", encoding="utf-8")

    result = TreeSitterExtractor("ruby").parse_repo(repo, paths=[wanted])
    assert [p.path.name for p in result.files] == ["keep.rb"]


# --- per-file guards ------------------------------------------------------


def test_deep_nesting_does_not_exhaust_the_python_stack(tmp_path: Path):
    """The single reason the walker must stay iterative.

    20,000 nested parens parse in well under a second and produce a tree far
    deeper than Python's recursion limit; the equivalent recursive walk raises
    RecursionError, which in-process would take the server down rather than a
    child process.
    """
    src = b"x = " + b"(" * 20_000 + b"1" + b")" * 20_000 + b"\n"
    parsed, skip = parse_file(_write(tmp_path, "deep.py", src))
    assert skip is None
    assert parsed is not None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("nul.rb", b"class A\x00\x00\ndef b\x00\nend\nend"),
        ("binary.rb", bytes(range(256)) * 40),
        ("unterminated.py", b'x = "' + b"a" * 5_000),
        ("empty.py", b""),
    ],
)
def test_hostile_input_parses_or_skips_without_raising(tmp_path: Path, name, content):
    parsed, skip = parse_file(_write(tmp_path, name, content))
    assert parsed is not None or skip is not None


def test_damaged_source_is_flagged_rather_than_silently_clean(tmp_path: Path):
    """tree-sitter is error-recovering, so a broken file still yields a tree.

    The recovery is why the diagnostics count has to be read off ERROR nodes:
    a damaged file that parsed is not the same as a clean one.
    """
    parsed, _ = parse_file(_write(tmp_path, "broken.py", b"def (:\n  pass\n"))
    assert parsed is not None
    assert parsed.parse_diagnostics_count > 0


def test_oversized_file_skips_on_the_size_cap(tmp_path: Path):
    src = b"x = 1\n" * 400_000
    parsed, skip = parse_file(_write(tmp_path, "huge.py", src))
    assert parsed is None
    assert skip == "file_too_large"


def test_symlink_is_refused(tmp_path: Path):
    real = _write(tmp_path, "real.py", b"x = 1\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    parsed, skip = parse_file(link)
    assert parsed is None
    assert skip == "symlink_refused"


def test_node_budget_skips_whole_rather_than_truncating(tmp_path: Path, monkeypatch):
    """A capped file is skipped, never half-reported.

    A truncated shape is indistinguishable downstream from a genuine one, so
    the file is dropped with a reason instead.
    """
    monkeypatch.setenv("CHAMELEON_TS_MAX_AST_NODES", "50")
    parsed, skip = parse_file(_write(tmp_path, "many.py", b"x = 1\n" * 500))
    assert parsed is None
    assert skip == "ast_node_ceiling_exceeded"


# --- shape contract -------------------------------------------------------


def test_class_body_calls_is_ruby_only(tmp_path: Path):
    """Absent and empty are different documents downstream.

    Receiverless class-body DSL macros are a Ruby concept; the other dumpers
    omit the key rather than emitting [].
    """
    rb, _ = parse_file(_write(tmp_path, "a.rb", b"class A\n  has_many :bs\nend\n"))
    py, _ = parse_file(_write(tmp_path, "a.py", b"class A:\n    x = 1\n"))

    assert "class_body_calls" in rb.extras
    assert "class_body_calls" not in py.extras


def test_nested_imports_are_recorded(tmp_path: Path):
    """A function-local import binds a name exactly as a top-level one does.

    Collecting only from the root's children missed every nested import in a
    real repo, while leaving `import_specifiers` (walked per node) correct --
    so the two disagreed with each other.
    """
    src = b"def f():\n    from a.b import C\n    return C\n"
    parsed, _ = parse_file(_write(tmp_path, "nested.py", src))
    assert [r["name"] for r in parsed.extras["import_symbols"]] == ["C"]


# --- known grammar defects ------------------------------------------------
#
# tree-sitter's TypeScript grammar rejects several constructs the TypeScript
# compiler accepts, and its grammar has not changed since September 2024 with
# every relevant issue still open upstream. Left uncorrected chameleon reports
# phantom syntax errors on ordinary React and NestJS code -- one construct alone
# (styled-components with an inline props type) accounted for 349 of 352 flagged
# files in a 21,904-file repo. These guard both directions of the suppression.


@pytest.mark.parametrize(
    ("name", "source"),
    [
        # a tagged template whose type argument is structural
        ("styled.ts", b"const A = styled.div<{ a?: boolean }>`x`;\n"),
        ("tuple.ts", b"const A = f<[number]>`x`;\n"),
        # a JSX attribute holding a URL query string (tree-sitter-typescript#320)
        ("jsx.tsx", b'const A = () => <a href="https://x.com/c?a=1&b=2" />;\n'),
        # variance annotations, TypeScript 4.7
        ("variance.ts", b"interface Foo<in T, out U> {}\n"),
        # export type * (#348, PR #358 open)
        ("exporttype.ts", b'export type * as N from "./m";\n'),
        # the import-type family (#322, #352)
        ("impgen.ts", b'const foo: import("./bar").Bar<string> = "baz";\n'),
        ("keyofimp.ts", b'let foo: keyof import("a").A;\n'),
        ("typeofimp.ts", b'f<typeof import("x")>();\n'),
    ],
)
def test_valid_code_a_grammar_defect_rejects_is_not_a_diagnostic(tmp_path: Path, name, source):
    """Every one of these is accepted by the TypeScript compiler."""
    parsed, _ = parse_file(_write(tmp_path, name, source))
    assert parsed is not None
    assert parsed.parse_diagnostics_count == 0


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("broken.ts", b"function ( { { ;\n"),
        ("unclosed.tsx", b"const A = () => <div><span></div>;\n"),
        # the load-bearing case: a REAL error sharing a file with a known defect
        # must still report, or the suppression becomes a way to hide breakage.
        ("mixed.ts", b"const A = styled.div<{ a?: boolean }>`x`;\nfunction ( { {\n"),
        # a genuinely broken line that merely mentions import()
        ("badimport.ts", b'const foo: import("./bar").Bar<string> = ;;;{\n'),
    ],
)
def test_real_syntax_errors_still_report(tmp_path: Path, name, source):
    assert parse_file(_write(tmp_path, name, source))[0].parse_diagnostics_count == 1


# --- ruby local scoping ---------------------------------------------------
#
# The differential measures these against Prism, but only over corpora that
# are not in this repo, so nothing else here would catch a regression. They are
# also the walker's one piece of real semantic machinery rather than table
# data: Ruby has no syntax separating a receiverless send from a local read, so
# every call row in a block-heavy repo depends on getting the scope chain right
# in both directions.


def _ruby_calls(tmp_path: Path, name: str, src: bytes) -> list[str]:
    parsed, _ = parse_file(_write(tmp_path, name, src))
    return [row["name"] for row in parsed.extras["call_sites"]]


def test_a_block_parameter_stops_binding_when_the_block_ends(tmp_path: Path):
    """The regression that cost the most call sites on a real repo.

    A block's `|params|` are scoped to the block, so the later `entry` is an
    unbound name and therefore a send. Binding it into the enclosing function
    instead left it looking like a variable read for the rest of the method,
    which silently dropped the row -- 127 files' worth on forem.
    """
    src = b"def run\n  items.each { |entry| log(entry) }\n  entry\nend\n"
    assert "entry" in _ruby_calls(tmp_path, "leak.rb", src)


def test_a_block_still_sees_the_locals_around_it(tmp_path: Path):
    """The opposite direction, which an independent per-block set would break.

    A block is a closure: `total` is assigned outside it and read inside, so it
    stays a variable read and contributes no call row.
    """
    src = b"def run\n  total = 0\n  items.each { |entry| total }\nend\n"
    assert "total" not in _ruby_calls(tmp_path, "closure.rb", src)


def test_a_method_does_not_close_over_the_locals_around_it(tmp_path: Path):
    """A Ruby method is not a closure, so its scope has no parent.

    `outer` is a local at the top level and an unbound name inside the method,
    which makes the read there a send rather than a variable.
    """
    src = b"outer = 1\ndef run\n  outer\nend\n"
    assert "outer" in _ruby_calls(tmp_path, "not_closure.rb", src)


def test_destructured_block_parameters_bind_at_every_depth(tmp_path: Path):
    """`|((a, b), c), d|` binds all four names, not just the outer ones.

    Binding one level deep left the inner names reading as receiverless sends.
    """
    src = b"def run\n  rows.each { |((date, domain), n), hash| use(date, domain, n, hash) }\nend\n"
    calls = _ruby_calls(tmp_path, "destructure.rb", src)
    assert not {"date", "domain", "n", "hash"} & set(calls)


def test_a_splat_target_in_a_multiple_assignment_binds(tmp_path: Path):
    """`_, *options = ...` binds `options` through a wrapper node.

    Reading only the direct identifier targets missed it, so every later use of
    `options` was promoted to a send it never was.
    """
    src = b"def run\n  _, *options = link.split\n  options.empty?\nend\n"
    assert "options" not in _ruby_calls(tmp_path, "splat.rb", src)


def test_a_function_local_import_is_not_a_module_export(tmp_path: Path):
    """The counterpart to the rule above: recorded, but not importable.

    `import_symbols` is a binding record; the export set is what another module
    can actually import, and a name bound inside a function is neither.
    """
    src = b"def f():\n    import json\n    return json\n"
    parsed, _ = parse_file(_write(tmp_path, "local.py", src))
    assert "json" not in parsed.extras["named_export_names"]
