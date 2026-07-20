"""Inheritance-convention-violation false-positive fixes.

Two independent FP drivers, each verified ~100% false-positive on real repos:

- Defect C: the Ruby inheritance check flags a base-less `class Foo` (no `< Base`)
  as a deviation, while the Python check exempts a base-less `class Foo:` -- a
  documented inconsistency. Base-less Ruby classes (middleware, config modules,
  standalone services) are legitimate, not a missed inheritance. Align Ruby to
  Python: only flag a class that DOES extend something outside the known bases.

- Defect B: `inheritance-convention-violation` presumes the file is a real member
  of the archetype whose dominant base it enforces. On a WEAK archetype match
  (match_quality fallback/none, or a path_only basis) the file is not a confident
  member (a lib/exceptions.rb path-matched to a CLI archetype, told to "inherit
  Base"), so the advisory is noise. Drop it on such matches. The block path is
  unaffected -- it already gates block-eligible inheritance on high+ast.
"""

from __future__ import annotations

from chameleon_mcp.hook_helper import _drop_inheritance_on_weak_match
from chameleon_mcp.lint_engine import lint_conventions

_RUBY_CONV = {
    "inheritance": {
        "dominant_base": "BaseService",
        "frequency": 0.84,
        "known_bases": ["BaseService"],
    }
}


def _rules(viols):
    return [v.rule for v in viols]


# ---- Defect C: Ruby base-less class exempt --------------------------------


def test_ruby_baseless_class_not_flagged():
    src = "class MiddlewareThing\n  def call(env)\n  end\nend\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, _RUBY_CONV, language="ruby")
    )


def test_ruby_wrong_base_still_flags():
    # FN GUARD: a class that really extends the WRONG base is still a deviation.
    src = "class WidgetService < SomethingUnrelated\n  def call\n  end\nend\n"
    assert "inheritance-convention-violation" in _rules(
        lint_conventions(src, _RUBY_CONV, language="ruby")
    )


def test_ruby_correct_base_clean():
    src = "class WidgetService < BaseService\n  def call\n  end\nend\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, _RUBY_CONV, language="ruby")
    )


def test_ruby_baseless_and_wrong_base_in_same_file():
    # The base-less class is exempt; the wrong-base sibling still flags.
    src = (
        "class Standalone\n  def run\n  end\nend\n"
        "class WidgetService < SomethingUnrelated\n  def call\n  end\nend\n"
    )
    assert (
        _rules(lint_conventions(src, _RUBY_CONV, language="ruby")).count(
            "inheritance-convention-violation"
        )
        == 1
    )


# ---- Defect B: drop inheritance advisory on a weak archetype match --------


def _inh(rule="inheritance-convention-violation"):
    return {"rule": rule}


def test_weak_match_fallback_drops_inheritance():
    viols = [_inh(), _inh("naming-convention-violation")]
    got = _drop_inheritance_on_weak_match(viols, "fallback", "path_only")
    assert [v["rule"] for v in got] == ["naming-convention-violation"]


def test_weak_match_path_only_drops_even_if_quality_missing():
    got = _drop_inheritance_on_weak_match([_inh()], None, "path_only")
    assert got == []


def test_confident_ast_match_keeps_inheritance():
    viols = [_inh()]
    assert _drop_inheritance_on_weak_match(viols, "ast", "path_and_ast") == viols


def test_exact_match_keeps_inheritance():
    viols = [_inh()]
    assert _drop_inheritance_on_weak_match(viols, "exact", "path_and_ast") == viols


def test_weak_match_never_drops_other_rules():
    viols = [_inh("secret-detected-in-content"), _inh("naming-convention-violation")]
    got = _drop_inheritance_on_weak_match(viols, "fallback", "path_only")
    assert {v["rule"] for v in got} == {"secret-detected-in-content", "naming-convention-violation"}


def test_empty_and_none_safe():
    assert _drop_inheritance_on_weak_match([], "fallback", "path_only") == []
    assert _drop_inheritance_on_weak_match(None, "fallback", "path_only") is None


# ---- Defect E: same-role extension is intra-role reuse, not a base deviation ----
# A controller extending another *Controller, a serializer extending another
# *Serializer, is reusing a sibling/intermediate of the SAME role (which itself
# roots at the archetype's base) -- not a wrong-base deviation. Only a CROSS-role
# base (a controller extending a Model) is flagged. Verified: 30 of 31 mastodon
# residual inheritance FPs are same-role extensions.

