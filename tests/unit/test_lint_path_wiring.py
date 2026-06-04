"""Wiring of required_guards, test-quality witness, and secret hardness through
both lint paths (the in-process hook path and the lint_file MCP tool).

These cover seams where the per-archetype convention slice the lint reads was
assembled by the caller: required_guards must be copied into the slice or the
advisory can never fire, and the test-quality pass needs the witness content
threaded to self-calibrate its gated checks.
"""

from __future__ import annotations

from types import SimpleNamespace

from chameleon_mcp import hook_helper


def _loaded(*, conventions=None, canonicals=None, language="ruby"):
    return SimpleNamespace(
        canonicals={"canonicals": canonicals or {}},
        conventions={"conventions": conventions or {}},
        rules={},
        profile={"language": language},
    )


class TestRequiredGuardThreadedInProcess:
    _CONV = {
        "required_guards": {
            "controller": {
                "required_guards": ["authorize!"],
                "known_guards": ["authorize!"],
            }
        }
    }

    def test_required_guard_advisory_fires(self, tmp_path):
        content = "class NewController < BaseController\n  before_action :set_thing\nend\n"
        out = hook_helper._lint_file_in_process(
            tmp_path,
            "controller",
            content,
            str(tmp_path / "app/controllers/new_controller.rb"),
            loaded=_loaded(conventions=self._CONV),
        )
        guard = [v for v in out if v.get("rule") == "required-guard-convention"]
        assert len(guard) == 1
        assert guard[0]["severity"] == "info"

    def test_present_guard_not_flagged(self, tmp_path):
        content = "class NewController < BaseController\n  before_action :authorize!\nend\n"
        out = hook_helper._lint_file_in_process(
            tmp_path,
            "controller",
            content,
            str(tmp_path / "app/controllers/new_controller.rb"),
            loaded=_loaded(conventions=self._CONV),
        )
        assert not [v for v in out if v.get("rule") == "required-guard-convention"]

    def test_no_guard_slice_no_advisory(self, tmp_path):
        # required_guards present for a DIFFERENT archetype -> the slice for this
        # archetype is empty, so the rule cannot fire (no cross-archetype leakage).
        content = "class NewController < BaseController\n  before_action :set_thing\nend\n"
        out = hook_helper._lint_file_in_process(
            tmp_path,
            "service",
            content,
            str(tmp_path / "app/services/thing.rb"),
            loaded=_loaded(conventions=self._CONV),
        )
        assert not [v for v in out if v.get("rule") == "required-guard-convention"]


class TestTestQualityWitnessThreadedInProcess:
    def test_witness_helper_suppresses_assertion_free(self, tmp_path):
        # The witness wraps its assert in a custom helper. With the witness threaded
        # through, a candidate that calls the same helper must NOT be flagged as
        # assertion-free; without it the helper-wrapped assert would misfire.
        witness_path = tmp_path / "spec" / "thing_spec.rb"
        witness_path.parent.mkdir(parents=True, exist_ok=True)
        witness_src = (
            "describe Thing do\n"
            "  it 'works' do\n"
            "    assert_ok(subject.call)\n"
            "    expect(subject).to be_valid\n"
            "  end\nend\n"
        )
        witness_path.write_text(witness_src, encoding="utf-8")

        canonicals = {
            "spec": [
                {
                    "normative_shape": {"ast_query": None},
                    "witness": {"path": "spec/thing_spec.rb"},
                }
            ]
        }
        # Candidate test asserts only via the witness's helper, no bare expect.
        candidate = "describe Other do\n  it 'works' do\n    assert_ok(subject.call)\n  end\nend\n"
        out = hook_helper._lint_file_in_process(
            tmp_path,
            "spec",
            candidate,
            str(tmp_path / "spec" / "other_spec.rb"),
            loaded=_loaded(canonicals=canonicals),
        )
        assert not [v for v in out if v.get("rule") == "assertion-free-test"]

    def test_assertion_free_fires_without_helper(self, tmp_path):
        # A genuinely assertion-free block (no assert token, no witness helper) is
        # still flagged, proving the witness threading did not silence the rule.
        witness_path = tmp_path / "spec" / "thing_spec.rb"
        witness_path.parent.mkdir(parents=True, exist_ok=True)
        witness_path.write_text(
            "describe Thing do\n  it 'works' do\n    expect(x).to eq(1)\n  end\nend\n",
            encoding="utf-8",
        )
        canonicals = {
            "spec": [
                {
                    "normative_shape": {"ast_query": None},
                    "witness": {"path": "spec/thing_spec.rb"},
                }
            ]
        }
        candidate = "describe Other do\n  it 'sets up' do\n    subject.call\n  end\nend\n"
        out = hook_helper._lint_file_in_process(
            tmp_path,
            "spec",
            candidate,
            str(tmp_path / "spec" / "other_spec.rb"),
            loaded=_loaded(canonicals=canonicals),
        )
        assert [v for v in out if v.get("rule") == "assertion-free-test"]
