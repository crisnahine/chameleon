"""Unit tests for the file-naming convention: derivation + lint rule.

Covers the path-only basename-casing / suffix-token derivation in conventions.py
and the file-naming-convention-violation rule in lint_engine.py.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.conventions import (
    _classify_casing,
    _split_compound_suffix,
    extract_all_conventions,
    extract_file_naming_convention,
    format_conventions_for_session,
)
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.lint_engine import lint_conventions


def _pf(path: str) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


class TestSplitCompoundSuffix:
    def test_plain_extension_has_no_suffix(self):
        assert _split_compound_suffix("user.ts") == ("user", None)
        assert _split_compound_suffix("billing.rb") == ("billing", None)

    def test_ts_compound_suffix(self):
        assert _split_compound_suffix("user.service.ts") == ("user", ".service.ts")
        assert _split_compound_suffix("order-detail.component.tsx") == (
            "order-detail",
            ".component.tsx",
        )

    def test_ruby_compound_suffix(self):
        assert _split_compound_suffix("billing_job.rb") == ("billing", "_job.rb")
        assert _split_compound_suffix("welcome_mailer.rb") == ("welcome", "_mailer.rb")

    def test_no_extension(self):
        assert _split_compound_suffix("Makefile") == ("Makefile", None)


class TestClassifyCasing:
    def test_kebab(self):
        assert _classify_casing("user-profile") == "kebab-case"

    def test_snake(self):
        assert _classify_casing("user_profile") == "snake_case"

    def test_camel(self):
        assert _classify_casing("userProfile") == "camelCase"

    def test_pascal(self):
        assert _classify_casing("UserProfile") == "PascalCase"

    def test_single_lowercase_word_has_no_signal(self):
        # A bare lowercase word conforms to kebab, snake, and camel at once, so
        # it carries no distinguishing signal and is not tallied.
        assert _classify_casing("user") is None
        assert _classify_casing("index") is None

    def test_no_signal_inputs(self):
        assert _classify_casing("") is None
        assert _classify_casing("123abc") is None

    def test_underscore_plus_embedded_uppercase_is_a_distinct_bucket(self):
        # Mixing a snake separator with an embedded uppercase letter conforms
        # to none of the four buckets, but it is unambiguously non-conforming
        # (unlike a bare lowercase word), so it must not silently vanish as
        # None — it needs its own distinct, non-canonical label.
        for stem in ("test_BadCasing", "user_APIClient", "my_Widget"):
            got = _classify_casing(stem)
            assert got is not None, stem
            assert got not in ("kebab-case", "snake_case", "camelCase", "PascalCase"), stem


class TestExtractFileNaming:
    def test_dominant_kebab_with_suffix(self):
        names = [f"thing-{i}.service.ts" for i in range(12)]
        out = extract_file_naming_convention(basenames=names)
        assert out["file_naming"]["casing"] == "kebab-case"
        assert out["file_naming"]["casing_consistency"] == 1.0
        assert out["file_naming"]["suffix"] == ".service.ts"
        assert out["file_naming"]["sample_size"] == 12

    def test_below_min_sample_returns_empty(self):
        names = ["a.service.ts", "b.service.ts", "c.service.ts"]
        assert extract_file_naming_convention(basenames=names) == {}

    def test_mixed_casing_below_floor_returns_empty(self):
        # Half kebab, half pascal: neither clears the 60% consistency floor, so
        # no casing convention is derived (the calibration story for mixed repos).
        names = [f"a-{i}.ts" for i in range(5)] + [f"B{i}.ts" for i in range(5)]
        assert extract_file_naming_convention(basenames=names) == {}

    def test_lone_suffix_does_not_become_convention(self):
        # One *.service.ts among plain files: suffix votes 1/10, below floor.
        names = [f"thing-{i}.ts" for i in range(9)] + ["special-thing.service.ts"]
        out = extract_file_naming_convention(basenames=names)
        assert out["file_naming"]["casing"] == "kebab-case"
        assert "suffix" not in out["file_naming"]

    def test_index_files_excluded_from_casing_tally(self):
        # index.ts carries no casing signal; the real convention still wins.
        names = ["index.ts"] * 3 + [f"user-{i}.ts" for i in range(8)]
        out = extract_file_naming_convention(basenames=names)
        assert out["file_naming"]["casing"] == "kebab-case"
        assert out["file_naming"]["sample_size"] == 8

    def test_ruby_snake_with_job_suffix(self):
        names = [f"thing_{i}_job.rb" for i in range(10)]
        out = extract_file_naming_convention(basenames=names)
        assert out["file_naming"]["casing"] == "snake_case"
        assert out["file_naming"]["suffix"] == "_job.rb"

    def test_mixed_underscore_uppercase_files_count_against_consistency(self):
        # 6 real snake_case files + 4 mixed-shape (underscore + embedded
        # uppercase) files: the mixed files must be tallied (not silently
        # excluded like a no-signal basename), pulling consistency below the
        # 100% it would otherwise report.
        names = [f"thing_{i}.py" for i in range(6)] + [f"thing_Bad{i}.py" for i in range(4)]
        out = extract_file_naming_convention(basenames=names)
        assert out["file_naming"]["casing"] == "snake_case"
        assert out["file_naming"]["sample_size"] == 10
        assert out["file_naming"]["casing_consistency"] == 0.6

    def test_mixed_case_never_wins_the_dominance_vote(self):
        # Regression: mixed_case is a non-conforming shape that must only
        # count against consistency, never become the declared convention
        # itself even when it is the largest single bucket. A plurality of
        # mixed-shape files (6) outnumbering a real snake_case majority (4)
        # must not report casing="mixed_case" -- that would flag every
        # genuinely-conforming sibling as a violator of a fabricated rule.
        names = [f"thing_Bad{i}.py" for i in range(6)] + [f"thing_{i}.py" for i in range(4)]
        out = extract_file_naming_convention(basenames=names)
        assert out == {}, f"mixed_case must never win the dominance vote, got {out}"


class TestExtractAllConventionsWiring:
    def test_file_naming_merged_into_archetype_naming(self):
        files = {"service": [_pf(f"/repo/src/services/thing-{i}.service.ts") for i in range(12)]}
        conv = extract_all_conventions(
            files_by_archetype=files,
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        entry = conv["conventions"]["naming"]["service"]
        assert entry["file_naming"]["casing"] == "kebab-case"
        assert entry["file_naming"]["suffix"] == ".service.ts"

    def test_file_naming_coexists_with_prefix(self):
        files = {"types": [_pf(f"/repo/src/types/thing-{i}.ts") for i in range(10)]}
        decls = {"types": {"interface": ["IFoo", "IBar", "IBaz", "IQux", "IZap"]}}
        conv = extract_all_conventions(
            files_by_archetype=files,
            declarations_by_archetype=decls,
            generation=1,
            language="typescript",
        )
        entry = conv["conventions"]["naming"]["types"]
        assert entry["interface_prefix"]["pattern"] == "I"
        assert entry["file_naming"]["casing"] == "kebab-case"


class TestFileNamingLintRule:
    _CONV = {
        "naming": {
            "file_naming": {
                "casing": "kebab-case",
                "casing_consistency": 0.97,
                "sample_size": 40,
                "suffix": ".service.ts",
                "suffix_consistency": 0.92,
            }
        }
    }

    def test_casing_violation(self):
        out = lint_conventions(
            "export const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/userProfile.service.ts",
        )
        rules = {v.rule for v in out}
        assert "file-naming-convention-violation" in rules
        casing_v = [v for v in out if v.expected == "kebab-case"]
        assert casing_v and casing_v[0].actual == "camelCase"

    def test_underscore_plus_embedded_uppercase_fires_against_snake_convention(self):
        # This shape used to classify as None (no signal) and so was silently
        # excluded from the rule entirely, even though it obviously breaks a
        # snake_case convention.
        conv = {
            "naming": {
                "file_naming": {
                    "casing": "snake_case",
                    "casing_consistency": 0.97,
                    "sample_size": 40,
                }
            }
        }
        out = lint_conventions(
            "def foo():\n    pass\n",
            conv,
            language="python",
            file_path="app/services/test_BadCasing.py",
        )
        casing_v = [v for v in out if v.rule == "file-naming-convention-violation"]
        assert casing_v and casing_v[0].actual == "mixed_case"

    def test_missing_suffix_violation(self):
        out = lint_conventions(
            "export const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/user-profile.ts",
        )
        suffix_v = [v for v in out if v.expected == ".service.ts"]
        assert suffix_v and suffix_v[0].actual == "(none)"

    def test_conformant_file_no_violation(self):
        out = lint_conventions(
            "export const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/user-profile.service.ts",
        )
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []

    def test_no_file_path_skips_rule(self):
        out = lint_conventions("export const x = 1;\n", self._CONV, language="typescript")
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []

    def test_index_file_casing_not_flagged(self):
        # index.ts has no casing signal, so the casing rule must not fire even
        # though "index" is not literally kebab-case.
        out = lint_conventions(
            "export const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/index.ts",
        )
        casing_v = [v for v in out if v.expected == "kebab-case"]
        assert casing_v == []

    def test_ignore_directive_suppresses_rule(self):
        out = lint_conventions(
            "// chameleon-ignore file-naming-convention\nexport const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/userProfile.service.ts",
        )
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []

    def test_ruby_ignore_directive_suppresses_rule(self):
        conv = {
            "naming": {
                "file_naming": {
                    "casing": "snake_case",
                    "casing_consistency": 0.99,
                    "sample_size": 30,
                }
            }
        }
        out = lint_conventions(
            "# chameleon-ignore file-naming-convention\nclass Foo\nend\n",
            conv,
            language="ruby",
            file_path="app/jobs/BillingJob.rb",
        )
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []

    def test_non_source_file_not_flagged(self):
        # A Makefile/README/config dropped into a governed cluster carries no
        # source-naming obligation; its casing must not be judged.
        for fp in (
            "src/services/Makefile",
            "src/services/README.md",
            "src/services/Dockerfile",
            "src/services/package.json",
        ):
            out = lint_conventions(
                "export const x = 1;\n",
                self._CONV,
                language="typescript",
                file_path=fp,
            )
            assert [v for v in out if v.rule == "file-naming-convention-violation"] == [], fp

    def test_dotfile_config_not_flagged_for_missing_suffix(self):
        # `.eslintrc.js` has an empty stem and never voted in the suffix tally,
        # so it must not be flagged for "missing" the .service.ts suffix.
        out = lint_conventions(
            "export const x = 1;\n",
            self._CONV,
            language="typescript",
            file_path="src/services/.eslintrc.js",
        )
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []

    def test_low_consistency_convention_does_not_fire(self):
        conv = {
            "naming": {
                "file_naming": {
                    "casing": "kebab-case",
                    "casing_consistency": 0.50,
                    "sample_size": 20,
                }
            }
        }
        out = lint_conventions(
            "export const x = 1;\n",
            conv,
            language="typescript",
            file_path="src/services/userProfile.ts",
        )
        assert [v for v in out if v.rule == "file-naming-convention-violation"] == []


class TestFileNamingSessionFormat:
    def test_file_naming_line_rendered(self):
        conv = {
            "conventions": {
                "naming": {
                    "service": {
                        "file_naming": {
                            "casing": "kebab-case",
                            "casing_consistency": 0.97,
                            "sample_size": 40,
                            "suffix": ".service.ts",
                            "suffix_consistency": 0.92,
                        }
                    }
                }
            }
        }
        out = format_conventions_for_session(conv)
        assert "NAMING:" in out
        assert "kebab-case" in out
        assert ".service.ts" in out
        assert "enforced" in out  # 97% clears the 95% enforce floor
