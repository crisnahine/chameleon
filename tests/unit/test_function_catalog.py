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

    def test_test_file_candidate_dropped_for_production_review(self):
        # A production function cannot reuse a test helper, and the candidate
        # cap means a token-overlap test match evicts a real production lead.
        catalog = _cat([("formatDate", 1, 1, "tests/utils/date_helpers.py")])
        new = [fc.NewFunction(name="toDisplayDate", kind="function", arity=1, required=1)]
        assert fc.select_candidates(catalog, new, exclude_file="src/fmt.ts") == []

    def test_test_file_candidate_kept_for_test_review(self):
        # A test file under review may genuinely re-implement a test helper.
        catalog = _cat([("formatDate", 1, 1, "tests/utils/date_helpers.py")])
        new = [fc.NewFunction(name="toDisplayDate", kind="function", arity=1, required=1)]
        out = fc.select_candidates(catalog, new, exclude_file="tests/test_render.py")
        assert out and out[0]["candidates"][0]["file"] == "tests/utils/date_helpers.py"

    def test_body_match_test_candidate_survives_production_review(self):
        # A byte-identical clone copy-pasted from a test is still worth surfacing.
        catalog = fc.FunctionCatalog(
            functions=[
                fc.CatalogedFunction(
                    name="obscureHelper",
                    file="tests/utils/clone_source.py",
                    kind="function",
                    arity=1,
                    required=1,
                    tokens=fc.name_tokens("obscureHelper"),
                    body_hash="abc123",
                    body_hash_pnorm=None,
                )
            ]
        )
        new = [
            fc.NewFunction(
                name="renamedThing",
                kind="function",
                arity=1,
                required=1,
                body_hash="abc123",
            )
        ]
        out = fc.select_candidates(catalog, new, exclude_file="src/fmt.ts")
        assert out and out[0]["candidates"][0]["body_match"] is True

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


RUBY_BODY = [
    "def valid_image_mimetypes",
    "  %w[image/png image/jpeg image/gif image/webp].freeze +",
    "    extra_image_mimetypes_for(current_settings)",
    "end",
]


class TestBodyHashFallback:
    """A body-exact clone with ZERO shared name tokens must still surface.

    Regression for the gitlabhq QA finding: `valid_image_mimetypes` cloned as
    `allowed_picture_content_types` returned only name-token candidates, none
    the real twin — exactly the LLM-duplication case the tool exists for.
    """

    def _ruby_file(self, tmp_path, rel, name):
        body = [f"def {name}"] + RUBY_BODY[1:]
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path, len(body)

    def test_normalized_body_hash_ignores_name_and_whitespace(self):
        a = fc.normalized_body_hash(RUBY_BODY, 1, 4)
        renamed = ["def allowed_picture_content_types"] + [
            "    " + ln.strip() for ln in RUBY_BODY[1:]
        ]
        b = fc.normalized_body_hash(renamed, 1, 4)
        assert a is not None
        assert a == b

    def test_short_bodies_carry_no_hash(self):
        assert fc.normalized_body_hash(["def a", "  1", "end"], 1, 3) is None

    def test_invalid_spans_are_none(self):
        assert fc.normalized_body_hash(RUBY_BODY, None, 4) is None
        assert fc.normalized_body_hash(RUBY_BODY, 3, 2) is None
        assert fc.normalized_body_hash(RUBY_BODY, 99, 120) is None

    def test_zero_token_overlap_clone_surfaces_via_body_hash(self, tmp_path):
        orig_path, n = self._ruby_file(tmp_path, "app/uploaders/checks.rb", "valid_image_mimetypes")
        sig = dict(_sig("valid_image_mimetypes", [], kind="method"))
        sig.update({"start_line": 1, "end_line": n})
        files = [_FakeParsed(path=orig_path, extras={"callable_signatures": [sig]})]
        payload = fc.build_function_catalog(files, tmp_path)
        row = payload["files"]["app/uploaders/checks.rb"][0]
        assert row.get("body_hash"), "catalog row must carry the body fingerprint"

        cham = tmp_path / ".chameleon"
        cham.mkdir()
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        catalog = fc.load_function_catalog(tmp_path)
        assert catalog is not None

        clone_lines = ["def allowed_picture_content_types"] + RUBY_BODY[1:]
        clone_hash = fc.normalized_body_hash(clone_lines, 1, len(clone_lines))
        nf = fc.NewFunction(
            name="allowed_picture_content_types",
            kind="method",
            arity=0,
            required=0,
            body_hash=clone_hash,
        )
        # Zero shared name tokens with the original — the name prefilter alone
        # would return nothing.
        assert not (fc.name_tokens(nf.name) & fc.name_tokens("valid_image_mimetypes"))
        matches = fc.select_candidates(catalog, [nf])
        assert matches, "body-identical clone must surface despite zero name overlap"
        cand = matches[0]["candidates"][0]
        assert cand["name"] == "valid_image_mimetypes"
        assert cand["body_match"] is True

    def test_name_only_candidates_marked_not_body_match(self, tmp_path):
        path, _ = self._ruby_file(tmp_path, "app/a.rb", "format_date")
        sig = _sig("format_date", _pos(1), kind="method")
        files = [_FakeParsed(path=path, extras={"callable_signatures": [sig]})]
        payload = fc.build_function_catalog(files, tmp_path)
        cham = tmp_path / ".chameleon"
        cham.mkdir()
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        catalog = fc.load_function_catalog(tmp_path)
        nf = fc.NewFunction(name="to_display_date", kind="method", arity=1, required=1)
        matches = fc.select_candidates(catalog, [nf])
        assert matches
        assert matches[0]["candidates"][0]["body_match"] is False


