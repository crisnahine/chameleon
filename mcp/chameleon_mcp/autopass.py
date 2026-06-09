"""Auto-pass router: classify a change as auto-pass-eligible or human-mandatory.

The honest ceiling for a machine review gate is not "catch every bug" (no 2026
system does; the measured frontier is ~45-55% of human-flagged issues). It is to
correctly identify the *routine slice* that is safe to auto-pass and route
everything else to a human with a reason. This module is the calibrated router
that draws that line.

A change is auto-pass-eligible only when ALL hold:

- no grounded blocking finding fired (an active block-eligible rule on the diff),
- it touches no security-sensitive surface (auth / payment / crypto / migration /
  infra) — those always go to a human regardless of how clean they look,
- it is small (within the file and line caps for a routine change),
- its blast radius is bounded (few cross-file importers of the symbols it changed),
- it stays inside profiled archetypes (a file the engine has no canonical for
  cannot be vouched for, so it is not auto-passable).

Each failing predicate adds a human-readable reason; an empty reason list means
the change cleared every gate. The router never *blocks* — it only decides
whether human review is mandatory; the residual it sends to humans is the real,
irreducible part of review the evidence says no machine removes.
"""

from __future__ import annotations

# Path substrings that mark a security-sensitive surface, by category. A change
# touching any of these always goes to a human, however clean it looks: these are
# the classes where a machine false-negative is most expensive (auth bypass, a bad
# charge, a leaked secret, an irreversible migration, a broken deploy). Heuristic
# and deliberately broad — a false "needs human" here only costs one review, while
# a false "safe to auto-pass" on an auth file is exactly the failure to avoid.
# Substrings are matched against the POSIX-normalized, lower-cased path.
_SECURITY_SURFACE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "auth",
        (
            "auth",
            "session",
            "login",
            "password",
            "/jwt",
            "oauth",
            "devise",
            "authoriz",
            "permission",
            "/policies/",
            "_policy",
            "ability",
        ),
    ),
    (
        "payment",
        ("payment", "billing", "invoice", "charge", "stripe", "subscription", "checkout"),
    ),
    (
        "crypto",
        ("crypto", "encrypt", "decrypt", "secret", "credential", "lockbox", "vault", "signing"),
    ),
    (
        "migration",
        ("db/migrate/", "schema.rb", "/migrations/", "structure.sql"),
    ),
    (
        "infra",
        (
            "dockerfile",
            "docker-compose",
            ".github/workflows/",
            "terraform",
            ".tf",
            "kubernetes",
            "k8s",
            "/helm/",
            "nginx",
            "/deploy",
        ),
    ),
)


def classify_security_surface(path: str) -> str | None:
    """Return the security-sensitive category a path falls in, or None.

    First matching category wins (auth before payment before crypto, etc.); the
    caller only needs to know the change touches *some* sensitive surface, the
    category is for the human-readable reason.
    """
    if not path:
        return None
    norm = str(path).replace("\\", "/").lower()
    for category, needles in _SECURITY_SURFACE_PATTERNS:
        if any(n in norm for n in needles):
            return category
    return None


def security_surface_categories(paths) -> set[str]:
    """The set of security-sensitive categories touched by a changeset's paths."""
    out: set[str] = set()
    for p in paths or ():
        cat = classify_security_surface(p)
        if cat is not None:
            out.add(cat)
    return out


