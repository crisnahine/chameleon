"""Unit tests for chameleon_mcp.profile.poisoning_scanner.

The scanner inspects canonical-excerpt content for dangerous coding patterns
an attacker might commit to steer Claude toward insecure habits. Five patterns
are flagged unconditionally (raw_sql_concat, eval_call, exec_call,
subprocess_shell_true, plus the unconditional half of weak-crypto detection)
and two are flagged only when a security keyword sits within +/-200 chars
(weak_hash via MD5/SHA1, math_random_for_security via Math.random).

This module is pure logic: no env reads at import time, no module-level
connection caches, no filesystem or network. It therefore needs no
CHAMELEON_PLUGIN_DATA isolation fixture (cf. sibling test_secret_scanner.py).
Synthetic string fixtures are built inline; nothing touches tmp_path, node,
or prism.
"""

from __future__ import annotations

from chameleon_mcp.profile.poisoning_scanner import (
    DANGEROUS_PATTERNS,
    _has_security_context,
    scan_for_dangerous_patterns,
)


def _kinds(content: str) -> list[str]:
    return [h["kind"] for h in scan_for_dangerous_patterns(content)]


# --------------------------------------------------------------------------- #
# Clean content / empty input
# --------------------------------------------------------------------------- #


def test_empty_content_is_safe():
    assert scan_for_dangerous_patterns("") == []


def test_ordinary_code_is_not_flagged():
    """Plain TS/Ruby source must produce zero hits."""
    samples = [
        "export const x = 1;\nfunction add(a, b) { return a + b; }",
        "class Listing < ApplicationRecord\n  belongs_to :user\nend",
        "import { Button } from '@/components/ui/button';",
        "const q = db.query('SELECT * FROM users');",  # no interpolation
    ]
    for src in samples:
        assert scan_for_dangerous_patterns(src) == [], src


# --------------------------------------------------------------------------- #
# Branch 1: raw_sql_concat (unconditional)
# --------------------------------------------------------------------------- #


def test_sql_concat_flagged_when_keyword_follows_interpolation():
    """Template literal with ${...} followed by a SQL keyword is flagged."""
    hits = scan_for_dangerous_patterns("`WHERE col = ${cond} SELECT now`")
    assert len(hits) == 1
    assert hits[0]["kind"] == "raw_sql_concat"
    assert hits[0]["position"] == 0
    assert hits[0]["match"].startswith("`")
    assert "SELECT" in hits[0]["match"]


def test_sql_concat_each_keyword_variant():
    """All five SQL verbs trip the pattern when they follow an interpolation."""
    for verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "DROP"):
        content = f"`${{tbl}} {verb} rows`"
        assert _kinds(content) == ["raw_sql_concat"], verb


def test_sql_concat_case_insensitive():
    assert _kinds("const s = `${id} delete from t`") == ["raw_sql_concat"]


def test_sql_concat_not_flagged_without_interpolation():
    """A SQL keyword in a literal with no ${...} is not a concat injection."""
    assert scan_for_dangerous_patterns("`SELECT * FROM users`") == []


def test_sql_concat_flagged_when_keyword_precedes_interpolation():
    """The verb may sit on EITHER side of the ${...} within one template literal.

    `SELECT * FROM t WHERE id=${id}` is the most common injection shape (verb
    before the interpolation) and must be flagged, not missed.
    """
    hits = scan_for_dangerous_patterns("db.query(`SELECT * FROM t WHERE id=${id}`)")
    assert len(hits) == 1
    assert hits[0]["kind"] == "raw_sql_concat"
    assert "SELECT" in hits[0]["match"]
    assert "${id}" in hits[0]["match"]


def test_sql_concat_flagged_verb_first_variants():
    """UPDATE and DELETE verb-first shapes are also flagged."""
    update_hits = scan_for_dangerous_patterns("db.query(`UPDATE t SET x=1 WHERE id=${id}`)")
    assert [h["kind"] for h in update_hits] == ["raw_sql_concat"]
    assert "UPDATE" in update_hits[0]["match"]

    delete_hits = scan_for_dangerous_patterns("db.query(`DELETE FROM t WHERE id=${id}`)")
    assert [h["kind"] for h in delete_hits] == ["raw_sql_concat"]
    assert "DELETE" in delete_hits[0]["match"]