# --------------------------------------------------------------------------
# qa25 P2 — two body-clone blind spots:
#   1. a generic-verb name (run/handle/process) tokenizes to nothing and was
#      skipped before the body-hash pairing could run, so the exact Sidekiq /
#      service entry-point naming hid clones completely;
#   2. a clone whose only body difference is renamed parameters defeated the
#      exact body hash (the most common shape of a copied-and-tweaked helper).


TS_WEIGHTED_BODY = [
    "export const computeWeightedScore = (items: Array<{ value: number; weight: number }>): number => {",
    "  let total = 0",
    "  let weightSum = 0",
    "  for (const entry of items) {",
    "    total += entry.value * entry.weight",
    "    weightSum += entry.weight",
    "  }",
    "  if (weightSum === 0) {",
    "    return 0",
    "  }",
    "  return total / weightSum",
    "}",
]


def _ts_clone_lines(name: str, param: str) -> list[str]:
    head = (
        f"export const {name} = ({param}: Array<{{ value: number; weight: number }}>): number => {{"
    )
    body = [ln.replace("items", param) for ln in TS_WEIGHTED_BODY[1:]]
    return [head] + body


class TestGenericVerbNameCloneSurfaces:
    def _catalog_with(self, tmp_path, rel, lines, name, params):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sig = dict(_sig(name, params, kind="function"))
        sig.update({"start_line": 1, "end_line": len(lines)})
        files = [_FakeParsed(path=path, extras={"callable_signatures": [sig]})]
        payload = fc.build_function_catalog(files, tmp_path)
        cham = tmp_path / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        return fc.load_function_catalog(tmp_path)

    def test_stopword_only_name_pairs_on_exact_body(self, tmp_path):
        # `run` is all stopwords; before the fix the query was dropped before
        # the body-hash comparison the prefilter exists for.
        catalog = self._catalog_with(
            tmp_path,
            "app/services/payment_processor.rb",
            ["def settle_invoice_balances"] + RUBY_BODY[1:],
            "settle_invoice_balances",
            [],
        )
        clone_lines = ["def run"] + RUBY_BODY[1:]
        nf = fc.NewFunction(
            name="run",
            kind="method",
            arity=0,
            required=0,
            body_hash=fc.normalized_body_hash(clone_lines, 1, len(clone_lines)),
        )
        assert fc.name_tokens("run") == frozenset()
        matches = fc.select_candidates(catalog, [nf])
        assert matches, "stopword-named body clone must surface"
        assert matches[0]["candidates"][0]["name"] == "settle_invoice_balances"
        assert matches[0]["candidates"][0]["body_match"] is True

    def test_stopword_only_name_without_body_hash_still_skipped(self):
        catalog = _cat([("formatDate", 1, 1, "a.ts")])
        nf = fc.NewFunction(name="run", kind="function", arity=1, required=1)
        assert fc.select_candidates(catalog, [nf]) == []


