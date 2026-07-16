"""Tests for Python (Django) role-based path bucketing.

Django expresses a file's role in its FILENAME (models.py, views.py) rather than
a directory chain like Rails (app/models/). So clustering must bucket Python
files by role across apps -- all models.py become one "model" archetype -- which
means ``path_pattern_bucket_for`` returns the role (not the app dir) for a
known role filename, with an EMPTY sub_bucket so the cross-app merge survives
``_split_by_sub_bucket``.
"""

from __future__ import annotations

from chameleon_mcp.signatures import path_pattern_bucket_for, python_role_for_path

# --------------------------------------------------------------------------- #
# python_role_for_path — the role detector
# --------------------------------------------------------------------------- #


def test_role_from_basename():
    assert python_role_for_path("readthedocs/analytics/models.py") == "model"
    assert python_role_for_path("readthedocs/api/v3/views.py") == "view"
    assert python_role_for_path("app/serializers.py") == "serializer"
    assert python_role_for_path("app/admin.py") == "admin"
    assert python_role_for_path("app/forms.py") == "form"
    assert python_role_for_path("app/apps.py") == "app-config"


def test_role_from_package_form_parent_dir():
    # Big apps split models into a package: app/models/__init__.py, base.py.
    assert python_role_for_path("shop/models/__init__.py") == "model"
    assert python_role_for_path("shop/models/base.py") == "model"
    assert python_role_for_path("api/serializers/user.py") == "serializer"


def test_role_from_migrations_dir():
    assert python_role_for_path("app/migrations/0001_initial.py") == "migration"


def test_fastapi_route_modules_from_routes_dir():
    # FastAPI route files have generic names (users.py, items.py) under a
    # routes/ dir; the directory is the role signal.
    assert python_role_for_path("backend/app/api/routes/users.py") == "route"
    assert python_role_for_path("backend/app/api/routes/items.py") == "route"
    assert python_role_for_path("app/endpoints/health.py") == "route"


def test_flask_blueprints_dir():
    assert python_role_for_path("app/blueprints/auth.py") == "blueprint"


def test_fastapi_dependency_module():
    assert python_role_for_path("backend/app/api/deps.py") == "dependency"


def test_no_role_for_ordinary_file():
    assert python_role_for_path("readthedocs/core/utils.py") is None
    assert python_role_for_path("pkg/helpers.py") is None


def test_no_role_for_non_python():
    assert python_role_for_path("app/models.ts") is None
    assert python_role_for_path("app/models.rb") is None


def test_tests_not_treated_as_role():
    # Test files fall through to the existing test-archetype machinery, not the
    # Django role bucket.
    assert python_role_for_path("app/tests.py") is None
    assert python_role_for_path("app/test_views.py") is None


def test_tests_under_role_dirs_not_treated_as_role():
    # A test file whose path runs through a role-named dir (routes/, models/,
    # views/) is still a test, not the production role of that dir.
    assert python_role_for_path("backend/tests/api/routes/test_users.py") is None
    assert python_role_for_path("app/tests/models/test_user.py") is None


def test_role_basenames_under_test_trees_not_treated_as_role():
    # A role-named basename inside a test tree (tests/views.py, tests/models.py)
    # is test scaffolding, not a view/model.
    assert python_role_for_path("tests/views.py") is None
    assert python_role_for_path("tests/models.py") is None


def test_production_role_files_unaffected_by_test_exclusion():
    assert python_role_for_path("backend/app/api/routes/login.py") == "route"
    assert python_role_for_path("app/models/user.py") == "model"
    assert python_role_for_path("shop/models/base.py") == "model"
    assert python_role_for_path("utils.py") is None


def test_alembic_versions_still_roled_as_migration():
    assert python_role_for_path("alembic/versions/9c0a54914c78_add_max_apples.py") == "migration"
    assert python_role_for_path("migrations/versions/0002_widen_scope.py") == "migration"


# --------------------------------------------------------------------------- #
# path_pattern_bucket_for — role bucket + empty sub_bucket (split survival)
# --------------------------------------------------------------------------- #


def test_role_bucket_is_role_with_empty_subbucket():
    bucket, sub = path_pattern_bucket_for("readthedocs/api/v3/views.py")
    assert bucket == "view"
    assert sub == ""  # critical: empty so _split_by_sub_bucket can't re-fragment


def test_role_bucket_with_extension():
    bucket, sub = path_pattern_bucket_for("readthedocs/analytics/models.py", include_extension=True)
    assert bucket == "model:py"
    assert sub == ""


def test_cross_app_models_share_a_bucket():
    a, _ = path_pattern_bucket_for("appA/models.py", include_extension=True)
    b, _ = path_pattern_bucket_for("appB/deep/nested/models.py", include_extension=True)
    assert a == b == "model:py"


def test_non_role_python_falls_through_to_directory_bucket():
    bucket, _ = path_pattern_bucket_for("readthedocs/core/utils.py", include_extension=True)
    assert bucket == "readthedocs/core:py"


def test_non_python_unaffected():
    bucket, _ = path_pattern_bucket_for("app/models/user.rb", include_extension=True)
    assert bucket == "app/models:rb"  # Rails dir-chain bucketing, unchanged


def test_fastapi_schemas_and_dependencies_package_forms_are_roles():
    # qa66 fastapi-1/-2: schemas/ and dependencies/ (and deps/) were role
    # names for the FILENAME form only; the package form fell through to
    # path-prefix fallback and mis-matched dependency edits to the Pydantic
    # schemas archetype.
    from chameleon_mcp.signatures import python_role_for_path

    assert python_role_for_path("app/schemas/item.py") == "schema"
    assert python_role_for_path("app/dependencies/auth.py") == "dependency"
    assert python_role_for_path("app/deps/db.py") == "dependency"
    assert python_role_for_path("app/crud/user.py") == "crud"