def test_sql_concat_interpolation_without_verb_not_flagged():
    """Guard against over-matching: a template literal with an interpolation
    but no SQL verb on either side yields nothing."""
    assert scan_for_dangerous_patterns("db.query(`row ${id} loaded`)") == []


def test_sql_concat_not_flagged_when_interpolation_lacks_keyword():
    """Interpolation present but no SQL verb -> no hit."""
    assert scan_for_dangerous_patterns("`hello ${name} world`") == []


# --------------------------------------------------------------------------- #
# Branch 2: eval_call (unconditional)
# --------------------------------------------------------------------------- #


def test_eval_call_flagged():
    hits = scan_for_dangerous_patterns("eval('1 + 1')")
    assert len(hits) == 1
    assert hits[0]["kind"] == "eval_call"
    assert hits[0]["match"] == "eval("
    assert hits[0]["position"] == 0


def test_eval_call_case_insensitive_and_whitespace_tolerant():
    hits = scan_for_dangerous_patterns("EVAL  (x)")
    assert _kinds("EVAL  (x)") == ["eval_call"]
    assert hits[0]["match"] == "EVAL  ("


def test_eval_requires_word_boundary():
    """A method named *eval glued to a word char must not match (\\beval)."""
    assert scan_for_dangerous_patterns("myeval(x)") == []


def test_eval_after_non_word_char_matches():
    """Non-word char before eval is a boundary, so vm.eval(...) is flagged."""
    hits = scan_for_dangerous_patterns("vm.eval(src)")
    assert _kinds("vm.eval(src)") == ["eval_call"]
    assert hits[0]["position"] == 3


# --------------------------------------------------------------------------- #
# Branch 3: exec_call (unconditional)
# --------------------------------------------------------------------------- #


def test_exec_call_flagged():
    hits = scan_for_dangerous_patterns("exec(code)")
    assert len(hits) == 1
    assert hits[0]["kind"] == "exec_call"
    assert hits[0]["match"] == "exec("
    assert hits[0]["position"] == 0


def test_exec_requires_word_boundary():
    assert scan_for_dangerous_patterns("myexec(x)") == []


# --------------------------------------------------------------------------- #
# Branch 4: subprocess_shell_true (unconditional)
# --------------------------------------------------------------------------- #


def test_shell_true_flagged():
    hits = scan_for_dangerous_patterns("subprocess.run(cmd, shell=True)")
    assert len(hits) == 1
    assert hits[0]["kind"] == "subprocess_shell_true"
    assert hits[0]["match"] == "shell=True"
    assert hits[0]["position"] == 20


def test_shell_true_whitespace_tolerant_and_case_insensitive():
    assert _kinds("shell = True") == ["subprocess_shell_true"]
    assert _kinds("SHELL=true") == ["subprocess_shell_true"]


def test_shell_false_not_flagged():
    assert scan_for_dangerous_patterns("subprocess.run(cmd, shell=False)") == []


# --------------------------------------------------------------------------- #
# Branch 5: weak_hash (conditional on security context)
# --------------------------------------------------------------------------- #


def test_weak_hash_flagged_with_security_context():
    """MD5 near a security keyword is flagged."""
    hits = scan_for_dangerous_patterns("hash = MD5(password)")
    assert len(hits) == 1
    assert hits[0]["kind"] == "weak_hash"
    assert hits[0]["match"] == "MD5"
    assert hits[0]["position"] == 7


def test_weak_hash_sha1_with_security_context():
    assert _kinds("digest = SHA1(secret)") == ["weak_hash"]


def test_weak_hash_suppressed_without_security_context():
    """MD5/SHA1 with no nearby security keyword (cache key, React key) is allowed."""
    assert scan_for_dangerous_patterns("const cacheKey = MD5(componentId);") == []
    assert scan_for_dangerous_patterns("const k = SHA1(filePath);") == []


