import json
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


def test_phantom_demoted_when_sibling_has_dangling_import(tmp_path):
    # The witness is clean, but an ordinary sibling file (same dir, same ext) has
    # a phantom import. Witnesses are the most-canonical files, so calibration that
    # samples witnesses only would wrongly mark phantom-import active. Sampling
    # siblings catches it.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { b } from './b'\nexport const a = 1\n", encoding="utf-8"
    )
    (repo / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")
    # Ordinary sibling of the witness, not a witness itself, with a dangling import.
    (repo / "src" / "sibling.ts").write_text(
        "import { x } from './nope'\nexport const s = 1\n", encoding="utf-8"
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
    # Sibling trips the rule -> phantom-import must NOT be allowed to block.
    assert result["phantom-import"]["active"] is False
    assert result["phantom-import"]["flagged"] >= 1


def test_witness_only_no_siblings_keeps_rule_active(tmp_path):
    # The witness is the only file of its extension in the directory; with no
    # sibling to sample, behavior is unchanged: a clean witness keeps the rule active.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text(
        "import { b } from './b'\nexport const a = 1\n", encoding="utf-8"
    )
    # ./b is a .tsx, so it's not a same-extension sibling of a.ts.
    (repo / "src" / "b.tsx").write_text("export const b = 2\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["phantom-import"]["sampled"] == 1
    assert result["phantom-import"]["active"] is True


def test_sibling_sample_is_bounded(tmp_path, monkeypatch):
    # Many siblings exist, but the per-archetype cap bounds how many are sampled.
    monkeypatch.setenv("CHAMELEON_CALIBRATION_MAX_SIBLINGS", "3")
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    for i in range(20):
        (repo / "src" / f"sib{i}.ts").write_text(f"export const s{i} = {i}\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    # 1 witness + at most 3 siblings sampled.
    assert result["phantom-import"]["sampled"] == 4


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


def test_naming_demoted_when_sibling_breaks_interface_prefix(tmp_path):
    # Witness conforms to the `I`-prefix convention; an ordinary sibling declares a
    # bare-named interface, so naming-convention-violation fires against the repo's
    # own committed code and the rule must NOT be allowed to block.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export interface IThing { id: number }\n", encoding="utf-8")
    (repo / "src" / "sibling.ts").write_text(
        "export interface Widget { id: number }\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {
            "conventions": {
                "naming": {"util": {"interface_prefix": {"pattern": "I", "consistency": 1.0}}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["naming-convention-violation"]["active"] is False
    assert result["naming-convention-violation"]["flagged"] == 1


def test_naming_active_when_all_files_match_prefix(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export interface IThing { id: number }\n", encoding="utf-8")
    (repo / "src" / "sibling.ts").write_text(
        "export interface IWidget { id: number }\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {
            "conventions": {
                "naming": {"util": {"interface_prefix": {"pattern": "I", "consistency": 1.0}}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["naming-convention-violation"]["active"] is True
    assert result["naming-convention-violation"]["flagged"] == 0


def test_inheritance_demoted_when_witness_breaks_dominant_base(tmp_path):
    # The witness declares a top-level class without the archetype's dominant base,
    # so inheritance-convention-violation fires and the rule must NOT block.
    repo = tmp_path
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "thing.rb").write_text("class Thing\nend\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "model": [
                    {"witness": {"path": "app/thing.rb"}, "normative_shape": {"ast_query": {}}}
                ]
            }
        }
        conventions = {
            "conventions": {
                "inheritance": {"model": {"dominant_base": "ApplicationRecord", "frequency": 1.0}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["inheritance-convention-violation"]["active"] is False
    assert result["inheritance-convention-violation"]["flagged"] == 1


def test_inheritance_active_when_witness_uses_dominant_base(tmp_path):
    repo = tmp_path
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "thing.rb").write_text(
        "class Thing < ApplicationRecord\nend\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "model": [
                    {"witness": {"path": "app/thing.rb"}, "normative_shape": {"ast_query": {}}}
                ]
            }
        }
        conventions = {
            "conventions": {
                "inheritance": {"model": {"dominant_base": "ApplicationRecord", "frequency": 1.0}}
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["inheritance-convention-violation"]["active"] is True
    assert result["inheritance-convention-violation"]["flagged"] == 0


def test_block_eligible_rules_all_present_in_result(tmp_path):
    # Every block-eligible rule, including the two new convention rules, must appear
    # in the calibration result with an active flag because calibrate_block_rules is
    # generic over BLOCK_ELIGIBLE_RULES.
    from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    for rule in BLOCK_ELIGIBLE_RULES:
        assert rule in result, rule
        assert "active" in result[rule], rule


def test_bootstrap_writes_enforcement_json(tmp_path, monkeypatch):
    # Lightweight: exercise the calibrate->write->read path the orchestrator
    # wiring uses. The full bootstrap_repo wiring is covered by the QA battery.
    from chameleon_mcp.enforcement_calibration import (
        active_block_rules,
        calibrate_block_rules,
        write_block_rules,
    )

    repo = tmp_path
    (repo / ".chameleon").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [{"witness": {"path": "src/a.ts"}, "normative_shape": {"ast_query": {}}}]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    write_block_rules(repo / ".chameleon", calibrate_block_rules(repo, _Loaded()))
    assert "phantom-import" in active_block_rules(repo / ".chameleon")


def test_partial_refresh_recalibrates_block_rules(tmp_path, monkeypatch):
    # The partial-refresh path rewrites canonicals.json (the witness set) in place
    # without re-deriving the whole profile, so it must re-run calibration itself;
    # otherwise enforcement.json stays pinned to the pre-refresh witnesses.
    from chameleon_mcp import index_db
    from chameleon_mcp import tools as t

    repo_root = (tmp_path / "repo").resolve()
    profile_dir = repo_root / ".chameleon"
    profile_dir.mkdir(parents=True)

    src = repo_root / "src"
    src.mkdir()
    cluster_id = "cluster-util"

    # One modified file in a large, otherwise-unchanged corpus so the change ratio
    # stays under the partial-refresh ceiling and the path commits instead of
    # falling back to a full rebuild. The modified file re-parses into the same
    # existing cluster, which the partial path accepts.
    candidates = []
    prev_state = {}
    for i in range(20):
        rel = f"src/comp{i}.ts"
        path = repo_root / rel
        path.write_text(f"export const c{i} = {i}\n", encoding="utf-8")
        candidates.append(path)
        prev_state[rel] = {"cluster_id": cluster_id, "sha_hint": f"hint-{i}"}
    changed_rel = "src/comp0.ts"

    (profile_dir / "archetypes.json").write_text(
        json.dumps({"schema_version": 8, "archetypes": {"util": {"cluster_id": cluster_id}}}),
        encoding="utf-8",
    )
    (profile_dir / "canonicals.json").write_text(
        json.dumps({"schema_version": 8, "canonicals": {"util": []}}),
        encoding="utf-8",
    )
    (profile_dir / "profile.json").write_text(
        json.dumps({"schema_version": 8, "archetype_count": 1}), encoding="utf-8"
    )
    (profile_dir / "rules.json").write_text(json.dumps({"schema_version": 8}), encoding="utf-8")

    # Only the first file's content sha drifts from prev_state; the rest match, so
    # exactly one file is "modified" (5% change ratio).
    def _sha(p: Path) -> str:
        rel = str(p.relative_to(repo_root))
        idx = int(rel.removeprefix("src/comp").removesuffix(".ts"))
        return "changed" if rel == changed_rel else f"hint-{idx}"

    monkeypatch.setattr(t, "_content_sha_hint", _sha)
    monkeypatch.setattr(
        t, "_reparse_changed_files", lambda _root, _paths: {changed_rel: (cluster_id, "changed")}
    )
    monkeypatch.setattr(index_db, "upsert_file_clusters", lambda *a, **k: None)
    monkeypatch.setattr(index_db, "delete_file_clusters_for_paths", lambda *a, **k: None)
    monkeypatch.setattr(index_db, "upsert_repo", lambda *a, **k: None)

    calibrated: list[Path] = []
    monkeypatch.setattr(t, "_calibrate_block_rules_for_repo", lambda root: calibrated.append(root))

    envelope = t._attempt_partial_refresh(
        repo_root,
        "repo-id",
        profile_dir,
        candidates,
        prev_state,
        started_at=0.0,
    )

    assert envelope is not None
    assert envelope["data"]["status"] == "partial_refresh"
    assert calibrated == [repo_root]


# --------------------------------------------------------------------------
# load_block_rules: process-level mtime-invalidated cache
# --------------------------------------------------------------------------


def test_load_block_rules_caches_across_calls(tmp_path, monkeypatch):
    import chameleon_mcp.enforcement_calibration as ec

    ec._clear_block_rules_cache()
    write_block_rules(tmp_path, {"phantom-import": {"active": True}})

    reads = {"n": 0}
    real_read = Path.read_text

    def _counting_read(self, *a, **k):
        if self.name == "enforcement.json":
            reads["n"] += 1
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _counting_read)
    for _ in range(5):
        assert load_block_rules(tmp_path) == {"phantom-import": {"active": True}}
    # Only the first call hits disk; the rest are served from cache.
    assert reads["n"] == 1


def test_load_block_rules_cache_invalidates_on_change(tmp_path):
    import chameleon_mcp.enforcement_calibration as ec

    ec._clear_block_rules_cache()
    write_block_rules(tmp_path, {"phantom-import": {"active": True}})
    assert load_block_rules(tmp_path)["phantom-import"]["active"] is True
    # Rewrite with different content; write_block_rules bumps mtime via rename.
    write_block_rules(tmp_path, {"phantom-import": {"active": False}})
    assert load_block_rules(tmp_path)["phantom-import"]["active"] is False


def test_load_block_rules_rejects_oversized_file(tmp_path):
    import chameleon_mcp.enforcement_calibration as ec

    ec._clear_block_rules_cache()
    # A committed profile is attacker-controlled; an absurdly large enforcement.json
    # must not be slurped into memory. Fail-open: treat as no rules.
    oversized = '{"block_rules": {"phantom-import": {"active": true, "pad": "'
    oversized += "x" * (ec._MAX_ENFORCEMENT_BYTES + 1)
    oversized += '"}}}'
    (tmp_path / "enforcement.json").write_text(oversized, encoding="utf-8")
    assert load_block_rules(tmp_path) == {}
    assert active_block_rules(tmp_path) == set()


def test_active_block_rules_filters_to_block_eligible(tmp_path):
    import chameleon_mcp.enforcement_calibration as ec

    ec._clear_block_rules_cache()
    # A poisoned enforcement.json marks a rule that is not block-eligible "active".
    # active_block_rules must drop it so it can never reach the block gate.
    write_block_rules(
        tmp_path,
        {
            "phantom-import": {"active": True},
            "secret-detected-in-content": {"active": True},
            "made-up-rule": {"active": True},
        },
    )
    assert active_block_rules(tmp_path) == {"phantom-import"}
