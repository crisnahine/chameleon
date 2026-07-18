from chameleon_mcp.autopass import _is_non_source_file, build_autopass_verdict


def test_dotfile_configs_are_non_source():
    for path in (
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        ".gitattributes",
        ".npmrc",
        "packages/web/.gitignore",
    ):
        assert _is_non_source_file(path) is True, path


def test_typescript_declaration_files_are_non_source():
    for path in ("next-env.d.ts", "src/types/api.d.ts", "global.d.ts"):
        assert _is_non_source_file(path) is True, path


def test_real_source_stays_source():
    for path in (
        "app/api/x/route.ts",
        "components/Card.tsx",
        "lib/db.ts",
        "main.py",
        "app/models/user.rb",
    ):
        assert _is_non_source_file(path) is False, path


def test_verdict_excludes_dotfiles_and_declarations_from_unarchetyped():
    # A routine PR: one real source file plus a touched .gitignore and a
    # regenerated next-env.d.ts. Only the source file is unarchetyped SOURCE;
    # the dotfile config and the generated declaration must not elevate risk.
    numstat = "5\t0\tapp/api/metrics/route.ts\n1\t0\t.gitignore\n0\t6\tnext-env.d.ts\n"
    name_status = "A\tapp/api/metrics/route.ts\nM\t.gitignore\nM\tnext-env.d.ts\n"
    verdict = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: True,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )
    assert verdict["facts"]["unarchetyped_files"] == 1
    assert verdict["facts"]["source_files_changed"] == 1
