import json
from pathlib import Path

from chameleon_mcp.enforcement_calibration import (
    SECURITY_BLOCK_RULES,
    active_block_rules,
    calibrate_block_rules,
    fp_demoted_rules,
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
    assert active_block_rules(tmp_path) == {"phantom-import"} | SECURITY_BLOCK_RULES


def test_fp_demoted_rules(tmp_path: Path):
    # active:false AND flagged>0 = measured FP on conforming committed code -> demote
    # tone to advisory. flagged==0 (never mis-fired / inert) is NOT demoted, an
    # active rule is NOT demoted, and a security rule is never demoted.
    write_block_rules(
        tmp_path,
        {
            "inheritance-convention-violation": {"active": False, "flagged": 11, "fp_rate": 0.036},
            "jsx-presence-mismatch": {"active": False, "flagged": 0, "inert_reason": "no-signal"},
            "import-preference-violation": {"active": True, "flagged": 0, "fp_rate": 0.0},
            "eval-call": {"active": False, "flagged": 5, "exempt_reason": "security-rule"},
        },
    )
    demoted = fp_demoted_rules(tmp_path)
    assert "inheritance-convention-violation" in demoted
    assert "jsx-presence-mismatch" not in demoted  # never mis-fired
    assert "import-preference-violation" not in demoted  # active
    assert not (demoted & SECURITY_BLOCK_RULES)  # security never demoted


def test_fp_demoted_rules_missing_file(tmp_path: Path):
    assert fp_demoted_rules(tmp_path) == frozenset()


def test_missing_file_keeps_only_security_rules(tmp_path: Path):
    # No enforcement.json: every MEASURED rule fails open to inactive, but the
    # calibration-exempt security rules stay active — a fresh or legacy profile
    # must not lose the credential/eval deny.
    assert load_block_rules(tmp_path) == {}
    assert active_block_rules(tmp_path) == SECURITY_BLOCK_RULES


def test_corrupt_file_keeps_only_security_rules(tmp_path: Path):
    # A torn/tampered artifact must not disarm the security rules either: the
    # exemption is read-time, so no enforcement.json state can switch the deny
    # off. (config.json's enforcement.mode remains the deliberate, surfaced
    # operator control — off/shadow still disables blocking downstream.)
    (tmp_path / "enforcement.json").write_text("{not json", encoding="utf-8")
    assert active_block_rules(tmp_path) == SECURITY_BLOCK_RULES


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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    # 1 witness + at most 3 siblings sampled.
    assert result["phantom-import"]["sampled"] == 4


def test_no_witnesses_keeps_measured_rules_inactive_security_exempt(tmp_path):
    # Empty/unbootstrapped profile: zero evidence must NOT greenlight MEASURED
    # blockers — but the security rules are calibration-exempt (the pass runs
    # no content scans, so n carries no information about them) and must stay
    # active precisely on this kind of fresh/sparse profile.
    class _Loaded:
        canonicals = {"canonicals": {}}
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(tmp_path, _Loaded())
    assert result["phantom-import"]["sampled"] == 0
    for rule, meta in result.items():
        if rule in SECURITY_BLOCK_RULES:
            assert meta["active"] is True, rule
            assert meta["exempt_reason"] == "security-rule", rule
        else:
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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


def test_file_naming_demoted_when_committed_siblings_break_casing(tmp_path):
    # The archetype's stored file_naming is PascalCase, but every committed
    # member is a camelCase `useXxx` hook. Calibration must run the file-naming
    # check (which is gated on a file_path) against the committed files and
    # measure its true false-positive rate, so the rule is demoted -- not ship
    # active and hard-block the repo's own correctly-named files. Regression for
    # the bug where calibration called lint_conventions without a file_path, so
    # the check was silent and the rule measured a 0.0 fp_rate.
    repo = tmp_path
    (repo / "src" / "hooks").mkdir(parents=True)
    (repo / "src" / "hooks" / "useCsv.tsx").write_text(
        "export const useCsv = () => null\n", encoding="utf-8"
    )
    (repo / "src" / "hooks" / "useAlert.tsx").write_text(
        "export const useAlert = () => null\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "hook": [
                    {
                        "witness": {"path": "src/hooks/useCsv.tsx"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {
            "conventions": {
                "naming": {
                    "hook": {
                        "file_naming": {
                            "casing": "PascalCase",
                            "casing_consistency": 1.0,
                            "sample_size": 13,
                        }
                    }
                }
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["file-naming-convention-violation"]["active"] is False
    assert result["file-naming-convention-violation"]["flagged"] >= 1


def test_file_naming_active_when_committed_files_match_casing(tmp_path):
    repo = tmp_path
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "components" / "Widget.tsx").write_text(
        "export const Widget = () => null\n", encoding="utf-8"
    )
    (repo / "src" / "components" / "Button.tsx").write_text(
        "export const Button = () => null\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "component": [
                    {
                        "witness": {"path": "src/components/Widget.tsx"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {
            "conventions": {
                "naming": {
                    "component": {
                        "file_naming": {
                            "casing": "PascalCase",
                            "casing_consistency": 1.0,
                            "sample_size": 13,
                        }
                    }
                }
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["file-naming-convention-violation"]["active"] is True
    assert result["file-naming-convention-violation"]["flagged"] == 0


def test_inheritance_demoted_when_witness_breaks_dominant_base(tmp_path):
    # The witness declares a top-level class extending a base OUTSIDE the archetype's
    # dominant base, so inheritance-convention-violation fires and the rule must NOT
    # block. (A base-less class is now exempt, aligning Ruby to Python, so the
    # witness carries a wrong base to still trigger the calibration demotion.)
    repo = tmp_path
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "thing.rb").write_text(
        "class Thing < SomeUnrelatedBase\nend\n", encoding="utf-8"
    )

    class _Loaded:
        canonicals = {
            "canonicals": {
                "model": [
                    {
                        "witness": {"path": "app/thing.rb"},
                        "normative_shape": {"ast_query": {}},
                    }
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
                    {
                        "witness": {"path": "app/thing.rb"},
                        "normative_shape": {"ast_query": {}},
                    }
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
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
        t,
        "_reparse_changed_files",
        lambda _root, _paths: {changed_rel: (cluster_id, "changed")},
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
    # Measured rules are gone; the calibration-exempt security rules survive
    # even this attacker-shaped artifact (the exemption is read-time).
    assert active_block_rules(tmp_path) == SECURITY_BLOCK_RULES


def test_active_block_rules_filters_to_block_eligible(tmp_path):
    import chameleon_mcp.enforcement_calibration as ec

    ec._clear_block_rules_cache()
    # A poisoned enforcement.json marks a rule that is not block-eligible "active".
    # active_block_rules must drop it so it can never reach the block gate, while
    # keeping the genuinely block-eligible rules it also marked active.
    write_block_rules(
        tmp_path,
        {
            "phantom-import": {"active": True},
            "secret-detected-in-content": {"active": True},
            "made-up-rule": {"active": True},
        },
    )
    assert active_block_rules(tmp_path) == {"phantom-import"} | SECURITY_BLOCK_RULES


def test_language_gating_demotes_ts_only_rules_on_ruby_profile(tmp_path):
    # A Ruby profile has no signal source for jsx-presence-mismatch: its 0.0
    # fp_rate is vacuous, so calibration must not certify it active (the
    # gitlabhq QA campaign shipped it "active" on a Ruby-only repo this way).
    repo = tmp_path
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "users_finder.rb").write_text(
        "class UsersFinder\n  def execute\n  end\nend\n", encoding="utf-8"
    )

    class _Loaded:
        profile = {"language": "ruby"}
        canonicals = {
            "canonicals": {
                "finder": [
                    {
                        "witness": {"path": "app/users_finder.rb"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["jsx-presence-mismatch"]["active"] is False
    assert result["jsx-presence-mismatch"]["inert_reason"] == "no-signal-for-language"
    # Empty conventions: like naming below, inheritance-convention-violation's
    # 0.0 fp_rate is vacuous (no dominant_base to compare against), so it is inert
    # for missing data, not certified active.
    assert result["inheritance-convention-violation"]["active"] is False
    assert result["inheritance-convention-violation"]["inert_reason"] == "missing-convention-data"
    # This profile carries no naming sub-conventions at all, so the rule is
    # inert for missing data (not for language): the vacuous 0.0 must not
    # certify it active either.
    assert result["naming-convention-violation"]["active"] is False
    assert result["naming-convention-violation"]["inert_reason"] == "missing-convention-data"


def test_language_gating_demotes_ruby_only_rules_on_ts_profile(tmp_path):
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    class _Loaded:
        profile = {"language": "typescript"}
        canonicals = {
            "canonicals": {
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    assert result["inheritance-convention-violation"]["active"] is False
    assert result["inheritance-convention-violation"]["inert_reason"] == "no-signal-for-language"
    assert result["jsx-presence-mismatch"]["active"] is True


def test_language_gating_fails_open_when_language_unknown(tmp_path):
    # Legacy profiles carry no `language` key; gating needs positive knowledge.
    repo = tmp_path
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    class _Loaded:
        canonicals = {
            "canonicals": {
                "util": [
                    {
                        "witness": {"path": "src/a.ts"},
                        "normative_shape": {"ast_query": {}},
                    }
                ]
            }
        }
        conventions = {"conventions": {}}
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    # jsx-presence-mismatch (TS-only) is NOT language-demoted when the language is
    # unknown: gating fails open on missing positive knowledge.
    assert result["jsx-presence-mismatch"]["active"] is True
    # inheritance-convention-violation is not language-demoted either, but with an
    # empty inheritance map it is inert for MISSING SIGNAL (not language) -- the
    # same vacuous-0.0 gate naming uses.
    assert result["inheritance-convention-violation"]["active"] is False
    assert result["inheritance-convention-violation"]["inert_reason"] == "missing-convention-data"


def test_get_status_surfaces_inert_reason(tmp_path, monkeypatch):
    # A rule demoted for language capability must carry its reason through
    # get_status, so /chameleon-status can say WHY it is inactive.
    import json

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "ruby"}))
    # Core trio: a real committed profile always carries these (bootstrap writes
    # them atomically); get_status reports corrupt on a missing core artifact.
    (cham / "archetypes.json").write_text(json.dumps({"generation": 1, "archetypes": {}}))
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "COMMITTED").touch()
    (cham / "enforcement.json").write_text(
        json.dumps(
            {
                "block_rules": {
                    "eval-call": {
                        "active": True,
                        "fp_rate": 0.0,
                        "sampled": 10,
                        "flagged": 0,
                    },
                    "jsx-presence-mismatch": {
                        "active": False,
                        "fp_rate": 0.0,
                        "sampled": 10,
                        "flagged": 0,
                        "inert_reason": "no-signal-for-language",
                    },
                    "file-naming-convention-violation": {
                        "active": False,
                        "fp_rate": 0.03,
                        "sampled": 10,
                        "flagged": 3,
                    },
                }
            }
        )
    )

    from chameleon_mcp.profile.trust import grant_trust
    from chameleon_mcp.tools import _compute_repo_id, get_status

    grant_trust(_compute_repo_id(repo), cham)
    data = get_status(str(repo))["data"]["enforcement"]
    demoted = {d["rule"]: d for d in data["demoted"]}
    assert demoted["jsx-presence-mismatch"]["inert_reason"] == "no-signal-for-language"
    # Measured demotions carry no reason key (they were measured, not inert).
    assert "inert_reason" not in demoted["file-naming-convention-violation"]
    assert "eval-call" in data["active"]


def test_stale_active_rule_gated_at_read_time(tmp_path):
    # A pre-language-gate engine wrote enforcement.json with jsx active=True on
    # a Ruby profile (the gate only ran at calibration time, so the stale
    # verdict survived an engine upgrade until the first refresh). The reader
    # must not surface — or act on — a rule that cannot fire for this language.
    from chameleon_mcp.enforcement_calibration import (
        _clear_block_rules_cache,
        active_block_rules,
        rule_inert_for_language,
    )

    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(json.dumps({"language": "ruby"}))
    (cham / "enforcement.json").write_text(
        json.dumps(
            {
                "block_rules": {
                    "jsx-presence-mismatch": {"active": True, "fp_rate": 0.0},
                    "naming-convention-violation": {"active": True, "fp_rate": 0.0},
                    "eval-call": {"active": True, "fp_rate": 0.0},
                }
            }
        )
    )
    _clear_block_rules_cache()

    active = active_block_rules(cham)
    assert "jsx-presence-mismatch" not in active
    # Rules with signal for ruby keep their measured verdict.
    assert "naming-convention-violation" in active
    assert "eval-call" in active
    assert rule_inert_for_language("jsx-presence-mismatch", cham)
    assert not rule_inert_for_language("eval-call", cham)


def test_read_time_gate_fails_open_without_language(tmp_path):
    # Gate only on POSITIVE knowledge: a legacy profile with no language key
    # keeps the measured behavior rather than demoting every scoped rule.
    from chameleon_mcp.enforcement_calibration import (
        _clear_block_rules_cache,
        active_block_rules,
    )

    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(json.dumps({"generation": 1}))
    (cham / "enforcement.json").write_text(
        json.dumps({"block_rules": {"jsx-presence-mismatch": {"active": True, "fp_rate": 0.0}}})
    )
    _clear_block_rules_cache()

    assert "jsx-presence-mismatch" in active_block_rules(cham)


def test_get_status_demotes_stale_active_rule_with_reason(tmp_path, monkeypatch):
    # The status display applies the same read-time gate, so an un-refreshed
    # profile shows the inert rule as demoted instead of listing it active.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "ruby"}))
    # Core trio: a real committed profile always carries these (bootstrap writes
    # them atomically); get_status reports corrupt on a missing core artifact.
    (cham / "archetypes.json").write_text(json.dumps({"generation": 1, "archetypes": {}}))
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "COMMITTED").touch()
    (cham / "enforcement.json").write_text(
        json.dumps(
            {
                "block_rules": {
                    "jsx-presence-mismatch": {"active": True, "fp_rate": 0.0},
                    "eval-call": {"active": True, "fp_rate": 0.0},
                }
            }
        )
    )

    from chameleon_mcp.enforcement_calibration import _clear_block_rules_cache
    from chameleon_mcp.profile.trust import grant_trust
    from chameleon_mcp.tools import _compute_repo_id, get_status

    _clear_block_rules_cache()
    grant_trust(_compute_repo_id(repo), cham)
    data = get_status(str(repo))["data"]["enforcement"]
    assert "jsx-presence-mismatch" not in data["active"]
    demoted = {d["rule"]: d for d in data["demoted"]}
    assert demoted["jsx-presence-mismatch"]["inert_reason"] == "no-signal-for-language"
    assert "eval-call" in data["active"]


# --------------------------------------------------------------------------
# qa25 P2 — active-but-inert on a stale profile: enforcement.json certified
# naming-convention-violation active (vacuous 0.0 fp_rate) while the profile's
# conventions carry only file_naming, so the rule could never fire. The
# missing-signal gate applies the same stale-verdict treatment as the language
# gate, one level deeper.


def _stale_naming_profile(tmp_path, *, naming_conventions):
    import json

    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "ruby"}))
    # Core trio: a real committed profile always carries these (bootstrap writes
    # them atomically); get_status reports corrupt on a missing core artifact.
    (cham / "archetypes.json").write_text(json.dumps({"generation": 1, "archetypes": {}}))
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n")
    (cham / "conventions.json").write_text(
        json.dumps(
            {
                "schema_version": 8,
                "generation": 1,
                "conventions": {"naming": naming_conventions},
            }
        )
    )
    (cham / "enforcement.json").write_text(
        json.dumps(
            {
                "block_rules": {
                    "naming-convention-violation": {
                        "active": True,
                        "fp_rate": 0.0,
                        "sampled": 80,
                        "flagged": 0,
                    },
                }
            }
        )
    )
    return repo, cham


def test_naming_active_but_inert_gated_at_read_time(tmp_path):
    from chameleon_mcp.enforcement_calibration import (
        active_block_rules,
        rule_inert_missing_signal,
    )

    _repo, cham = _stale_naming_profile(
        tmp_path,
        naming_conventions={
            "service": {"file_naming": {"casing": "snake_case", "casing_consistency": 1.0}},
            "model": {"file_naming": {"casing": "snake_case", "casing_consistency": 1.0}},
        },
    )
    assert rule_inert_missing_signal("naming-convention-violation", cham) is True
    assert "naming-convention-violation" not in active_block_rules(cham)


def test_naming_gate_lifts_once_casing_conventions_derived(tmp_path):
    from chameleon_mcp.enforcement_calibration import (
        active_block_rules,
        rule_inert_missing_signal,
    )

    _repo, cham = _stale_naming_profile(
        tmp_path,
        naming_conventions={
            "model": {
                "file_naming": {"casing": "snake_case", "casing_consistency": 1.0},
                "method_casing": {"pattern": "snake_case", "consistency": 0.98},
            },
        },
    )
    assert rule_inert_missing_signal("naming-convention-violation", cham) is False
    assert "naming-convention-violation" in active_block_rules(cham)


def test_naming_gate_keeps_measured_behavior_without_conventions_file(tmp_path):
    # Positive knowledge only: no conventions.json (or unreadable) must not
    # demote the rule the calibration measured.
    from chameleon_mcp.enforcement_calibration import rule_inert_missing_signal

    _repo, cham = _stale_naming_profile(tmp_path, naming_conventions={})
    (cham / "conventions.json").unlink()
    assert rule_inert_missing_signal("naming-convention-violation", cham) is False


def test_get_status_reports_missing_convention_data(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo, cham = _stale_naming_profile(
        tmp_path,
        naming_conventions={
            "service": {"file_naming": {"casing": "snake_case", "casing_consistency": 1.0}},
        },
    )

    from chameleon_mcp.profile.trust import grant_trust
    from chameleon_mcp.tools import _compute_repo_id, get_status

    grant_trust(_compute_repo_id(repo), cham)
    data = get_status(str(repo))["data"]["enforcement"]
    assert "naming-convention-violation" not in data["active"]
    demoted = {d["rule"]: d for d in data["demoted"]}
    assert demoted["naming-convention-violation"]["inert_reason"] == "missing-convention-data"
    _ = _json  # keep the import shape parallel with the sibling test


def test_calibration_writes_missing_convention_inert_reason(tmp_path):
    # Write-time: a vacuous 0.0 fp_rate from a rule whose driving data is
    # absent must not certify it active.
    from chameleon_mcp.enforcement_calibration import calibrate_block_rules

    repo = tmp_path / "repo"
    (repo / "app" / "models").mkdir(parents=True)
    (repo / "app" / "models" / "user.rb").write_text("class User < ApplicationRecord\nend\n")

    class _Loaded:
        profile = {"language": "ruby"}
        archetypes = {
            "archetypes": {
                "model": {
                    "witnesses": ["app/models/user.rb"],
                    "paths_pattern": "app/models",
                }
            }
        }
        conventions = {
            "conventions": {
                "naming": {
                    "model": {
                        "file_naming": {
                            "casing": "snake_case",
                            "casing_consistency": 1.0,
                        }
                    }
                }
            }
        }
        rules = {}

    result = calibrate_block_rules(repo, _Loaded())
    entry = result["naming-convention-violation"]
    assert entry["active"] is False
    assert entry["inert_reason"] == "missing-convention-data"


# --------------------------------------------------------------------------
# enforcement.calibration: tools._calibrate_block_rules_for_repo reads
# cfg.enforcement.calibration and either skips apply_override_feedback_demotion
# entirely (auto_demote=false) or forwards the configured thresholds to it,
# instead of always driving it off the global _thresholds.py env defaults.
# --------------------------------------------------------------------------


def test_calibration_auto_demote_false_skips_demotion(tmp_path, monkeypatch):
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools as t
    from chameleon_mcp.enforcement_calibration import load_block_rules

    repo_root = (tmp_path / "repo").resolve()
    profile_dir = repo_root / ".chameleon"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.json").write_text(
        json.dumps({"enforcement": {"calibration": {"auto_demote": False}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "chameleon_mcp.profile.loader.load_profile_dir", lambda profile_dir: object()
    )
    monkeypatch.setattr(
        ec, "calibrate_block_rules", lambda root, loaded: {"phantom-import": {"active": True}}
    )
    demote_calls: list = []
    monkeypatch.setattr(
        ec,
        "apply_override_feedback_demotion",
        lambda *a, **k: demote_calls.append((a, k)) or a[0],
    )
    # A rate high enough (and with enough events/sessions) to demote under the
    # DEFAULT thresholds -- proving the skip is due to auto_demote=false, not
    # a thin-evidence floor.
    monkeypatch.setattr(
        t,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "phantom-import": {"rate": 0.95, "events": 50, "distinct_sessions": 8}
        },
    )

    t._calibrate_block_rules_for_repo(repo_root)

    assert demote_calls == []
    verdicts = load_block_rules(profile_dir)
    assert verdicts["phantom-import"]["active"] is True
    assert "demotion_proposed" not in verdicts["phantom-import"]


def test_calibration_custom_thresholds_forwarded(tmp_path, monkeypatch):
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools as t

    repo_root = (tmp_path / "repo").resolve()
    profile_dir = repo_root / ".chameleon"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.json").write_text(
        json.dumps(
            {
                "enforcement": {
                    "calibration": {
                        "override_rate_threshold": 0.9,
                        "min_events": 42,
                        "min_distinct_sessions": 7,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "chameleon_mcp.profile.loader.load_profile_dir", lambda profile_dir: object()
    )
    monkeypatch.setattr(
        ec, "calibrate_block_rules", lambda root, loaded: {"phantom-import": {"active": True}}
    )
    calls: list = []

    def _fake_demote(verdicts, override_rates, *, threshold, min_events, min_distinct_sessions):
        calls.append(
            {
                "threshold": threshold,
                "min_events": min_events,
                "min_distinct_sessions": min_distinct_sessions,
            }
        )
        return verdicts

    monkeypatch.setattr(ec, "apply_override_feedback_demotion", _fake_demote)
    monkeypatch.setattr(
        t,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "phantom-import": {"rate": 0.95, "events": 50, "distinct_sessions": 8}
        },
    )

    t._calibrate_block_rules_for_repo(repo_root)

    assert calls == [{"threshold": 0.9, "min_events": 42, "min_distinct_sessions": 7}]


def test_calibration_defaults_match_thresholds_when_no_config(tmp_path, monkeypatch):
    # No config.json at all -- the caller must forward the SAME values the
    # global _thresholds.py defaults produce, so behavior is byte-identical
    # to before enforcement.calibration existed.
    from chameleon_mcp import enforcement_calibration as ec
    from chameleon_mcp import tools as t

    repo_root = (tmp_path / "repo").resolve()
    profile_dir = repo_root / ".chameleon"
    profile_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "chameleon_mcp.profile.loader.load_profile_dir", lambda profile_dir: object()
    )
    monkeypatch.setattr(
        ec, "calibrate_block_rules", lambda root, loaded: {"phantom-import": {"active": True}}
    )
    calls: list = []

    def _fake_demote(verdicts, override_rates, *, threshold, min_events, min_distinct_sessions):
        calls.append(
            {
                "threshold": threshold,
                "min_events": min_events,
                "min_distinct_sessions": min_distinct_sessions,
            }
        )
        return verdicts

    monkeypatch.setattr(ec, "apply_override_feedback_demotion", _fake_demote)
    monkeypatch.setattr(
        t,
        "_override_rates_for_demotion",
        lambda repo_id, window_days=None: {
            "phantom-import": {"rate": 0.95, "events": 50, "distinct_sessions": 8}
        },
    )

    t._calibrate_block_rules_for_repo(repo_root)

    assert calls == [{"threshold": 0.5, "min_events": 5, "min_distinct_sessions": 2}]
