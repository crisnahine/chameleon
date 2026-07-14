"""Idiom truth: one schema-validated JSON file per idiom under .chameleon/idioms/.

The only module allowed to read or write idiom truth. idioms.md and the
conventions.md TEAM IDIOMS section are generated views of this store. Per-file
storage keeps git merges trivial (two taught idioms = two added files) and
shrinks the injection-scan blast radius to a single idiom.
"""

from __future__ import annotations

import fnmatch
import hashlib
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
        if not isinstance(data, dict):
            raise ValueError(f"idiom record root must be an object, got {type(data).__name__}")
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for name in ("languages", "archetypes", "paths", "examples", "counterexamples"):
            v = kwargs.get(name)
            kwargs[name] = [str(x) for x in v] if isinstance(v, list) else []
        for name in (
            "title",
            "rationale",
            "provenance",
            "evidence",
            "added_date",
            "deprecated_date",
            "status",
            "source",
            "slug",
        ):
            if name in kwargs and kwargs[name] is not None:
                kwargs[name] = str(kwargs[name])
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
        [
            rec.title,
            rec.rationale,
            rec.provenance,
            rec.evidence,
            *rec.examples,
            *rec.counterexamples,
        ]
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
            hit, label = _scan_suspicious(_record_scan_text(rec))
        except Exception as exc:
            print(
                f"chameleon: idiom file skipped ({path.name}): {exc}",
                file=sys.stderr,
            )
            continue
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
    if not _SLUG_RE.match(record.slug):
        raise ValueError(f"invalid idiom slug: {record.slug!r}")
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


_VIEW_DIGEST_NAME = ".view_digest"
_GENERATED_HEADER = "# idioms"


