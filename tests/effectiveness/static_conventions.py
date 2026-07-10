"""Static-conventions control arm: the profile rendered once into CLAUDE.md.

The "static" arm isolates chameleon's delivery mechanism (per-edit,
archetype-aware, tiered injection) from its derived content. The realistic
alternative to chameleon is not "no conventions at all" — it is a static
conventions file checked into the repo — so the control arm gets the same
knowledge chameleon derived, written once into the worktree's CLAUDE.md,
while the plugin itself stays disabled (CHAMELEON_DISABLE=1). Both arms then
see the same knowledge; only chameleon delivers it contextually. Measured
lift over this arm is attributable to the delivery mechanism, not to "any
conventions help".

The conventions section is EXACTLY chameleon's own SessionStart injection:
it is produced by the same ``format_conventions_for_session`` call the
SessionStart hook makes, fed through the same data pipeline (conventions.json
parsed from disk, prose-scrubbed, recursively sanitized; principles.md read
via ``safe_prose_text`` and sanitized). Every section SessionStart renders —
IMPORTS, NAMING, INHERITANCE, CONTRACT, AUTHZ, PATTERNS, REUSE, SHAPE, ERROR
HANDLING, IMPORT ORDERING, DOC COVERAGE, TEST PAIRING, PRINCIPLES, and the
ANTI-HALLUCINATION PROTOCOL — appears here verbatim, so the control arm can
never be under-informed relative to the chameleon arm's session block.

Two sections are deliberately MORE generous than SessionStart injects:
profile.summary.md (chameleon surfaces it via tools, not at SessionStart)
and the active team idioms (chameleon injects idioms per-edit at Tier-2, not
at SessionStart). Both are included on purpose: the control gets everything
chameleon knows, written down once, so delivery is the only variable.

Eval infrastructure, not the fail-open plugin: a missing or corrupt profile
artifact RAISES so a broken fixture can never silently run an empty control
arm. principles.md is the one artifact read with SessionStart's own
tolerance (``safe_prose_text``: missing / unreadable / injection-tripping
reads as absent) because the chameleon arm would drop it the same way —
raising here would make the control stricter than the treatment. Output is
deterministic so repeated runs are hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

_SENTINEL = "<!-- chameleon-static-conventions -->"
_HEADER = "## Codebase conventions"
# The summary and idioms are bounded here; the conventions section is bounded
# upstream (format_conventions_for_session's own CHAMELEON_MAX_CONVENTION_ITEMS
# caps) and is never truncated locally — cutting it would re-open the
# under-informed-control gap this renderer exists to close.
_MAX_SUMMARY_LINES = 80
_MAX_IDIOM_LINES = 120


class StaticConventionsError(Exception):
    pass


def render_static_conventions(worktree: Path) -> str:
    """Write the static-conventions CLAUDE.md into ``worktree``; return its text.

    Reads the worktree's committed ``.chameleon`` profile (profile.summary.md,
    conventions.json, principles.md, idioms.md) and writes a deterministic
    conventions block into ``<worktree>/CLAUDE.md``. An existing CLAUDE.md is
    preserved and the block appended under "## Codebase conventions".
    Idempotent: the sentinel comment marks a rendered block, so a second call
    changes nothing.
    """
    profile_dir = worktree / ".chameleon"
    if not profile_dir.is_dir():
        raise StaticConventionsError(f"no .chameleon profile directory in {worktree}")

    claude_md = worktree / "CLAUDE.md"
    existing = ""
    if claude_md.is_file():
        existing = claude_md.read_text(encoding="utf-8")
        if _SENTINEL in existing:
            return existing  # already rendered — never duplicate the block

    block = _render_block(profile_dir)
    if existing.strip():
        text = existing.rstrip("\n") + "\n\n" + block + "\n"
    else:
        text = block + "\n"
    claude_md.write_text(text, encoding="utf-8")
    return text


def _render_block(profile_dir: Path) -> str:
    summary = _load_summary(profile_dir)
    conventions_block = _session_conventions_block(profile_dir)
    idioms = _active_idioms(profile_dir)

    body: list[str] = []
    body.append("### Profile summary")
    body.append("")
    body.extend(_capped(summary.strip().splitlines(), _MAX_SUMMARY_LINES, "summary"))
    body.append("")
    body.append("### Derived conventions (chameleon SessionStart block)")
    body.append("")
    if conventions_block:
        body.extend(conventions_block.splitlines())
    else:
        body.append("- (no conventions cleared the derivation floors for this profile)")
    if idioms:
        body.append("")
        body.append("### Team idioms (active)")
        body.append("")
        body.extend(_capped(idioms.splitlines(), _MAX_IDIOM_LINES, "idioms"))

    lines = [_HEADER, _SENTINEL, ""]
    lines.extend(body)
    return "\n".join(lines).rstrip("\n")


def _session_conventions_block(profile_dir: Path) -> str:
    """Chameleon's own SessionStart conventions block for this profile.

    Replicates the SessionStart hook's data pipeline: parse conventions.json
    straight from disk, scrub injection-bearing prose values in place,
    sanitize every string at the boundary (inputs, not the assembled block —
    the block's own ``<chameleon-conventions>`` wrapper must survive), and
    read principles.md through the same ``safe_prose_text`` helper. The one
    deliberate divergence is fail-loud on a missing/corrupt conventions.json:
    the hook degrades to an empty dict, a broken eval fixture must raise.
    """
    from chameleon_mcp.conventions import format_conventions_for_session
    from chameleon_mcp.hook_helper import _sanitize_profile_obj
    from chameleon_mcp.profile.loader import safe_prose_text, scrub_conventions_prose
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    data = _load_conventions(profile_dir)
    scrub_conventions_prose(data)
    principles_text = safe_prose_text(profile_dir / "principles.md")
    return format_conventions_for_session(
        _sanitize_profile_obj(data),
        principles_text=sanitize_for_chameleon_context(principles_text),
    )


def _capped(lines: list[str], cap: int, label: str) -> list[str]:
    if len(lines) <= cap:
        return lines
    return lines[:cap] + [f"... ({label} truncated at {cap} lines)"]


def _load_summary(profile_dir: Path) -> str:
    path = profile_dir / "profile.summary.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StaticConventionsError(f"profile.summary.md unreadable: {exc}") from exc
    if not text.strip():
        raise StaticConventionsError(f"profile.summary.md is empty: {path}")
    return text


def _load_conventions(profile_dir: Path) -> dict:
    """The full parsed conventions.json (the shape ``format_conventions_for_session``
    takes), fail-loud on missing/corrupt/mis-shaped."""
    path = profile_dir / "conventions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StaticConventionsError(f"conventions.json unreadable: {exc}") from exc
    except ValueError as exc:
        raise StaticConventionsError(f"conventions.json is corrupt: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("conventions"), dict):
        raise StaticConventionsError(f"conventions.json has no 'conventions' object: {path}")
    return data


def _active_idioms(profile_dir: Path) -> str:
    """The '## active' section of idioms.md, or "" when absent/empty.

    A profile with no taught idioms is legitimate (bootstrap scaffolds an
    empty section), so this is the one artifact whose absence is tolerated —
    the control is still summary + conventions, never empty.
    """
    path = profile_dir / "idioms.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StaticConventionsError(f"idioms.md unreadable: {exc}") from exc
    collected: list[str] = []
    in_active = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower() == "## active":
            in_active = True
            continue
        if in_active and stripped.startswith("## "):
            break
        if in_active:
            collected.append(line)
    return "\n".join(collected).strip()
