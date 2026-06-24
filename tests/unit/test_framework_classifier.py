"""D2: discrete framework-family classifier + stored ``framework`` tag.

Descriptive metadata only -- persisted in profile.json (optional key, no schema
bump) and surfaced in detect_repo. Nothing gates behavior on it. Classified from
cheap file markers + dependency manifests (no repo-code execution).
"""

from __future__ import annotations

import json

from chameleon_mcp.bootstrap.orchestrator import _classify_framework

# --- Python ---------------------------------------------------------------- #


def test_django_via_manage_py(tmp_path):
    (tmp_path / "manage.py").write_text("import django\n")
    assert _classify_framework(tmp_path, "python") == "django"


def test_django_via_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("Django==4.2\ndjangorestframework==3.14\n")
    assert _classify_framework(tmp_path, "python") == "django"


def test_fastapi_via_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==0.110\nuvicorn\n")
    assert _classify_framework(tmp_path, "python") == "fastapi"


def test_flask_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["flask>=3"]\n')
    assert _classify_framework(tmp_path, "python") == "flask"


def test_manage_py_wins_over_deps(tmp_path):
    # A DRF repo (django + drf) has manage.py -> family is django.
    (tmp_path / "manage.py").write_text("import django\n")
    (tmp_path / "requirements.txt").write_text("djangorestframework\nfastapi\n")
    assert _classify_framework(tmp_path, "python") == "django"


def test_python_no_marker_none(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")
    assert _classify_framework(tmp_path, "python") is None


def test_fastapi_in_backend_workspace_member(tmp_path):
    # uv/monorepo: the root manifest only declares the workspace; the framework
    # dep lives in a member subdir (backend/).
    (tmp_path / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["backend"]\n')
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.110"]\n')
    assert _classify_framework(tmp_path, "python") == "fastapi"


def test_django_manage_py_in_subdir(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["server"]\n')
    server = tmp_path / "server"
    server.mkdir()
    (server / "manage.py").write_text("import django\n")
    assert _classify_framework(tmp_path, "python") == "django"


# --- Ruby ------------------------------------------------------------------ #


def test_rails_via_application_rb(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "application.rb").write_text("module App\nend\n")
    assert _classify_framework(tmp_path, "ruby") == "rails"


def test_rails_via_gemfile(tmp_path):
    (tmp_path / "Gemfile").write_text("gem 'rails', '~> 7.0'\n")
    assert _classify_framework(tmp_path, "ruby") == "rails"


def test_ruby_no_rails_none(tmp_path):
    (tmp_path / "Gemfile").write_text("gem 'sinatra'\n")
    assert _classify_framework(tmp_path, "ruby") is None


# --- TypeScript ------------------------------------------------------------ #


def test_nextjs_via_config(tmp_path):
    (tmp_path / "next.config.js").write_text("module.exports = {}\n")
    assert _classify_framework(tmp_path, "typescript") == "nextjs"


def test_nextjs_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "^14"}}))
    assert _classify_framework(tmp_path, "typescript") == "nextjs"


def test_nestjs_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"@nestjs/core": "^10"}}))
    assert _classify_framework(tmp_path, "typescript") == "nestjs"


def test_nextjs_in_frontend_workspace_member(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["frontend"]}))
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text(json.dumps({"dependencies": {"next": "^14"}}))
    assert _classify_framework(tmp_path, "typescript") == "nextjs"


def test_ts_no_framework_none(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}))
    assert _classify_framework(tmp_path, "typescript") is None


def test_classifier_fails_open(tmp_path):
    # A malformed package.json must not raise -- fail open to None.
    (tmp_path / "package.json").write_text("{not valid json")
    assert _classify_framework(tmp_path, "typescript") is None
