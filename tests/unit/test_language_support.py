"""The language-capability registry: what each language is declared to get.

The registry exists to stop one failure: a language wired into the extractor but
not into the lint engine derives archetypes and conventions perfectly well while
every rule silently returns nothing, and that silence reads as "your code is
clean". These tests pin the declaration against the wiring, so a language cannot
be added to one and forgotten in the other.
"""

from __future__ import annotations

import json

from chameleon_mcp import language_support as ls


def test_the_three_first_class_languages_are_declared_first_class():
    for language in ("typescript", "ruby", "python"):
        caps = ls.capabilities_for(language)
        assert caps is not None
        assert caps.tier == ls.FIRST_CLASS
        assert caps.lint_rules and caps.security_lint and caps.cross_file_index
        assert caps.missing() == (), f"{language} should lack nothing"


def test_every_spec_driven_language_is_declared_extraction_tier():
    """A spec-driven language must appear here, or it is silently
    half-supported -- the exact state this module exists to make visible."""
    from chameleon_mcp.extractors.treesitter.lang.specs import ALL

    for spec in ALL:
        caps = ls.capabilities_for(spec.name)
        assert caps is not None, f"{spec.name} has a spec but no declared capabilities"
        assert caps.tier == ls.EXTRACTION
        # What it DOES get is what a profile is built from.
        assert caps.archetypes and caps.conventions and caps.signatures and caps.imports
        # What it does NOT get must be named, not implied.
        assert caps.missing(), f"{spec.name} claims to lack nothing but is extraction tier"


def test_the_declaration_matches_the_actual_lint_wiring():
    """The registry must agree with lint_engine, not merely assert.

    `detect_language` returning None is what makes every rule silent for a
    language; a language declared to have lint rules while the engine refuses to
    classify its files would be a lie the registry exists to prevent.
    """
    from chameleon_mcp.lint_engine import detect_language

    probe = {
        "typescript": "a.ts",
        "ruby": "a.rb",
        "python": "a.py",
        "go": "a.go",
        "rust": "a.rs",
        "java": "a.java",
        "csharp": "a.cs",
        "php": "a.php",
    }
    for language, filename in probe.items():
        caps = ls.capabilities_for(language)
        assert caps is not None, language
        classified = detect_language(filename) is not None
        assert caps.lint_rules == classified, (
            f"{language}: registry says lint_rules={caps.lint_rules} but "
            f"detect_language({filename!r}) classified={classified}"
        )


def test_the_declaration_matches_the_cross_file_index_wiring():
    from chameleon_mcp.symbol_index import REVERSE_INDEXED_LANGUAGES

    for language in ls.supported_languages():
        caps = ls.capabilities_for(language)
        assert caps is not None
        if caps.tier == ls.EXTRACTION:
            assert language not in REVERSE_INDEXED_LANGUAGES, (
                f"{language} is declared extraction tier but builds a reverse index"
            )


def test_lint_file_states_when_nothing_was_checked(tmp_path, monkeypatch):
    """An empty `violations` list means two opposite things depending on the
    language, and the payload has to say which.

    For a first-class language it means the file was checked and is clean. For
    an extraction-tier one it means no rule exists to run -- and a caller
    reading `violations: []` as "clean" is exactly the vacuous silence the tier
    registry exists to prevent, now at the tool surface.

    The fixture is polyglot because an extraction-tier language is never a
    repo's PRIMARY: bootstrap refuses a tree with no TypeScript, Ruby or Python
    signal, so a Go-only fixture cannot produce a profile at all. TypeScript
    primary with a Go service beside it is the shape cross-language indexing
    actually derives, and the shape where reading `violations: []` as "clean" is
    a live hazard.
    """
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))

    repo = tmp_path / "svc"
    (repo / "web" / "src").mkdir(parents=True)
    (repo / "internal" / "service").mkdir(parents=True)
    (repo / "package.json").write_text(
        json.dumps({"name": "svc", "devDependencies": {"typescript": "^5.0.0"}}),
        encoding="utf-8",
    )
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")
    for name in ("alpha", "beta", "gamma", "delta"):
        (repo / "web" / "src" / f"{name}.ts").write_text(
            f"export class {name.title()}Client {{\n"
            f"  find(id: string): string {{\n    return id;\n  }}\n}}\n",
            encoding="utf-8",
        )
        (repo / "internal" / "service" / f"{name}.go").write_text(
            f"package service\n\ntype {name}Service struct {{}}\n\n"
            f"func (s *{name}Service) Find(id string) string {{\n\treturn id\n}}\n",
            encoding="utf-8",
        )

    report = tools.bootstrap_repo(str(repo)).get("data", {})
    assert report.get("status") == "success", report.get("error")

    detected = tools.detect_repo(str(repo / "go.mod")).get("data", {})
    tools.trust_profile(detected["repo_id"], repo.name)
    archetypes = list(
        tools.describe_codebase(detected["repo_id"]).get("data", {}).get("archetypes") or []
    )
    go_archetypes = [a for a in archetypes if str(a.get("witness", "")).endswith(".go")]
    assert go_archetypes, f"the Go corpus derived no archetype: {archetypes}"

    result = tools.lint_file(
        detected["repo_id"],
        go_archetypes[0]["name"],
        "package service\n\nfunc X() {}\n",
        str(repo / "internal" / "service" / "x.go"),
    ).get("data", {})
    assert result.get("lint_coverage") == "extraction-tier"
    assert "no per-edit lint rules" in (result.get("lint_coverage_note") or "")

    # The control that makes the marker mean something: a first-class file in
    # the SAME repo carries no tier note, so an empty `violations` there really
    # does say "checked and clean".
    checked = tools.lint_file(
        detected["repo_id"],
        archetypes[0]["name"],
        "export const x = 1;\n",
        str(repo / "web" / "src" / "x.ts"),
    ).get("data", {})
    assert "lint_coverage" not in checked


def test_an_unsupported_language_says_so_rather_than_guessing():
    assert ls.tier_for("zig") == ls.UNSUPPORTED
    assert ls.tier_for(None) == ls.UNSUPPORTED
    assert ls.tier_for("") == ls.UNSUPPORTED
    assert ls.capabilities_for("zig") is None
    assert "not supported" in ls.describe("zig")


def test_describe_names_what_is_missing_for_an_extraction_language():
    """A user wondering why a rule never fired must be able to read the answer,
    not infer it from silence."""
    text = ls.describe("go")
    assert "extraction tier" in text
    assert "lint rules" in text
    assert "reverse/exports index" in text
    # Secret and eval detection are NOT missing at this tier: they read content
    # rather than a derived shape, so they need no extractor. Naming them here
    # would send someone hunting for a gap that was closed.
    assert "secret" not in text


def test_first_class_languages_are_listed_before_extraction_ones():
    listed = ls.supported_languages()
    tiers = [ls.tier_for(language) for language in listed]
    assert tiers == sorted(tiers, key=lambda t: 0 if t == ls.FIRST_CLASS else 1)
    assert set(listed) >= {"typescript", "ruby", "python", "go", "rust", "java", "csharp", "php"}
