"""Idiom truth: one schema-validated JSON file per idiom under .chameleon/idioms/.

The only module allowed to read or write idiom truth. idioms.md and the
conventions.md TEAM IDIOMS section are generated views of this store. Per-file
storage keeps git merges trivial (two taught idioms = two added files) and
shrinks the injection-scan blast radius to a single idiom.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

STORE_SCHEMA = "chameleon-idiom-1"
STORE_DIRNAME = "idioms"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_STATUSES = ("active", "deprecated")
_SOURCES = ("taught", "auto", "learned")


def store_dir(profile_dir: Path) -> Path:
    return profile_dir / STORE_DIRNAME


def store_exists(profile_dir: Path) -> bool:
    try:
        return store_dir(profile_dir).is_dir()
    except OSError:
        return False


def slug_for_title(title: str) -> str:
    """Filename-safe slug from a display title; stable and deterministic."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:64].strip("-")
    if not _SLUG_RE.match(s):
        s = ("idiom-" + s).strip("-")[:64]
    return s if _SLUG_RE.match(s) else "idiom-unnamed"


@dataclass
class IdiomRecord:
    slug: str
    title: str
    rationale: str
    languages: list[str] = field(default_factory=list)
    archetypes: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    status: str = "active"
    added_date: str = ""
    deprecated_date: str = ""
    examples: list[str] = field(default_factory=list)
    counterexamples: list[str] = field(default_factory=list)
    source: str = "taught"
    provenance: str = ""
    evidence: str = ""
    rank: int = 0

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug):
            raise ValueError(f"invalid idiom slug: {self.slug!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"invalid idiom status: {self.status!r}")
        if self.source not in _SOURCES:
            raise ValueError(f"invalid idiom source: {self.source!r}")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("idiom rationale must be non-empty")

    def to_dict(self) -> dict:
        d = {"schema": STORE_SCHEMA}
        for f in fields(self):
            d[f.name] = getattr(self, f.name)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> IdiomRecord:
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for name in ("languages", "archetypes", "paths", "examples", "counterexamples"):
            v = kwargs.get(name)
            kwargs[name] = [str(x) for x in v] if isinstance(v, list) else []
        rank = kwargs.get("rank")
        kwargs["rank"] = rank if isinstance(rank, int) and not isinstance(rank, bool) else 0
        return cls(**kwargs)


def _scan_suspicious(text: str) -> tuple[bool, str | None]:
    """Injection scan via the shared pattern table; fails open on scanner error."""
    try:
        from chameleon_mcp.tools import _looks_suspicious

        return _looks_suspicious(text)
    except Exception:
        return False, None


def _record_scan_text(rec: IdiomRecord) -> str:
    return "\n".join(
        [rec.title, rec.rationale, rec.provenance, *rec.examples, *rec.counterexamples]
    )


def load_store(profile_dir: Path) -> list[IdiomRecord]:
    """All records, rank-ascending (rank 1 = newest, rendered first).

    Fail-open per file: a corrupt file or a record that trips the injection
    scan is skipped with a stderr warning; the rest of the store still loads.
    """
    records: list[IdiomRecord] = []
    sdir = store_dir(profile_dir)
    try:
        paths = sorted(sdir.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rec = IdiomRecord.from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            print(
                f"chameleon: idiom file skipped ({path.name}): {exc}",
                file=sys.stderr,
            )
            continue
        hit, label = _scan_suspicious(_record_scan_text(rec))
        if hit:
            print(
                f"chameleon: idiom '{rec.slug}' dropped from context: matched {label!r} "
                "(edit or re-teach it with safe prose)",
                file=sys.stderr,
            )
            continue
        records.append(rec)
    records.sort(key=lambda r: (r.rank, r.slug))
    return records


def find_by_slug(records: list[IdiomRecord], slug: str) -> IdiomRecord | None:
    for r in records:
        if r.slug == slug:
            return r
    return None


def upsert_idiom(profile_dir: Path, record: IdiomRecord) -> None:
    """Atomic per-file write. Callers hold the store lock and regenerate views."""
    sdir = store_dir(profile_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{record.slug}.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def idioms_for_scope(
    records: list[IdiomRecord],
    *,
    languages: set[str],
    archetypes: set[str],
    paths: list[str],
) -> list[IdiomRecord]:
    """Active records whose scope intersects the edit. An EMPTY record dimension
    is a wildcard — migrated legacy idioms (no archetypes/paths) must stay in
    review scope, and a record must match on EVERY dimension it declares."""
    selected: list[IdiomRecord] = []
    for rec in records:
        if rec.status != "active":
            continue
        if rec.languages and languages and not (set(rec.languages) & languages):
            continue
        if rec.archetypes and archetypes and not (set(rec.archetypes) & archetypes):
            continue
        if rec.paths and paths:
            if not any(fnmatch.fnmatch(p, pat) for p in paths for pat in rec.paths):
                continue
        selected.append(rec)
    return selected
