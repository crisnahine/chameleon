from pathlib import Path

from chameleon_mcp.enforcement_calibration import (
    active_block_rules,
    calibrate_block_rules,
    load_block_rules,
    write_block_rules,
)


def test_roundtrip(tmp_path: Path):
    data = {
        "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100},
        "jsx-presence-mismatch": {"active": False, "fp_rate": 0.02, "sampled": 50},
    }
    write_block_rules(tmp_path, data)
    loaded = load_block_rules(tmp_path)
    assert loaded["phantom-import"]["active"] is True
    assert active_block_rules(tmp_path) == {"phantom-import"}


def test_missing_file_is_empty(tmp_path: Path):
    assert load_block_rules(tmp_path) == {}
    assert active_block_rules(tmp_path) == set()


def test_corrupt_file_is_empty(tmp_path: Path):
    (tmp_path / "enforcement.json").write_text("{not json", encoding="utf-8")
    assert active_block_rules(tmp_path) == set()


def test_clean_repo_activates_phantom(tmp_path):
    # A witness file with a valid relative import -> phantom-import sees 0 FPs.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { b } from './b'\nexport const a = 1\n", encoding="utf-8"
    )
    (repo / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["phantom-import"]["active"] is True
    assert result["phantom-import"]["fp_rate"] == 0.0


def test_phantom_demoted_when_witness_has_dangling_import(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { x } from './nope'\nexport const a = 1\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    # Repo's own committed file trips the rule -> it must NOT be allowed to block.
    assert result["phantom-import"]["active"] is False


def test_no_witnesses_keeps_all_rules_inactive(tmp_path):
    # Empty/unbootstrapped profile: zero evidence must NOT greenlight blockers.
    class _Loaded:
        canonicals = {"canonicals": {}}
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(tmp_path, _Loaded())
    assert result["phantom-import"]["sampled"] == 0
    for rule, meta in result.items():
        assert meta["active"] is False, rule


def test_jsx_demoted_when_sampled_file_breaks_nonjsx_baseline(tmp_path):
    # First witness defines a non-JSX baseline; a second sampled file of the same
    # archetype contains JSX, so jsx-presence-mismatch fires against the baseline.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "plain.tsx").write_text(
        "export const a = 1\nexport function f() { return 2 }\n", encoding="utf-8"
    )
    (repo / "src" / "withjsx.tsx").write_text(
        "export const C = () => <div>hi</div>\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "view": [
                    {
                        "witness": {"path": "src/plain.tsx"},
                        "normative_shape": {"ast_query": {"jsx_present": False}},
                    },
                    {
                        "witness": {"path": "src/withjsx.tsx"},
                        "normative_shape": {"ast_query": {"jsx_present": False}},
                    },
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["jsx-presence-mismatch"]["active"] is False
    assert result["jsx-presence-mismatch"]["flagged"] == 1


def test_jsx_active_when_baseline_matches_witnesses(tmp_path):
    # Witness defines a JSX-present baseline; sampled files conform, so jsx stays active.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "comp.tsx").write_text(
        "export const C = () => <div>hi</div>\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "view": [
                    {
                        "witness": {"path": "src/comp.tsx"},
                        "normative_shape": {"ast_query": {"jsx_present": True}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["jsx-presence-mismatch"]["active"] is True
    assert result["jsx-presence-mismatch"]["flagged"] == 0


def test_import_preference_demoted_when_witness_uses_over_module(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { map } from 'lodash'\nexport const a = 1\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {
            "conventions": {
                "imports": {"util": {"competing": [{"over": "lodash", "preferred": "ramda"}]}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["import-preference-violation"]["active"] is False
    assert result["import-preference-violation"]["flagged"] == 1


def test_import_preference_active_when_witness_uses_preferred(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { map } from 'ramda'\nexport const a = 1\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {
            "conventions": {
                "imports": {"util": {"competing": [{"over": "lodash", "preferred": "ramda"}]}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["import-preference-violation"]["active"] is True
    assert result["import-preference-violation"]["flagged"] == 0
