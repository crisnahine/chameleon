from pathlib import Path

from chameleon_mcp.lint_engine import Violation
from chameleon_mcp.phantom_imports import lint_phantom_imports


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestTypeScriptRelative:
    def test_existing_sibling_is_clean(self, tmp_path):
        _write(tmp_path / "user-service.ts")
        editing = tmp_path / "index.ts"
        content = "import { foo } from './user-service';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_typo_sibling_is_flagged(self, tmp_path):
        _write(tmp_path / "user-service.ts")
        editing = tmp_path / "index.ts"
        content = "import { foo } from './uesr-service';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert len(v) == 1
        assert v[0].rule == "phantom-import"
        assert v[0].severity == "warning"
        assert "uesr-service" in v[0].actual

    def test_index_resolution_is_clean(self, tmp_path):
        _write(tmp_path / "widgets" / "index.tsx")
        editing = tmp_path / "app.ts"
        content = "import { W } from './widgets';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_existing_directory_is_clean(self, tmp_path):
        (tmp_path / "components").mkdir()
        editing = tmp_path / "app.ts"
        content = "import x from './components';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_require_and_dynamic_import(self, tmp_path):
        editing = tmp_path / "app.ts"
        content = (
            "const a = require('./missing-a');\n"
            "const b = await import('./missing-b');\n"
        )
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        specs = {x.actual for x in v}
        assert specs == {"./missing-a", "./missing-b"}

    def test_missing_file_path_returns_empty(self, tmp_path):
        content = "import { foo } from './whatever';\n"
        assert lint_phantom_imports(
            content, file_path=None, repo_root=str(tmp_path),
            language="typescript", rules={},
        ) == []

    def test_file_outside_repo_returns_empty(self, tmp_path):
        outside = tmp_path.parent / "elsewhere.ts"
        content = "import { foo } from './nope';\n"
        assert lint_phantom_imports(
            content, file_path=str(outside), repo_root=str(tmp_path),
            language="typescript", rules={},
        ) == []


