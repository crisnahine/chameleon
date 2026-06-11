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
