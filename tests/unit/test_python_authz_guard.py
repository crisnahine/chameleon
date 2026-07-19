"""DRF/Django authz-guard derivation + lint (the Python analog of the Rails
required-guard convention).

The guard signal per view is PRESENCE-based: a class that assigns
``permission_classes`` / ``authentication_classes`` (any value, including
``AllowAny``) has made an authz decision; so has one extending a
``LoginRequiredMixin`` / ``PermissionRequiredMixin`` base or carrying a
``@login_required`` / ``@permission_required`` decorator. A view cohort that
conventionally guards (>=60%) makes an unguarded view an advisory outlier.
Advisory info only, never block-eligible.
"""

from __future__ import annotations

from chameleon_mcp.conventions import extract_python_authz_guard_conventions
from chameleon_mcp.extractors.python import PythonExtractor
from chameleon_mcp.lint_engine import lint_conventions


def _rules(violations) -> set[str]:
    return {v.rule for v in violations}


# --------------------------------------------------------------------------- #
# D1a: libcst class-body attribute capture (presence signal for permission_classes)
# --------------------------------------------------------------------------- #


def test_class_attrs_captures_permission_classes(tmp_path):
    f = tmp_path / "views.py"
    f.write_text(
        "class UserView(APIView):\n"
        "    permission_classes = [IsAuthenticated]\n"
        "    queryset = User.objects.all()\n"
        "    def get(self):\n        return None\n",
        encoding="utf-8",
    )
    pf = PythonExtractor().parse_repo(tmp_path, paths=[f]).files[0]
    uv = next(s for s in pf.extras.get("class_shapes", []) if s["name"] == "UserView")
    assert "permission_classes" in uv["class_attrs"]
    assert "queryset" in uv["class_attrs"]


def test_class_attrs_annotated_assignment(tmp_path):
    f = tmp_path / "views.py"
    f.write_text(
        "class UserView(APIView):\n    permission_classes: list = [IsAuthenticated]\n",
        encoding="utf-8",
    )
    pf = PythonExtractor().parse_repo(tmp_path, paths=[f]).files[0]
    uv = next(s for s in pf.extras.get("class_shapes", []) if s["name"] == "UserView")
    assert "permission_classes" in uv["class_attrs"]


def test_class_attrs_ignores_method_body_assignments(tmp_path):
    # An assignment inside a method body is not a class attribute.
    f = tmp_path / "views.py"
    f.write_text(
        "class UserView(APIView):\n    def get(self):\n        permission_classes = 1\n        return permission_classes\n",
        encoding="utf-8",
    )
    pf = PythonExtractor().parse_repo(tmp_path, paths=[f]).files[0]
    uv = next(s for s in pf.extras.get("class_shapes", []) if s["name"] == "UserView")
    assert "permission_classes" not in uv.get("class_attrs", [])


# --------------------------------------------------------------------------- #
# D1b: cohort derivation (presence-based, 60% floor, MIN_SAMPLE 10)
# --------------------------------------------------------------------------- #


def _guarded(i: int) -> str:
    return f"class V{i}(APIView):\n    permission_classes = [IsAuthenticated]\n    def get(self):\n        return None\n"


def _unguarded(i: int) -> str:
    return f"class U{i}(APIView):\n    def get(self):\n        return None\n"


def test_authz_guard_derivation_above_floor(tmp_path):
    for i in range(8):
        (tmp_path / f"g{i}.py").write_text(_guarded(i), encoding="utf-8")
    for i in range(2):
        (tmp_path / f"u{i}.py").write_text(_unguarded(i), encoding="utf-8")
    files = PythonExtractor().parse_repo(tmp_path).files
    conv = extract_python_authz_guard_conventions(files)
    assert conv.get("authz_required") is True
    assert conv["sample_size"] == 10


def test_authz_guard_below_floor_empty(tmp_path):
    for i in range(5):
        (tmp_path / f"g{i}.py").write_text(_guarded(i), encoding="utf-8")
    for i in range(5):
        (tmp_path / f"u{i}.py").write_text(_unguarded(i), encoding="utf-8")
    files = PythonExtractor().parse_repo(tmp_path).files
    assert extract_python_authz_guard_conventions(files) == {}


def test_authz_guard_below_min_sample_empty(tmp_path):
    # Sized off the constant so the invariant survives a floor recalibration.
    from chameleon_mcp.conventions import MIN_SAMPLE_SIZE

    for i in range(MIN_SAMPLE_SIZE - 1):
        (tmp_path / f"g{i}.py").write_text(_guarded(i), encoding="utf-8")
    files = PythonExtractor().parse_repo(tmp_path).files
    assert extract_python_authz_guard_conventions(files) == {}


