"""The compiled-in taxonomy: loading it, and what it can detect.

Two things are pinned here. First that the shipped data is intact -- a truncated
or reshaped `taxonomy.json` must fail loudly in CI rather than quietly halving
the vocabulary. Second that framework detection follows the architecture spec's
own scoring, including the two walkthroughs the spec works by hand, so a change
to the scorer that breaks them is visible.
"""

from __future__ import annotations

import json

from chameleon_mcp.knowledge import detect, loader

# --- the data itself -------------------------------------------------------- #


def test_taxonomy_loads_every_declared_concept():
    doc = loader.load_taxonomy()
    counts = doc["meta"]["entry_counts"]
    # The file states its own totals; a load that drops entries silently is the
    # failure this catches.
    assert len(doc["concepts"]) == counts["total"] == 541


def test_every_declared_kind_is_populated_and_known():
    by_kind = {kind: len(loader.concepts(kind)) for kind in loader.KINDS}
    declared = loader.load_taxonomy()["meta"]["entry_counts"]["by_kind"]
    assert by_kind == declared, "KINDS drifted from the shipped data"
    # And nothing in the data uses a kind KINDS does not name.
    assert {c["kind"] for c in loader.concepts()} <= set(loader.KINDS)


def test_the_coverage_the_taxonomy_exists_for():
    # The point of shipping this is breadth: 25 languages and 64 frameworks
    # against chameleon's own 3 and 6.
    assert len(loader.concepts("language-profile")) == 25
    assert len(loader.concepts("framework-profile")) == 64
    # Every framework chameleon already classified is still described.
    for known in ("rails", "django", "fastapi", "flask", "nextjs", "nestjs"):
        assert loader.framework_profile(known) is not None, known


def test_lookup_is_case_insensitive_and_alias_aware():
    assert loader.concept("KISS") is not None
    srp = loader.concept("single responsibility principle (srp)")
    assert srp is not None and srp["kind"] == "principle"
    # An alias resolves to the same entry as the term.
    conv = loader.concept("coding convention")
    assert conv is not None and conv["term"] == "convention"


def test_a_term_wins_over_a_conflicting_alias():
    # `Proxy` is a design pattern in its own right. A caller asking for it means
    # that entry, not whatever else happens to list it as an alias.
    hit = loader.concept("Proxy")
    assert hit is not None and hit["term"] == "Proxy"


def test_profiles_resolve_only_to_their_own_kind():
    assert loader.language_profile("python") is not None
    assert loader.language_profile("rails") is None, "a framework is not a language"
    assert loader.framework_profile("rails") is not None
    assert loader.framework_profile("python") is None


def test_frameworks_for_language_spans_the_js_pair():
    ts = {f["term"] for f in loader.frameworks_for_language("typescript")}
    assert {"nextjs", "nestjs", "react"} <= ts
    py = {f["term"] for f in loader.frameworks_for_language("python")}
    assert {"django", "flask", "fastapi"} <= py
    assert not ts & {"rails"}


def test_search_ranks_an_exact_term_above_a_passing_mention():
    hits = loader.search("repository", limit=5)
    assert hits and hits[0]["term"] == "Repository"


def test_search_and_lookup_tolerate_junk():
    assert loader.search("") == ()
    assert loader.search("zzzz-no-such-concept") == ()
    assert loader.concept("") is None
    assert loader.concept(None) is None  # type: ignore[arg-type]


def test_the_kill_switch_empties_the_layer(monkeypatch):
    # Default-on with a kill switch: off must yield an empty vocabulary, not an
    # error, so every caller degrades to its pre-taxonomy behavior.
    loader.load_taxonomy.cache_clear()
    monkeypatch.setenv(loader.DISABLE_ENV, "0")
    try:
        assert loader.load_taxonomy()["concepts"] == []
    finally:
        loader.load_taxonomy.cache_clear()


# --- the glob translator ---------------------------------------------------- #


