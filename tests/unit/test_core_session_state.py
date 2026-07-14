"""SessionDoc: flocked read-modify-write, concurrent-writer safety, reaping."""

from __future__ import annotations

import json
import threading

from chameleon_mcp.core.session_state import (
    SessionDoc,
    _doc_path,
    read_session_doc,
    reap_stale_docs,
    update_session_doc,
)
from chameleon_mcp.locks import acquire_advisory_lock


def _iso(repo_id="a" * 64):
    return repo_id


def test_read_missing_returns_empty_doc(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    doc = read_session_doc(_iso(), "sess-1")
    assert isinstance(doc, SessionDoc)
    assert doc.idioms_shown_slugs == set()
    assert doc.spawn_count == 0


def test_update_persists_and_rereads(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    update_session_doc(_iso(), "sess-1", lambda d: d.idioms_shown_slugs.add("use-api-client"))
    doc = read_session_doc(_iso(), "sess-1")
    assert doc.idioms_shown_slugs == {"use-api-client"}
    raw = json.loads(_doc_path(_iso(), "sess-1").read_text())
    assert raw["idioms_shown_slugs"] == ["use-api-client"]


def test_concurrent_updates_lose_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))

    def bump():
        for _ in range(25):
            update_session_doc(_iso(), "s", lambda d: setattr(d, "spawn_count", d.spawn_count + 1))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert read_session_doc(_iso(), "s").spawn_count == 100


def test_corrupt_doc_fails_open_to_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    p = _doc_path(_iso(), "sess-x")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    doc = read_session_doc(_iso(), "sess-x")
    assert doc.spawn_count == 0


def test_read_session_doc_fails_open_when_data_dir_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(blocker / "data"))
    doc = read_session_doc(_iso(), "sess-1")
    assert isinstance(doc, SessionDoc)
    assert doc.spawn_count == 0


def test_reap_stale_docs(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    update_session_doc(_iso(), "old", lambda d: None)
    update_session_doc(_iso(), "new", lambda d: None)
    old_path = _doc_path(_iso(), "old")
    new_path = _doc_path(_iso(), "new")
    old_lock = old_path.with_name(old_path.name + ".lock")
    new_lock = new_path.with_name(new_path.name + ".lock")
    stale = time.time() - 72 * 3600
    os.utime(old_path, (stale, stale))
    assert reap_stale_docs(_iso(), max_age_hours=48) == 1
    assert not old_path.exists()
    assert not old_lock.exists()
    assert new_path.exists()
    assert new_lock.exists()


def test_reap_skips_doc_with_held_lock(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    update_session_doc(_iso(), "held", lambda d: None)
    held_path = _doc_path(_iso(), "held")
    stale = time.time() - 72 * 3600
    os.utime(held_path, (stale, stale))
    lock_path = held_path.with_name(held_path.name + ".lock")
    with acquire_advisory_lock(lock_path):
        assert reap_stale_docs(_iso(), max_age_hours=48) == 0
    assert held_path.exists()


def test_job_fields_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))

    def claim(d):
        d.job_inflight = "/data/.job_heartbeat.abc123"
        d.job_started_at = 1752480000.5
        d.review_spawns = 2

    update_session_doc(_iso(), "sess-j", claim)
    doc = read_session_doc(_iso(), "sess-j")
    assert doc.job_inflight == "/data/.job_heartbeat.abc123"
    assert doc.job_started_at == 1752480000.5
    assert doc.review_spawns == 2
    raw = json.loads(_doc_path(_iso(), "sess-j").read_text())
    assert raw["job_inflight"] == "/data/.job_heartbeat.abc123"
    assert raw["job_started_at"] == 1752480000.5
    assert raw["review_spawns"] == 2


def test_job_fields_default_empty():
    doc = SessionDoc()
    assert doc.job_inflight == ""
    assert doc.job_started_at == 0.0
    assert doc.review_spawns == 0


def test_job_fields_reject_malformed_values():
    # Wrong types (including the classic bool-passes-isinstance-int trap) fall
    # back to the empty defaults rather than poisoning later arithmetic.
    doc = SessionDoc.from_dict(
        {
            "job_inflight": ["not", "a", "string"],
            "job_started_at": True,
            "review_spawns": True,
        }
    )
    assert doc.job_inflight == ""
    assert doc.job_started_at == 0.0
    assert doc.review_spawns == 0

    doc = SessionDoc.from_dict({"job_started_at": "soon", "review_spawns": -3})
    assert doc.job_started_at == 0.0
    assert doc.review_spawns == 0

    # An int timestamp is fine (coerced to float); a legit spawn count sticks.
    doc = SessionDoc.from_dict({"job_started_at": 1752480000, "review_spawns": 4})
    assert doc.job_started_at == 1752480000.0
    assert isinstance(doc.job_started_at, float)
    assert doc.review_spawns == 4
