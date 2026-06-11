"""Tests for call_sites extraction in prism_dump.rb."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby unavailable")
def test_ruby_call_sites(tmp_path):
    f = tmp_path / "foo_service.rb"
    f.write_text(FIXTURE, encoding="utf-8")
    out = subprocess.run(
        ["ruby", "scripts/prism_dump.rb"],
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
    # Api::Client.new.post: the .post call's receiver is the result of .new
    # (a CallNode). The helper returns nil for chained-call receivers, so
    # "post" must not appear as a constant-kind site.
    constant_posts = [s for s in rec["call_sites"] if s["name"] == "post" and s["kind"] == "constant"]
    assert constant_posts == []
