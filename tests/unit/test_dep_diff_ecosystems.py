"""Unit tests for the supply-chain diff parser's non-npm/Bundler arms.

The four Step 2.5 checks were npm- and Bundler-only; Python, Go, Rust, PHP and
Java manifests were routed to `uncovered_manifests` instead. These cover the
arms that close that gap:
  2.5a new direct dependency (NIT)
  2.5c new install/build hook, where the ecosystem has one (FIX)
  2.5d non-registry dependency source (FIX)

Every check is a PURE PARSE of unified-diff text: no network, no subprocess.
The negative tests carry as much weight as the positive ones -- a version bump,
a commented-out line, a `conflict`/`<exclusions>` block and a `[[bin]]` target
path all LOOK like the thing they are not, and reporting one is the false
dependency claim the shared readers exist to prevent.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.dep_diff import (
    MANIFEST_LOCKFILE_BASENAMES,
    UNCOVERED_MANIFEST_BASENAMES,
    collect_dependency_findings,
    is_uncovered_manifest,
    render_findings,
    scan_dependency_diff,
)


def _scan(path, diff):
    return scan_dependency_diff({path: diff})


def _checks(findings, check):
    return [f for f in findings if f.check == check]


def _names(findings):
    return sorted(f.detail.get("name", "") for f in findings if f.check == "new-dependency")


def _sources(findings):
    return sorted(f.detail.get("kind", "") for f in findings if f.check == "non-registry-source")


def _hooks(findings):
    return sorted(f.detail.get("script", "") for f in findings if f.check == "install-script")


# ---------------------------------------------------------------------------
# Coverage boundary — what moved from "not covered" to parsed, and what did not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements/base.txt",
        "backend/requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.cfg",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ],
)
def test_newly_parsed_manifests_are_not_reported_as_uncovered(path):
    # A parsed manifest must not ALSO be listed as hand-review-only, or the
    # consumer double-counts one change as both reviewed and not reviewed.
    assert is_uncovered_manifest(path) is False


@pytest.mark.parametrize(
    "path",
    ["setup.py", "poetry.lock", "Pipfile.lock", "go.sum", "Cargo.lock", "composer.lock"],
)
def test_setup_py_and_foreign_lockfiles_remain_uncovered(path):
    # setup.py is arbitrary Python and the lockfiles carry no resolved-host
    # keyword, so both stay an honest "not covered" rather than a silent clean.
    assert is_uncovered_manifest(path) is True


@pytest.mark.parametrize("path", ["src/app.py", "README.txt", "notes.txt", "config.yml"])
def test_ordinary_files_are_still_not_manifests(path):
    assert is_uncovered_manifest(path) is False


def test_manifest_basenames_include_every_new_ecosystem():
    assert {
        "pyproject.toml",
        "Pipfile",
        "setup.cfg",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    } <= set(MANIFEST_LOCKFILE_BASENAMES)


def test_covered_and_uncovered_sets_do_not_overlap():
    assert not (MANIFEST_LOCKFILE_BASENAMES & UNCOVERED_MANIFEST_BASENAMES)


# ---------------------------------------------------------------------------
# Python — requirements*.txt, pyproject.toml, Pipfile, setup.cfg
# ---------------------------------------------------------------------------


def test_requirements_new_dependency_and_version_bump():
    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " flask>=3.0\n"
        "-requests==2.31.0\n"
        "+requests==2.32.0\n"
        "+pyyaml==6.0.1\n"
    )
    findings = _scan("requirements.txt", diff)
    # The bump is not new; only pyyaml is. `flask` is context, never reported.
    assert _names(findings) == ["pyyaml"]
    assert _checks(findings, "new-dependency")[0].severity == "NIT"
    assert "pyyaml==6.0.1" in _checks(findings, "new-dependency")[0].evidence


def test_requirements_pep508_direct_reference_is_non_registry():
    diff = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " flask>=3.0\n"
        "+telemetry @ git+https://acme.example/telemetry.git@main\n"
    )
    findings = _scan("requirements.txt", diff)
    hits = _checks(findings, "non-registry-source")
    assert [h.severity for h in hits] == ["FIX"]
    assert hits[0].detail["kind"] == "vcs-url"
    assert "acme.example" in hits[0].detail["source"]
    # It is a new dependency too -- the two checks are independent.
    assert _names(findings) == ["telemetry"]


def test_requirements_alternate_index_and_editable_install():
    diff = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,1 +1,3 @@\n"
        " flask\n"
        "+--extra-index-url https://pypi.internal.example/simple\n"
        "+-e git+https://github.com/acme/lib.git#egg=lib\n"
    )
    assert _sources(_scan("requirements.txt", diff)) == ["alternate-index", "vcs-url"]


def test_requirements_comment_lines_declare_nothing():
    # A commented requirement is the record of one REMOVED; reporting it inverts
    # what the file says.
    diff = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,1 +1,3 @@\n"
        " flask\n"
        "+# evil-pkg @ git+https://evil.example/x.git\n"
        "+# --index-url https://evil.example/simple\n"
    )
    assert _scan("requirements.txt", diff) == []


def test_pyproject_pep621_hunk_without_the_array_opener_in_context():
    # The realistic shape: a long `dependencies = [` array, so neither the opener
    # nor the closer lands in the hunk's context. Only git's @@ trailer names the
    # construct, and without the fragment repair the reader sees nothing.
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -14,6 +14,7 @@ dependencies = [\n"
        '     "sqlalchemy>=2.0",\n'
        '     "alembic",\n'
        '+    "evil-pkg==0.0.1",\n'
        '     "uvicorn",\n'
        '     "httpx",\n'
    )
    assert _names(_scan("pyproject.toml", diff)) == ["evil-pkg"]


def test_pyproject_tool_config_change_declares_no_dependency():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -30,3 +30,3 @@ line-length = 88\n"
        " [tool.ruff]\n"
        "-line-length = 88\n"
        "+line-length = 100\n"
    )
    assert _scan("pyproject.toml", diff) == []


def test_pyproject_poetry_table_and_git_source():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -12,3 +12,4 @@\n"
        " [tool.poetry.dependencies]\n"
        ' python = "^3.11"\n'
        '+internal-sdk = { git = "https://git.internal.example/sdk.git" }\n'
    )
    findings = _scan("pyproject.toml", diff)
    # `python` is poetry's interpreter constraint, not a package -- the shared
    # reader excludes it, and this arm must not reintroduce it.
    assert _names(findings) == ["internal-sdk"]
    assert _sources(findings) == ["vcs-url"]


def test_dependency_moved_between_poetry_groups_is_not_new():
    # The one case the two-sided parse alone gets wrong: the hunk it LEAVES has
    # no table header in its context (so that side reads as nothing) while the
    # hunk it ARRIVES in does. Without the removed-line check the move would read
    # as a brand-new dependency.
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -14,3 +14,2 @@\n"
        '     alembic = "^1.13"\n'
        '-    requests = "^2.31"\n'
        '     httpx = "^0.27"\n'
        "@@ -30,2 +30,3 @@\n"
        " [tool.poetry.group.dev.dependencies]\n"
        ' pytest = "^8.0"\n'
        '+requests = "^2.31"\n'
    )
    assert _names(_scan("pyproject.toml", diff)) == []


def test_pyproject_alternate_source_table_is_flagged():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -40,1 +40,3 @@\n"
        " [tool.poetry]\n"
        "+[[tool.poetry.source]]\n"
        '+url = "https://pypi.internal.example/simple"\n'
    )
    assert _sources(_scan("pyproject.toml", diff)) == ["alternate-index"]


def test_pyproject_build_backend_change_is_an_install_hook():
    # A build backend runs at install time, which is the Python analogue of an
    # npm postinstall: swapping it swaps what executes on `pip install`.
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1,3 +1,3 @@\n"
        " [build-system]\n"
        '-build-backend = "setuptools.build_meta"\n'
        '+build-backend = "acme_backend"\n'
    )
    hits = _checks(_scan("pyproject.toml", diff), "install-script")
    assert [h.severity for h in hits] == ["FIX"]
    assert hits[0].detail["script"] == "acme_backend"


def test_pipfile_new_package_and_path_source():
    diff = (
        "--- a/Pipfile\n"
        "+++ b/Pipfile\n"
        "@@ -1,3 +1,5 @@\n"
        " [packages]\n"
        ' flask = "*"\n'
        '+vendored = { path = "./vendor/lib" }\n'
    )
    findings = _scan("Pipfile", diff)
    assert _names(findings) == ["vendored"]
    assert _sources(findings) == ["local-path"]


def test_setup_cfg_install_requires_continuation_lines():
    diff = (
        "--- a/setup.cfg\n"
        "+++ b/setup.cfg\n"
        "@@ -3,4 +3,5 @@\n"
        " [options]\n"
        " install_requires =\n"
        "     flask>=3.0\n"
        "+    boto3\n"
    )
    assert _names(_scan("setup.cfg", diff)) == ["boto3"]


def test_setup_cfg_non_requirement_key_block_is_not_dependencies():
    # An entry-points block is indented exactly like an install_requires block
    # and its entries are name-shaped (`mytool = pkg.cli:main`), so only the KEY
    # tells them apart -- console scripts are things this package PROVIDES.
    diff = (
        "--- a/setup.cfg\n"
        "+++ b/setup.cfg\n"
        "@@ -8,3 +8,4 @@\n"
        " [options.entry_points]\n"
        " console_scripts =\n"
        "     mytool = mypkg.cli:main\n"
        "+    othertool = mypkg.other:main\n"
    )
    assert _scan("setup.cfg", diff) == []


# ---------------------------------------------------------------------------
# Go — go.mod
# ---------------------------------------------------------------------------


def test_go_mod_new_require_is_a_new_dependency():
    diff = (
        "--- a/go.mod\n"
        "+++ b/go.mod\n"
        "@@ -3,3 +3,4 @@ require (\n"
        " \tgithub.com/gin-gonic/gin v1.9.1\n"
        "+\tgithub.com/acme/telemetry v1.0.0\n"
        " )\n"
    )
    assert _names(_scan("go.mod", diff)) == ["github.com/acme/telemetry"]


def test_go_mod_indirect_requirement_is_not_a_direct_dependency():
    diff = (
        "--- a/go.mod\n"
        "+++ b/go.mod\n"
        "@@ -3,2 +3,3 @@ require (\n"
        " \tgithub.com/a/b v1.0.0\n"
        "+\tgithub.com/transitive/x v0.1.0 // indirect\n"
    )
    assert _scan("go.mod", diff) == []


def test_go_mod_replace_directive_names_the_replacement_target():
    # The redirect target is the code that actually gets built, so it -- not the
    # module being replaced -- is what the finding must name.
    diff = (
        "--- a/go.mod\n"
        "+++ b/go.mod\n"
        "@@ -8,1 +8,2 @@\n"
        " )\n"
        "+replace github.com/gin-gonic/gin => github.com/attacker/gin v1.9.1\n"
    )
    hits = _checks(_scan("go.mod", diff), "non-registry-source")
    assert [h.severity for h in hits] == ["FIX"]
    assert hits[0].detail["kind"] == "module-replacement"
    assert hits[0].detail["source"] == "github.com/attacker/gin"


def test_go_mod_commented_replace_declares_nothing():
    diff = (
        "--- a/go.mod\n"
        "+++ b/go.mod\n"
        "@@ -8,1 +8,2 @@\n"
        " )\n"
        "+// replace github.com/a/b => ../local/b\n"
    )
    assert _scan("go.mod", diff) == []


def test_go_mod_replace_block_body_is_flagged_without_the_keyword():
    diff = (
        "--- a/go.mod\n"
        "+++ b/go.mod\n"
        "@@ -8,1 +8,4 @@\n"
        " )\n"
        "+replace (\n"
        "+\tgithub.com/a/b v1.0.0 => ../local/b\n"
        "+)\n"
    )
    hits = _checks(_scan("go.mod", diff), "non-registry-source")
    # The block opener names no module, so only the arrow line is reported.
    assert [h.detail["source"] for h in hits] == ["../local/b"]


# ---------------------------------------------------------------------------
# Rust — Cargo.toml
# ---------------------------------------------------------------------------


def test_cargo_new_crate_and_git_source():
    diff = (
        "--- a/Cargo.toml\n"
        "+++ b/Cargo.toml\n"
        "@@ -5,2 +5,3 @@\n"
        " [dependencies]\n"
        ' serde = "1.0"\n'
        '+internal = { git = "https://git.internal.example/crate.git" }\n'
    )
    findings = _scan("Cargo.toml", diff)
    assert _names(findings) == ["internal"]
    assert _sources(findings) == ["vcs-url"]


def test_cargo_build_script_is_an_install_hook():
    diff = (
        "--- a/Cargo.toml\n"
        "+++ b/Cargo.toml\n"
        "@@ -1,3 +1,4 @@\n"
        " [package]\n"
        ' name = "app"\n'
        '+build = "build.rs"\n'
    )
    assert _hooks(_scan("Cargo.toml", diff)) == ["build.rs"]


def test_cargo_binary_target_path_is_not_a_local_dependency():
    # `[[bin]] path = "src/bin/tool.rs"` is a compilation target, not a path
    # dependency; flagging it would be a FIX-severity false positive on an
    # ordinary refactor.
    diff = (
        "--- a/Cargo.toml\n"
        "+++ b/Cargo.toml\n"
        "@@ -10,1 +10,3 @@\n"
        " [[bin]]\n"
        '+name = "tool"\n'
        '+path = "src/bin/tool.rs"\n'
    )
    assert _checks(_scan("Cargo.toml", diff), "non-registry-source") == []


def test_cargo_patch_table_is_flagged():
    diff = (
        "--- a/Cargo.toml\n"
        "+++ b/Cargo.toml\n"
        "@@ -20,1 +20,3 @@\n"
        " [dev-dependencies]\n"
        "+[patch.crates-io]\n"
        '+serde = { git = "https://github.com/fork/serde" }\n'
    )
    assert "patched-source" in _sources(_scan("Cargo.toml", diff))


# ---------------------------------------------------------------------------
# PHP — composer.json
# ---------------------------------------------------------------------------


def test_composer_new_require_entry():
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -3,4 +3,5 @@\n"
        '     "require": {\n'
        '         "monolog/monolog": "^2.0",\n'
        '+        "acme/telemetry": "^1.0",\n'
        '         "symfony/console": "^5.0"\n'
    )
    assert _names(_scan("composer.json", diff)) == ["acme/telemetry"]


def test_composer_platform_requirements_are_not_dependencies():
    # `php`, `ext-*` and `composer-*` constrain the runtime; nothing installs
    # them from a registry, so neither reader may call one a new package.
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -3,3 +3,6 @@\n"
        '     "require": {\n'
        '+        "php": ">=8.2",\n'
        '+        "ext-json": "*",\n'
        '+        "composer-runtime-api": "^2.0",\n'
        '         "monolog/monolog": "^2.0"\n'
    )
    assert _scan("composer.json", diff) == []


def test_composer_conflict_section_is_not_a_dependency():
    # conflict/replace/provide/suggest use the SAME vendor/package key shape as
    # require; only the enclosing section tells them apart.
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -8,3 +8,4 @@\n"
        '     "conflict": {\n'
        '+        "bad/lib": "<2.0",\n'
        '         "old/thing": "*"\n'
    )
    assert _scan("composer.json", diff) == []


def test_composer_require_and_conflict_in_one_diff_report_only_the_require():
    # Both sections are touched by the same change, so the section a key sits
    # under is the only thing that separates them -- and getting it wrong in
    # either direction (missing the require key, or claiming the conflict one)
    # is a defect this pins from both sides.
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -3,6 +3,8 @@\n"
        '     "require": {\n'
        '         "monolog/monolog": "^2.0",\n'
        '+        "acme/telemetry": "^1.0"\n'
        "     },\n"
        '     "conflict": {\n'
        '+        "bad/lib": "<2.0",\n'
        '         "old/thing": "*"\n'
        "     }\n"
    )
    assert _names(_scan("composer.json", diff)) == ["acme/telemetry"]


def test_composer_vcs_repository_is_a_non_registry_source():
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -20,1 +20,5 @@\n"
        '     "config": {},\n'
        '+    "repositories": [\n'
        '+        { "type": "vcs", "url": "https://git.internal.example/lib.git" }\n'
        "+    ],\n"
    )
    hits = _checks(_scan("composer.json", diff), "non-registry-source")
    assert [h.severity for h in hits] == ["FIX"]
    assert hits[0].detail["kind"] == "alternate-repository"


def test_composer_install_lifecycle_script_is_flagged():
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -25,1 +25,4 @@\n"
        '     "config": {},\n'
        '+    "scripts": {\n'
        '+        "post-install-cmd": "php scripts/setup.php"\n'
        "+    },\n"
    )
    hits = _checks(_scan("composer.json", diff), "install-script")
    assert [h.severity for h in hits] == ["FIX"]
    assert hits[0].detail["script"] == "post-install-cmd"


def test_composer_ordinary_script_key_is_not_an_install_hook():
    diff = (
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -25,2 +25,3 @@\n"
        '     "scripts": {\n'
        '+        "test": "phpunit",\n'
        '         "lint": "php-cs-fixer fix"\n'
    )
    assert _checks(_scan("composer.json", diff), "install-script") == []


# ---------------------------------------------------------------------------
# Java — pom.xml, build.gradle, build.gradle.kts
# ---------------------------------------------------------------------------


def test_pom_new_artifact_id():
    diff = (
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "@@ -10,4 +10,4 @@\n"
        "     <dependency>\n"
        "       <groupId>org.apache.commons</groupId>\n"
        "-      <artifactId>commons-lang3</artifactId>\n"
        "+      <artifactId>commons-text</artifactId>\n"
        "     </dependency>\n"
    )
    assert _names(_scan("pom.xml", diff)) == ["commons-text"]


def test_pom_exclusions_block_declares_nothing():
    # <exclusions> names what must NOT be pulled in -- the opposite of a
    # declaration. The shared reader strips it and this arm inherits that.
    diff = (
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "@@ -5,2 +5,5 @@\n"
        "     <exclusions>\n"
        "+      <exclusion>\n"
        "+        <artifactId>banned-lib</artifactId>\n"
        "+      </exclusion>\n"
        "     </exclusions>\n"
    )
    assert _scan("pom.xml", diff) == []


def test_pom_single_line_comment_declares_no_source():
    # A commented-out <systemPath> is a local jar dependency someone REMOVED.
    diff = (
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "@@ -30,1 +30,2 @@\n"
        "   </dependencies>\n"
        "+      <!-- <systemPath>${basedir}/lib/old.jar</systemPath> -->\n"
    )
    assert _checks(_scan("pom.xml", diff), "non-registry-source") == []


def test_pom_repository_and_system_path_are_non_registry_sources():
    diff = (
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "@@ -30,1 +30,7 @@\n"
        "   </dependencies>\n"
        "+  <repositories>\n"
        "+    <repository>\n"
        "+      <id>internal</id>\n"
        "+      <url>https://maven.internal.example/repo</url>\n"
        "+    </repository>\n"
        "+  </repositories>\n"
        "+      <systemPath>${basedir}/lib/vendor.jar</systemPath>\n"
    )
    assert _sources(_scan("pom.xml", diff)) == ["declared-repository", "local-path"]


def test_gradle_coordinate_is_one_finding_not_two():
    # The shared reader records BOTH halves of `group:artifact` because a profile
    # may name either; a single added line must still read as one dependency.
    diff = (
        "--- a/build.gradle\n"
        "+++ b/build.gradle\n"
        "@@ -5,2 +5,3 @@ dependencies {\n"
        "     implementation 'com.google.guava:guava:32.0-jre'\n"
        "+    implementation 'com.acme:telemetry:1.0'\n"
        " }\n"
    )
    hits = _checks(_scan("build.gradle", diff), "new-dependency")
    assert [h.detail["name"] for h in hits] == ["com.acme:telemetry"]


def test_gradle_commented_lines_declare_nothing():
    # A commented-out `implementation` is how a removed dependency lingers, and a
    # `//` inside a URL string is not a comment at all.
    diff = (
        "--- a/build.gradle\n"
        "+++ b/build.gradle\n"
        "@@ -1,1 +1,3 @@\n"
        " dependencies {\n"
        "+//  implementation 'com.evil:payload:1.0'\n"
        "+    // apply from: 'https://evil.example/x.gradle'\n"
    )
    assert _scan("build.gradle", diff) == []


def test_gradle_local_file_dependency_and_remote_script():
    diff = (
        "--- a/build.gradle\n"
        "+++ b/build.gradle\n"
        "@@ -5,1 +5,3 @@ dependencies {\n"
        "     implementation 'com.google.guava:guava:32.0-jre'\n"
        "+    implementation files('libs/vendor.jar')\n"
        "+apply from: 'https://build.internal.example/common.gradle'\n"
    )
    findings = _scan("build.gradle", diff)
    assert _sources(findings) == ["local-file-dependency"]
    assert _hooks(findings) == ["https://build.internal.example/common.gradle"]


def test_gradle_kts_uses_the_same_rules():
    diff = (
        "--- a/build.gradle.kts\n"
        "+++ b/build.gradle.kts\n"
        "@@ -5,1 +5,2 @@ dependencies {\n"
        '     implementation("com.google.guava:guava:32.0-jre")\n'
        '+    implementation("com.acme:telemetry:1.0")\n'
    )
    assert _names(_scan("build.gradle.kts", diff)) == ["com.acme:telemetry"]


# ---------------------------------------------------------------------------
# Routing, rendering, and fail-open
# ---------------------------------------------------------------------------


def test_collect_fetches_the_new_manifests_including_variable_requirements_names():
    fetched: list[str] = []

    def fetcher(path):
        fetched.append(path)
        return ""

    collect_dependency_findings(
        [
            "requirements-dev.txt",
            "backend/requirements/base.txt",
            "pyproject.toml",
            "go.mod",
            "Cargo.toml",
            "composer.json",
            "pom.xml",
            "build.gradle.kts",
            "src/app.py",
            "README.md",
        ],
        fetcher,
    )
    assert "src/app.py" not in fetched and "README.md" not in fetched
    assert sorted(fetched) == [
        "Cargo.toml",
        "backend/requirements/base.txt",
        "build.gradle.kts",
        "composer.json",
        "go.mod",
        "pom.xml",
        "pyproject.toml",
        "requirements-dev.txt",
    ]


def test_collect_routes_a_nested_manifest_to_its_arm():
    diff = (
        "--- a/services/api/go.mod\n"
        "+++ b/services/api/go.mod\n"
        "@@ -3,1 +3,2 @@ require (\n"
        " \tgithub.com/a/b v1.0.0\n"
        "+\tgithub.com/acme/telemetry v1.0.0\n"
    )
    findings = collect_dependency_findings(["services/api/go.mod"], lambda p: diff)
    assert [f.path for f in findings] == ["services/api/go.mod"]


def test_real_git_diff_hunk_trailer_carries_the_toml_array_opener():
    # Verbatim `git diff` output, not a hand-written fixture: the whole TOML
    # fragment repair rests on git putting `dependencies = [` in the @@ trailer,
    # so that assumption is pinned against the real format.
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "index dc29ab4..1b481be 100644\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -11,5 +11,6 @@ dependencies = [\n"
        '     "uvicorn",\n'
        '     "httpx",\n'
        '     "pydantic",\n'
        '+    "sketchy-pkg==0.0.1",\n'
        '     "structlog",\n'
        " ]\n"
    )
    assert _names(_scan("pyproject.toml", diff)) == ["sketchy-pkg"]


def test_composer_require_opener_outside_the_context_window_claims_nothing():
    # Verbatim `git diff` output for a require block whose opener sits above the
    # three context lines. This is a KNOWN, deliberate blind spot: JSON has no @@
    # funcname trailer to recover the section from, and guessing "require" is
    # what would turn a `conflict` entry into a new dependency. Pinned so that
    # closing it is a decision someone makes on purpose -- at the fetch site,
    # with a wider `git diff -U` -- rather than by teaching the parser to guess.
    diff = (
        "diff --git a/composer.json b/composer.json\n"
        "--- a/composer.json\n"
        "+++ b/composer.json\n"
        "@@ -4,6 +4,7 @@\n"
        '     "monolog/monolog": "^2.0",\n'
        '     "symfony/console": "^5.0",\n'
        '-    "twig/twig": "^3.0"\n'
        '+    "twig/twig": "^3.0",\n'
        '+    "acme/sketchy": "dev-master"\n'
        "   }\n"
        " }\n"
    )
    assert _names(_scan("composer.json", diff)) == []


def test_render_findings_sanitizes_python_manifest_evidence():
    diff = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,1 +1,3 @@\n"
        " flask\n"
        "+telemetry @ git+https://acme.example/\x07t.git\n"
        "+pyyaml==6.0.1\n"
    )
    lines = render_findings(_scan("requirements.txt", diff))
    body = "\n".join(lines)
    assert "\x07" not in body
    # FIX sorts before NIT, same ordering the npm arm renders with.
    assert lines[0].startswith("[FIX]")
    assert any("pyyaml" in line for line in lines)


@pytest.mark.parametrize(
    "diff",
    ["", "not a diff at all", "@@@@@@", "+++\n---\n", "\x00\x01", "+" + "x" * 50_000],
)
@pytest.mark.parametrize(
    "path", ["pyproject.toml", "go.mod", "Cargo.toml", "composer.json", "pom.xml", "build.gradle"]
)
def test_degenerate_diffs_fail_open_to_no_findings(path, diff):
    # Every arm must yield an empty result rather than raise: this runs behind an
    # MCP tool boundary where an exception would surface as a failed review.
    assert _scan(path, diff) == []


def test_unicode_and_control_bytes_do_not_break_a_scan():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1,1 +1,2 @@ dependencies = [\n"
        '     "flask",\n'
        '+    "包裹-📦",\n'
    )
    # No crash; the name is not requirement-shaped, so nothing is claimed.
    assert _scan("pyproject.toml", diff) == []


def test_names_come_from_the_shared_detect_readers():
    # Guard against a second parser creeping back in: the arm's name set must be
    # exactly what knowledge.detect reads from the same text.
    from chameleon_mcp.dep_diff import _declared_names
    from chameleon_mcp.knowledge.detect import _declared_deps

    text = 'dependencies = [\n  "flask>=3.0",\n  "requests",\n]\n'
    assert _declared_names("pyproject.toml", text) == _declared_deps("pyproject.toml", text)
    go = "require (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v2.0.0 // indirect\n)\n"
    assert _declared_names("go.mod", go) == _declared_deps("go.mod", go)