def test_weak_hash_security_keyword_within_window_triggers():
    """A security keyword comfortably within 200 chars enables the flag."""
    content = "MD5(" + ("x" * 100) + ") token"
    assert _kinds(content) == ["weak_hash"]


def test_weak_hash_security_keyword_beyond_window_suppressed():
    """A security keyword far past the +/-200 window does not enable the flag."""
    content = "MD5" + (" " * 250) + "token"
    assert scan_for_dangerous_patterns(content) == []


def test_weak_hash_security_keyword_before_match_counts():
    """The window is symmetric: a keyword preceding MD5 also counts."""
    content = "password = compute(); " + ("y" * 50) + " MD5(x)"
    assert _kinds(content) == ["weak_hash"]


# --------------------------------------------------------------------------- #
# Branch 6: math_random_for_security (conditional on security context)
# --------------------------------------------------------------------------- #


def test_math_random_flagged_with_security_context():
    hits = scan_for_dangerous_patterns("const token = Math.random();")
    assert len(hits) == 1
    assert hits[0]["kind"] == "math_random_for_security"
    assert hits[0]["match"] == "Math.random("


def test_math_random_suppressed_without_security_context():
    """Math.random for UI jitter / non-crypto is not flagged."""
    assert scan_for_dangerous_patterns("const jitter = Math.random() * 10;") == []


# --------------------------------------------------------------------------- #
# Security-context helper directly
# --------------------------------------------------------------------------- #


def test_has_security_context_recognizes_keyword_variants():
    """Each documented security keyword family is recognized in-window."""
    for kw in (
        "password",
        "passwd",
        "secret",
        "token",
        "signature",
        "auth",
        "hmac",
        "csrf",
        "session",
        "api_key",
        "api-key",
        "access_token",
        "nonce",
        "salt",
        "crypto",
        "encrypt",
        "decrypt",
        "sign",
    ):
        content = f"value {kw} here"
        # match span = the whole string so window covers it all
        assert _has_security_context(content, 0, len(content)) is True, kw


def test_has_security_context_false_for_plain_text():
    content = "compute a stable cache key for the component tree"
    assert _has_security_context(content, 0, len(content)) is False


def test_has_security_context_respects_window_bound():
    """Keyword outside the +/-window is invisible to the helper."""
    # 'password' sits 300 chars before the (tiny) match span -> out of a 200 window.
    content = "password " + ("y" * 300) + " ZZ"
    idx = content.index("ZZ")
    assert _has_security_context(content, idx, idx + 2) is False
    # Same keyword 50 chars before -> inside the window.
    content2 = "password " + ("y" * 50) + " ZZ"
    idx2 = content2.index("ZZ")
    assert _has_security_context(content2, idx2, idx2 + 2) is True


def test_has_security_context_clamps_negative_start():
    """A match at offset 0 must not raise on the start-of-string clamp."""
    content = "MD5 with token"
    assert _has_security_context(content, 0, 3) is True


# --------------------------------------------------------------------------- #
# Multiple hits, ordering, dedicated hit shape
# --------------------------------------------------------------------------- #


def test_multiple_distinct_patterns_all_reported():
    content = "eval(a)\nexec(b)\nsubprocess.run(c, shell=True)"
    kinds = set(_kinds(content))
    assert kinds == {"eval_call", "exec_call", "subprocess_shell_true"}


def test_hits_are_grouped_by_pattern_order_not_position():
    """Iteration is pattern-major: eval (earlier in DANGEROUS_PATTERNS) is
    reported before exec even when exec appears first in the text."""
    content = "exec(a)\neval(b)"
    hits = scan_for_dangerous_patterns(content)
    assert [h["kind"] for h in hits] == ["eval_call", "exec_call"]
    # positions reflect real text offsets, not report order
    assert hits[0]["position"] == 8  # eval is the second line
    assert hits[1]["position"] == 0  # exec is the first line


def test_repeated_same_pattern_each_occurrence_reported():
    content = "eval(a); eval(b); eval(c)"
    hits = scan_for_dangerous_patterns(content)
    assert [h["kind"] for h in hits] == ["eval_call", "eval_call", "eval_call"]
    assert [h["position"] for h in hits] == [0, 9, 18]


