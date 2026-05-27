"""Unit tests for chameleon_mcp.index_db — init, upsert, resolve, list."""
from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.index_db import (
    _get_index_conn_readonly,
    close_index_connections,
    init_index_db,
    list_repos,
    resolve_repo_root,
    upsert_repo,
)


@pytest.fixture(autouse=True)
def _isolate_index(tmp_path: Path, monkeypatch):
    """Point CHAMELEON_PLUGIN_DATA to tmp_path and reset connection cache."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    close_index_connections()
    yield
    close_index_connections()


# ---------------------------------------------------------------------------
# init_index_db
# ---------------------------------------------------------------------------

class TestInitIndexDb:
    def test_creates_tables_on_first_run(self, tmp_path: Path):
        db_path = tmp_path / "index.db"
        conn = init_index_db(db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "repos" in tables
        assert "schema_meta" in tables
        assert "file_clusters" in tables

    def test_idempotent_on_second_call(self, tmp_path: Path):
        db_path = tmp_path / "index.db"
        conn1 = init_index_db(db_path)
        conn1.close()
        # second call should not raise
        conn2 = init_index_db(db_path)
        ver = conn2.execute(
            "SELECT v FROM schema_meta WHERE k = 'schema_version'"
        ).fetchone()
        conn2.close()
        assert ver is not None
        assert ver[0] == "1"


# ---------------------------------------------------------------------------
# upsert_repo + resolve_repo_root round-trip
# ---------------------------------------------------------------------------

class TestUpsertAndResolve:
    def test_round_trip(self, tmp_path: Path):
        """upsert then resolve returns the same repo_root."""
        upsert_repo(
            "repo-abc",
            "/home/user/projects/abc",
            profile_sha256="sha_abc",
            archetype_count=3,
            files_indexed=42,
        )
        # flush the write connection so readers see the data
        close_index_connections()

        root = resolve_repo_root("repo-abc")
        assert root == "/home/user/projects/abc"

    def test_upsert_updates_existing(self, tmp_path: Path):
        upsert_repo(
            "repo-upd",
            "/home/user/old",
            profile_sha256="sha_v1",
            archetype_count=2,
        )
        # re-insert same (repo_id, repo_root) with updated fields
        upsert_repo(
            "repo-upd",
            "/home/user/old",
            profile_sha256="sha_v2",
            archetype_count=5,
        )
        close_index_connections()

        db_path = tmp_path / "index.db"
        conn = init_index_db(db_path)
        row = conn.execute(
            "SELECT profile_sha256, archetype_count FROM repos "
            "WHERE repo_id = ? AND repo_root = ?",
            ("repo-upd", "/home/user/old"),
        ).fetchone()
        conn.close()
        assert row["profile_sha256"] == "sha_v2"
        assert row["archetype_count"] == 5

    def test_resolve_returns_none_for_unknown(self):
        # prime the DB so index.db exists
        upsert_repo("seed", "/seed", profile_sha256="x")
        close_index_connections()
        assert resolve_repo_root("no-such-repo") is None


# ---------------------------------------------------------------------------
# list_repos pagination
# ---------------------------------------------------------------------------

class TestListRepos:
    def test_basic_pagination(self, tmp_path: Path):
        # Insert 5 repos with ascending timestamps so ordering is predictable
        for i in range(5):
            upsert_repo(
                f"repo-{i:03d}",
                f"/path/{i}",
                last_seen_at=f"2025-01-{10 + i:02d}T00:00:00.000000Z",
            )
        close_index_connections()

        # Page 1: first 2 (ordered by last_seen_at DESC)
        page, cursor, total = list_repos(None, 2)
        assert total == 5
        assert len(page) == 2
        assert cursor is not None

        # Page 2: next 2
        page2, cursor2, total2 = list_repos(cursor, 2)
        assert total2 == 5
        assert len(page2) == 2
        assert cursor2 is not None

        # Page 3: last 1
        page3, cursor3, _ = list_repos(cursor2, 2)
        assert len(page3) == 1
        assert cursor3 is None  # no more pages

    def test_empty_db_returns_empty(self):
        # index.db doesn't exist yet, so list_repos returns empty
        page, cursor, total = list_repos(None, 10)
        assert page == []
        assert cursor is None
        assert total == 0

    def test_invalid_cursor_raises(self):
        upsert_repo("r", "/p", last_seen_at="2025-01-01T00:00:00.000000Z")
        close_index_connections()

        with pytest.raises(ValueError, match="unknown cursor"):
            list_repos("bogus", 10)


# ---------------------------------------------------------------------------
# _get_index_conn_readonly — BUG-032
# ---------------------------------------------------------------------------

class TestReadOnlyConnection:
    def test_readonly_reads_existing_data(self, tmp_path: Path):
        """Read-only connection can read data written by a prior write conn."""
        upsert_repo("ro-test", "/ro/path", profile_sha256="sha_ro")
        close_index_connections()

        conn = _get_index_conn_readonly()
        row = conn.execute(
            "SELECT repo_root FROM repos WHERE repo_id = ?",
            ("ro-test",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["repo_root"] == "/ro/path"

    def test_readonly_on_missing_db_raises(self, tmp_path: Path, monkeypatch):
        """When index.db doesn't exist, read-only should raise, not create it."""
        empty = tmp_path / "empty_data"
        empty.mkdir()
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(empty))
        close_index_connections()

        import sqlite3
        with pytest.raises((sqlite3.OperationalError, sqlite3.Error)):
            _get_index_conn_readonly()

    def test_resolve_repo_root_uses_readonly(self, tmp_path: Path):
        """resolve_repo_root should succeed via read-only when data exists."""
        upsert_repo("resolve-ro", "/resolve/path", profile_sha256="sha")
        close_index_connections()

        root = resolve_repo_root("resolve-ro")
        assert root == "/resolve/path"
