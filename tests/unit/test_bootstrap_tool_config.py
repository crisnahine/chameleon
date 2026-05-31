"""Unit tests for chameleon_mcp.bootstrap.tool_config.

Covers linter / formatter / type-check config detection during bootstrap:
prettier, tsconfig (+ extends-chain resolution), eslint (JSON / YAML / JS
static parse), editorconfig, and rubocop. Asserts detection, parsed values,
sane defaults when configs are absent, and malformed-config handling.

Fixtures are tiny synthetic config files written into tmp_path. The module
is pure-logic (no network, no node, no prism) for everything we exercise
here: the eslint JS path is tested via the *static* regex parser, which the
module uses by default (CHAMELEON_ALLOW_ESLINT_EVAL is left unset).
"""

from __future__ import annotations

import json
import shutil

import pytest

from chameleon_mcp.bootstrap import tool_config as tool_config_mod
from chameleon_mcp.bootstrap.tool_config import (
    _jsish_to_json,
    _parse_editorconfig,
    _parse_eslint_js,
    _parse_eslint_yaml,
    _parse_rubocop_yaml,
    _scan_balanced_braces,
    _strip_jsonc_comments,
    read_tool_configs,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_data(monkeypatch, tmp_path):
    """Replicate the suite-wide isolation: pin CHAMELEON_PLUGIN_DATA at a
    throwaway dir and make sure the eslint-eval opt-in is OFF so the static
    parser path is exercised. This module reads only from the repo_root we
    pass in, but the isolation keeps any incidental env reads sandboxed.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "_plugin_data"))
    monkeypatch.delenv("CHAMELEON_ALLOW_ESLINT_EVAL", raising=False)
    yield


# --------------------------------------------------------------------------
# Defaults: empty repo
# --------------------------------------------------------------------------


class TestEmptyRepoDefaults:
    def test_no_configs_yields_all_none(self, tmp_path):
        res = read_tool_configs(tmp_path)
        assert res.prettier is None
        assert res.tsconfig is None
        assert res.eslint is None
        assert res.editorconfig is None
        assert res.rubocop is None

    def test_no_configs_yields_empty_collections_and_flags(self, tmp_path):
        res = read_tool_configs(tmp_path)
        assert res.sources == {}
        assert res.parse_warnings == {}
        assert res.tsconfig_extends_chain == []
        assert res.has_prettier_js_plugins is False
        assert res.has_eslint_js_plugins is False


# --------------------------------------------------------------------------
# Prettier
# --------------------------------------------------------------------------


class TestPrettier:
    def test_prettierrc_json_parsed(self, tmp_path):
        (tmp_path / ".prettierrc").write_text(
            json.dumps({"semi": False, "singleQuote": True, "printWidth": 100})
        )
        res = read_tool_configs(tmp_path)
        assert res.prettier == {"semi": False, "singleQuote": True, "printWidth": 100}
        assert res.sources["prettier"] == ".prettierrc"
        assert res.has_prettier_js_plugins is False

    def test_prettierrc_precedence_over_prettierrc_json(self, tmp_path):
        # `.prettierrc` is read first and breaks the loop, so it wins.
        (tmp_path / ".prettierrc").write_text(json.dumps({"tag": "rc"}))
        (tmp_path / ".prettierrc.json").write_text(json.dumps({"tag": "json"}))
        res = read_tool_configs(tmp_path)
        assert res.prettier == {"tag": "rc"}
        assert res.sources["prettier"] == ".prettierrc"

    def test_prettier_plugins_array_sets_invisible_flag(self, tmp_path):
        (tmp_path / ".prettierrc.json").write_text(
            json.dumps({"plugins": ["@trivago/prettier-plugin-sort-imports"]})
        )
        res = read_tool_configs(tmp_path)
        assert res.has_prettier_js_plugins is True
        assert res.sources["prettier"] == ".prettierrc.json"

    def test_prettier_config_js_sets_flag_without_parsing(self, tmp_path):
        (tmp_path / "prettier.config.js").write_text("module.exports = { semi: false };")
        res = read_tool_configs(tmp_path)
        assert res.prettier is None
        assert res.has_prettier_js_plugins is True
        assert res.sources["prettier"] == "prettier.config.js"

    def test_malformed_prettier_json_ignored(self, tmp_path):
        (tmp_path / ".prettierrc").write_text("{not valid json,,,}")
        res = read_tool_configs(tmp_path)
        assert res.prettier is None
        # malformed JSON breaks before sources is set
        assert "prettier" not in res.sources
        assert res.has_prettier_js_plugins is False


# --------------------------------------------------------------------------
# tsconfig (basic detection + extends-chain resolution)
# --------------------------------------------------------------------------


class TestTsconfigBasic:
    def test_plain_tsconfig_parsed(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"strict": True, "target": "ES2020"}})
        )
        res = read_tool_configs(tmp_path)
        assert res.tsconfig == {"compilerOptions": {"strict": True, "target": "ES2020"}}
        assert res.sources["tsconfig"] == "tsconfig.json"
        assert res.tsconfig_extends_chain == ["tsconfig.json"]
        assert "tsconfig" not in res.parse_warnings

    def test_tsconfig_with_jsonc_comments_and_schema_url(self, tmp_path):
        # $schema URL contains `//` which must NOT be eaten as a comment.
        (tmp_path / "tsconfig.json").write_text(
            "{\n"
            '  "$schema": "https://json.schemastore.org/tsconfig", // schema ref\n'
            "  /* block */\n"
            '  "compilerOptions": { "strict": true, }\n'
            "}\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.tsconfig == {
            "$schema": "https://json.schemastore.org/tsconfig",
            "compilerOptions": {"strict": True},
        }

    def test_malformed_tsconfig_records_warning_and_no_config(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{ totally broken ][ }")
        res = read_tool_configs(tmp_path)
        assert res.tsconfig is None
        assert res.parse_warnings["tsconfig"] == "tsconfig.json failed to parse"
        assert "tsconfig" not in res.sources
        assert res.tsconfig_extends_chain == []


class TestTsconfigExtends:
    def test_relative_extends_closest_wins_merge(self, tmp_path):
        (tmp_path / "base.json").write_text(
            json.dumps(
                {"compilerOptions": {"strict": True, "target": "ES2020", "module": "commonjs"}}
            )
        )
        (tmp_path / "tsconfig.json").write_text(
            json.dumps(
                {
                    "extends": "./base.json",
                    "compilerOptions": {"module": "ESNext"},
                    "include": ["src"],
                }
            )
        )
        res = read_tool_configs(tmp_path)
        # derived `module` wins; parent strict/target merged in; include kept
        assert res.tsconfig == {
            "compilerOptions": {"strict": True, "target": "ES2020", "module": "ESNext"},
            "include": ["src"],
        }
        assert res.tsconfig_extends_chain == ["tsconfig.json", "base.json"]
        # extends key itself is stripped from the merged result
        assert "extends" not in res.tsconfig

    def test_extensionless_relative_extends_resolves(self, tmp_path):
        (tmp_path / "base.json").write_text(json.dumps({"compilerOptions": {"strict": True}}))
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"extends": "./base", "compilerOptions": {"target": "ES2021"}})
        )
        res = read_tool_configs(tmp_path)
        assert res.tsconfig == {"compilerOptions": {"strict": True, "target": "ES2021"}}
        assert res.tsconfig_extends_chain == ["tsconfig.json", "base.json"]

    def test_bare_specifier_resolved_via_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "@tsconfig" / "strictest"
        nm.mkdir(parents=True)
        (nm / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"strict": True, "noUncheckedIndexedAccess": True}})
        )
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"extends": "@tsconfig/strictest", "compilerOptions": {"target": "ES2022"}})
        )
        res = read_tool_configs(tmp_path)
        assert res.tsconfig == {
            "compilerOptions": {
                "strict": True,
                "noUncheckedIndexedAccess": True,
                "target": "ES2022",
            }
        }
        assert res.tsconfig_extends_chain == [
            "tsconfig.json",
            "node_modules/@tsconfig/strictest/tsconfig.json",
        ]
        assert "tsconfig" not in res.parse_warnings

    def test_missing_extends_target_warns_but_keeps_local(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"extends": "./nope.json", "compilerOptions": {"strict": True}})
        )
        res = read_tool_configs(tmp_path)
        # local compilerOptions survive even though parent could not resolve
        assert res.tsconfig == {"compilerOptions": {"strict": True}}
        assert res.tsconfig_extends_chain == ["tsconfig.json"]
        assert res.parse_warnings["tsconfig"] == (
            "tsconfig extends target './nope.json' could not be resolved from tsconfig.json"
        )

    def test_extends_cycle_detected(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"extends": "./a.json", "compilerOptions": {"strict": True}})
        )
        (tmp_path / "a.json").write_text(
            json.dumps({"extends": "./tsconfig.json", "compilerOptions": {"noImplicitAny": True}})
        )
        res = read_tool_configs(tmp_path)
        assert res.tsconfig_extends_chain == ["tsconfig.json", "a.json"]
        assert res.parse_warnings["tsconfig"] == (
            "tsconfig extends cycle detected at tsconfig.json (already visited)"
        )
        # the one valid hop is still merged
        assert res.tsconfig == {"compilerOptions": {"noImplicitAny": True, "strict": True}}

    def test_extends_chain_hop_cap_warns(self, tmp_path):
        # tsconfig -> c0 -> c1 -> ... -> c10 : exceeds the 8-hop cap.
        (tmp_path / "tsconfig.json").write_text(json.dumps({"extends": "./c0.json"}))
        for i in range(10):
            (tmp_path / f"c{i}.json").write_text(
                json.dumps({"extends": f"./c{i + 1}.json", "compilerOptions": {f"opt{i}": True}})
            )
        (tmp_path / "c10.json").write_text(json.dumps({"compilerOptions": {"final": True}}))
        res = read_tool_configs(tmp_path)
        # root + 8 resolved hops before the cap trips on hop 9.
        assert len(res.tsconfig_extends_chain) == 9
        assert res.parse_warnings["tsconfig"] == (
            "tsconfig extends chain exceeded 8 hops; stopping to avoid runaway resolution"
        )


# --------------------------------------------------------------------------
# ESLint
# --------------------------------------------------------------------------


class TestEslint:
    def test_eslintrc_json_with_comments_and_trailing_comma(self, tmp_path):
        (tmp_path / ".eslintrc.json").write_text(
            "{\n"
            "  // a comment\n"
            '  "extends": ["eslint:recommended"],\n'
            '  "rules": {"no-console": "warn",},\n'
            "}\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.eslint == {"extends": ["eslint:recommended"], "rules": {"no-console": "warn"}}
        assert res.sources["eslint"] == ".eslintrc.json"
        assert res.has_eslint_js_plugins is False

    def test_eslint_plugins_array_sets_flag(self, tmp_path):
        (tmp_path / ".eslintrc.json").write_text(json.dumps({"plugins": ["react"], "rules": {}}))
        res = read_tool_configs(tmp_path)
        assert res.eslint == {"plugins": ["react"], "rules": {}}
        assert res.has_eslint_js_plugins is True

    def test_eslintrc_yaml_parsed(self, tmp_path):
        (tmp_path / ".eslintrc.yml").write_text(
            "extends:\n  - eslint:recommended\nrules:\n  no-console: warn\nplugins:\n  - react\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.eslint == {
            "extends": ["eslint:recommended"],
            "rules": {"no-console": "warn"},
            "plugins": ["react"],
        }
        assert res.sources["eslint"] == ".eslintrc.yml"
        # plugins key triggers invisibility flag
        assert res.has_eslint_js_plugins is True

    def test_malformed_eslint_yaml_records_warning(self, tmp_path):
        (tmp_path / ".eslintrc.yaml").write_text("rules:\n  - : : bad\n   indent broken")
        res = read_tool_configs(tmp_path)
        assert res.eslint is None
        assert res.sources["eslint"] == ".eslintrc.yaml"
        assert res.parse_warnings["eslint"].startswith("malformed YAML in .eslintrc.yaml:")

    def test_eslintrc_js_static_parse_succeeds(self, tmp_path):
        # Default path (no eval): static regex parser handles simple literals.
        (tmp_path / ".eslintrc.js").write_text(
            "module.exports = {\n"
            "  extends: ['eslint:recommended'],\n"
            "  rules: { 'no-console': 'warn' },\n"
            "};\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.eslint == {"extends": ["eslint:recommended"], "rules": {"no-console": "warn"}}
        assert res.sources["eslint"] == ".eslintrc.js"
        assert res.has_eslint_js_plugins is False

    def test_eslintrc_js_unparseable_sets_flag_and_warning(self, tmp_path):
        (tmp_path / ".eslintrc.js").write_text(
            "const x = require('foo'); module.exports = { ...x, rules: makeRules() };\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.eslint is None
        assert res.has_eslint_js_plugins is True
        assert res.sources["eslint"] == ".eslintrc.js"
        assert res.parse_warnings["eslint"].startswith(".eslintrc.js: object literal not")

    def test_json_eslint_wins_over_flat_js_config(self, tmp_path):
        # When .eslintrc.json resolves, the flat eslint.config.js is not
        # parsed and (per current logic) does not flip the invisibility flag,
        # because the eslint source slot is already filled.
        (tmp_path / ".eslintrc.json").write_text(json.dumps({"rules": {}}))
        (tmp_path / "eslint.config.js").write_text("export default [];")
        res = read_tool_configs(tmp_path)
        assert res.eslint == {"rules": {}}
        assert res.sources["eslint"] == ".eslintrc.json"
        assert res.has_eslint_js_plugins is False


# --------------------------------------------------------------------------
# ESLint node-eval path (CHAMELEON_ALLOW_ESLINT_EVAL gate)
# --------------------------------------------------------------------------


class TestEslintNodeEvalGate:
    """Cover the opt-in Node-eval reader (`_parse_eslint_js_via_node`) and the
    default-OFF safety behaviour.

    The static regex parser can only read a `module.exports = { ... }` /
    `export default { ... }` object literal. A *flat* config (an exported
    array of rule blocks) is invisible to it. The Node-eval path — gated
    behind CHAMELEON_ALLOW_ESLINT_EVAL=1 — actually executes the config via
    `import()` and so can read the flat array. These tests pin both halves of
    that gate.
    """

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_flag_on_node_eval_reads_flat_config_array(self, monkeypatch, tmp_path):
        # Opt into the Node-eval reader for this trusted repo.
        monkeypatch.setenv("CHAMELEON_ALLOW_ESLINT_EVAL", "1")
        # `eslint.config.js` with `export default [...]` is ESM; mark the dir
        # so node's import() treats the .js file as a module (real flat-config
        # repos either use .mjs or a package.json `type: module`).
        (tmp_path / "package.json").write_text(json.dumps({"type": "module"}))
        (tmp_path / "eslint.config.js").write_text(
            "export default [\n"
            "  {\n"
            "    rules: {\n"
            "      'no-console': 'warn',\n"
            "      eqeqeq: 'error',\n"
            "    },\n"
            "  },\n"
            "  {\n"
            "    plugins: { react: {} },\n"
            "    rules: { 'no-unused-vars': 'error' },\n"
            "  },\n"
            "];\n"
        )
        res = read_tool_configs(tmp_path)
        # The flat-array merge collapses every block's `rules` into one map,
        # collects plugin names (dict form -> keys), and leaves `extends` empty.
        assert res.eslint == {
            "flat": True,
            "rules": {
                "no-console": "warn",
                "eqeqeq": "error",
                "no-unused-vars": "error",
            },
            "extends": [],
            "plugins": ["react"],
        }
        assert res.sources["eslint"] == "eslint.config.js"
        # merged dict has a non-empty `plugins` -> invisibility flag set
        assert res.has_eslint_js_plugins is True
        assert "eslint" not in res.parse_warnings

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_flag_on_node_eval_reads_object_export(self, monkeypatch, tmp_path):
        # A legacy `.eslintrc.js` exporting an object the *static* parser
        # cannot coerce (computed values, require). With the flag ON the
        # Node-eval path resolves it to the real value the static parser
        # would have given up on.
        monkeypatch.setenv("CHAMELEON_ALLOW_ESLINT_EVAL", "1")
        (tmp_path / ".eslintrc.js").write_text(
            "const base = ['eslint:recommended'];\n"
            "module.exports = {\n"
            "  extends: [...base],\n"
            "  rules: Object.assign({}, { 'no-console': 'warn' }),\n"
            "};\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.eslint == {
            "extends": ["eslint:recommended"],
            "rules": {"no-console": "warn"},
        }
        assert res.sources["eslint"] == ".eslintrc.js"
        # static parser would have failed here -> no warning means eval won
        assert "eslint" not in res.parse_warnings

    def test_flag_off_does_not_invoke_node(self, monkeypatch, tmp_path):
        """Default-OFF safety: without CHAMELEON_ALLOW_ESLINT_EVAL the Node-eval
        helper must never be called, so an untrusted repo's config code is not
        executed. A flat-array config (unreadable by the static parser) falls
        back to the invisible-plugin warning instead of being evaluated.
        """
        # Autouse fixture already deletes the env var; assert that here too.
        monkeypatch.delenv("CHAMELEON_ALLOW_ESLINT_EVAL", raising=False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("Node-eval path was taken with CHAMELEON_ALLOW_ESLINT_EVAL unset")

        monkeypatch.setattr(tool_config_mod, "_parse_eslint_js_via_node", _boom)

        (tmp_path / "eslint.config.js").write_text(
            "export default [{ rules: { 'no-console': 'warn' } }];\n"
        )
        res = read_tool_configs(tmp_path)
        # static parser can't read a flat array; no node execution happened.
        assert res.eslint is None
        assert res.sources["eslint"] == "eslint.config.js"
        assert res.has_eslint_js_plugins is True
        assert res.parse_warnings["eslint"] == (
            "eslint.config.js: no top-level module.exports assignment found"
        )

    def test_flag_off_static_parser_still_reads_simple_object(self, monkeypatch, tmp_path):
        """Default-OFF must still read a simple object literal via the static
        parser (no node), so the safety default doesn't regress detection.
        """
        monkeypatch.delenv("CHAMELEON_ALLOW_ESLINT_EVAL", raising=False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("Node-eval path taken with flag OFF")

        monkeypatch.setattr(tool_config_mod, "_parse_eslint_js_via_node", _boom)

        # Call the helper directly to assert the gate, independent of the
        # full read_tool_configs candidate ordering.
        (tmp_path / ".eslintrc.js").write_text(
            "module.exports = { rules: { 'no-console': 'warn' } };\n"
        )
        parsed, warning = _parse_eslint_js(tmp_path / ".eslintrc.js")
        assert parsed == {"rules": {"no-console": "warn"}}
        assert warning is None


# --------------------------------------------------------------------------
# .editorconfig
# --------------------------------------------------------------------------


class TestEditorconfig:
    def test_sections_and_root_parsed(self, tmp_path):
        (tmp_path / ".editorconfig").write_text(
            "root = true\n"
            "\n"
            "[*]\n"
            "indent_style = space\n"
            "indent_size = 2\n"
            "# a comment\n"
            "; another comment\n"
            "[*.md]\n"
            "trim_trailing_whitespace = false\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.editorconfig == {
            "root": {"root": "true"},
            "*": {"indent_style": "space", "indent_size": "2"},
            "*.md": {"trim_trailing_whitespace": "false"},
        }
        assert res.sources["editorconfig"] == ".editorconfig"

    def test_parse_editorconfig_skips_comments_and_blanks(self, tmp_path):
        p = tmp_path / ".editorconfig"
        p.write_text("# only comments\n\n; nothing else\n")
        parsed = _parse_editorconfig(p)
        # root section always seeded, but stays empty
        assert parsed == {"root": {}}


# --------------------------------------------------------------------------
# RuboCop
# --------------------------------------------------------------------------


class TestRubocop:
    def test_rubocop_yml_parsed(self, tmp_path):
        (tmp_path / ".rubocop.yml").write_text(
            "AllCops:\n"
            "  TargetRubyVersion: 3.2\n"
            "  NewCops: enable\n"
            "Style/StringLiterals:\n"
            "  EnforcedStyle: double_quotes\n"
        )
        res = read_tool_configs(tmp_path)
        assert res.rubocop == {
            "AllCops": {"TargetRubyVersion": 3.2, "NewCops": "enable"},
            "Style/StringLiterals": {"EnforcedStyle": "double_quotes"},
        }
        assert res.sources["rubocop"] == ".rubocop.yml"

    def test_rubocop_yaml_extension_also_detected(self, tmp_path):
        (tmp_path / ".rubocop.yaml").write_text("AllCops:\n  DisabledByDefault: true\n")
        res = read_tool_configs(tmp_path)
        assert res.rubocop == {"AllCops": {"DisabledByDefault": True}}
        assert res.sources["rubocop"] == ".rubocop.yaml"

    def test_yml_precedence_over_yaml(self, tmp_path):
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n  TargetRubyVersion: 3.3\n")
        (tmp_path / ".rubocop.yaml").write_text("AllCops:\n  TargetRubyVersion: 2.7\n")
        res = read_tool_configs(tmp_path)
        assert res.rubocop == {"AllCops": {"TargetRubyVersion": 3.3}}
        assert res.sources["rubocop"] == ".rubocop.yml"

    def test_malformed_rubocop_yaml_records_warning(self, tmp_path):
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n  - : : bad\n   broken indent")
        res = read_tool_configs(tmp_path)
        assert res.rubocop is None
        assert res.parse_warnings["rubocop"].startswith("malformed YAML in .rubocop.yml:")
        # source only set when parse succeeds
        assert "rubocop" not in res.sources

    def test_scalar_rubocop_yaml_not_a_mapping(self, tmp_path):
        (tmp_path / ".rubocop.yml").write_text("just a scalar string\n")
        res = read_tool_configs(tmp_path)
        assert res.rubocop is None
        assert res.parse_warnings["rubocop"] == ".rubocop.yml did not parse to a mapping"


# --------------------------------------------------------------------------
# Aggregation: all configs together
# --------------------------------------------------------------------------


class TestAggregation:
    def test_all_tools_detected_together(self, tmp_path):
        (tmp_path / ".prettierrc").write_text(json.dumps({"semi": False}))
        (tmp_path / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True}}))
        (tmp_path / ".eslintrc.json").write_text(json.dumps({"rules": {"eqeqeq": "error"}}))
        (tmp_path / ".editorconfig").write_text("[*]\nindent_size = 4\n")
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n  NewCops: enable\n")
        res = read_tool_configs(tmp_path)
        assert res.prettier == {"semi": False}
        assert res.tsconfig == {"compilerOptions": {"strict": True}}
        assert res.eslint == {"rules": {"eqeqeq": "error"}}
        assert res.editorconfig["*"] == {"indent_size": "4"}
        assert res.rubocop == {"AllCops": {"NewCops": "enable"}}
        assert set(res.sources) == {"prettier", "tsconfig", "eslint", "editorconfig", "rubocop"}
        assert res.parse_warnings == {}


# --------------------------------------------------------------------------
# Pure helpers (direct)
# --------------------------------------------------------------------------


class TestStripJsoncComments:
    def test_line_and_block_comments_stripped(self):
        out = _strip_jsonc_comments('{\n  "a": 1, // c\n  /* b */ "b": 2\n}')
        assert json.loads(out) == {"a": 1, "b": 2}

    def test_url_inside_string_preserved(self):
        out = _strip_jsonc_comments('{"u": "https://example.com/x"}')
        assert json.loads(out) == {"u": "https://example.com/x"}

    def test_trailing_comma_removed(self):
        out = _strip_jsonc_comments('{"a": [1, 2,], "b": 3,}')
        assert json.loads(out) == {"a": [1, 2], "b": 3}

    def test_escaped_quote_inside_string_survives(self):
        out = _strip_jsonc_comments(r'{"q": "a\"b // not a comment"}')
        assert json.loads(out) == {"q": 'a"b // not a comment'}


class TestJsishToJson:
    def test_unquoted_keys_single_quotes_trailing_commas(self):
        out = _jsish_to_json("{ rules: { 'no-console': 'warn', }, }")
        assert json.loads(out) == {"rules": {"no-console": "warn"}}

    def test_double_quoted_values_unchanged(self):
        out = _jsish_to_json('{ "a": "b" }')
        assert json.loads(out) == {"a": "b"}


class TestScanBalancedBraces:
    def test_brace_inside_string_ignored(self):
        text = 'x = { a: "}" } trailing'
        assert _scan_balanced_braces(text, 4) == '{ a: "}" }'

    def test_nested_braces(self):
        text = "{ a: { b: 1 } }"
        assert _scan_balanced_braces(text, 0) == "{ a: { b: 1 } }"

    def test_unbalanced_returns_none(self):
        assert _scan_balanced_braces("{ a: 1 ", 0) is None

    def test_start_not_a_brace_returns_none(self):
        assert _scan_balanced_braces("xyz", 0) is None


class TestParseEslintYamlDirect:
    def test_non_mapping_yaml_warns(self, tmp_path):
        p = tmp_path / ".eslintrc.yml"
        p.write_text("- just\n- a\n- list\n")
        parsed, warning = _parse_eslint_yaml(p)
        assert parsed is None
        assert warning == ".eslintrc.yml did not parse to a mapping"


class TestParseRubocopYamlDirect:
    def test_large_payload_truncated_then_parsed(self, tmp_path):
        # Build a >200KB valid YAML mapping; parser truncates to 200_000 bytes
        # but the head must still parse to a mapping.
        p = tmp_path / ".rubocop.yml"
        lines = ["AllCops:", "  NewCops: enable"]
        # pad with many distinct cop entries to blow past 200KB
        for i in range(20000):
            lines.append(f"Cop/Rule{i}:")
            lines.append("  Enabled: true")
        text = "\n".join(lines) + "\n"
        assert len(text) > 200_000
        p.write_text(text)
        parsed, warning = _parse_rubocop_yaml(p)
        assert warning is None
        assert isinstance(parsed, dict)
        assert parsed["AllCops"] == {"NewCops": "enable"}
