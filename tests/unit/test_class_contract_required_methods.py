"""Class C: the class_contract required-methods check.

chameleon advertises an archetype's mandatory methods (`perform` on an
ApplicationJob, `call` on a BaseService) in the PRE-edit block but, before this
check, never verified them post-edit. The check is deliberately FP-safe:

- Only a method the cohort follows at >= 95% frequency is enforced (a 66%-common
  method like a policy's `index?` is never nagged).
- The file must contain a top-level class DIRECTLY extending the archetype's
  dominant base (a helper class, or a subclass of a sibling, is not in the cohort).
- The base class itself is exempt.
- The method is flagged missing only when it is defined NOWHERE in the file
  (maximally FP-safe: if it exists on any class in the file, no nag).
- Advisory only -- never block-eligible.
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import _required_method_violations
from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

_JOB_CONTRACT = {
    "required_methods": ["perform"],
    "frequencies": {"perform": 1.0, "ApplicationJob": 1.0},
    "base": "ApplicationJob",
    "sample_size": 16,
}
_SERVICE_CONTRACT = {
    "required_methods": ["call"],
    "frequencies": {"call": 1.0},
    "base": "BaseService",
    "sample_size": 82,
}


def _rules(viols):
    return [v.rule for v in viols]


def test_ruby_job_missing_perform_flags():
    src = "class BackfillJob < ApplicationJob\n  def other\n  end\nend\n"
    viols = _required_method_violations(src, _JOB_CONTRACT, "ruby")
    assert _rules(viols) == ["missing-required-method"]
    assert "perform" in viols[0].message


def test_ruby_job_with_perform_clean():
    src = "class BackfillJob < ApplicationJob\n  def perform(id)\n  end\nend\n"
    assert _required_method_violations(src, _JOB_CONTRACT, "ruby") == []


def test_ruby_job_with_self_perform_clean():
    # `def self.perform` is still defining perform -> no nag.
    src = "class BackfillJob < ApplicationJob\n  def self.perform(id)\n  end\nend\n"
    assert _required_method_violations(src, _JOB_CONTRACT, "ruby") == []


def test_ruby_non_cohort_class_not_flagged():
    # A plain helper that does NOT extend the dominant base is not in the cohort.
    src = "class Helper\n  def other\n  end\nend\n"
    assert _required_method_violations(src, _JOB_CONTRACT, "ruby") == []


def test_ruby_subclass_of_sibling_not_flagged():
    # A subclass of another job (not the dominant base directly) inherits perform;
    # only a DIRECT extender of the dominant base is in the cohort.
    src = "class ChildJob < BackfillJob\n  def other\n  end\nend\n"
    assert _required_method_violations(src, _JOB_CONTRACT, "ruby") == []


def test_ruby_base_class_itself_exempt():
    src = "class ApplicationJob < ActiveJob::Base\n  def around_perform\n  end\nend\n"
    assert _required_method_violations(src, _JOB_CONTRACT, "ruby") == []


def test_low_frequency_method_never_flagged():
    # A method below the 0.95 gate is never enforced even if missing.
    contract = {
        "required_methods": ["index?"],
        "frequencies": {"index?": 0.659},
        "base": "ApplicationPolicy",
        "sample_size": 40,
    }
    src = "class WidgetPolicy < ApplicationPolicy\n  def show?\n  end\nend\n"
    assert _required_method_violations(src, contract, "ruby") == []


def test_small_sample_not_flagged():
    contract = dict(_JOB_CONTRACT, sample_size=3)
    src = "class BackfillJob < ApplicationJob\n  def other\n  end\nend\n"
    assert _required_method_violations(src, contract, "ruby") == []


def test_python_class_missing_method_flags():
    contract = {
        "required_methods": ["validate"],
        "frequencies": {"validate": 1.0},
        "base": "BaseValidator",
        "sample_size": 14,
    }
    src = "class EmailValidator(BaseValidator):\n    def other(self):\n        pass\n"
    viols = _required_method_violations(src, contract, "python")
    assert _rules(viols) == ["missing-required-method"]


def test_python_class_with_method_clean():
    contract = {
        "required_methods": ["validate"],
        "frequencies": {"validate": 1.0},
        "base": "BaseValidator",
        "sample_size": 14,
    }
    src = "class EmailValidator(BaseValidator):\n    def validate(self, v):\n        pass\n"
    assert _required_method_violations(src, contract, "python") == []


def test_service_call_flags_and_reuses_base_tail():
    # Namespaced base: a service extending the short-form base is still in cohort.
    src = "class WidgetService < BaseService\n  def run\n  end\nend\n"
    viols = _required_method_violations(src, _SERVICE_CONTRACT, "ruby")
    assert _rules(viols) == ["missing-required-method"]


def test_rule_is_advisory_only():
    assert "missing-required-method" not in BLOCK_ELIGIBLE_RULES


def test_sub_one_frequency_method_not_enforced():
    # A genuine abstract-method contract is derived at EXACTLY 1.0 (every cohort
    # member implements it because the base raises NotImplementedError). A method in
    # the 0.95-0.99 band is a commonly-overridden method WITH a base default (e.g. a
    # SystemCheck#show_error at 0.96) or one some members get via a mixin -- flagging
    # it is a false positive, since the file-level check cannot see the inherited /
    # mixed-in definition. Enforce only 1.0.
    for freq in (0.96, 0.98, 0.999):
        contract = {
            "required_methods": ["show_error"],
            "frequencies": {"show_error": freq},
            "base": "BaseCheck",
            "sample_size": 25,
        }
        src = "class GitalyCheck < BaseCheck\n  def multi_check\n  end\nend\n"
        assert _required_method_violations(src, contract, "ruby") == [], f"freq {freq}"


def test_exactly_one_frequency_method_enforced():
    contract = {
        "required_methods": ["render"],
        "frequencies": {"render": 1.0},
        "base": "LiquidTagBase",
        "sample_size": 80,
    }
    src = "class FooTag < LiquidTagBase\n  def other\n  end\nend\n"
    assert _rules(_required_method_violations(src, contract, "ruby")) == ["missing-required-method"]


def test_qualified_base_tail_collision_not_flagged():
    # A class extending a QUALIFIED intermediate that merely shares the base tail
    # (`Models::Notifications::Create::Base` vs the contract base
    # `ActiveInteraction::Base`, both tail `Base`) is NOT a direct extender of the
    # contract base -- it inherits the method through the intermediate. Full-name
    # matching (not tail) keeps it out of the cohort.
    contract = {
        "required_methods": ["execute"],
        "frequencies": {"execute": 1.0},
        "base": "ActiveInteraction::Base",
        "sample_size": 1200,
    }
    src = "class Confidential < Models::Notifications::Create::Base\n  def perform\n  end\nend\n"
    assert _required_method_violations(src, contract, "ruby") == []


def test_short_form_unqualified_base_still_flagged():
    # A class extending the UNqualified short form of the contract base
    # (`< Base` inside the module that defines ActiveInteraction::Base) IS a cohort
    # member and still flags a missing method.
    contract = {
        "required_methods": ["execute"],
        "frequencies": {"execute": 1.0},
        "base": "ActiveInteraction::Base",
        "sample_size": 1200,
    }
    src = "class MyInteraction < Base\n  def other\n  end\nend\n"
    assert _rules(_required_method_violations(src, contract, "ruby")) == ["missing-required-method"]


def test_message_names_the_full_base_not_bare_tail():
    contract = {
        "required_methods": ["execute"],
        "frequencies": {"execute": 1.0},
        "base": "ActiveInteraction::Base",
        "sample_size": 1200,
    }
    src = "class MyInteraction < ActiveInteraction::Base\n  def other\n  end\nend\n"
    viols = _required_method_violations(src, contract, "ruby")
    assert viols and "ActiveInteraction::Base" in viols[0].message


def test_empty_or_bad_contract_safe():
    assert _required_method_violations("class X < ApplicationJob\nend\n", {}, "ruby") == []
    assert _required_method_violations("", _JOB_CONTRACT, "ruby") == []
    assert _required_method_violations("class X\nend", _JOB_CONTRACT, "typescript") == []


def test_lint_conventions_threads_class_contract():
    # Integration: lint_conventions must READ class_contract and fire the check.
    from chameleon_mcp.lint_engine import lint_conventions

    conv = {"class_contract": _JOB_CONTRACT}
    src = "class BackfillJob < ApplicationJob\n  def other\n  end\nend\n"
    viols = lint_conventions(src, conv, language="ruby")
    assert "missing-required-method" in [v.rule for v in viols]
