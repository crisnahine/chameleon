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


def test_reap_stale_docs(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    update_session_doc(_iso(), "old", lambda d: None)
    update_session_doc(_iso(), "new", lambda d: None)
    old_path = _doc_path(_iso(), "old")
    stale = time.time() - 72 * 3600
    os.utime(old_path, (stale, stale))
    assert reap_stale_docs(_iso(), max_age_hours=48) == 1
    assert not old_path.exists()
    assert _doc_path(_iso(), "new").exists()