def test_double_star_spans_zero_directories():
    # The reason this module carries its own translator: `PurePath.match` reads
    # `**` as a single `*`, so it misses the Next.js ROOT page, and
    # `full_match` needs 3.13 while the package supports 3.11.
    rx = detect._glob_regex("app/**/page.tsx")
    assert rx is not None
    assert rx.match("app/page.tsx"), "zero intermediate dirs must match"
    assert rx.match("app/dash/settings/page.tsx")
    assert not rx.match("src/app/page.tsx")


def test_star_stops_at_a_separator_and_braces_alternate():
    star = detect._glob_regex("src/*.py")
    assert star is not None and star.match("src/a.py") and not star.match("src/pkg/a.py")
    braces = detect._glob_regex("{apps,packages}/*/package.json")
    assert braces is not None
    assert braces.match("apps/web/package.json") and braces.match("packages/ui/package.json")
    assert not braces.match("libs/ui/package.json")


# --- hint parsing ----------------------------------------------------------- #


def test_hint_parsing_splits_dependencies_from_config_files():
    react = detect.parse_hint(loader.framework_profile("react")["detection_hint"])
    assert set(react["deps"]) == {"react", "react-dom"}
    django = detect.parse_hint(loader.framework_profile("django")["detection_hint"])
    assert "django" in django["deps"]
    assert "manage.py" in django["files"]
    # A code idiom is neither, and is dropped rather than guessed at.
    assert detect.parse_hint("express dep; app.listen(port)")["files"] == ()


def test_hint_parsing_expands_a_filename_alternation():
    got = detect.parse_hint("something; next.config.(js|ts|mjs)")["files"]
    assert {"next.config.js", "next.config.ts", "next.config.mjs"} <= set(got)


def test_hint_parsing_tolerates_junk():
    assert detect.parse_hint("") == {"deps": (), "files": ()}
    assert detect.parse_hint(None) == {"deps": (), "files": ()}  # type: ignore[arg-type]


# --- detection: the spec's own walkthroughs --------------------------------- #


def _repo(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def test_walkthrough_a_next_and_prisma(tmp_path):
    """Architecture spec 5.2, Walkthrough A: all three reported, nextjs first."""
    root = _repo(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "dependencies": {
                        "next": "15",
                        "react": "19",
                        "react-dom": "19",
                        "@prisma/client": "6",
                    },
                    "devDependencies": {"prisma": "6"},
                }
            ),
            "next.config.ts": "export default {}",
            "app/page.tsx": "export default function P() {}",
            "app/api/users/route.ts": "export function GET() {}",
            "middleware.ts": "export function middleware() {}",
            "prisma/schema.prisma": "generator client {}",
        },
    )
    ranked = detect.score_frameworks(root)
    names = [r["framework"] for r in ranked]
    assert {"nextjs", "react", "prisma"} <= set(names)
    assert all(r["score"] == 1.0 for r in ranked if r["framework"] in {"nextjs", "react", "prisma"})
    # "ordered nextjs > react" -- the more specific framework leads.
    assert names.index("nextjs") < names.index("react")
    assert detect.primary_framework(root) == "nextjs"


def test_walkthrough_b_django_ignores_a_stray_idiom(tmp_path):
    """Spec 5.2, Walkthrough B: a lone flask idiom is correctly not reported."""
    root = _repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\ndependencies = ["django>=4.2"]\n',
            "manage.py": "import django\n",
            "myapp/settings.py": "INSTALLED_APPS = []\n",
            "myapp/models.py": "from django.db import models\n",
            "myapp/migrations/0001_initial.py": "",
            "scripts/one_off.py": "from flask import Flask\n",
        },
    )
    names = [r["framework"] for r in detect.score_frameworks(root)]
    assert names == ["django"]


def test_a_plain_repo_claims_no_framework(tmp_path):
    """The failure mode that matters most: a manifest every JS repo has, and a
    directory name several frameworks share, must not invent a framework."""
    root = _repo(
        tmp_path,
        {
            "package.json": json.dumps({"dependencies": {"lodash": "4"}}),
            "src/index.js": "module.exports = {}\n",
            "app/helper.js": "",
        },
    )
    assert detect.score_frameworks(root) == ()
    assert detect.primary_framework(root) is None