def test_authz_guard_mixin_base_counts(tmp_path):
    for i in range(10):
        (tmp_path / f"v{i}.py").write_text(
            f"class V{i}(LoginRequiredMixin, View):\n    def get(self):\n        return None\n",
            encoding="utf-8",
        )
    files = PythonExtractor().parse_repo(tmp_path).files
    assert extract_python_authz_guard_conventions(files).get("authz_required") is True


def test_authz_guard_decorator_counts(tmp_path):
    for i in range(10):
        (tmp_path / f"v{i}.py").write_text(
            f"@login_required\ndef view_{i}(request):\n    return None\n",
            encoding="utf-8",
        )
    files = PythonExtractor().parse_repo(tmp_path).files
    assert extract_python_authz_guard_conventions(files).get("authz_required") is True


def test_non_view_cohort_derives_nothing(tmp_path):
    # A model cohort has no authz signal -> derivation self-gates to empty.
    for i in range(10):
        (tmp_path / f"m{i}.py").write_text(
            f"class M{i}(models.Model):\n    name = CharField()\n",
            encoding="utf-8",
        )
    files = PythonExtractor().parse_repo(tmp_path).files
    assert extract_python_authz_guard_conventions(files) == {}


# --------------------------------------------------------------------------- #
# D1c/D1d: edit-time lint (advisory info, presence-semantics escape hatches)
# --------------------------------------------------------------------------- #

_REQUIRED = {"required_guards": {"authz_required": True, "sample_size": 12}}


def test_authz_lint_flags_unguarded_view():
    content = "class PublicView(APIView):\n    def get(self):\n        return None\n"
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" in _rules(v)


def test_authz_lint_satisfied_by_permission_classes_allowany():
    # An explicit AllowAny IS an authz decision -> never flag (presence-semantics).
    content = "class V(APIView):\n    permission_classes = [AllowAny]\n    def get(self):\n        return None\n"
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_satisfied_by_decorator():
    content = "@login_required\ndef my_view(request):\n    return None\n"
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_satisfied_by_mixin_base():
    content = "class V(LoginRequiredMixin, View):\n    def get(self):\n        return None\n"
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_satisfied_by_mixin_base_pep695_generic():
    # PEP 695 generic view (Python 3.12+): the `[T]` type-parameter list sits
    # between the class name and its bases. The guard regex must still see the
    # authz mixin, or a properly-guarded generic view is falsely flagged.
    content = "class V[T](LoginRequiredMixin, View):\n    def get(self):\n        return None\n"
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_generic_dominant_base_does_not_suppress():
    # The cohort's GENERIC base (DRF APIView, in known_bases) carries no authz;
    # a view extending only it with no permission_classes must still flag. This
    # is the real-repo bug the synthetic DRF fixture surfaced.
    conv = {
        "required_guards": {
            "authz_required": True,
            "sample_size": 13,
            "known_bases": ["APIView"],
        }
    }
    content = "class PublicView(APIView):\n    def get(self, request):\n        return None\n"
    v = lint_conventions(content, conv, language="python", archetype_name="view")
    assert "required-guard-convention" in _rules(v)


def test_authz_lint_satisfied_by_known_cohort_base():
    conv = {
        "required_guards": {
            "authz_required": True,
            "sample_size": 12,
            "known_bases": ["AuthedAPIView"],
        }
    }
    content = "class V(AuthedAPIView):\n    def get(self):\n        return None\n"
    v = lint_conventions(content, conv, language="python", archetype_name="view")
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_is_advisory_info():
    content = "class PublicView(APIView):\n    def get(self):\n        return None\n"
    hits = [
        x
        for x in lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
        if x.rule == "required-guard-convention"
    ]
    assert hits and hits[0].severity == "info"


def test_authz_lint_silent_without_convention():
    content = "class PublicView(APIView):\n    def get(self):\n        return None\n"
    v = lint_conventions(
        content,
        {"imports": {"competing": []}},
        language="python",
        archetype_name="view",
    )
    assert "required-guard-convention" not in _rules(v)


def test_authz_lint_permission_classes_in_docstring_does_not_satisfy():
    # A mention inside a docstring is not a real assignment -> still flags.
    content = '"""set permission_classes = [IsAuthenticated] here"""\nclass V(APIView):\n    def get(self):\n        return None\n'
    v = lint_conventions(content, _REQUIRED, language="python", archetype_name="view")
    assert "required-guard-convention" in _rules(v)