def test_hit_dict_has_exact_keys():
    hits = scan_for_dangerous_patterns("eval(x)")
    assert set(hits[0].keys()) == {"kind", "match", "position"}


# --------------------------------------------------------------------------- #
# Module-level table invariants
# --------------------------------------------------------------------------- #


def test_dangerous_patterns_table_shape():
    """raw_sql_concat now spans three syntaxes (TS backtick, Ruby #{}, Python
    f-string); only weak_hash and math_random require security context."""
    kinds = [k for _, k, _ in DANGEROUS_PATTERNS]
    assert kinds == [
        "raw_sql_concat",
        "raw_sql_concat",
        "raw_sql_concat",
        "eval_call",
        "exec_call",
        "subprocess_shell_true",
        "weak_hash",
        "math_random_for_security",
    ]
    conditional = {k for _, k, req in DANGEROUS_PATTERNS if req}
    assert conditional == {"weak_hash", "math_random_for_security"}


# --------------------------------------------------------------------------- #
# raw_sql_concat: Ruby #{} and Python f-string interpolation (cross-language)
# --------------------------------------------------------------------------- #


def test_sql_concat_flagged_for_ruby_interpolation():
    # Ruby "...#{x}..." interpolation around a SQL verb is the same injection
    # class as the TS backtick case, and must not slip past.
    content = 'User.where("SELECT * FROM users WHERE id = #{params[:id]}")'
    assert "raw_sql_concat" in _kinds(content)


def test_sql_concat_flagged_for_ruby_interpolation_before_keyword():
    content = 'db.exec("#{table} DELETE FROM things")'
    assert "raw_sql_concat" in _kinds(content)


def test_sql_concat_flagged_for_python_fstring():
    # Python f"...{x}..." around a SQL verb.
    content = 'cur.execute(f"SELECT * FROM users WHERE id = {user_id}")'
    assert "raw_sql_concat" in _kinds(content)


def test_sql_concat_flagged_for_python_fstring_single_quotes():
    content = "cur.execute(f'DELETE FROM t WHERE k = {key}')"
    assert "raw_sql_concat" in _kinds(content)


def test_ruby_parameterized_query_not_flagged():
    # A parameterized query (no interpolation) is the safe pattern; not flagged.
    content = 'User.where("SELECT * FROM users WHERE id = ?", params[:id])'
    assert "raw_sql_concat" not in _kinds(content)


def test_python_non_fstring_sql_not_flagged():
    # A plain (non-f) string with a placeholder is not interpolation.
    content = 'cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))'
    assert "raw_sql_concat" not in _kinds(content)


# raw_sql_concat: tightened to require real SQL-statement shape (verb + clause),
# so SQL verbs occurring as ordinary English words near an interpolation do not
# false-positive (which was poisoning canonical-witness selection on Rails repos).


def test_ruby_benign_string_with_sql_word_not_flagged():
    benign = [
        'logger.info("Selected #{record.id} for update")',
        'redirect_to "/update/#{resource.id}/edit"',
        'flash[:notice] = "Deleted #{count} items"',
        'raise "Insert #{name} failed"',
    ]
    for s in benign:
        assert "raw_sql_concat" not in _kinds(s), s


def test_ruby_real_sql_statement_still_flagged():
    assert _kinds('db.query("SELECT * FROM users WHERE id = #{params[:id]}")') == ["raw_sql_concat"]
    assert "raw_sql_concat" in _kinds('exec("#{t} DELETE FROM things")')
    assert "raw_sql_concat" in _kinds('q("INSERT INTO logs VALUES (#{msg})")')
    assert "raw_sql_concat" in _kinds('q("UPDATE users SET name = #{n} WHERE id = 1")')


def test_python_benign_fstring_with_sql_word_not_flagged():
    assert "raw_sql_concat" not in _kinds('log.info(f"User {name} selected for update")')


def test_python_real_fstring_sql_still_flagged():
    assert "raw_sql_concat" in _kinds('cur.execute(f"SELECT * FROM users WHERE id = {uid}")')
