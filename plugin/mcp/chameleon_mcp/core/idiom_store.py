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
import shutil
import sys
import time
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
    from chameleon_mcp.safe_open import safe_read_profile_artifact

    records: list[IdiomRecord] = []
    sdir = store_dir(profile_dir)
    try:
        paths = sorted(sdir.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            raw = json.loads(safe_read_profile_artifact(path))
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
    regenerated, so silent drops are forbidden here -- and that no-silent-drop
    contract covers ALL of idioms.md, not just its "### " blocks: content that
    lives outside every block and every fence (a hand-written preamble, or a
    whole legacy file the pre-store system injected as plain prose with no
    header structure at all) is carried over too, as a single synthesized
    'legacy-notes' record (or quarantined, same as a poisoned block).

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
    from chameleon_mcp.idiom_coverage import (
        _parse_idioms_raw_ordered,
        _parse_loose_prose,
        parse_idiom_blocks,
    )

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

    # Content outside every block: a hand-written preamble, or (headerless
    # legacy files) the whole document. The old system injected such text
    # wholesale, so dropping it here would silently erase guidance a user
    # relied on -- carry it into one synthesized record, or quarantine it
    # like a poisoned block, same as every other path through this function.
    prose = _parse_loose_prose(text)
    if prose:
        hit, label = _scan_suspicious(prose)
        if hit:
            print(
                f"chameleon: legacy prose outside any idiom block quarantined during import: "
                f"matched {label!r} (edit or re-teach it with safe prose)",
                file=sys.stderr,
            )
            quarantined.append(prose)
        else:
            slug = "legacy-notes"
            if slug in taken_slugs:
                n = 2
                while f"{slug}-{n}" in taken_slugs:
                    n += 1
                slug = f"{slug}-{n}"
            taken_slugs.add(slug)
            try:
                rec = IdiomRecord(
                    slug=slug,
                    title=slug,
                    rationale=prose,
                    languages=[],
                    status="active",
                    rank=rank + 1,
                )
            except ValueError:
                quarantined.append(prose)
            else:
                records.append(rec)

    return records, quarantined


_QUARANTINE_NAME = ".quarantine.md"


def _idioms_lock_path(profile_dir: Path, repo_id: str | None) -> Path:
    """Per-repo lock path. A falsy repo_id (a caller that hasn't resolved one)
    must not bucket every such repo under one shared "unknown" lock -- that
    would serialize unrelated repos' migrations against each other and let one
    repo's lock hold block another's. Derive a real id from the profile's own
    repo root instead; only fall back to the shared bucket if that derivation
    itself fails."""
    from chameleon_mcp.profile.trust import repo_data_dir

    if isinstance(repo_id, str) and repo_id:
        rid = repo_id
    else:
        try:
            from chameleon_mcp.tools import _compute_repo_id

            rid = _compute_repo_id(profile_dir.parent)
        except Exception:
            rid = "unknown"
    return repo_data_dir(rid) / ".idioms.lock"


def _write_quarantine(profile_dir: Path, blocks: list[str]) -> None:
    """Append newly-quarantined blocks to the review file, never overwrite it.

    A migration and a later legacy-write re-import are independent quarantine
    events; a prior batch is preserved verbatim until a human reviews it, so a
    second event must not silently destroy the first (same "no silent drops"
    contract records_from_markdown holds for the store itself).
    """
    if not blocks:
        return
    path = store_dir(profile_dir) / _QUARANTINE_NAME
    try:
        existing = path.read_text(encoding="utf-8")
    except Exception:
        # An unreadable prior file (missing, or corrupt -- e.g. non-UTF-8
        # bytes from a damaged write) must not abort this write: the NEW
        # batch is never allowed to be lost, so an unreadable existing file
        # is treated as empty and the header is re-rendered from scratch.
        existing = ""
    new_section = "\n\n".join(b.rstrip() for b in blocks) + "\n"
    if existing.strip():
        payload = existing.rstrip("\n") + "\n\n" + new_section
    else:
        payload = (
            "# quarantined idiom blocks (preserved verbatim; review and re-teach)\n\n" + new_section
        )
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def migrate_idioms_md(profile_dir: Path, *, repo_id: str | None) -> dict:
    """One-time idioms.md -> store migration. User-initiated write paths only.

    Order: parse once, preserve the original as idioms.md.legacy, write the
    records, quarantine what cannot carry over, regenerate the views (a store
    write, so the conventions.md mirror re-syncs), record the view digest.
    Trust is re-stamped only when the profile was already trusted AND nothing
    was quarantined: a migration that dropped content must leave trust for the
    user to re-review, never bless restructured content automatically.
    """
    from chameleon_mcp.locks import acquire_advisory_lock

    if store_exists(profile_dir):
        return {"status": "noop"}
    was_trusted = False
    try:
        from chameleon_mcp.tools import _profile_trusted_now

        was_trusted = bool(repo_id) and _profile_trusted_now(repo_id, profile_dir)
    except Exception:
        was_trusted = False
    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        if store_exists(profile_dir):
            return {"status": "noop"}
        md_path = profile_dir / "idioms.md"
        try:
            original = md_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        records, quarantined = records_from_markdown(original)
        legacy = profile_dir / "idioms.md.legacy"
        try:
            store_dir(profile_dir).mkdir(parents=True, exist_ok=True)
            # Write-once: a prior crash may already have preserved the true
            # original here. Overwriting it on a retry would burn that copy
            # in favor of whatever idioms.md happens to hold post-crash
            # (possibly the already-regenerated, no-longer-original view).
            if original and not legacy.exists():
                tmp = legacy.with_name(f"{legacy.name}.{os.getpid()}.tmp")
                tmp.write_text(original, encoding="utf-8")
                os.replace(tmp, legacy)
            for rec in records:
                upsert_idiom(profile_dir, rec)
            _write_quarantine(profile_dir, quarantined)
            regenerate_views(profile_dir)
        except Exception:
            # A half-written store would permanently short-circuit every future
            # call through the store_exists() guard above -- roll back so a
            # retry starts clean instead of silently losing the unwritten
            # records forever.
            shutil.rmtree(store_dir(profile_dir), ignore_errors=True)
            # regenerate_views() may have already replaced idioms.md with the
            # clean regenerated view before the crash (e.g. a failure inside
            # _record_view_digest, which runs after _write_idioms_atomic has
            # already succeeded). Left alone, a retry would re-derive records
            # and quarantine from that already-migrated output instead of the
            # true original -- fabricating an empty quarantine and risking an
            # auto trust re-grant over content that was originally poisoned.
            # Restore from the write-once legacy copy so a retry always
            # re-parses the true original.
            if legacy.exists():
                try:
                    legacy_text = legacy.read_text(encoding="utf-8")
                    tmp = md_path.with_name(f"{md_path.name}.{os.getpid()}.tmp")
                    tmp.write_text(legacy_text, encoding="utf-8")
                    os.replace(tmp, md_path)
                except OSError:
                    pass
            if store_exists(profile_dir):
                # A partial unlink (e.g. a permissions error mid-tree) left
                # the store directory behind; the store_exists() guard above
                # will treat that as an already-migrated repo forever, so say
                # so instead of letting the caller believe rollback succeeded.
                print(
                    "chameleon: idiom migration rollback incomplete -- "
                    ".chameleon/idioms/ still exists after a failed migration; "
                    "remove it manually before retrying",
                    file=sys.stderr,
                )
            raise
    n_in = len(records) + len(quarantined)
    print(
        f"chameleon: idioms migrated to .chameleon/idioms/ "
        f"({len(records)}/{n_in} carried, {len(quarantined)} quarantined; "
        "original kept as idioms.md.legacy)",
        file=sys.stderr,
    )
    if quarantined:
        print(
            "chameleon: trust NOT re-stamped (quarantined blocks need review; "
            "run /chameleon-trust after checking .chameleon/idioms/.quarantine.md)",
            file=sys.stderr,
        )
    else:
        try:
            from chameleon_mcp.tools import _regrant_trust_if_was_trusted

            _regrant_trust_if_was_trusted(was_trusted, repo_id, profile_dir)
        except Exception:
            pass
    return {
        "status": "migrated",
        "idioms_in": n_in,
        "idioms_out": len(records),
        "quarantined": len(quarantined),
    }


def ensure_store_fresh(profile_dir: Path, *, repo_id: str | None) -> dict:
    """Detect a legacy write (v3 teammate teach, hand edit) to the generated
    idioms.md and fold the DELTA into the store before the next store write
    regenerates the view — a teammate's idiom must never be silently discarded.
    Additive only: store records absent from the view are kept (a hand-truncated
    view must not delete truth).

    A view edit that moves an already-known idiom from active to deprecated
    (a v3 teammate's deprecation, or a hand edit) is folded into the store the
    same way a legacy addition is. The reverse — deprecated to active via the
    view — is never auto-applied: reactivating a retired idiom requires an
    explicit teach, so a stray or malicious view edit cannot silently revive
    one.

    Returns ``{"added": int, "folded": int, "quarantined": int}``.
    """
    from chameleon_mcp.locks import acquire_advisory_lock

    _noop = {"added": 0, "folded": 0, "quarantined": 0}
    if not store_exists(profile_dir):
        return _noop
    md_path = profile_dir / "idioms.md"
    try:
        current = md_path.read_text(encoding="utf-8")
    except OSError:
        return _noop
    if view_digest_of(current) == read_view_digest(profile_dir):
        return _noop
    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        try:
            current = md_path.read_text(encoding="utf-8")
        except OSError:
            return _noop
        if view_digest_of(current) == read_view_digest(profile_dir):
            return _noop
        incoming, quarantined = records_from_markdown(current)
        existing = load_store(profile_dir)
        known_slugs = {r.slug for r in existing}
        known_titles = {r.title for r in existing}
        added = 0
        folded = 0
        min_rank = min((r.rank for r in existing), default=1)
        for rec in incoming:
            if rec.slug in known_slugs or rec.title in known_titles:
                match = next((r for r in existing if r.slug == rec.slug), None)
                if match is None:
                    match = next((r for r in existing if r.title == rec.title), None)
                if match is not None and match.status != rec.status:
                    if match.status == "active" and rec.status == "deprecated":
                        match.status = "deprecated"
                        match.deprecated_date = rec.deprecated_date or time.strftime(
                            "%Y-%m-%d", time.gmtime()
                        )
                        upsert_idiom(profile_dir, match)
                        folded += 1
                    else:
                        print(
                            f"chameleon: idioms.md edit to '{rec.title}' not folded into "
                            "the store (status change via the view is ignored; use "
                            "/chameleon-teach)",
                            file=sys.stderr,
                        )
                continue
            rec.rank = min_rank - 1 - added
            upsert_idiom(profile_dir, rec)
            added += 1
        _write_quarantine(profile_dir, quarantined)
        regenerate_views(profile_dir)
    print(
        f"chameleon: legacy idioms.md write detected; {added} idiom(s) re-imported, "
        f"{folded} status transition(s) folded, into the store "
        f"({len(quarantined)} quarantined)",
        file=sys.stderr,
    )
    return {"added": added, "folded": folded, "quarantined": len(quarantined)}


def _norm_rationale(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def teach_record(profile_dir: Path, record: IdiomRecord, *, repo_id: str | None) -> str:
    """Add one idiom. Duplicate = same normalized rationale + same archetype set
    among ACTIVE records (the dedup contract the markdown teach used)."""
    from chameleon_mcp.locks import acquire_advisory_lock

    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        existing = load_store(profile_dir)
        want = (_norm_rationale(record.rationale), tuple(sorted(record.archetypes)))
        for r in existing:
            if r.status != "active":
                continue
            if (_norm_rationale(r.rationale), tuple(sorted(r.archetypes))) == want:
                return "duplicate"
        if find_by_slug(existing, record.slug) is not None:
            n = 2
            while find_by_slug(existing, f"{record.slug}-{n}") is not None:
                n += 1
            # Every caller sets title == slug before this rename fires; keep
            # them matched so the rendered `### {title}` header agrees with
            # the slug the record is actually addressed by afterward.
            if record.title == record.slug:
                record.title = f"{record.slug}-{n}"
            record.slug = f"{record.slug}-{n}"
        record.rank = min((r.rank for r in existing), default=1) - 1
        upsert_idiom(profile_dir, record)
        regenerate_views(profile_dir)
    return "added"


def deprecate_record(
    profile_dir: Path,
    slug: str,
    *,
    timestamp: str,
    rationale: str = "",
    example: str | None = None,
    counterexample: str | None = None,
    provenance: str | None = None,
    repo_id: str | None,
) -> str:
    """Move a slug to deprecated, PRESERVING its body. The optional arguments
    are appended as a deprecation note, never a replacement."""
    from chameleon_mcp.locks import acquire_advisory_lock

    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        records = load_store(profile_dir)
        rec = find_by_slug(records, slug)
        if rec is None or rec.status != "active":
            return "absent"
        rec.status = "deprecated"
        rec.deprecated_date = timestamp
        note = (rationale or "").strip()
        if note and _norm_rationale(note) != _norm_rationale(rec.rationale):
            rec.rationale = f"{rec.rationale.rstrip()}\n\nDeprecated: {note}"
        if example:
            rec.examples.append(example)
        if counterexample:
            rec.counterexamples.append(counterexample)
        if provenance:
            rec.provenance = provenance
        upsert_idiom(profile_dir, rec)
        regenerate_views(profile_dir)
    return "deprecated"


def reactivate_record(profile_dir: Path, slug: str, *, timestamp: str, repo_id: str | None) -> str:
    from chameleon_mcp.locks import acquire_advisory_lock

    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        records = load_store(profile_dir)
        rec = find_by_slug(records, slug)
        if rec is None or rec.status != "deprecated":
            return "absent"
        rec.status = "active"
        rec.added_date = timestamp
        rec.deprecated_date = ""
        upsert_idiom(profile_dir, rec)
        regenerate_views(profile_dir)
    return "reactivated"


def tombstone_record(profile_dir: Path, record: IdiomRecord, *, repo_id: str | None) -> None:
    """Write a brand-new record directly as deprecated (a tombstone teach:
    'never do X' recorded without ever having been active). No dedup — a
    tombstone for an existing rationale is still a distinct decision."""
    from chameleon_mcp.locks import acquire_advisory_lock

    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        existing = load_store(profile_dir)
        if find_by_slug(existing, record.slug) is not None:
            n = 2
            while find_by_slug(existing, f"{record.slug}-{n}") is not None:
                n += 1
            # Every caller sets title == slug before this rename fires; keep
            # them matched so the rendered `### {title}` header agrees with
            # the slug the record is actually addressed by afterward.
            if record.title == record.slug:
                record.title = f"{record.slug}-{n}"
            record.slug = f"{record.slug}-{n}"
        record.status = "deprecated"
        record.rank = min((r.rank for r in existing), default=1) - 1
        upsert_idiom(profile_dir, record)
        regenerate_views(profile_dir)


def rename_archetypes(profile_dir: Path, renames: dict[str, str], *, repo_id: str | None) -> int:
    """Rewrite record archetype tags after a profile-wide archetype rename.

    The store is truth: without this, a rename is inert for idiom scoping and
    the view reverts on the next regeneration. Returns records changed."""
    from chameleon_mcp.locks import acquire_advisory_lock

    if not store_exists(profile_dir) or not renames:
        return 0
    changed = 0
    with acquire_advisory_lock(_idioms_lock_path(profile_dir, repo_id), blocking_timeout=10.0):
        for rec in load_store(profile_dir):
            new = [renames.get(a, a) for a in rec.archetypes]
            if new != rec.archetypes:
                rec.archetypes = new
                upsert_idiom(profile_dir, rec)
                changed += 1
        if changed:
            regenerate_views(profile_dir)
    return changed