class TestTypeScriptAlias:
    def _rules(self, source="tsconfig.json", paths=None):
        return {"rules": {"typescript": {"source": source, "paths": paths or {}}}}

    def test_alias_resolves_clean(self, tmp_path):
        _write(tmp_path / "src" / "user.ts")
        editing = tmp_path / "src" / "app.ts"
        content = "import { u } from '@/user';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(paths={"@/*": ["./src/*"]}),
        )
        assert v == []

    def test_alias_typo_in_real_dir_is_flagged(self, tmp_path):
        _write(tmp_path / "src" / "user.ts")
        editing = tmp_path / "src" / "app.ts"
        content = "import { u } from '@/usr';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(paths={"@/*": ["./src/*"]}),
        )
        assert len(v) == 1
        assert v[0].actual == "@/usr"

    def test_unmapped_alias_is_skipped(self, tmp_path):
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True)
        content = "import { u } from '~/whatever';\n"  # no ~/* mapping
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(paths={"@/*": ["./src/*"]}),
        )
        assert v == []

    def test_bare_package_is_skipped(self, tmp_path):
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True)
        content = "import React from 'react';\nimport { z } from '@scope/pkg';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(paths={"@/*": ["./src/*"]}),
        )
        assert v == []

    def test_alias_into_missing_dir_is_skipped(self, tmp_path):
        # baseUrl uncertainty guard: resolved parent dir does not exist -> skip
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True)
        content = "import { u } from '@/nowhere/user';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(paths={"@/*": ["./src/*"]}),
        )
        assert v == []

    def test_monorepo_alias_anchors_to_nearest_tsconfig(self, tmp_path):
        # Two apps, each with its own tsconfig + same @/* mapping. The profile
        # stores app-b's tsconfig as `source`, but an edit in app-a must resolve
        # @/* against app-a's own tsconfig (the nearest), not app-b's.
        _write(tmp_path / "apps" / "a" / "tsconfig.json", "{}")
        _write(tmp_path / "apps" / "b" / "tsconfig.json", "{}")
        _write(tmp_path / "apps" / "a" / "src" / "user.ts")
        editing = tmp_path / "apps" / "a" / "src" / "app.ts"
        rules = {"rules": {"typescript": {
            "source": "apps/b/tsconfig.json", "paths": {"@/*": ["./src/*"]},
        }}}
        clean = lint_phantom_imports(
            "import { u } from '@/user';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules=rules,
        )
        assert clean == []
        typo = lint_phantom_imports(
            "import { u } from '@/usr';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules=rules,
        )
        assert len(typo) == 1 and typo[0].actual == "@/usr"

    def test_non_code_extension_is_skipped(self, tmp_path):
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True)
        content = "import './styles.css';\nimport logo from './logo.svg';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules=self._rules(),
        )
        assert v == []


class TestNoiseAndTypegen:
    def test_import_inside_backtick_template_is_skipped(self, tmp_path):
        # codemod / eslint-rule test fixtures embed import statements in
        # template literals; those must not be flagged.
        editing = tmp_path / "remove.spec.ts"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = (
            'import { it } from "vitest";\n'
            "const input = `\n"
            "  import db from './db';\n"
            "  import root from './create-root';\n"
            "`;\n"
        )
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_import_as_string_value_other_quote_is_skipped(self, tmp_path):
        # A code snippet stored as a regular-string value, using the OTHER quote
        # for its inner specifier, must not be mistaken for a real import.
        editing = tmp_path / "gen.ts"
        for snippet in (
            "const code = \"import x from './does_not_exist';\";\n",
            "const code = 'import x from \"./nope\";';\n",
            "const code = \"require('./gone')\";\n",
        ):
            v = lint_phantom_imports(
                snippet, file_path=str(editing), repo_root=str(tmp_path),
                language="typescript", rules={},
            )
            assert v == [], f"string-value snippet should not flag: {snippet!r}"

    def test_real_import_after_string_value_still_flags(self, tmp_path):
        # Guard the mask doesn't over-suppress: a real import on a later line
        # after a code-as-string assignment must still be checked.
        editing = tmp_path / "gen.ts"
        content = (
            "const code = \"import a from './a'\";\n"
            "import { real } from './missing-real';\n"
        )
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert [x.actual for x in v] == ["./missing-real"]

    def test_import_inside_block_comment_is_skipped(self, tmp_path):
        editing = tmp_path / "app.ts"
        content = "/* import x from './gone'; */\nexport const y = 1;\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_query_suffix_svg_is_skipped(self, tmp_path):
        # vite-plugin-svgr: ./icon.svg?react - the ?react query defeated a bare
        # `.svg$` check; the spec must be cleaned before the extension test.
        editing = tmp_path / "src" / "Icon.tsx"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = "import Logo from './assets/logo.svg?react';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_double_slash_in_relative_path_not_mistaken_for_comment(self, tmp_path):
        # `../..//shortcut` contains `//` mid-string; it must not be treated as a
        # line comment (which would truncate the closing quote).
        _write(tmp_path / "shortcut.ts")
        editing = tmp_path / "a" / "b" / "c.tsx"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = 'import { k } from "../..//shortcut";\n'
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_full_line_commented_import_is_skipped(self, tmp_path):
        editing = tmp_path / "app.ts"
        content = "  // import x from './deleted';\nexport const y = 1;\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_plus_typegen_segment_is_skipped(self, tmp_path):
        # React Router v7 typegen: ./+types/page resolves via rootDirs.
        editing = tmp_path / "app" / "routes" / "page.tsx"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "import type { Route } from './+types/page';\n"
            "import type { L } from '../+types/layout';\n"
        )
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []


