"""Tests for call_sites extraction in prism_dump.rb."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_PRISM_DUMP = Path(__file__).resolve().parents[2] / "scripts" / "prism_dump.rb"


def _have_prism() -> bool:
    if not shutil.which("ruby"):
        return False
    try:
        return (
            subprocess.run(
                ["ruby", "-e", "require 'prism'"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


FIXTURE = """
class FooService
  def perform
    helper
    User.find(1)
    Api::Client.new.post
    self.flush
    other.compute
  end

  def helper; end
  def flush; end
end
"""

FIXTURE_OPERATORS = """
def calc
  a = 1 + 2
  arr = []
  arr << a
  arr[0]
  helper
end
def helper; end
"""

FIXTURE_NAMESPACED = """
module A
  class Settings
    def self.get
      1
    end
  end
end

module Outer
  module Inner
    class Deep
      def run
        1
      end
    end
  end
end

class Utils::Helper
  def self.assist
    1
  end
end

class TopLevel
  def plain
    1
  end
end

module Util
  def self.helper
    1
  end

  class Worker
    class << self
      def go
        1
      end
    end
  end
end
"""


def _dump(path):
    import subprocess

    out = subprocess.run(
        ["ruby", str(_PRISM_DUMP)],
        input=str(path) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_ruby_call_sites(tmp_path):
    f = tmp_path / "foo_service.rb"
    f.write_text(FIXTURE, encoding="utf-8")
    out = subprocess.run(
        ["ruby", str(_PRISM_DUMP)],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(out.stdout.strip().splitlines()[-1])
    sites = {(s["name"], s["kind"], s.get("receiver"), s["caller"]) for s in rec["call_sites"]}
    assert ("helper", "bare", None, "perform") in sites
    assert ("find", "constant", "User", "perform") in sites
    assert ("new", "constant", "Api::Client", "perform") in sites
    assert ("flush", "self", "self", "perform") in sites
    assert ("compute", "member", "other", "perform") in sites
    # Api::Client.new.post: the chained call's receiver is the result of .new
    # (a CallNode). call_site_of keeps the innermost call's name as a member
    # receiver, so "post" records as kind="member", receiver="new" -- never
    # as a constant-kind site.
    constant_posts = [
        s for s in rec["call_sites"] if s["name"] == "post" and s["kind"] == "constant"
    ]
    assert constant_posts == []


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_operator_sends_excluded(tmp_path):
    f = tmp_path / "calc.rb"
    f.write_text(FIXTURE_OPERATORS, encoding="utf-8")
    out = subprocess.run(
        ["ruby", str(_PRISM_DUMP)],
        input=str(f) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(out.stdout.strip().splitlines()[-1])
    names = {s["name"] for s in rec["call_sites"]}
    assert "helper" in names, "identifier-named call must be recorded"
    assert "+" not in names, "operator + must be excluded"
    assert "<<" not in names, "operator << must be excluded"
    assert "[]" not in names, "operator [] must be excluded"
    # call_sites_total counts only what the helper returns (identifier sends);
    # operator sends are nil from the helper and never reach the counter.
    assert rec["call_sites_total"] == len(rec["call_sites"])


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_enclosing_class_path_is_fully_qualified(tmp_path):
    # enclosing_class stays the lexical class name (the conventions derivation
    # consumes it); enclosing_class_path is additive and joins the live
    # module/class nesting, so "Settings" inside module A keys as "A::Settings".
    f = tmp_path / "namespaced.rb"
    f.write_text(FIXTURE_NAMESPACED, encoding="utf-8")
    rec = _dump(f)
    sigs = {s["name"]: s for s in rec["callable_signatures"]}

    get = sigs["get"]
    assert get["enclosing_class"] == "Settings"
    assert get["enclosing_class_path"] == "A::Settings"
    assert get["kind"] == "singleton_method"

    run = sigs["run"]
    assert run["enclosing_class"] == "Deep"
    assert run["enclosing_class_path"] == "Outer::Inner::Deep"

    plain = sigs["plain"]
    assert plain["enclosing_class"] == "TopLevel"
    assert plain["enclosing_class_path"] == "TopLevel"


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_compact_constant_path_class_keeps_full_name(tmp_path):
    # `class Utils::Helper` at the top level: constant_path already carries the
    # qualified name, and the empty nesting stack adds nothing.
    f = tmp_path / "namespaced.rb"
    f.write_text(FIXTURE_NAMESPACED, encoding="utf-8")
    rec = _dump(f)
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    assist = sigs["assist"]
    assert assist["enclosing_class"] == "Utils::Helper"
    assert assist["enclosing_class_path"] == "Utils::Helper"
    assert assist["kind"] == "singleton_method"


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_module_level_def_and_singleton_class_paths(tmp_path):
    # `def self.helper` directly inside `module Util` is invoked as `Util.helper`
    # (the constant_receiver call shape), so the module IS its enclosing identity
    # -- the dump records enclosing_class/path = the module. A `class << self`
    # scope keeps the enclosing class's qualified path exactly as it keeps its
    # name.
    f = tmp_path / "namespaced.rb"
    f.write_text(FIXTURE_NAMESPACED, encoding="utf-8")
    rec = _dump(f)
    sigs = {s["name"]: s for s in rec["callable_signatures"]}

    helper = sigs["helper"]
    assert helper["enclosing_class"] == "Util"
    assert helper["enclosing_class_path"] == "Util"
    assert helper["kind"] == "singleton_method"

    go = sigs["go"]
    assert go["enclosing_class"] == "Worker"
    assert go["enclosing_class_path"] == "Util::Worker"
    assert go["kind"] == "singleton_method"