def test_frameworks_beyond_the_six_chameleon_hardcoded(tmp_path):
    """The point of the exercise: a framework nobody wrote an arm for."""
    laravel = _repo(
        tmp_path / "laravel",
        {
            "composer.json": json.dumps({"require": {"laravel/framework": "^11"}}),
            "routes/web.php": "<?php\n",
            "app/Http/Controllers/UserController.php": "<?php\n",
        },
    )
    assert detect.primary_framework(laravel) == "laravel"


def test_a_dependency_family_wildcard_resolves(tmp_path):
    """`spring-boot-starter-*` is how that ecosystem ships. An exact-membership
    test misses every one of them -- which reported a Spring repo as Quarkus,
    since the two share `src/main/java/`."""
    root = _repo(
        tmp_path,
        {
            "pom.xml": (
                "<project><dependencies><dependency>"
                "<artifactId>spring-boot-starter-web</artifactId>"
                "</dependency></dependencies></project>"
            ),
            "src/main/java/App.java": "class App {}\n",
            "src/main/resources/application.yml": "server:\n",
        },
    )
    names = [r["framework"] for r in detect.score_frameworks(root)]
    assert names == ["spring-boot"], "a shared layout must not outrank a named dependency"


def test_one_shared_directory_is_not_a_framework(tmp_path):
    """`app/` alone scores exactly at the spec's 0.60 floor, which would make
    every repo with an `app/` directory a Remix repo."""
    root = _repo(tmp_path, {"app/thing.ts": "export const x = 1\n"})
    assert "remix" not in {r["framework"] for r in detect.score_frameworks(root)}


def test_language_filter_narrows_the_sweep(tmp_path):
    root = _repo(
        tmp_path,
        {
            "Gemfile": "gem 'rails'\n",
            "config/application.rb": "",
            "app/models/user.rb": "",
        },
    )
    assert detect.primary_framework(root, "ruby") == "rails"
    # A Ruby repo scored as Python claims nothing.
    assert detect.score_frameworks(root, "python") == ()


def test_detection_fails_open(tmp_path, monkeypatch):
    assert detect.score_frameworks(tmp_path / "nope") == ()
    monkeypatch.setenv(loader.DISABLE_ENV, "0")
    loader.load_taxonomy.cache_clear()
    try:
        assert detect.score_frameworks(tmp_path) == ()
    finally:
        loader.load_taxonomy.cache_clear()


# --- the pull tool ---------------------------------------------------------- #


def test_explain_concept_returns_one_entry_for_an_exact_term():
    from chameleon_mcp import tools

    got = tools.explain_concept("Repository")["data"]["concept"]
    assert got["term"] == "Repository"
    assert got["kind"] == "design-pattern"
    assert got["detection_hint"]
    # `section` is bookkeeping about which codex chapter an entry came from;
    # it means nothing to a caller.
    assert "section" not in got


def test_explain_concept_resolves_an_alias():
    from chameleon_mcp import tools

    assert tools.explain_concept("coding convention")["data"]["concept"]["term"] == "convention"


def test_explain_concept_falls_back_to_search():
    from chameleon_mcp import tools

    matches = tools.explain_concept("hexagonal")["data"]["matches"]
    assert any("Hexagonal" in m["term"] for m in matches)


def test_explain_concept_lists_a_kind_and_reports_truncation():
    from chameleon_mcp import tools

    out = tools.explain_concept(kind="code-smell", limit=3)
    assert out["data"]["kind"] == "code-smell"
    assert out["data"]["total"] == 23
    assert len(out["data"]["concepts"]) == 3
    assert out["truncated"] is True


def test_explain_concept_with_no_arguments_describes_the_catalogue():
    from chameleon_mcp import tools

    data = tools.explain_concept()["data"]
    assert data["total"] == 541
    assert data["kinds"]["framework-profile"] == 64


