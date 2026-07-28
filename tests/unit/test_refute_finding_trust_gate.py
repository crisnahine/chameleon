"""refute_finding is the one model-facing tool whose post-gate action SPAWNS.

Every sibling read tool's untrusted branch merely withholds data; past this gate
refute_finding prefetches a git-diff excerpt per finding and launches up to
REFUTER_MAX_SPAWNS_PER_INVOCATION hardened `claude -p` refuters with that repo
text inlined in the prompt. The gate is also the only one that reports itself
through a bespoke `refuter` key rather than the usual `status`, so a sweep over
`status == "untrusted"` never covers it. Pin it directly.
"""

from __future__ import annotations

import pytest

from chameleon_mcp import tools


@pytest.fixture
def untrusted_repo(tmp_path, monkeypatch):
    """A resolvable repo with a profile nobody has granted trust to."""
    monkeypatch.delenv("CHAMELEON_REVIEW_REFUTER", raising=False)
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    # The CLI gate sits immediately AFTER the trust gate; force it open so an
    # "untrusted" result can only have come from the trust gate itself.
    monkeypatch.setattr("chameleon_mcp.refuter.refuter_cli_absent", lambda: None)
    return repo


def test_untrusted_profile_refuses_and_unverifies_every_finding(untrusted_repo):
    out = tools.refute_finding(
        str(untrusted_repo),
        [
            {"id": "f1", "kind": "correctness", "claim": "c", "evidence": "e"},
            {"id": "f2", "kind": "correctness", "claim": "c", "evidence": "e"},
        ],
    )
    assert out["data"]["refuter"] == "untrusted"
    # One verdict per input finding -- a withheld answer is never a dropped one.
    assert [v["id"] for v in out["data"]["verdicts"]] == ["f1", "f2"]
    assert [v["verdict"] for v in out["data"]["verdicts"]] == ["unverified", "unverified"]
    assert {v["reason"] for v in out["data"]["verdicts"]} == {"profile untrusted"}


def test_untrusted_profile_never_reaches_the_spawn_or_excerpt_seams(untrusted_repo, monkeypatch):
    """The gate must return BEFORE any repo text is read or any process starts."""
    touched: list[str] = []

    def _boom_batch(*_a, **_k):
        touched.append("run_batch")
        raise AssertionError("spawned refuters for an untrusted profile")

    def _boom_excerpt(*_a, **_k):
        touched.append("excerpt")
        raise AssertionError("read repo text for an untrusted profile")

    monkeypatch.setattr("chameleon_mcp.refuter.run_batch", _boom_batch)
    monkeypatch.setattr(tools, "_refuter_excerpt_for", _boom_excerpt)

    out = tools.refute_finding(
        str(untrusted_repo), [{"id": "f1", "kind": "correctness", "claim": "c", "evidence": "e"}]
    )
    assert out["data"]["refuter"] == "untrusted"
    assert touched == []