def view_digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_view_digest(profile_dir: Path) -> str:
    try:
        return (store_dir(profile_dir) / _VIEW_DIGEST_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _record_view_digest(profile_dir: Path, text: str) -> None:
    sdir = store_dir(profile_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / _VIEW_DIGEST_NAME
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(view_digest_of(text) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _escape_body(text: str) -> str:
    """Escape structure-forking # / ## lines outside fences, same as teach does."""
    try:
        from chameleon_mcp.tools import _escape_markdown_section_headings

        return _escape_markdown_section_headings(text)
    except Exception:
        return text


def _render_block(rec: IdiomRecord) -> str:
    """One idiom block in the exact byte format today's structured teach writes.

    Deprecated blocks carry no Language line; the Archetype line renders only
    for a single declared archetype (the Stop renderer drops blocks whose tag
    is not an edited archetype, so a joined multi-tag would hide the idiom —
    omitting the line keeps it always-visible, the safe direction). Same logic
    makes a multi-language tag safe: unrecognized tags survive the language
    filter.
    """
    lines: list[str] = [f"### {rec.title}"]
    if rec.status == "active":
        lines.append(f"Language: {', '.join(rec.languages) if rec.languages else 'any'}")
        lines.append(f"Status: active (added {rec.added_date or 'unknown'})")
    else:
        lines.append(f"Status: deprecated {rec.deprecated_date or rec.added_date or 'unknown'}")
    if len(rec.archetypes) == 1:
        lines.append(f"Archetype: {rec.archetypes[0]}")
    if rec.provenance:
        lines.append(f"Source: {' '.join(rec.provenance.split())}")
    lines.append(_escape_body(rec.rationale.strip()))
    for label, items in (("Example:", rec.examples), ("Counterexample:", rec.counterexamples)):
        for code in items:
            lines.append("")
            lines.append(label)
            lines.append("```")
            lines.append(code.rstrip())
            lines.append("```")
    return "\n".join(lines) + "\n"


def render_idioms_md(records: list[IdiomRecord]) -> str:
    """The whole idioms.md view: byte-deterministic, newest-first per section."""
    ordered = sorted(records, key=lambda r: (r.rank, r.slug))
    active = [r for r in ordered if r.status == "active"]
    deprecated = [r for r in ordered if r.status == "deprecated"]
    parts: list[str] = [_GENERATED_HEADER, "", "## active", ""]
    for rec in active:
        parts.append(_render_block(rec))
    parts.append("## deprecated")
    if deprecated:
        parts.append("")
        for rec in deprecated:
            parts.append(_render_block(rec))
    text = "\n".join(parts)
    if not text.endswith("\n"):
        text += "\n"
    return text


def regenerate_views(profile_dir: Path) -> str:
    """Render idioms.md from the store, write it atomically (which re-syncs the
    conventions.md mirror structurally), and record the view digest so a later
    hand- or old-version edit of idioms.md is detectable as a legacy write."""
    from chameleon_mcp.tools import _write_idioms_atomic

    text = render_idioms_md(load_store(profile_dir))
    _write_idioms_atomic(profile_dir / "idioms.md", text)
    _record_view_digest(profile_dir, text)
    return text


_LANGUAGE_LINE_RE = re.compile(r"(?im)^[ \t]*Language:[ \t]*(.+?)[ \t]*$")
_STATUS_ACTIVE_RE = re.compile(
    r"(?im)^[ \t]*Status:[ \t]*active(?:[ \t]*\(added[ \t]+([0-9-]+)\))?"
)
_STATUS_DEPRECATED_RE = re.compile(r"(?im)^[ \t]*Status:[ \t]*deprecated[ \t]*([0-9-]+)?")


def records_from_markdown(text: str) -> tuple[list[IdiomRecord], list[str]]:
    """Import a legacy idioms.md. Every ### block lands in exactly one output:
    a validated record, or the quarantine list (verbatim raw block) when it
    cannot be represented or trips the injection scan. Taught idioms cannot be
    regenerated, so silent drops are forbidden here.

    Two independent fence-aware walks of the same text (`parse_idiom_blocks`
    for structured fields, `_parse_idioms_raw_ordered` for verbatim raw text)
    are joined POSITIONALLY, not by (section, title): a title lookup collapses
    same-titled blocks onto each other (last-wins raw paired with every
    block sharing that title), so a poisoned duplicate's scan could run over
    a benign sibling's raw and vice versa, and Language/Status metadata could
    cross-attribute between them. Both walks share identical block-boundary
    logic, so they line up index-for-index by construction; a mismatch is
    still checked for and quarantines only the affected block rather than
    guessing which raw text belongs to it.
    """
    from chameleon_mcp.idiom_coverage import _parse_idioms_raw_ordered, parse_idiom_blocks

    if not text or not text.strip():
        return [], []
    structured = parse_idiom_blocks(text)
    raw_ordered = _parse_idioms_raw_ordered(text)
    records: list[IdiomRecord] = []
    quarantined: list[str] = []
    taken_slugs: set[str] = set()
    rank = 0
    for idx, block in enumerate(structured):
        title = (block.get("slug") or "").strip()
        section = block.get("section") or "active"
        rationale = (block.get("rationale") or "").strip() or (block.get("body") or "").strip()
        entry = raw_ordered[idx] if idx < len(raw_ordered) else None
        if entry is None or entry.get("title") != title or entry.get("section") != section:
            # The positional-alignment invariant does not hold at this index.
            # Quarantine whatever raw text exists AT THIS POSITION (if any)
            # rather than pair this block's metadata with a different block's
            # raw text.
            raw = entry.get("raw", "") if entry is not None else ""
            quarantined.append(
                raw or (f"### {title}\n{rationale}" if title else rationale) or "### (unparsed)"
            )
            continue
        raw = entry.get("raw", "")
        if not title or not rationale:
            quarantined.append(raw or f"### {title}")
            continue
        hit, label = _scan_suspicious(raw)
        if hit:
            print(
                f"chameleon: idiom {title!r} quarantined during import: matched {label!r} "
                "(edit or re-teach it with safe prose)",
                file=sys.stderr,
            )
            quarantined.append(raw)
            continue
        slug = slug_for_title(title)
        if slug in taken_slugs:
            n = 2
            while f"{slug}-{n}" in taken_slugs:
                n += 1
            slug = f"{slug}-{n}"
        taken_slugs.add(slug)
        # Language:/Status: lines are metadata only in the PROSE region; a
        # fenced example (worst on deprecated blocks, which have no real
        # Language line) can contain the literal text "Language: python" as
        # payload, not a real tag. Same sniff _render_stop_idioms uses.
        pre_fence = raw.split("```", 1)[0]
        lang_m = _LANGUAGE_LINE_RE.search(pre_fence)
        languages = []
        if lang_m:
            languages = [w.strip().lower() for w in lang_m.group(1).split(",") if w.strip()]
            if languages == ["any"]:
                languages = []
        added, deprecated_on = "", ""
        m = _STATUS_ACTIVE_RE.search(pre_fence)
        if m:
            added = m.group(1) or ""
        m = _STATUS_DEPRECATED_RE.search(pre_fence)
        if m:
            deprecated_on = m.group(1) or ""
        try:
            rec = IdiomRecord(
                slug=slug,
                title=title,
                rationale=rationale,
                languages=languages,
                archetypes=[a for a in [block.get("archetype")] if a],
                status="deprecated" if section == "deprecated" else "active",
                added_date=added,
                deprecated_date=deprecated_on,
                examples=[e for e in [block.get("example") or ""] if e],
                counterexamples=[c for c in [block.get("counterexample") or ""] if c],
                provenance=(block.get("source") or "").strip(),
                rank=rank + 1,
            )
        except ValueError:
            quarantined.append(raw)
            continue
        rank += 1
        records.append(rec)
    return records, quarantined
