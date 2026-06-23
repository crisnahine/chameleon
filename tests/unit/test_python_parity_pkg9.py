"""PKG-9: Python framework awareness (cochange + naming fallback)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chameleon_mcp.bootstrap.naming import _base_name_for
from chameleon_mcp.cochange import _normalize_language, changeset_completeness_items


def test_normalize_language_python():
    assert _normalize_language("python") == "python"
    assert _normalize_language("ruby") == "ruby"
    assert _normalize_language("go") is None


def _rule_ids(repo, new_rels, edited_rels=()):
    new_abs = {str(repo / r) for r in new_rels}
    edited_abs = {str(repo / r) for r in edited_rels} | new_abs
    items = changeset_completeness_items(
        repo_root=repo,
        new_files_abs=new_abs,
        edited_abs=edited_abs,
        language_of=lambda _ap: "python",
    )
    return {it.rule_id for it in items}


def test_django_model_without_migration_flagged(tmp_path):
    ids = _rule_ids(tmp_path, ["readthedocs/projects/models.py"])
    assert "cochange-django-model-migration" in ids


def test_django_model_with_migration_satisfied(tmp_path):
    ids = _rule_ids(
        tmp_path,
        ["readthedocs/projects/models.py", "readthedocs/projects/migrations/0002_x.py"],
    )
    assert "cochange-django-model-migration" not in ids


def _cluster(bucket, default_export, members):
    return SimpleNamespace(
        key=SimpleNamespace(
            path_pattern_bucket=bucket,
            default_export_kind=default_export,
            top_level_node_kinds=(),
            jsx_present=False,
        ),
        members=members,
        cluster_id=None,
    )


def test_ast_shape_fallback_names_python_class():
    # A Python cluster of single-top-level-class files with no role/dir signal
    # falls back to "class" via the AST-shape rule (default_export_kind=ClassDef).
    members = [SimpleNamespace(path=Path(f"domain/thing{i}.py")) for i in range(6)]
    c = _cluster("domain", "ClassDef", members)
    # Named via the class AST-shape fallback (possibly with a path disambiguator),
    # not the generic cluster-<hash>.
    name = _base_name_for(c)
    assert name == "class" or name.startswith("class-")


# --------------------------------------------------------------------------- #
# Hybrid frontend: python<->ts language_hint, both directions.
# --------------------------------------------------------------------------- #


def test_count_source_files_prunes_node_modules(tmp_path):
    from chameleon_mcp.bootstrap.orchestrator import _count_source_files

    (tmp_path / "src").mkdir()
    for i in range(3):
        (tmp_path / "src" / f"a{i}.tsx").write_text("x", encoding="utf-8")
    # A node_modules full of vendored .d.ts must NOT inflate the count.
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    for i in range(100):
        (nm / f"t{i}.d.ts").write_text("x", encoding="utf-8")
    assert _count_source_files(tmp_path, (".ts", ".tsx")) == 3


def test_count_source_files_prunes_venv(tmp_path):
    from chameleon_mcp.bootstrap.orchestrator import _count_source_files

    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    venv = tmp_path / ".venv" / "lib" / "site-packages" / "dep"
    venv.mkdir(parents=True)
    for i in range(100):
        (venv / f"m{i}.py").write_text("x", encoding="utf-8")
    assert _count_source_files(tmp_path, (".py",)) == 1


def test_js_frontend_dir_recognized_and_none(tmp_path):
    from chameleon_mcp.bootstrap.orchestrator import _js_frontend_dir

    assert _js_frontend_dir(tmp_path) is None
    (tmp_path / "frontend").mkdir()
    assert _js_frontend_dir(tmp_path) == tmp_path / "frontend"


def test_python_backend_marker(tmp_path):
    from chameleon_mcp.bootstrap.orchestrator import _has_python_backend_marker

    assert _has_python_backend_marker(tmp_path) is False
    (tmp_path / "manage.py").write_text("x", encoding="utf-8")
    assert _has_python_backend_marker(tmp_path) is True


def test_is_django_model_excludes_sibling_roles():
    # Regression (cloud review): non-model files in a models/ package must not
    # be treated as models (no makemigrations nudge for a manager/queryset).
    from chameleon_mcp.cochange import _is_django_model

    assert _is_django_model("app/models.py") is True
    assert _is_django_model("app/models/user.py") is True
    assert _is_django_model("app/models/managers.py") is False
    assert _is_django_model("app/models/querysets.py") is False
    assert _is_django_model("app/models/signals.py") is False
    assert _is_django_model("app/models/__init__.py") is False
