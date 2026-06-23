"""PKG-7: Python test-quality lint (pytest/unittest)."""

from __future__ import annotations

from chameleon_mcp.bootstrap.naming import _looks_like_test
from chameleon_mcp.lint_engine import _test_quality_violations


def _rules(content, witness=None):
    return {
        v.rule
        for v in _test_quality_violations(content, language="python", witness_content=witness)
    }


def test_skipped_pytest_mark():
    assert "skipped-test" in _rules("@pytest.mark.skip\ndef test_x():\n    assert f()\n")
    assert "skipped-test" in _rules("@pytest.mark.skipif(cond)\ndef test_x():\n    assert f()\n")
    assert "skipped-test" in _rules("def test_x():\n    pytest.skip('wip')\n")


def test_skipped_unittest():
    assert "skipped-test" in _rules(
        "@unittest.skip('x')\ndef test_x(self):\n    self.assertTrue(f())\n"
    )


def test_tautological_assertion():
    assert "tautological-assertion" in _rules("def test_x():\n    assert 1 == 1\n")
    assert "tautological-assertion" in _rules("def test_x():\n    self.assertEqual(True, True)\n")


def test_real_sleep():
    assert "real-sleep-in-test" in _rules("def test_x():\n    time.sleep(2)\n    assert ok\n")


def test_random_in_test():
    assert "random-in-test" in _rules("def test_x():\n    v = random.random()\n    assert v\n")
    assert "random-in-test" in _rules("def test_x():\n    u = uuid.uuid4()\n    assert u\n")


def test_assertion_free_flagged():
    # A test function that sets up state but never asserts.
    assert "assertion-free-test" in _rules("def test_x():\n    obj = build()\n    obj.run()\n")


def test_assertion_present_not_flagged():
    assert "assertion-free-test" not in _rules("def test_x():\n    assert build().ok\n")


def test_unstubbed_network_witness_gated():
    witness = "import responses\n@responses.activate\ndef test_w():\n    assert requests.get(u)\n"
    candidate = "def test_x():\n    r = requests.get(url)\n    assert r\n"
    assert "unstubbed-network" in _rules(candidate, witness=witness)


def test_unfrozen_clock_witness_gated():
    witness = "from freezegun import freeze_time\ndef test_w():\n    assert datetime.now()\n"
    candidate = "def test_x():\n    now = datetime.now()\n    assert now\n"
    assert "unfrozen-clock" in _rules(candidate, witness=witness)


def test_looks_like_test_python_prefix():
    # Co-located pytest files (test_ prefix) are recognized as a test cluster.
    assert _looks_like_test("app", ["app/test_views.py", "app/test_models.py"]) is True
    assert _looks_like_test("app", ["app/conftest.py", "app/test_x.py"]) is True
    assert _looks_like_test("app", ["app/views.py", "app/models.py"]) is False
