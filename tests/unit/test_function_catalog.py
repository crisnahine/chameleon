"""Unit tests for the function catalog and the duplication-candidate prefilter.

Covers the three pure halves of cross-file duplication detection:
  - name tokenization + signature shape reduction
  - build_function_catalog / load_function_catalog round-trip + fail-open
  - select_candidates prefilter ranking, gating, and exclusions

The LLM semantic-equivalence judging is the CALLER's job; these tests only
verify the catalog records correctly and the prefilter narrows to the right
candidates without fabricating any.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp import function_catalog as fc


@dataclass
class _FakeParsed:
    path: Path
    extras: dict


def _sig(name, params, kind="function"):
    return {"name": name, "kind": kind, "params": params}


def _pos(n, optional=False):
    return [{"name": f"p{i}", "optional": optional, "kind": "positional"} for i in range(n)]


class TestNameTokens:
    def test_camel_case_split(self):
        assert fc.name_tokens("toDisplayDate") == frozenset({"display", "date"})

    def test_snake_case_split(self):
        assert fc.name_tokens("format_date") == frozenset({"format", "date"})

    def test_stopwords_stripped(self):
        # get/to/is are generic verbs carrying no reuse signal.
        assert fc.name_tokens("getUserData") == frozenset({"user"})

    def test_single_char_tokens_dropped(self):
        assert fc.name_tokens("aB") == frozenset()

    def test_empty_and_nonstr(self):
        assert fc.name_tokens("") == frozenset()
        assert fc.name_tokens(None) == frozenset()

    def test_acronym_boundary(self):
        # PascalCase with a trailing acronym splits on the casing boundary.
        assert "html" in {t for t in fc.name_tokens("parseHTMLString")}


class TestSignatureShape:
    def test_all_required(self):
        assert fc._signature_shape(_pos(3)) == (3, 3)

    def test_mixed_optional(self):
        params = _pos(2) + _pos(1, optional=True)
        assert fc._signature_shape(params) == (3, 2)

    def test_non_list_is_zero(self):
        assert fc._signature_shape(None) == (0, 0)
        assert fc._signature_shape("x") == (0, 0)


class TestBuildCatalog:
    def test_round_trip(self, tmp_path):
        files = [
            _FakeParsed(
                path=tmp_path / "src" / "fmt.ts",
                extras={"callable_signatures": [_sig("formatDate", _pos(1))]},
            ),
            _FakeParsed(
                path=tmp_path / "src" / "money.ts",
                extras={"callable_signatures": [_sig("sumLineItems", _pos(1))]},
            ),
        ]
        (tmp_path / "src").mkdir()
        payload = fc.build_function_catalog(files, tmp_path)
        assert payload["schema_version"] == fc.SCHEMA_VERSION
        assert set(payload["files"]) == {"src/fmt.ts", "src/money.ts"}
        assert payload["files"]["src/fmt.ts"][0]["name"] == "formatDate"

    def test_files_without_signatures_omitted(self, tmp_path):
        files = [_FakeParsed(path=tmp_path / "empty.ts", extras={})]
        payload = fc.build_function_catalog(files, tmp_path)
        assert payload["files"] == {}

    def test_overload_dedup_within_file(self, tmp_path):
        files = [
            _FakeParsed(
                path=tmp_path / "a.ts",
                extras={
                    "callable_signatures": [
                        _sig("overloaded", _pos(1)),
                        _sig("overloaded", _pos(1)),  # same shape -> deduped
                    ]
                },
            )
        ]
        payload = fc.build_function_catalog(files, tmp_path)
        assert len(payload["files"]["a.ts"]) == 1

    def test_per_file_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DUPLICATION_CATALOG_MAX_FNS_PER_FILE", "2")
        sigs = [_sig(f"fn{i}", _pos(i % 4)) for i in range(10)]
        files = [_FakeParsed(path=tmp_path / "a.ts", extras={"callable_signatures": sigs})]
        payload = fc.build_function_catalog(files, tmp_path)
        assert len(payload["files"]["a.ts"]) == 2

    def test_file_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DUPLICATION_CATALOG_MAX_FILES", "1")
        files = [
            _FakeParsed(path=tmp_path / "a.ts", extras={"callable_signatures": [_sig("a", [])]}),
            _FakeParsed(path=tmp_path / "b.ts", extras={"callable_signatures": [_sig("b", [])]}),
        ]
        payload = fc.build_function_catalog(files, tmp_path)
        # sorted-path order keeps the deterministic first file.
        assert set(payload["files"]) == {"a.ts"}


class TestLoadCatalog:
    def _write(self, repo: Path, payload: dict):
        cham = repo / ".chameleon"
        cham.mkdir(parents=True, exist_ok=True)
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_is_none(self, tmp_path):
        assert fc.load_function_catalog(tmp_path) is None

    def test_none_repo_is_none(self):
        assert fc.load_function_catalog(None) is None

    def test_corrupt_is_none(self, tmp_path):
        cham = tmp_path / ".chameleon"
        cham.mkdir()
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
        assert fc.load_function_catalog(tmp_path) is None

    def test_future_schema_is_none(self, tmp_path):
        self._write(tmp_path, {"schema_version": fc.SCHEMA_VERSION + 1, "files": {}})
        assert fc.load_function_catalog(tmp_path) is None

    def test_load_round_trip(self, tmp_path):
        self._write(
            tmp_path,
            {
                "schema_version": fc.SCHEMA_VERSION,
                "files": {
                    "src/fmt.ts": [
                        {"name": "formatDate", "kind": "function", "arity": 1, "required": 1}
                    ]
                },
            },
        )
        cat = fc.load_function_catalog(tmp_path)
        assert cat is not None and len(cat) == 1
        f = cat.functions[0]
        assert f.name == "formatDate" and f.file == "src/fmt.ts"
        assert f.arity == 1 and f.required == 1
        assert f.tokens == frozenset({"format", "date"})

    def test_malformed_rows_skipped(self, tmp_path):
        self._write(
            tmp_path,
            {
                "schema_version": fc.SCHEMA_VERSION,
                "files": {
                    "a.ts": ["notadict", {"name": ""}, {"name": "ok", "arity": 0, "required": 0}]
                },
            },
        )
        cat = fc.load_function_catalog(tmp_path)
        assert cat is not None
        assert {f.name for f in cat.functions} == {"ok"}


def _cat(rows: list[tuple[str, int, int, str]]) -> fc.FunctionCatalog:
    return fc.FunctionCatalog(
        [
            fc.CatalogedFunction(
                name=n, kind="function", file=f, arity=a, required=r, tokens=fc.name_tokens(n)
            )
            for (n, a, r, f) in rows
        ]
    )


class TestSelectCandidates:
    def test_renamed_reimplementation_surfaces(self):
        catalog = _cat([("formatDate", 1, 1, "src/fmt.ts")])
        new = [fc.NewFunction(name="toDisplayDate", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new)
        assert len(out) == 1
        cands = out[0]["candidates"]
        assert cands[0]["name"] == "formatDate"
        assert cands[0]["shared_tokens"] == ["date"]

    def test_no_token_overlap_skipped(self):
        catalog = _cat([("sumLineItems", 1, 1, "src/money.ts")])
        new = [fc.NewFunction(name="toDisplayDate", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new) == []

    def test_exact_name_excluded(self):
        # Exact-name collision is the flat key_exports signal's job.
        catalog = _cat([("formatDate", 1, 1, "other.ts")])
        new = [fc.NewFunction(name="formatDate", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new) == []

    def test_self_file_excluded(self):
        catalog = _cat([("formatDate", 1, 1, "src/fmt.ts")])
        new = [fc.NewFunction(name="toDisplayDate", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new, exclude_file="src/fmt.ts") == []

    def test_arity_far_apart_skipped(self):
        # Same domain token but a 0-arg getter vs a 3-arg builder is not the same
        # intent.
        catalog = _cat([("buildDateRange", 3, 3, "x.ts")])
        new = [fc.NewFunction(name="currentDate", kind="function", arity=0, required=0)]
        assert fc.select_candidates(catalog, new) == []

    def test_arity_off_by_one_allowed(self):
        catalog = _cat([("formatDate", 2, 2, "x.ts")])
        new = [fc.NewFunction(name="renderDate", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new)
        assert out and out[0]["candidates"][0]["name"] == "formatDate"

    def test_ranking_by_overlap(self):
        catalog = _cat(
            [
                ("formatDate", 1, 1, "a.ts"),  # shares {date}
                ("formatUserDate", 1, 1, "b.ts"),  # shares {user, date}
            ]
        )
        new = [fc.NewFunction(name="renderUserDate", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new)
        names = [c["name"] for c in out[0]["candidates"]]
        assert names[0] == "formatUserDate"  # higher overlap ranks first

    def test_candidate_cap(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DUPLICATION_MAX_CANDIDATES_PER_FN", "2")
        verbs = ["format", "render", "show", "print", "emit", "write"]
        catalog = _cat([(f"{v}Date", 1, 1, f"f{i}.ts") for i, v in enumerate(verbs)])
        new = [fc.NewFunction(name="displayDate", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new)
        assert len(out[0]["candidates"]) == 2

    def test_no_domain_token_new_fn_skipped(self):
        # A new function whose name is all stopwords yields no query tokens.
        catalog = _cat([("formatDate", 1, 1, "a.ts")])
        new = [fc.NewFunction(name="getData", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new) == []

    def test_connector_token_only_match_is_noise(self):
        # `shuffleDeckInPlace` and `updateAccountInCache` share only the
        # connector token `in`, which carries no reuse signal. It must not pair
        # them; matching on `in` crowded the real counterpart out of the cap.
        catalog = _cat([("updateAccountInCache", 2, 2, "a.ts")])
        new = [fc.NewFunction(name="shuffleDeckInPlace", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new) == []

    def test_jaccard_tiebreak_sinks_longer_name_noise(self):
        # Two candidates share the single common token `name`. The one whose
        # whole token set is closer to the query (a 2-token name) must rank above
        # a longer multi-token name, so the cap keeps the better lead.
        catalog = _cat(
            [
                ("fieldReceivingAccountName", 1, 1, "noise.ts"),  # 4 tokens, low jaccard
                ("getFullName", 1, 1, "user.ts"),  # 2 tokens, higher jaccard
            ]
        )
        new = [fc.NewFunction(name="buildDisplayName", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new)
        names = [c["name"] for c in out[0]["candidates"]]
        assert names[0] == "getFullName"
