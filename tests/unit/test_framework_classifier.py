"""D2: discrete framework-family classifier + stored ``framework`` tag.

Descriptive metadata only -- persisted in profile.json (optional key, no schema
bump) and surfaced in detect_repo. Nothing gates behavior on it. Classified from
cheap file markers + dependency manifests (no repo-code execution).
"""

from __future__ import annotations

import json

from chameleon_mcp.bootstrap.orchestrator import _classify_framework
from chameleon_mcp.knowledge.detect import score_frameworks

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


def test_flask_extension_only_classifies_as_flask(tmp_path):
    # A repo pinning only Flask extensions (no bare `flask`) is still a Flask app,
    # the same way `djangorestframework`/`fastapi-*` imply their framework.
    (tmp_path / "requirements.txt").write_text("flask-login>=0.6\nflask-cors\n")
    assert _classify_framework(tmp_path, "python") == "flask"


def test_pyproject_prose_does_not_misclassify(tmp_path):
    # A description field mentioning a framework is prose, not a dependency.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndescription = "A lightweight alternative to flask"\n'
        'dependencies = ["click>=8"]\n'
    )
    assert _classify_framework(tmp_path, "python") is None


def test_setup_py_prose_does_not_misclassify(tmp_path):
    # setup.py is arbitrary code/prose; a long_description mentioning a framework
    # must not classify. Its real deps live in the declarative manifests.
    (tmp_path / "setup.py").write_text(
        'setup(name="x", long_description="Built like FastAPI but simpler, not django")\n'
    )
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


def test_ruby_framework_beyond_the_rails_arm(tmp_path):
    """Sinatra is a framework. This asserted ``None`` when the Ruby arm knew only
    Rails and dead-ended, so every other Ruby framework was invisible; the arm now
    hands a miss to the taxonomy fallback, which names it."""
    (tmp_path / "Gemfile").write_text("gem 'sinatra'\n")
    assert _classify_framework(tmp_path, "ruby") == "sinatra"


def test_ruby_library_still_none(tmp_path):
    """A gem that is a LIBRARY, not a framework, still yields None -- the taxonomy
    describes no profile for it, so nothing clears the bar."""
    (tmp_path / "Gemfile").write_text("gem 'nokogiri'\n")
    assert _classify_framework(tmp_path, "ruby") is None


def test_rails_arm_refusal_is_not_overturned_by_the_fallback(tmp_path):
    """The arms adjudicate their own names, and this pins the Ruby half of that.

    The fixture is built so rails ACTUALLY SCORES in the fallback -- Rails'
    conventional layout is present, which is a strong_convention signal, and it
    clears the report floor on its own. What is absent is the corroboration the
    arm requires: no `gem 'rails'` line and no `config/application.rb`. So the
    arm refuses, and the skip is what keeps the fallback from re-asserting the
    name on the weaker, file-name-only evidence.

    A fixture without the layout would pass with `adjudicated=frozenset()` too,
    since rails would never score at all -- the assertion would hold for the
    wrong reason and pin nothing.
    """
    (tmp_path / "Gemfile").write_text("gem 'nokogiri'\n")
    for rel in ("app/models", "app/controllers", "app/views", "config", "db"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "models" / "user.rb").write_text("class User; end\n")
    (tmp_path / "app" / "controllers" / "users_controller.rb").write_text("class C; end\n")
    (tmp_path / "config" / "routes.rb").write_text("Rails.application.routes.draw do\nend\n")
    (tmp_path / "db" / "schema.rb").write_text("ActiveRecord::Schema.define do\nend\n")

    # The premise: rails is a live candidate here, not an absent one.
    scored = {row["framework"] for row in score_frameworks(tmp_path, "ruby")}
    assert "rails" in scored, "fixture no longer makes rails score; the test would be vacuous"

    assert _classify_framework(tmp_path, "ruby") != "rails"


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


def test_ts_framework_beyond_the_hardcoded_arms(tmp_path):
    """Express is a framework. This asserted ``None`` when the classifier knew
    only six names and every other framework was invisible; the taxonomy
    fallback answers for the other 58, so the honest expectation is its name."""
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}))
    assert _classify_framework(tmp_path, "typescript") == "express"


def test_ts_no_framework_none(tmp_path):
    """A dependency that is a LIBRARY, not a framework, still yields None --
    the taxonomy describes no profile for it, so nothing clears the bar."""
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"lodash": "^4"}}))
    assert _classify_framework(tmp_path, "typescript") is None


def test_classifier_fails_open(tmp_path):
    # A malformed package.json must not raise -- fail open to None.
    (tmp_path / "package.json").write_text("{not valid json")
    assert _classify_framework(tmp_path, "typescript") is None
