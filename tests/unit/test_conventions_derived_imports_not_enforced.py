"""Derived import frequency must not ship under the enforce header.

``format_conventions_for_session`` rendered every import line under "IMPORTS
(enforce — team decision; files still using the discouraged form are
mid-migration, do not imitate them)", gated only on the list being non-empty.
That list mixes two kinds of line with opposite standing:

- ``- Use X, not Y`` comes from a taught competing pair. It is a real decision,
  it is the only form ``lint_engine`` enforces, and the header is true of it.
- ``- Prefer X`` is a frequency observation. ``extract_repo_wide_import_conventions``
  documents it as advisory and it never reaches the enforcement rule, so every
  such line was an advisory wearing an enforcement header.

The block is delivered through the memory channel, which carries the highest
instruction authority chameleon has, so a false "team decision" costs the most
exactly where it is cheapest to believe.

Which derived modules are worth rendering at all is a separate question this
file does not answer: ``__future__`` is a real per-file decision a team makes,
while ``json`` is not, and nothing in the frequency signal tells them apart.
Splitting the header fixes the authority claim without needing to.
"""

from __future__ import annotations

from chameleon_mcp.conventions import (
    ADVISORY_IMPORTS_HEADER,
    ENFORCED_IMPORTS_HEADER,
    format_conventions_for_session,
)


def _wrap(sections: dict) -> dict:
    # format_conventions_for_session reads conventions["conventions"].
    return {"conventions": sections}


def _imports(preferred: list[str] | None = None, competing: list[dict] | None = None) -> dict:
    return _wrap(
        {
            "imports": {
                "service": {
                    "preferred": [
                        {"module": m, "frequency": 9, "total": 10} for m in preferred or []
                    ],
                    "competing": competing or [],
                }
            }
        }
    )


def test_derived_preferred_renders_under_the_advisory_header() -> None:
    out = format_conventions_for_session(_imports(preferred=["@/lib/api-client"]))
    assert ADVISORY_IMPORTS_HEADER in out
    assert "- Prefer @/lib/api-client" in out


def test_derived_preferred_alone_never_renders_the_enforce_header() -> None:
    out = format_conventions_for_session(_imports(preferred=["@/lib/api-client", "zod"]))
    assert ENFORCED_IMPORTS_HEADER not in out


def test_taught_competing_keeps_the_enforce_header() -> None:
    out = format_conventions_for_session(
        _imports(competing=[{"preferred": "@/lib/http", "over": "axios"}])
    )
    assert ENFORCED_IMPORTS_HEADER in out
    assert "- Use @/lib/http, not axios" in out


def test_taught_and_derived_render_under_their_own_headers() -> None:
    out = format_conventions_for_session(
        _imports(
            preferred=["zod"],
            competing=[{"preferred": "@/lib/http", "over": "axios"}],
        )
    )
    enforced_at = out.index(ENFORCED_IMPORTS_HEADER)
    advisory_at = out.index(ADVISORY_IMPORTS_HEADER)
    # The taught decision leads; the observation follows it.
    assert enforced_at < advisory_at
    taught_line = out.index("- Use @/lib/http, not axios")
    derived_line = out.index("- Prefer zod")
    assert enforced_at < taught_line < advisory_at < derived_line


def test_derived_only_repo_still_renders_a_block() -> None:
    # The empty-block guard must count the advisory list too, or a repo whose
    # only derived signal is import frequency loses its whole conventions block.
    out = format_conventions_for_session(_imports(preferred=["@/lib/api-client"]))
    assert "PROJECT CONVENTIONS" in out
