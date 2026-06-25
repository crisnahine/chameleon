"""Ruby service-object shape: a class method + instance method of the same name
must not drop the singleton's contract (it is the constant-receiver target)."""

from types import SimpleNamespace

from chameleon_mcp.signature_diff import _callables_of_parsed_file


def _pf(rows):
    return SimpleNamespace(extras={"callable_signatures": rows})


def _p(*names):
    return [{"name": n, "optional": False, "kind": "positional"} for n in names]


def test_singleton_plus_instance_keeps_singleton():
    pf = _pf(
        [
            {"name": "call", "params": _p("amount"), "kind": "singleton_method"},
            {"name": "call", "params": _p("amount", "currency"), "kind": "method"},
        ]
    )
    out = _callables_of_parsed_file(pf)
    assert "call" in out
    # the singleton's params (1 positional) win, not the instance's (2)
    assert [p["name"] for p in out["call"]] == ["amount"]


def test_two_instance_methods_same_name_still_dropped():
    pf = _pf(
        [
            {"name": "save", "params": _p("a"), "kind": "method"},
            {"name": "save", "params": _p("a", "b"), "kind": "method"},
        ]
    )
    assert "save" not in _callables_of_parsed_file(pf)


def test_two_singletons_same_name_still_dropped():
    pf = _pf(
        [
            {"name": "build", "params": _p("a"), "kind": "singleton_method"},
            {"name": "build", "params": _p("a", "b"), "kind": "singleton_method"},
        ]
    )
    assert "build" not in _callables_of_parsed_file(pf)


def test_ts_overloads_still_dropped():
    # TS overloads carry no singleton_method kind -> still ambiguous -> dropped.
    pf = _pf(
        [
            {"name": "fn", "params": _p("a"), "kind": "function"},
            {"name": "fn", "params": _p("a", "b"), "kind": "function"},
        ]
    )
    assert "fn" not in _callables_of_parsed_file(pf)


def test_unique_names_unchanged():
    pf = _pf(
        [
            {"name": "charge", "params": _p("amount"), "kind": "singleton_method"},
            {"name": "refund", "params": _p("id"), "kind": "method"},
        ]
    )
    out = _callables_of_parsed_file(pf)
    assert set(out) == {"charge", "refund"}
