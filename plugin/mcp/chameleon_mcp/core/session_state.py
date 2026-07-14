"""One JSON state doc per (session, repo_id), mutated only under a flock.

Replaces the per-session marker-file zoo. The consolidated doc must not be
less race-tolerant than the atomic marker files it subsumes: concurrent Stop
and SubagentStop invocations read-modify-write the same doc, so every write
holds the doc's advisory lock for the full load-mutate-save cycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_DOC_PREFIX = ".session_doc."


def _session_key(session_id: str) -> str:
    return hashlib.sha256((session_id or "unknown").encode("utf-8")).hexdigest()[:16]


def _doc_path(repo_id: str, session_id: str) -> Path:
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(repo_id) / f"{_DOC_PREFIX}{_session_key(session_id)}.json"


@dataclass
class SessionDoc:
    idioms_shown_slugs: set[str] = field(default_factory=set)
    delivered_gist_slugs: set[str] = field(default_factory=set)
    judged_digests: dict[str, str] = field(default_factory=dict)
    spawn_count: int = 0
    stop_blocks_by_root: dict[str, int] = field(default_factory=dict)
    intent_tokens: list[str] = field(default_factory=list)
    delivery_cursor: str = ""

    def to_dict(self) -> dict:
        return {
            "idioms_shown_slugs": sorted(self.idioms_shown_slugs),
            "delivered_gist_slugs": sorted(self.delivered_gist_slugs),
            "judged_digests": dict(self.judged_digests),
            "spawn_count": self.spawn_count,
            "stop_blocks_by_root": dict(self.stop_blocks_by_root),
            "intent_tokens": list(self.intent_tokens),
            "delivery_cursor": self.delivery_cursor,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SessionDoc:
        if not isinstance(data, dict):
            return cls()
        doc = cls()
        try:
            doc.idioms_shown_slugs = {str(s) for s in data.get("idioms_shown_slugs") or []}
            doc.delivered_gist_slugs = {str(s) for s in data.get("delivered_gist_slugs") or []}
            jd = data.get("judged_digests")
            doc.judged_digests = (
                {str(k): str(v) for k, v in jd.items()} if isinstance(jd, dict) else {}
            )
            sc = data.get("spawn_count")
            doc.spawn_count = (
                sc if isinstance(sc, int) and not isinstance(sc, bool) and sc >= 0 else 0
            )
            br = data.get("stop_blocks_by_root")
            doc.stop_blocks_by_root = (
                {
                    str(k): int(v)
                    for k, v in br.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                }
                if isinstance(br, dict)
                else {}
            )
            doc.intent_tokens = [str(t) for t in data.get("intent_tokens") or []]
            dc = data.get("delivery_cursor")
            doc.delivery_cursor = dc if isinstance(dc, str) else ""
        except Exception:
            return cls()
        return doc


def read_session_doc(repo_id: str, session_id: str) -> SessionDoc:
    """Lock-free snapshot read; corrupt, missing, or unresolvable docs fail open to empty."""
    try:
        path = _doc_path(repo_id, session_id)
        return SessionDoc.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return SessionDoc()


def update_session_doc(
    repo_id: str, session_id: str, mutate: Callable[[SessionDoc], None]
) -> SessionDoc:
    """Load-mutate-save under the doc's flock. The only write path."""
    from chameleon_mcp.locks import acquire_advisory_lock

    path = _doc_path(repo_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
        try:
            doc = SessionDoc.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            doc = SessionDoc()
        mutate(doc)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc.to_dict(), separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    return doc


def reap_stale_docs(repo_id: str, *, max_age_hours: int = 48) -> int:
    """Delete session docs (and their lock sidecars) older than max_age_hours.

    Each candidate is reaped under its own advisory lock so a doc mid-write by
    a live holder is never yanked out from under it. A doc contended by another
    holder is skipped this pass rather than waited on, since a stale-enough doc
    is by definition not on anyone's hot path.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock
    from chameleon_mcp.profile.trust import repo_data_dir

    cutoff = time.time() - max_age_hours * 3600
    reaped = 0
    try:
        candidates = list(repo_data_dir(repo_id).glob(f"{_DOC_PREFIX}*.json"))
    except OSError:
        return 0

    for p in candidates:
        try:
            if p.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue

        lock_path = p.with_name(p.name + ".lock")
        try:
            with acquire_advisory_lock(lock_path, blocking_timeout=0.5):
                try:
                    if p.stat().st_mtime >= cutoff:
                        continue  # refreshed between the outer check and the lock
                    p.unlink(missing_ok=True)
                    reaped += 1
                    # Unlink the sidecar while still holding it, not after: a
                    # waiter blocked on this exact inode racing a session that
                    # writes to a doc this stale is implausible, and the
                    # fallout is bounded to one lost update in a dead session.
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    continue
        except LockHeldError:
            continue
    return reaped