def test_explain_concept_rejects_junk_with_a_failed_envelope():
    from chameleon_mcp import tools

    for bad in (tools.explain_concept("zzzz-nope"), tools.explain_concept(kind="not-a-kind")):
        assert bad["data"]["status"] == "failed"
        assert bad["data"]["error"]
    assert tools.explain_concept(term=123)["data"]["status"] == "failed"  # type: ignore[arg-type]


def test_explain_concept_returns_the_standard_envelope():
    from chameleon_mcp import tools

    out = tools.explain_concept("KISS")
    assert out["api_version"] == "1"
    assert set(out) <= {"api_version", "data", "truncated", "next_cursor"}


# --- the classifier fallback ------------------------------------------------ #


def test_classifier_still_prefers_its_hardcoded_arms(tmp_path):
    """The six arms encode corroboration the profiles do not -- a `manage.py`
    whose CONTENT is a real Django entrypoint outranks an incidental dep. The
    taxonomy answers only where they return None."""
    from chameleon_mcp.bootstrap.orchestrator import _classify_framework

    (tmp_path / "manage.py").write_text("import django\n")
    (tmp_path / "requirements.txt").write_text("django\nfastapi\n")
    assert _classify_framework(tmp_path, "python") == "django"


def test_classifier_now_answers_for_a_language_it_never_covered(tmp_path):
    from chameleon_mcp.bootstrap.orchestrator import _classify_framework

    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^11"}}))
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "web.php").write_text("<?php\n")
    (tmp_path / "app" / "Http" / "Controllers").mkdir(parents=True)
    (tmp_path / "app" / "Http" / "Controllers" / "A.php").write_text("<?php\n")
    assert _classify_framework(tmp_path, "php") == "laravel"


def test_prose_in_a_manifest_is_never_a_dependency():
    """Framework names are ordinary words -- flask, express, next, solid, astro,
    hono -- so a scrape of package-shaped tokens turns any description, comment,
    or README excerpt that spells one into a dependency claim. Each manifest is
    read through its own declaration surface instead; these are the exact traps
    the scrape fell into, one per format.
    """
    traps = {
        "pyproject.toml": (
            '[project]\ndescription = "a lightweight alternative to flask"\n'
            'dependencies = ["click>=8"]\n',
            {"click"},
        ),
        "Cargo.toml": (
            '[package]\ndescription = "a fast alternative to axum"\n\n'
            '[dependencies]\nactix-web = "4"\n',
            {"actix-web"},
        ),
        "composer.json": (
            '{"description": "like laravel but small", "require": {"symfony/console": "^6"}}',
            {"symfony/console"},
        ),
        "build.gradle": (
            'dependencies {\n  implementation "org.springframework.boot:spring-boot-starter-web:3.2"\n}\n'
            "// mentions ktor in a comment\n",
            {"org.springframework.boot", "spring-boot-starter-web"},
        ),
        "requirements.txt": (
            '# fastapi is not used here\nflask-login>=0.6 ; python_version < "3.12"\n-r base.txt\n',
            {"flask-login"},
        ),
        "pom.xml": (
            "<project><description>not spring</description><dependencies><dependency>"
            "<groupId>org.example</groupId><artifactId>widget</artifactId>"
            "</dependency></dependencies></project>",
            {"org.example", "widget"},
        ),
        "go.mod": (
            "module example.com/x\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n",
            {"github.com/gin-gonic/gin"},
        ),
    }
    for manifest, (text, expected) in traps.items():
        assert detect._declared_deps(manifest, text) == expected, manifest


