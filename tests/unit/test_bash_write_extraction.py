"""Unit tests for _extract_bash_write_targets() in hook_helper.py.

This is the pure-regex pre-filter that decides whether a Bash command has a
single, unambiguous file-write target the recorder can lint. It is the FP engine
for Bash-mutation coverage: it must catch the clear `>`/`>>`/`tee`/`sed -i`
shapes and reject everything ambiguous (globs, variables, fd dups, command
substitution) so a write-free command stays cheap and an unparseable target is
never guessed at.
"""

from __future__ import annotations

from chameleon_mcp.hook_helper import _extract_bash_write_targets as extract

# --- the four supported shapes ---------------------------------------------


def test_redirect_truncate():
    assert extract("cat > foo.ts") == ["foo.ts"]


def test_redirect_append():
    assert extract("printf x >> bar/baz.rb") == ["bar/baz.rb"]


def test_tee_basic():
    assert extract("echo hi | tee app/models/user.rb") == ["app/models/user.rb"]


def test_tee_append_flag():
    assert extract("echo hi | tee -a config/routes.rb") == ["config/routes.rb"]


def test_tee_long_flag():
    assert extract("echo hi | tee --append lib/a.rb") == ["lib/a.rb"]


def test_tee_in_a_pipeline_middle():
    assert extract("foo | bar | tee app/x.rb | baz") == ["app/x.rb"]


def test_sed_inplace_gnu():
    assert extract("sed -i 's/a/b/' src/index.ts") == ["src/index.ts"]


def test_sed_inplace_bsd_suffix():
    assert extract("sed -i.bak 's/a/b/' lib/thing.rb") == ["lib/thing.rb"]


def test_sed_inplace_bsd_empty_suffix():
    assert extract('sed -i "" src/a.ts') == ["src/a.ts"]


def test_sed_file_is_last_operand():
    # sed's mutated file is always its trailing operand, not the script.
    assert extract("sed -i -e 's/x/y/' -e 's/p/q/' app/conf.rb") == ["app/conf.rb"]


# --- quoting ----------------------------------------------------------------


def test_double_quoted_target_with_space():
    assert extract('echo > "with space.ts"') == ["with space.ts"]


def test_single_quoted_target_with_space():
    assert extract("cat > 'spaced name.rb'") == ["spaced name.rb"]


# --- multiple targets, de-duplication --------------------------------------


def test_two_distinct_targets():
    assert extract("echo x > a.ts; echo y >> b.rb") == ["a.ts", "b.rb"]


def test_duplicate_target_collapsed():
    assert extract("echo > foo.ts && echo > foo.ts") == ["foo.ts"]


def test_mixed_shapes_in_one_command():
    out = extract("cat > a.ts && echo hi | tee b.rb")
    assert out == ["a.ts", "b.rb"]


# --- rejected: ambiguous / non-file targets --------------------------------


def test_fd_dup_not_a_file():
    assert extract("node build.js 2>&1") == []


def test_fd_dup_after_real_redirect_kept_only_for_file():
    # /dev/null is a literal target; the 2>&1 fd dup contributes nothing.
    assert extract("cmd > /dev/null 2>&1") == ["/dev/null"]


def test_variable_target_rejected():
    assert extract("cat > $DEST") == []


def test_partial_variable_target_rejected():
    # Must not extract the literal prefix "out." before the variable.
    assert extract("cat > out.$EXT") == []


def test_glob_target_rejected():
    assert extract("cat > *.ts") == []


def test_command_substitution_target_rejected():
    assert extract("cat > $(mktemp).ts") == []


def test_brace_expansion_target_rejected():
    assert extract("cat > out.{ts,rb}") == []


def test_tilde_target_rejected():
    # Tilde expands to $HOME; the literal path is unknown pre-shell.
    assert extract("cat > ~/notes.ts") == []


# --- no write at all --------------------------------------------------------


def test_plain_read_command():
    assert extract("grep foo bar.ts") == []


def test_listing_command():
    assert extract("ls -la") == []


def test_git_apply_out_of_scope():
    # Paths live inside the patch body, not on the command line.
    assert extract("git apply changes.diff") == []


# --- defensive inputs -------------------------------------------------------


def test_empty_command():
    assert extract("") == []


def test_non_string_command():
    assert extract(None) == []  # type: ignore[arg-type]


def test_oversize_command_capped():
    # A pathologically long command is not a single-target write; bail.
    huge = "cat > a.ts " + ("x" * 9000)
    assert extract(huge) == []


def test_heredoc_to_tee_is_a_literal_file():
    # The heredoc body feeds tee, which writes a literal file — in scope.
    assert extract("cat <<EOF | tee app/x.rb") == ["app/x.rb"]


# --- backslash-escaped spaces in an unquoted target -------------------------


def test_redirect_backslash_escaped_space():
    # An unquoted path with a backslash-escaped space must not truncate at the
    # space; the escape is collapsed so the full on-disk path is recovered.
    assert extract(r"echo x > /a/Testing\ Apps/x.rb") == ["/a/Testing Apps/x.rb"]


def test_redirect_append_backslash_escaped_space():
    assert extract(r"echo x >> /a/Testing\ Apps/y.rb") == ["/a/Testing Apps/y.rb"]


def test_tee_backslash_escaped_space():
    assert extract(r"echo x | tee /a/Testing\ Apps/z.rb") == ["/a/Testing Apps/z.rb"]


def test_sed_inplace_backslash_escaped_space():
    assert extract(r"sed -i 's/a/b/' /a/Testing\ Apps/w.rb") == ["/a/Testing Apps/w.rb"]


def test_multiple_escaped_spaces_in_one_path():
    assert extract(r"echo x > /a/My\ Cool\ Dir/file.rb") == ["/a/My Cool Dir/file.rb"]


def test_escaped_dollar_unescapes_to_literal_then_rejected():
    # `\$` is an escaped literal dollar; after unescaping it is a metachar, so
    # the target is treated as unparseable rather than guessed.
    assert extract(r"echo x > /a/out\$EXT") == []
