"""The TDD brief must promote archetypes whose WITNESS is a test file.

``test-driven-development``'s brief tells the reader "the canonical witness
below is the file to imitate" while writing a first failing test, so the rows it
leads with have to be tests. Promotion matched a substring against the DERIVED
CLUSTER NAME instead, and a cluster earns a ``test-*`` name from any path token
while keeping a source witness -- ``bootstrap/canonical.py`` re-admits test
files to the witness pool only for an all-test cluster.

The result is a map that leads with source files under a test-shaped heading.
Measured on live profiles, gitlabhq filled 11 of its 12 rendered rows with
non-tests (``test-gitlab-http`` -> ``gems/gitlab-http/lib/gitlab/http_v2/patches.rb``),
py-django-readthedocs 5, bulletproof-react 2 -- so the skill was handed a map
with every witness except the one it was told to imitate.

Classify the witness path, not the archetype name.
"""

from __future__ import annotations

from chameleon_mcp.peer_skill_context import _witness_sort_key


def _order(archetype_paths: list[tuple[str, str]]) -> list[str]:
    """Archetype names in rendered order, tests first."""
    return [a for a, _ in sorted(archetype_paths, key=lambda r: _witness_sort_key(r[0], r[1]))]


def test_test_shaped_name_with_a_source_witness_does_not_lead() -> None:
    # The real gitlabhq row: the cluster is named test-* but its witness is a
    # library source file, so it must not be promoted over an actual spec.
    order = _order(
        [
            ("test-gitlab-http", "gems/gitlab-http/lib/gitlab/http_v2/patches.rb"),
            ("service", "spec/services/issues/create_service_spec.rb"),
        ]
    )
    assert order[0] == "service"


def test_source_shaped_name_with_a_test_witness_leads() -> None:
    # The mirror case: nothing in the archetype name says "test", but the
    # witness is one, and that is the file the skill was told to imitate.
    order = _order(
        [
            ("component", "src/components/card.tsx"),
            ("cluster-e7130c25", "src/components/__tests__/card.test.tsx"),
        ]
    )
    assert order[0] == "cluster-e7130c25"


def test_python_and_ruby_witnesses_are_recognized_without_a_language() -> None:
    # _pattern_facts has no language in hand, so the classifier probes all three.
    order = _order(
        [
            ("alpha", "src/lib/client.ts"),
            ("beta", "tests/api/test_routes.py"),
        ]
    )
    assert order[0] == "beta"
    order = _order(
        [
            ("alpha", "app/models/user.rb"),
            ("beta", "spec/models/user_spec.rb"),
        ]
    )
    assert order[0] == "beta"


def test_ties_keep_deterministic_alphabetical_order() -> None:
    # Two non-tests, or two tests, must not reorder run to run.
    assert _order([("zeta", "src/z.ts"), ("alpha", "src/a.ts")]) == ["alpha", "zeta"]
    assert _order([("zeta", "src/z.test.ts"), ("alpha", "src/a.test.ts")]) == ["alpha", "zeta"]


def test_a_repo_with_no_test_witness_sorts_exactly_alphabetically() -> None:
    # The no-match guarantee the retired name-based sort also held: a repo with
    # nothing to promote must render in plain alphabetical order.
    rows = [("service", "app/s.rb"), ("model", "app/m.rb"), ("controller", "app/c.rb")]
    assert _order(rows) == ["controller", "model", "service"]