def test_a_removed_or_excluded_dependency_is_not_a_declaration():
    """The build files carry their own negations, and each means the OPPOSITE of
    a declaration: a commented-out coordinate records a dependency that was
    dropped, `<exclusions>` names what must not be pulled in, go's `exclude` and
    `retract` blocks the same, and `// indirect` marks a transitive. Reading any
    of them as a dependency classifies a repo as a framework it removed.
    """
    # A pom that DROPPED Spring must not classify as Spring.
    pom = (
        "<project><dependencies>"
        "<!-- dropped in v3: <groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId> -->"
        "<dependency><groupId>junit</groupId><artifactId>junit</artifactId></dependency>"
        "</dependencies></project>"
    )
    assert detect._declared_deps("pom.xml", pom) == {"junit"}

    excluded = (
        "<dependency><artifactId>real</artifactId><exclusions><exclusion>"
        "<artifactId>spring-boot-starter-web</artifactId>"
        "</exclusion></exclusions></dependency>"
    )
    assert detect._declared_deps("pom.xml", excluded) == {"real"}

    # A commented-out Gradle coordinate is the commonest way a removed
    # dependency lingers, in both comment forms.
    gradle = (
        "dependencies {\n"
        "  // implementation 'org.springframework.boot:spring-boot-starter-web:3.2'\n"
        "  /* implementation 'io.ktor:ktor-server-core:2.3' */\n"
        "  implementation 'com.google.guava:guava:33.0-jre'\n"
        "}\n"
    )
    assert detect._declared_deps("build.gradle", gradle) == {"com.google.guava", "guava"}

    # But a URL scheme's own `//` is not a comment: stripping it would take any
    # coordinate sharing the line with a `maven { url ... }` along with it.
    same_line = 'maven { url "https://x.io/m2" }; implementation "io.ktor:ktor-core:2"\n'
    assert detect._declared_deps("build.gradle", same_line) == {"io.ktor", "ktor-core"}

    # go.mod: an excluded module, and a transitive one, are not dependencies.
    # The block bodies are INDENTED, so the directive keyword never appears on
    # the line carrying the module path -- a per-line prefix check misses it.
    gomod = (
        "module example.com/x\n\n"
        "require (\n"
        "\tgithub.com/labstack/echo/v4 v4.11.0\n"
        "\tgithub.com/spf13/cobra v1.8.0 // indirect\n"
        ")\n\n"
        "exclude (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n"
    )
    assert detect._declared_deps("go.mod", gomod) == {"github.com/labstack/echo/v4"}