class TestParamRenamedCloneSurfaces:
    def _catalog_with_ts_original(self, tmp_path):
        path = tmp_path / "src/utils/score.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(TS_WEIGHTED_BODY) + "\n", encoding="utf-8")
        sig = dict(
            _sig(
                "computeWeightedScore",
                [{"name": "items", "optional": False, "kind": "positional"}],
            )
        )
        sig.update({"start_line": 1, "end_line": len(TS_WEIGHTED_BODY)})
        files = [_FakeParsed(path=path, extras={"callable_signatures": [sig]})]
        payload = fc.build_function_catalog(files, tmp_path)
        cham = tmp_path / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / fc.FUNCTION_CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        return fc.load_function_catalog(tmp_path)

    def _new_fn(self, name: str, param: str) -> fc.NewFunction:
        lines = _ts_clone_lines(name, param)
        return fc.NewFunction(
            name=name,
            kind="function",
            arity=1,
            required=1,
            body_hash=fc.normalized_body_hash(lines, 1, len(lines)),
            body_hash_pnorm=fc.normalized_body_hash(lines, 1, len(lines), param_names=[param]),
        )

    def test_param_renamed_disjoint_name_clone_pairs(self, tmp_path):
        # The qa25 fixture shape: blendRatingTally(records) is a verbatim copy
        # of computeWeightedScore(items) with only the parameter renamed. The
        # exact hash differs (the param appears in the body); the
        # param-normalized hash must pair them.
        catalog = self._catalog_with_ts_original(tmp_path)
        nf = self._new_fn("blendRatingTally", "records")
        assert nf.body_hash != catalog.functions[0].body_hash
        matches = fc.select_candidates(catalog, [nf])
        assert matches, "param-renamed clone must surface via the pnorm hash"
        cand = matches[0]["candidates"][0]
        assert cand["name"] == "computeWeightedScore"
        assert cand["body_match"] is True

    def test_genuinely_different_body_does_not_pair(self, tmp_path):
        catalog = self._catalog_with_ts_original(tmp_path)
        lines = [
            "export const blendRatingTally = (records: Array<number>): number => {",
            "  let total = 0",
            "  for (const entry of records) {",
            "    total += entry * 2",
            "  }",
            "  if (total > 100) {",
            "    return 100",
            "  }",
            "  return total",
            "}",
        ]
        nf = fc.NewFunction(
            name="blendRatingTally",
            kind="function",
            arity=1,
            required=1,
            body_hash=fc.normalized_body_hash(lines, 1, len(lines)),
            body_hash_pnorm=fc.normalized_body_hash(lines, 1, len(lines), param_names=["records"]),
        )
        matches = fc.select_candidates(catalog, [nf])
        for m in matches:
            for cand in m["candidates"]:
                assert cand["body_match"] is False

    def test_param_names_skips_non_identifier_markers(self):
        assert fc._param_names(
            [
                {"name": "items", "optional": False, "kind": "positional"},
                {"name": "{}", "optional": False, "kind": "destructured"},
                {"name": "_", "optional": False, "kind": "positional"},
                {"name": "*", "optional": True, "kind": "rest"},
                "not-a-dict",
            ]
        ) == ["items", "", "", "", ""]
        assert fc._param_names(None) == []
