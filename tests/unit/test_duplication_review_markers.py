"""Tests for per-(file,digest) duplication-judged markers (Task 9)."""

from __future__ import annotations

from chameleon_mcp.duplication_review import already_judged, mark_judged


def test_marker_roundtrip(tmp_path):
    repo_data = tmp_path
    assert already_judged(repo_data, "sess", "app/a.rb", "digest1") is False
    mark_judged(repo_data, "sess", "app/a.rb", "digest1")
    assert already_judged(repo_data, "sess", "app/a.rb", "digest1") is True
    # different digest (file changed) -> not judged
    assert already_judged(repo_data, "sess", "app/a.rb", "digest2") is False


def test_different_session_not_judged(tmp_path):
    mark_judged(tmp_path, "sess1", "app/a.rb", "d1")
    assert already_judged(tmp_path, "sess2", "app/a.rb", "d1") is False


def test_different_file_not_judged(tmp_path):
    mark_judged(tmp_path, "sess", "app/a.rb", "d1")
    assert already_judged(tmp_path, "sess", "app/b.rb", "d1") is False


def test_mark_judged_idempotent(tmp_path):
    # Calling twice should not raise.
    mark_judged(tmp_path, "s", "f.rb", "d")
    mark_judged(tmp_path, "s", "f.rb", "d")
    assert already_judged(tmp_path, "s", "f.rb", "d") is True


def test_default_prefix_filename_is_stable(tmp_path):
    # Pin the on-disk name: existing .dup_judged. markers from earlier sessions
    # must stay valid across the prefix-parameter change.
    import hashlib

    mark_judged(tmp_path, "sess", "app/a.rb", "digest1")
    key = hashlib.sha256(b"sess\x00app/a.rb\x00digest1").hexdigest()[:32]
    assert (tmp_path / f".dup_judged.{key}").exists()


def test_corr_and_dup_marker_namespaces_isolated(tmp_path):
    mark_judged(tmp_path, "sess", "app/a.rb", "d1", prefix=".corr_judged.")
    # Invisible to the default dup namespace and vice versa.
    assert already_judged(tmp_path, "sess", "app/a.rb", "d1") is False
    assert already_judged(tmp_path, "sess", "app/a.rb", "d1", prefix=".corr_judged.") is True

    mark_judged(tmp_path, "sess", "app/b.rb", "d2")
    assert already_judged(tmp_path, "sess", "app/b.rb", "d2", prefix=".corr_judged.") is False
    assert already_judged(tmp_path, "sess", "app/b.rb", "d2") is True