def test_a_truncated_read_loses_names_but_never_invents_them():
    """The read cap makes truncation a correctness question, not just a bound.

    A cut between `<!--` and its `-->` leaves the comment body live, so a
    coordinate the team REMOVED comes back as a declaration; a cut mid-token
    leaves a partial requirement, and end-of-string is a legal name terminator,
    so `django-environ` reads as `django`. Both fabricate a name the manifest
    never declares -- the exact class the parsers exist to prevent.
    """
    cap = detect._MANIFEST_MAX_CHARS

    head, pad = (
        "<project><dependencies>",
        "<dependency><artifactId>filler</artifactId></dependency>",
    )
    dropped = (
        "<!-- dropped in v3: <groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId> -->"
    )
    pom = head + pad * ((cap - len(head) - 40) // len(pad)) + dropped
    cut = pom[:cap]
    assert cut.rfind("<!--") > cut.rfind("-->"), "fixture must cut inside the comment"
    assert detect._declared_deps("pom.xml", detect._trim_truncated("pom.xml", cut)) == {"filler"}

    req = "flask==3\n" + ("# pad\n" * 3000) + "django-environ==0.11\n"
    mid_token = req[: req.index("django-environ") + 6]
    trimmed = detect._trim_truncated("requirements.txt", mid_token)
    assert detect._declared_deps("requirements.txt", trimmed) == {"flask"}

    # And the repair must not become the opposite error. `<!--` inside a TOML
    # string is not a comment opener, so trimming from it would drop every real
    # dependency after it -- quieter than a false claim, and no less wrong.
    toml_with_marker = 'name = "a <!-- b"\nflask==3\n'
    assert detect._trim_truncated("pyproject.toml", toml_with_marker) == toml_with_marker


def test_the_read_cap_and_its_repair_are_wired(tmp_path):
    """Drives a real over-cap manifest from DISK through `_manifest_dep_names`.

    The other truncation tests call `_trim_truncated` on hand-built strings, so
    they would all still pass if the trim call were deleted or the cap
    comparison flipped. This one fails if the wiring is wrong rather than the
    function.
    """
    cap = detect._MANIFEST_MAX_CHARS
    pad = "<dependency><artifactId>filler</artifactId></dependency>\n"
    dropped = (
        "<!-- dropped in v3: <groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId>"
    )
    body = "<project><dependencies>\n" + pad * ((cap // len(pad)) + 5) + dropped
    (tmp_path / "pom.xml").write_text(body, encoding="utf-8")
    assert len(body) > cap, "fixture must exceed the cap for the repair to run"

    found = detect._manifest_dep_names(tmp_path, 40)
    assert "filler" in found, "the readable head must still be mined"
    assert "org.springframework.boot" not in found
    assert "spring-boot-starter-web" not in found


def test_an_earlier_unterminated_comment_is_also_trimmed():
    """Cutting at the LAST unterminated opener can expose an EARLIER one that is
    also unterminated; stopping there leaves its body live, which is the same
    fabrication one nesting level up."""
    nested = (
        "<project><dependencies>\n<!-- disabled block:\n"
        "<dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency>\n"
        "<!-- TODO restore before v4\n"
    )
    trimmed = detect._trim_truncated("pom.xml", nested)
    assert trimmed.count("<!--") <= trimmed.count("-->")
    assert detect._declared_deps("pom.xml", trimmed) == set()
    # A properly closed comment is still left alone.
    ok = "<project>\n<!-- note -->\n<dependency><artifactId>guava</artifactId></dependency>\n"
    assert detect._declared_deps("pom.xml", detect._trim_truncated("pom.xml", ok)) == {"guava"}


def test_go_block_directives_survive_a_trailing_comment():
    """`exclude ( // dropped: CVE-...` is legal go.mod. Reading the raw line end
    misses the paren, so the block never opens and the EXCLUDED module inside it
    scores as a declared dependency."""
    gomod = (
        "require (\n\tgithub.com/labstack/echo/v4 v4.11.0\n)\n\n"
        "exclude ( // dropped: CVE-2023-29401\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n"
    )
    assert detect._declared_deps("go.mod", gomod) == {"github.com/labstack/echo/v4"}


def test_a_self_closing_exclusions_tag_does_not_swallow_the_next_dependency():
    """Left to the paired pattern, `<exclusions/>` opens a match that only ends
    at the NEXT block's closing tag -- turning a strip-the-negation fix into a
    drop-the-declaration bug."""
    pom = (
        "<dependencies>"
        "<dependency><artifactId>guava</artifactId><exclusions/></dependency>"
        "<dependency><artifactId>spring-boot-starter-web</artifactId>"
        "<exclusions><exclusion><artifactId>logback</artifactId></exclusion></exclusions>"
        "</dependency></dependencies>"
    )
    assert detect._declared_deps("pom.xml", pom) == {"guava", "spring-boot-starter-web"}

    # An <exclusions> left unclosed (malformed, or cut by the cap) must not spill
    # its excluded coordinates into the result.
    unclosed = (
        "<dependency><artifactId>keep</artifactId>"
        "<exclusions><exclusion><artifactId>drop</artifactId>"
    )
    assert detect._declared_deps("pom.xml", unclosed) == {"keep"}


def test_the_poetry_interpreter_constraint_is_not_a_package():
    """`python` is poetry's interpreter pin. orchestrator._python_dep_names
    excludes it, and these two readers answer the same question about the same
    file."""
    poetry = '[tool.poetry.dependencies]\npython = "^3.11"\nflask = "^3"\n'
    assert detect._declared_deps("pyproject.toml", poetry) == {"flask"}


def test_packages_means_dependencies_only_in_a_pipfile():
    """`packages` is the most overloaded key in pyproject.toml: under
    `[tool.setuptools]` and `[tool.mypy]` it names the project's OWN modules.
    Matching it at any depth reads a vendored `flask/` directory as a Flask
    dependency -- a false claim arriving through a structural read rather than a
    scrape, which is subtler but no less wrong."""
    own_modules = (
        '[project]\nname = "myapp"\ndependencies = ["click>=8"]\n\n'
        '[tool.setuptools]\npackages = ["flask", "myapp"]\n'
    )
    assert detect._declared_deps("pyproject.toml", own_modules) == {"click"}
    # But a Pipfile's own top-level table still means dependencies.
    assert detect._declared_deps("Pipfile", '[packages]\ndjango = "*"\n') == {"django"}


def test_every_conventional_dependency_table_is_read():
    """Reading only the well-known PATHS misses the next tool. These are the
    shapes a paths-based reader dropped -- PEP 735, pdm, hatch environments, and
    Cargo's workspace and target-specific tables -- each of which can be the ONLY
    place a framework is declared."""
    shapes = {
        '[dependency-groups]\ndev = ["pytest>=8"]\n': {"pytest"},
        '[tool.pdm.dev-dependencies]\nlint = ["ruff"]\n': {"ruff"},
        '[tool.hatch.envs.default]\ndependencies = ["pytest"]\n': {"pytest"},
        '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n': {"pytest"},
        '[project]\ndependencies = ["flask>=3"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n': {"flask", "pytest"},
    }
    for text, expected in shapes.items():
        assert detect._declared_deps("pyproject.toml", text) == expected, text

    cargo = {
        "[target.'cfg(unix)'.dependencies]\nnix = '0.27'\n": {"nix"},
        '[workspace.dependencies]\naxum = "0.7"\n': {"axum"},
        # The detailed form's VALUES are constraints, never names.
        '[dependencies]\nserde = { version = "1", features = ["derive"] }\n': {"serde"},
    }
    for text, expected in cargo.items():
        assert detect._declared_deps("Cargo.toml", text) == expected, text


def test_a_dependency_name_must_end_where_a_name_legally_can():
    """A prefix match would read any token that merely STARTS with a framework's
    name as that framework, which is the same false claim one layer down: a
    package spelled `flaske-login` with a non-ASCII e truncates to `flask` at the
    first character the pattern cannot consume."""
    assert detect._dep_name("flask-login>=0.6") == "flask-login"
    assert detect._dep_name("requests[socks]==2.0") == "requests"
    assert detect._dep_name("django>=4.2,<5") == "django"
    # Truncation at an illegal character yields nothing, not the prefix.
    assert detect._dep_name("flaské-login") == ""
    # And no single token may be unbounded.
    assert detect._dep_name("x" * 300) == ""
    assert detect._declared_deps("requirements.txt", "flaské-login\nflask-login>=1\n") == {
        "flask-login"
    }


def test_a_gem_clause_is_a_dependency_signal(tmp_path):
    """Ruby profiles spell the manifest word as a noun ("sinatra gem"), which the
    marker table missed -- so both Ruby profiles beyond Rails contributed no
    dependency signal at all and could never be detected."""
    assert detect.parse_hint("sinatra gem; get/post blocks")["deps"] == ("sinatra",)
    assert detect.parse_hint("rspec-rails gem; .rspec")["deps"] == ("rspec-rails",)
    # "Gemfile with rails" is a FILE clause, not a dependency one; the marker
    # needs a following space, so the word inside "Gemfile" cannot trigger it.
    assert detect.parse_hint("Gemfile with rails; bin/rails")["deps"] == ()


def test_every_framework_gets_an_anti_hallucination_line():
    """Six hand-written strings meant the other 58 got no line at all, which is
    the same silence as not detecting them."""
    from chameleon_mcp.principles import _FRAMEWORK_PROTOCOL, _framework_protocol_line

    for profile in loader.concepts("framework-profile"):
        line = _framework_protocol_line(profile["term"])
        assert line, f"{profile['term']} has no protocol line"
        assert line.startswith("Don't invent ")
    # The curated six are used verbatim, never regenerated.
    for fw, curated in _FRAMEWORK_PROTOCOL.items():
        assert _framework_protocol_line(fw) == curated
    assert _framework_protocol_line(None) is None
    assert _framework_protocol_line("not-a-framework") is None