_CONTROLLER_CONV = {
    "inheritance": {
        "dominant_base": "Api::BaseController",
        "frequency": 0.74,
        "known_bases": ["Api::BaseController", "Admin::BaseController"],
    }
}


def test_ruby_same_role_intermediate_not_flagged():
    # A controller extending a project intermediate controller (not a known base)
    # is same-role reuse -> exempt.
    src = "class FooController < Admin::SettingsController\n  def index\n  end\nend\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, _CONTROLLER_CONV, language="ruby")
    )


def test_ruby_cross_role_base_still_flagged():
    # FN GUARD: a controller extending a NON-controller base (cross-role) is a real
    # deviation and still flags.
    src = "class FooController < SomeDomainModel\n  def index\n  end\nend\n"
    assert "inheritance-convention-violation" in _rules(
        lint_conventions(src, _CONTROLLER_CONV, language="ruby")
    )


def test_ruby_generic_base_word_is_not_a_role():
    # `Base` (and Error/Class/...) is a GENERIC trailing word, not a role, so an
    # archetype whose dominant base ends in a generic word grants no role exemption:
    # a class extending an unrelated `*Base` is still flagged.
    conv = {"inheritance": {"dominant_base": "ActiveInteraction::Base", "frequency": 0.82}}
    src = "class MyService < SomeUnrelatedBase\n  def execute\n  end\nend\n"
    assert "inheritance-convention-violation" in _rules(
        lint_conventions(src, conv, language="ruby")
    )


def test_python_same_role_serializer_not_flagged():
    conv = {"inheritance": {"dominant_base": "serializers.ModelSerializer", "frequency": 0.7}}
    src = "class PageSearchSerializer(PageResultSerializer):\n    pass\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, conv, language="python")
    )


def test_python_cross_role_still_flagged():
    conv = {"inheritance": {"dominant_base": "serializers.ModelSerializer", "frequency": 0.7}}
    src = "class WidgetThing(SomeExternalModel):\n    pass\n"
    assert "inheritance-convention-violation" in _rules(
        lint_conventions(src, conv, language="python")
    )


def test_base_role_suffix_helper():
    from chameleon_mcp.lint_engine import _base_role_suffix

    assert _base_role_suffix("Api::BaseController") == "Controller"
    assert _base_role_suffix("serializers.ModelSerializer") == "Serializer"
    assert _base_role_suffix("BaseService") == "Service"
    assert _base_role_suffix("ApplicationRecord") == "Record"
    # generic trailing words are not roles
    assert _base_role_suffix("ActiveInteraction::Base") is None
    assert _base_role_suffix("StandardError") is None


# ---- Python subscripted-generic base normalization ------------------------
# Derivation strips the generic subscript (conventions._strip_type_params) so a
# typed cohort's dominant base is `BaseRepository`, but the lint compared the raw
# `BaseRepository[Shipment]` and never matched, firing a false positive on every
# generic repository on every edit. Lint must strip the subscript symmetrically.

_PY_GENERIC_CONV = {
    "inheritance": {
        "dominant_base": "BaseRepository",
        "frequency": 0.89,
        "known_bases": ["BaseRepository"],
    }
}


def test_python_generic_base_matches_dominant():
    src = "class ShipmentRepository(BaseRepository[Shipment]):\n    pass\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, _PY_GENERIC_CONV, language="python")
    )


def test_python_multiarg_generic_base_matches():
    # A multi-type-arg generic base carries an internal comma; the base split
    # must not fracture on it (Generic[T, U] is ONE base, not two).
    conv = {
        "inheritance": {
            "dominant_base": "Base",
            "frequency": 0.9,
            "known_bases": ["Base", "Generic"],
        }
    }
    src = "class Repo(Base, Generic[T, U]):\n    pass\n"
    assert "inheritance-convention-violation" not in _rules(
        lint_conventions(src, conv, language="python")
    )


def test_python_wrong_generic_base_still_flags():
    # FN GUARD: a genuinely wrong base still deviates even when subscripted.
    src = "class ShipmentRepository(SomethingElse[Shipment]):\n    pass\n"
    assert "inheritance-convention-violation" in _rules(
        lint_conventions(src, _PY_GENERIC_CONV, language="python")
    )
