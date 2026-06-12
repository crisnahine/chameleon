"""Duplication scorer with canned catalog and parse results."""

from __future__ import annotations

from tests.effectiveness.scorers import duplication
from tests.effectiveness.tests.test_scorer_base import _ctx

from chameleon_mcp.function_catalog import CatalogedFunction, FunctionCatalog, ParsedFn

CATALOG = FunctionCatalog(
    [
        CatalogedFunction(
            name="slugify",
            kind="function",
            file="src/utils/slugify.ts",
            arity=1,
            required=1,
            tokens=frozenset({"slugify"}),
            body_hash="aaaa000011112222",
            body_hash_pnorm="bbbb000011112222",
        ),
        CatalogedFunction(
            name="clamp",
            kind="function",
            file="src/utils/clamp.ts",
            arity=3,
            required=3,
            tokens=frozenset({"clamp"}),
            body_hash="cccc000011112222",
            body_hash_pnorm="dddd000011112222",
        ),
    ]
)


def _pf(name, body_hash, pnorm):
    return ParsedFn(
        name=name,
        kind="function",
        arity=1,
        required=1,
        start_line=1,
        body_hash=body_hash,
        body_hash_pnorm=pnorm,
        excerpt="...",
        end_line=4,
    )


def _make_ctx(tmp_path, monkeypatch, parsed_by_file, catalog=CATALOG):
    (tmp_path / "src" / "components").mkdir(parents=True)
    for rel in parsed_by_file:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("// changed\n")
    monkeypatch.setattr(duplication, "_load_catalog", lambda root: catalog)
    monkeypatch.setattr(
        duplication,
        "_parse",
        lambda root, path: parsed_by_file.get(str(path).replace(str(tmp_path) + "/", ""), []),
    )
    ctx = _ctx(tmp_path)
    ctx.changed_files = sorted(parsed_by_file)
    ctx.pack.duplication_targets[ctx.task.task_id] = {
        "existing_name": "slugify",
        "existing_file": "src/utils/slugify.ts",
        "needle": "slugify",
    }
    return ctx


def test_body_hash_clone_counted_as_duplicate(tmp_path, monkeypatch):
    ctx = _make_ctx(
        tmp_path,
        monkeypatch,
        {"src/components/Card.tsx": [_pf("makeSlug", "aaaa000011112222", "zzzz")]},
    )
    out = duplication.score(ctx)
    assert out["added_functions"] == 1
    assert out["body_hash_duplicates"] == 1
    assert out["reuse_credit"] is False  # changed file never references slugify


def test_reuse_credit_when_existing_helper_referenced(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/components/Card.tsx": []})
    (tmp_path / "src/components/Card.tsx").write_text(
        'import { slugify } from "../utils/slugify";\nexport const x = slugify("A");\n'
    )
    out = duplication.score(ctx)
    assert out["added_functions"] == 0
    assert out["body_hash_duplicates"] == 0
    assert out["reuse_credit"] is True


def test_reuse_credit_only_from_source_files(tmp_path, monkeypatch):
    # A harness artifact or doc that mentions the helper must never mint
    # reuse credit; only source files count.
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/components/Card.tsx": []})
    (tmp_path / "notes.md").write_text("we should call slugify here\n")
    ctx.changed_files = sorted(ctx.changed_files + ["notes.md"])
    out = duplication.score(ctx)
    assert out["reuse_credit"] is False


def test_existing_catalog_function_not_counted_as_added(tmp_path, monkeypatch):
    ctx = _make_ctx(
        tmp_path,
        monkeypatch,
        {"src/utils/clamp.ts": [_pf("clamp", "cccc000011112222", "dddd000011112222")]},
    )
    del ctx.pack.duplication_targets[ctx.task.task_id]
    out = duplication.score(ctx)
    assert out["added_functions"] == 0
    assert out["body_hash_duplicates"] == 0


def test_missing_catalog_is_unscored(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/components/Card.tsx": []}, catalog=None)
    out = duplication.score(ctx)
    assert set(out) == {"unscored"}
    assert "catalog" in out["unscored"]


def test_dead_extractor_probe_is_unscored(tmp_path, monkeypatch):
    # The probe file (the catalog's first entry) exists on disk but parses to
    # nothing: the extractor is unavailable and a zero would be fabricated.
    ctx = _make_ctx(tmp_path, monkeypatch, {"src/components/Card.tsx": []})
    probe = tmp_path / "src/utils/slugify.ts"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("export function slugify(s: string) { return s; }\n")
    out = duplication.score(ctx)
    assert set(out) == {"unscored"}
    assert "parse unavailable" in out["unscored"]