def parse_numstat(text: str) -> list[dict]:
    """Parse ``git diff --numstat`` output into per-file line deltas.

    Each non-empty line is ``<added>\\t<removed>\\t<path>``; binary files render
    the counts as ``-`` and carry no line count (recorded as 0/0). The path is
    everything after the second tab, so spaces and rename arrows survive intact.
    Malformed lines are skipped rather than raising, since this feeds an advisory
    verdict that must fail open.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_s, removed_s, path = parts
        if not path:
            continue
        added = 0 if added_s.strip() == "-" else _safe_int(added_s)
        removed = 0 if removed_s.strip() == "-" else _safe_int(removed_s)
        if added is None or removed is None:
            continue
        rows.append({"path": path, "added": added, "removed": removed})
    return rows


def _safe_int(s: str) -> int | None:
    try:
        return int(s.strip())
    except (TypeError, ValueError):
        return None


def count_added_files(name_status_text: str) -> int:
    """Count newly-added files in ``git diff --name-status`` output.

    Lines are ``<status>\\t<path>`` (renames carry two paths); a brand-new file
    is status ``A``. New files weigh on the verdict because a freshly-added file
    has no prior canonical the engine calibrated against.
    """
    count = 0
    for line in (name_status_text or "").splitlines():
        if not line.strip():
            continue
        status = line.split("\t", 1)[0].strip()
        if status[:1] == "A":
            count += 1
    return count


def build_autopass_verdict(
    numstat_text: str,
    name_status_text: str,
    *,
    is_unarchetyped,
    importers_of,
    block_findings_for,
    type_error_files=None,
    max_files: int = 10,
    max_lines: int = 150,
    max_blast_radius: int = 10,
) -> dict:
    """End-to-end auto-pass verdict from a branch's raw git diff output.

    Composes the pure pipeline (parse -> assemble -> classify) so the only thing
    a caller supplies beyond the two git outputs is the three engine adapters
    (archetype coverage, cross-file importers, active block findings). Returns the
    classifier verdict plus the changed-file list and the assembled facts, so a
    reviewer can see *what* drove the decision.
    """
    rows = parse_numstat(numstat_text)
    changed = [r["path"] for r in rows]
    added = sum(r["added"] for r in rows)
    removed = sum(r["removed"] for r in rows)
    facts = assemble_facts(
        changed,
        added_lines=added,
        removed_lines=removed,
        new_files=count_added_files(name_status_text),
        is_unarchetyped=is_unarchetyped,
        importers_of=importers_of,
        block_findings_for=block_findings_for,
        type_error_files=type_error_files,
    )
    verdict = classify_change(
        facts,
        max_files=max_files,
        max_lines=max_lines,
        max_blast_radius=max_blast_radius,
    )
    return {"changed_files": changed, "facts": facts, **verdict}


def assemble_facts(
    changed_files,
    *,
    added_lines: int,
    removed_lines: int,
    new_files: int,
    is_unarchetyped,
    importers_of,
    block_findings_for,
    type_error_files=None,
) -> dict:
    """Turn a changeset into the fact dict ``classify_change`` consumes.

    The I/O is injected so the orchestration is testable apart from the engine
    plumbing: ``is_unarchetyped(path)`` (the engine has no canonical to vouch for
    it), ``importers_of(path)`` (cross-file fan-out from the reverse index), and
    ``block_findings_for(path)`` (active block-eligible violations on the file).
    ``type_error_files`` is the optional set of files a typecheck (R4 grounding)
    reported errors in; only changed files in it count, so a change that does not
    compile routes to a human. ``blast_radius`` is the worst single-file fan-out,
    not the sum, so one widely-imported file in the set is what gates, not a count
    inflated by many leaf files.
    """
    files = list(changed_files or ())
    type_errs = set(type_error_files or ())
    return {
        "files_changed": len(files),
        "lines_changed": int(added_lines) + int(removed_lines),
        "new_files": int(new_files),
        "unarchetyped_files": sum(1 for p in files if is_unarchetyped(p)),
        "blast_radius": max((int(importers_of(p)) for p in files), default=0),
        "active_block_findings": sum(int(block_findings_for(p)) for p in files),
        "type_errors": sum(1 for p in files if p in type_errs),
        "security_surface": bool(security_surface_categories(files)),
    }


def classify_change(
    facts: dict,
    *,
    max_files: int = 10,
    max_lines: int = 150,
    max_blast_radius: int = 10,
) -> dict:
    """Return an auto-pass verdict for one change.

    ``facts`` carries the assembled signals (files_changed, lines_changed,
    new_files, unarchetyped_files, blast_radius, active_block_findings,
    security_surface). Missing keys are treated as their safe default (0 / False),
    so a partially-assembled fact set never silently auto-passes on absent data.
    """
    reasons: list[str] = []

    def _int(key: str) -> int:
        try:
            return int(facts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    findings = _int("active_block_findings")
    if findings > 0:
        reasons.append(f"{findings} blocking finding(s) unresolved")

    type_errors = _int("type_errors")
    if type_errors > 0:
        reasons.append(f"{type_errors} file(s) with type errors")

    if bool(facts.get("security_surface")):
        reasons.append("touches a security-sensitive surface")

    files = _int("files_changed")
    lines = _int("lines_changed")
    if files > max_files or lines > max_lines:
        reasons.append(f"change too large ({files} files / {lines} lines)")

    blast = _int("blast_radius")
    if blast > max_blast_radius:
        reasons.append(f"high blast radius ({blast} importers)")

    unarchetyped = _int("unarchetyped_files")
    if unarchetyped > 0:
        reasons.append(f"{unarchetyped} file(s) outside profiled archetypes")

    eligible = not reasons
    if eligible:
        risk = "low"
    elif findings > 0 or type_errors > 0 or bool(facts.get("security_surface")):
        # Grounded failures (a block finding, a type error) and the security
        # surface are high-confidence reasons; size/blast/archetype are softer.
        risk = "high"
    else:
        risk = "elevated"

    return {"auto_pass_eligible": eligible, "risk": risk, "reasons": reasons}