class TestNodeNextAndContainment:
    def test_js_specifier_maps_to_ts_source(self, tmp_path):
        # NodeNext/ESM: import './bar.js' where the source on disk is bar.ts.
        _write(tmp_path / "src" / "bar.ts")
        editing = tmp_path / "src" / "app.ts"
        for spec in ("./bar.js",):
            v = lint_phantom_imports(
                f"import {{ x }} from '{spec}';\n", file_path=str(editing),
                repo_root=str(tmp_path), language="typescript", rules={},
            )
            assert v == [], f"{spec} should resolve to bar.ts"

    def test_mjs_specifier_maps_to_mts_source(self, tmp_path):
        _write(tmp_path / "src" / "util.mts")
        editing = tmp_path / "src" / "app.ts"
        v = lint_phantom_imports(
            "import { x } from './util.mjs';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules={},
        )
        assert v == []

    def test_alias_js_specifier_maps_to_ts_source(self, tmp_path):
        _write(tmp_path / "src" / "user.ts")
        editing = tmp_path / "src" / "app.ts"
        rules = {"rules": {"typescript": {"source": "tsconfig.json", "paths": {"@/*": ["./src/*"]}}}}
        v = lint_phantom_imports(
            "import { u } from '@/user.js';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules=rules,
        )
        assert v == []

    def test_out_of_repo_relative_is_skipped(self, tmp_path):
        # A spec escaping the repo must not be statted outside or flagged.
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True, exist_ok=True)
        v = lint_phantom_imports(
            "import x from '../../../../../../etc/passwd';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules={},
        )
        assert v == []

    def test_expected_is_repo_relative(self, tmp_path):
        editing = tmp_path / "src" / "app.ts"
        editing.parent.mkdir(parents=True, exist_ok=True)
        v = lint_phantom_imports(
            "import x from './missing';\n", file_path=str(editing),
            repo_root=str(tmp_path), language="typescript", rules={},
        )
        assert len(v) == 1
        # `expected` must not leak an absolute path
        assert not v[0].expected.startswith("/")
        assert v[0].expected == "src"

    def test_unterminated_template_does_not_hang(self, tmp_path):
        # ReDoS guard: an unterminated backtick template with thousands of
        # escaped backticks must be stripped in linear time, not catastrophic
        # backtracking. The embedded import is inside the template -> not flagged.
        editing = tmp_path / "app.ts"
        content = "const doc = `" + ("\\`" * 30000) + "\n import x from './nope';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []


class TestRuby:
    def test_require_relative_hit_is_clean(self, tmp_path):
        _write(tmp_path / "helper.rb")
        editing = tmp_path / "main.rb"
        content = "require_relative 'helper'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []

    def test_require_relative_miss_is_flagged(self, tmp_path):
        editing = tmp_path / "main.rb"
        editing.write_text("require_relative 'helpr'\n", encoding="utf-8")
        v = lint_phantom_imports(
            content="require_relative 'helpr'\n", file_path=str(editing),
            repo_root=str(tmp_path), language="ruby", rules={},
        )
        assert len(v) == 1
        assert v[0].actual == "helpr"

    def test_plain_require_is_skipped(self, tmp_path):
        editing = tmp_path / "main.rb"
        content = "require 'json'\nrequire 'some/lib/path'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []

    def test_absolute_require_relative_is_skipped(self, tmp_path):
        editing = tmp_path / "config" / "puma.rb"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = "require_relative '/home/git/app/lib/thing'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []

    def test_missing_parent_dir_is_skipped(self, tmp_path):
        # EE/CE split: ../../../ee/... points outside this checkout.
        editing = tmp_path / "spec" / "helper.rb"
        editing.parent.mkdir(parents=True, exist_ok=True)
        content = "require_relative '../ee/support/thing'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []

    def test_interpolation_spec_is_skipped(self, tmp_path):
        # require_relative "sub/#{name}" - the interpolated part can't be verified.
        (tmp_path / "sub").mkdir()
        editing = tmp_path / "main.rb"
        content = 'require_relative "sub/#{name}"\n'
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []

    def test_typo_in_existing_dir_still_flags(self, tmp_path):
        _write(tmp_path / "lib" / "helper.rb")
        editing = tmp_path / "lib" / "main.rb"
        content = "require_relative 'helpr'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert len(v) == 1 and v[0].actual == "helpr"


class TestIgnoreDirective:
    def test_ts_ignore_suppresses(self, tmp_path):
        editing = tmp_path / "app.ts"
        content = "// chameleon-ignore phantom-import\nimport x from './nope';\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="typescript", rules={},
        )
        assert v == []

    def test_ruby_ignore_suppresses(self, tmp_path):
        editing = tmp_path / "main.rb"
        content = "# chameleon-ignore phantom-import\nrequire_relative 'nope'\n"
        v = lint_phantom_imports(
            content, file_path=str(editing), repo_root=str(tmp_path),
            language="ruby", rules={},
        )
        assert v == []


def test_returns_violation_instances(tmp_path):
    editing = tmp_path / "app.ts"
    out = lint_phantom_imports(
        "import x from './missing';\n", file_path=str(editing),
        repo_root=str(tmp_path), language="typescript", rules={},
    )
    assert len(out) == 1 and isinstance(out[0], Violation)
