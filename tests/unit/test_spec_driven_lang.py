"""The spec-driven languages: their specs, their tables, and their wiring.

A language described by a table of string constants has one failure mode that
matters more than the rest: a node kind or field name that the grammar does not
actually have. Nothing raises -- the match simply never fires, and the language
looks supported while producing no facts at all. `test_every_spec_matches_its_
grammar` is the guard: it loads each real grammar and asserts every name the
spec uses exists in it, so a typo or a grammar bump fails in CI instead of
silently emptying a profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Language

from chameleon_mcp.extractors.treesitter.extractor import TreeSitterExtractor
from chameleon_mcp.extractors.treesitter.lang import specs
from chameleon_mcp.extractors.treesitter.lang.spec_driven import LanguageSpec

# The wheel and factory each spec's grammar loads from, mirroring
# grammars._SPEC_GRAMMAR_MODULES.
_LOADERS: dict[str, tuple[str, str]] = {
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "php": ("tree_sitter_php", "language_php"),
}


def _grammar(name: str) -> Language:
    module_name, factory = _LOADERS[name]
    module = __import__(module_name, fromlist=[factory])
    return Language(getattr(module, factory)())


def _node_kinds(language: Language) -> set[str]:
    return {
        kind
        for kind in (language.node_kind_for_id(i) for i in range(language.node_kind_count))
        if kind
    }


def _field_names(language: Language) -> set[str]:
    return {
        name
        for name in (language.field_name_for_id(i) for i in range(language.field_count + 1))
        if name
    }


@pytest.mark.parametrize("spec", specs.ALL, ids=lambda s: s.name)
def test_every_spec_matches_its_grammar(spec: LanguageSpec):
    """Every node kind and field a spec names must exist in the real grammar.

    Without this, a misspelled node is indistinguishable from a language that
    genuinely has no such construct: both produce zero matches and a profile
    that looks fine.
    """
    language = _grammar(spec.name)
    kinds = _node_kinds(language)
    declared_kinds = (
        set(spec.top_level_kinds)
        | set(spec.function_nodes)
        | set(spec.class_nodes)
        | set(spec.branch_nodes)
        | set(spec.nesting_nodes)
        | set(spec.skip_subtree_nodes)
        | set(spec.call_nodes)
        | set(spec.member_nodes)
        | set(spec.import_nodes)
        | set(spec.import_descend_nodes)
        | set(spec.param_nodes)
        | set(spec.param_rest_nodes)
    )
    missing_kinds = sorted(k for k in declared_kinds if k not in kinds)
    assert not missing_kinds, f"{spec.name}: node kinds absent from the grammar: {missing_kinds}"

    fields = _field_names(language)
    declared_fields = {
        f
        for f in (
            spec.name_field,
            spec.parameters_field,
            spec.body_field,
            spec.return_type_field,
            spec.call_function_field,
            spec.member_object_field,
            spec.member_property_field,
            spec.import_module_field,
            spec.class_bases_field,
            spec.param_name_field,
            *spec.call_receiver_fields,
        )
        if f
    }
    missing_fields = sorted(f for f in declared_fields if f not in fields)
    assert not missing_fields, f"{spec.name}: fields absent from the grammar: {missing_fields}"


def test_every_spec_is_reachable_end_to_end():
    """Each spec has tables, a grammar mapping and a registered extractor.

    A spec that any one of the three does not know about is inert: it parses
    nothing, or it parses and no extractor ever selects it.
    """
    from chameleon_mcp.extractors.registry import EXTRACTORS
    from chameleon_mcp.extractors.treesitter.grammars import language_for_path

    registered = {getattr(cls(), "language", None) for cls in EXTRACTORS}
    for spec in specs.ALL:
        assert TreeSitterExtractor.supports(spec.name), f"{spec.name} has no tables"
        assert spec.name in registered, f"{spec.name} has no registered extractor"
        for ext in spec.extensions:
            assert language_for_path(f"x{ext}") == spec.name, (
                f"{ext} does not resolve to {spec.name}"
            )


# --- the facts each language actually produces ------------------------------ #

# filename, source, and the one call site the source contains as
# (callee, receiver). Every fixture carries a call because a language that
# parses and signs its callables while recording zero call edges looks healthy
# from every other assertion in this module.
_FIXTURES: dict[str, tuple[str, str, tuple[str, str]]] = {
    "go": (
        "svc.go",
        # The GROUPED import form. Go's ungrouped `import "fmt"` hangs the path
        # directly off the declaration while the grouped block wraps each path
        # in an import_spec, so a spec that only descends the first shape
        # records nothing for the form real Go code overwhelmingly uses.
        'package shop\n\nimport (\n\t"fmt"\n\t"net/http"\n)\n\n'
        "type Service struct {\n\trepo Repo\n}\n\n"
        "func NewService(repo Repo) *Service {\n\treturn &Service{repo: repo}\n}\n\n"
        "func (s *Service) Get(id string) error {\n\tfmt.Println(id)\n\treturn nil\n}\n",
        ("Println", "fmt"),
    ),
    "rust": (
        "lib.rs",
        "use std::collections::HashMap;\n\npub struct OrderService {\n    repo: Repo,\n}\n\n"
        "pub fn build(repo: Repo) -> OrderService {\n    repo.warm();\n    OrderService { repo }\n}\n",
        ("warm", "repo"),
    ),
    "java": (
        "OrderService.java",
        "package com.example;\n\nimport java.util.List;\n\n"
        "public class OrderService extends BaseService {\n"
        "    public String find(String id, int limit) {\n        return repo.load(id);\n    }\n}\n",
        ("load", "repo"),
    ),
    "csharp": (
        "OrderService.cs",
        "using System;\n\nnamespace Shop {\n  public class OrderService {\n"
        "    public string Find(string id) { return repo.Load(id); }\n  }\n}\n",
        ("Load", "repo"),
    ),
    "php": (
        "OrderService.php",
        "<?php\nnamespace App;\n\nuse App\\Repos\\OrderRepo;\n\n"
        "class OrderService {\n    public function find(string $id): string {\n"
        "        return $repo->load($id);\n    }\n}\n",
        ("load", "$repo"),
    ),
}


@pytest.mark.parametrize("language", sorted(_FIXTURES), ids=sorted(_FIXTURES))
def test_each_language_extracts_real_facts(tmp_path, language: str):
    """Parsing must yield the facts the profile is built from, not empty shells."""
    name, source, expected_call = _FIXTURES[language]
    (tmp_path / name).write_text(source, encoding="utf-8")
    spec = next(s for s in specs.ALL if s.name == language)

    result = TreeSitterExtractor(language).parse_repo(tmp_path, glob=f"**/*{spec.extensions[0]}")
    assert len(result.files) == 1, f"{language}: {result.skipped}"
    parsed = result.files[0]

    assert parsed.top_level_node_kinds, f"{language} produced no top-level kinds"
    signatures = parsed.extras.get("callable_signatures") or []
    assert signatures, f"{language} produced no callable signatures"
    assert all(s.get("name") for s in signatures), f"{language} emitted an unnamed signature"
    assert all(
        s.get("start_line", 0) >= 1 and s.get("end_line", 0) >= s.get("start_line", 0)
        for s in signatures
    ), f"{language} emitted an impossible span"

    # Call edges are the third fact a profile is built from, and the one that
    # fails silently: a call shape a spec does not name yields no row, which is
    # indistinguishable from a file that calls nothing.
    sites = parsed.extras.get("call_sites") or []
    assert (expected_call[0], expected_call[1]) in [
        (site.get("name"), site.get("receiver")) for site in sites
    ], f"{language} recorded no call edge for {expected_call}: {sites}"
    assert all(site.get("caller") for site in sites), (
        f"{language} recorded a call edge with no enclosing callable: {sites}"
    )


def test_an_import_records_the_module_not_the_statement():
    """Downstream consumers key on the import ROOT, so recording the whole
    statement ("import java.util.List;") makes every import its own unique
    string and the convention unminable."""
    expectations = {
        "rust": ("lib.rs", "use std::collections::HashMap;\n", ("std::collections::HashMap",)),
        "java": ("A.java", "import java.util.List;\n", ("java.util.List",)),
        "csharp": ("A.cs", "using System.Text;\n", ("System.Text",)),
        "php": ("A.php", "<?php\nuse App\\Repos\\OrderRepo;\n", ("App\\Repos\\OrderRepo",)),
        # Both Go forms: the single-path declaration and the parenthesized
        # block. They are different node shapes, and the block is the one real
        # Go code uses once a file imports more than one package.
        "go": ("a.go", 'package p\n\nimport "fmt"\n', ("fmt",)),
        "go_grouped": (
            "b.go",
            'package p\n\nimport (\n\t"fmt"\n\t"net/http"\n)\n',
            ("fmt", "net/http"),
        ),
    }
    import tempfile

    for case, (name, source, expected) in expectations.items():
        language = case.split("_", 1)[0]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / name).write_text(source, encoding="utf-8")
            spec = next(s for s in specs.ALL if s.name == language)
            result = TreeSitterExtractor(language).parse_repo(
                root, glob=f"**/*{spec.extensions[0]}"
            )
            modules = [module for module, _kind in result.files[0].import_specifiers]
            for wanted in expected:
                assert wanted in modules, f"{case}: got {modules}, wanted {wanted!r}"


# filename, source, the resolvable call as (callee, receiver), the chained
# callee that must NOT be recorded, and the chain HEAD that must be.
_CHAINED_FIXTURES: dict[str, tuple[str, str, tuple[str, str], str, str]] = {
    "go": (
        "a.go",
        'package shop\n\nfunc Run(repo Repo) {\n\trepo.Load("x")\n\tbuild().Chained("x")\n}\n',
        ("Load", "repo"),
        "Chained",
        "build",
    ),
    "rust": (
        "a.rs",
        'pub fn run(repo: Repo) {\n    repo.load("x");\n    build().chained("x");\n}\n',
        ("load", "repo"),
        "chained",
        "build",
    ),
    "java": (
        "A.java",
        'class A {\n  void run() {\n    repo.load("x");\n    build().chained("x");\n  }\n}\n',
        ("load", "repo"),
        "chained",
        "build",
    ),
    "csharp": (
        "A.cs",
        'class A {\n  public void Run() {\n    repo.Load("x");\n    Build().Chained("x");\n  }\n}\n',
        ("Load", "repo"),
        "Chained",
        "Build",
    ),
    "php": (
        "A.php",
        "<?php\nclass A {\n  public function run() {\n"
        "    $repo->load('x');\n    build()->chained('x');\n  }\n}\n",
        ("load", "$repo"),
        "chained",
        "build",
    ),
}


@pytest.mark.parametrize("language", sorted(_CHAINED_FIXTURES), ids=sorted(_CHAINED_FIXTURES))
def test_a_call_with_an_unresolvable_receiver_is_not_recorded(tmp_path, language: str):
    """Conservative by design: a chained or computed receiver cannot be resolved
    against the index, and a half-resolved edge is worse than an absent one --
    an absent edge reads as "unknown", a wrong one reads as fact.

    The head of the chain is asserted present alongside the dropped edge, so a
    language that stopped recording call sites entirely cannot pass this by
    producing nothing.
    """
    name, source, resolvable, chained, head = _CHAINED_FIXTURES[language]
    (tmp_path / name).write_text(source, encoding="utf-8")
    spec = next(s for s in specs.ALL if s.name == language)

    result = TreeSitterExtractor(language).parse_repo(tmp_path, glob=f"**/*{spec.extensions[0]}")
    sites = result.files[0].extras.get("call_sites") or []
    pairs = [(site.get("name"), site.get("receiver")) for site in sites]
    names = {site.get("name") for site in sites}

    assert resolvable in pairs, f"{language} dropped the resolvable receiver: {sites}"
    assert head in names, f"{language} dropped the head of the chain: {sites}"
    assert chained not in names, (
        f"{language} recorded {chained!r} against a receiver it cannot resolve: {sites}"
    )


# Go is absent: its method receiver is an ordinary named variable, so it has no
# self form to normalize.
_SELF_CALL_FIXTURES: dict[str, tuple[str, str, str]] = {
    "rust": (
        "a.rs",
        "struct S;\n\nimpl S {\n    pub fn run(&self) {\n        self.load();\n    }\n\n"
        "    fn load(&self) {}\n}\n",
        "load",
    ),
    "java": (
        "A.java",
        "class A {\n  void run() {\n    this.load();\n  }\n\n  void load() {}\n}\n",
        "load",
    ),
    "csharp": (
        "A.cs",
        "class A {\n  public void Run() {\n    this.Load();\n  }\n\n  public void Load() {}\n}\n",
        "Load",
    ),
    "php": (
        "A.php",
        "<?php\nclass A {\n  public function run() {\n    $this->load();\n  }\n\n"
        "  public function load() {}\n}\n",
        "load",
    ),
}


@pytest.mark.parametrize("language", sorted(_SELF_CALL_FIXTURES), ids=sorted(_SELF_CALL_FIXTURES))
def test_a_call_on_self_is_recorded_under_the_normalized_receiver(tmp_path, language: str):
    """The intra-class edge is the most common one a class file has, and each
    grammar spells its own receiver differently (`self`, `this`, `$this`). A
    reader that only accepts a plain identifier drops every one of them and
    leaves a file full of method calls looking like it calls nothing."""
    name, source, callee = _SELF_CALL_FIXTURES[language]
    (tmp_path / name).write_text(source, encoding="utf-8")
    spec = next(s for s in specs.ALL if s.name == language)

    result = TreeSitterExtractor(language).parse_repo(tmp_path, glob=f"**/*{spec.extensions[0]}")
    sites = result.files[0].extras.get("call_sites") or []
    assert (callee, "self") in [(site.get("name"), site.get("receiver")) for site in sites], (
        f"{language} did not normalize its self receiver: {sites}"
    )


def test_unknown_language_extensions_fail_loudly(tmp_path):
    """The discovery glob must never silently fall back to another language's
    extensions: it would search for the wrong files, find none, and write a
    clean-looking profile over zero files."""
    from chameleon_mcp.bootstrap.orchestrator import _extensions_for_extractor

    class _Unregistered:
        language = "cobol"

    with pytest.raises(ValueError, match="cobol"):
        _extensions_for_extractor(_Unregistered())

    # And every registered language still resolves.
    for spec in specs.ALL:

        class _Known:
            language = spec.name

        assert _extensions_for_extractor(_Known()) == spec.extensions
