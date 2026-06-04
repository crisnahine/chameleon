"""Shared renderer for profile.summary.md.

Both the bootstrap orchestrator (_build_summary_md) and the rename tool
(_rewrite_summary_md) produce the same Markdown shape. This module owns
that shape so the two call sites stay in sync.
"""

from __future__ import annotations


def count_terminal_rules(block: dict, depth: int = 0) -> int:
    """Return a rough count of terminal rule entries in a nested config block.

    Counts every leaf (each scalar, each list element), so a cop with five
    Exclude paths counts as five. That over-counts the cop SET roughly 2x, which
    is why the summary renderer no longer uses it for the "rules extracted" line;
    ``count_config_rules`` counts cop/rule keys instead. Kept for callers that
    genuinely want a leaf tally. Caps recursion at depth 6.
    """
    if depth > 6 or not isinstance(block, dict):
        return 0
    count = 0
    for v in block.values():
        if isinstance(v, dict):
            count += count_terminal_rules(v, depth + 1)
        elif isinstance(v, list):
            count += len(v)
        else:
            count += 1
    return count


# Top-level keys in a parsed tool config that are config scaffolding, not a
# cop/rule the user wrote. Excluding them keeps the "rules extracted" count to
# the actual cop set a human means (a rubocop file's ``AllCops`` / ``require`` /
# ``inherit_from`` are setup, not cops).
_CONFIG_META_KEYS: frozenset[str] = frozenset(
    {
        "allcops",
        "require",
        "inherit_from",
        "inherit_mode",
        "inherit_gem",
        "plugins",
        "extends",
        "env",
        "parser",
        "parseroptions",
        "settings",
        "overrides",
        "globals",
        "root",
        "ignorepatterns",
    }
)


def count_config_rules(tool_block: dict) -> int:
    """Count the cop/rule KEYS a tool config declares, not its config leaves.

    ``count_terminal_rules`` counts every nested leaf, so a rubocop block with
    ~36 cops reads as ~71 because each cop's sub-options and Exclude paths are
    tallied. A human reading "N rule(s) extracted" means the cop set. This counts
    the rule-bearing keys instead:

    - eslint-shaped configs nest the rules under a ``rules`` sub-dict; count its
      keys.
    - rubocop / editorconfig / tsconfig put the entries at the top level; count
      those keys minus config scaffolding (``AllCops``, ``require``, ...).

    The ``source`` / ``parse_warning`` / ``extends_chain`` wrapper keys added by
    the orchestrator are stripped first so they never inflate the count.
    """
    if not isinstance(tool_block, dict):
        return 0
    inner = tool_block.get("rules")
    if not isinstance(inner, dict):
        # No nested config payload (e.g. a parse-warning-only block).
        return 0
    nested = inner.get("rules")
    if isinstance(nested, dict):
        # eslint shape: the real rule map is one level down.
        return len(nested)
    # Flat shape (rubocop / editorconfig / tsconfig): top-level keys are the
    # cops/rules; drop the known scaffolding keys.
    return sum(1 for k in inner if str(k).lower() not in _CONFIG_META_KEYS)


def extract_idioms_section(idioms_md: str, marker: str) -> str:
    """Return the contents of the given level-2 section of an idioms.md doc.

    Returns an empty string when the marker is absent OR when the section
    body is just the ``_(none)_`` / "no idioms yet" placeholder.
    """
    if marker not in idioms_md:
        return ""
    after = idioms_md.split(marker, 1)[1]
    section = after.split("\n## ", 1)[0] if "\n## " in after else after
    section = section.strip()
    if not section or section == "_(none)_" or "no idioms yet" in section:
        return ""
    return section


def render_summary_md(
    *,
    archetypes: dict,
    canonicals: dict,
    profile_meta: dict,
    idioms_text: str,
    rules_data: dict | None = None,
    engine_version: str | None = None,
) -> str:
    """Generate the human-readable profile.summary.md.

    Parameters
    ----------
    archetypes:
        The parsed archetypes.json (must contain an ``"archetypes"`` key).
    canonicals:
        The parsed canonicals.json (must contain a ``"canonicals"`` key).
    profile_meta:
        The parsed profile.json metadata dict.
    idioms_text:
        The raw contents of idioms.md.
    rules_data:
        The parsed rules.json bundle, or None when unavailable.
    engine_version:
        Engine version string for the header. When None, falls back to
        ``profile_meta["engine_min_version"]`` (set by the rename path
        which reads it from the on-disk profile).
    """
    version = engine_version or profile_meta.get("engine_min_version", "")

    lines = [
        "# chameleon profile summary",
        "",
        f"Generated: {profile_meta.get('created_at', '')}",
        f"Engine: chameleon v{version}",
        f"Language: {profile_meta.get('language', '')}",
        f"Source: {profile_meta.get('source', 'bootstrap')}",
        f"Generation: {profile_meta.get('generation', '')}",
        f"Schema version: {profile_meta.get('schema_version', '')}",
        "",
    ]

    hint = profile_meta.get("language_hint")
    if isinstance(hint, dict) and hint.get("secondary_detected"):
        lines.extend(
            [
                "## Secondary language detected",
                "",
                (
                    f"This bootstrap scanned **{hint.get('primary', '?')}** only. "
                    f"A sibling **{hint['secondary_detected']}** codebase "
                    f"({hint.get('secondary_file_count', 0)} files at "
                    f"`{hint.get('secondary_path', '')}`) was deliberately excluded."
                ),
                "",
                hint.get("note", ""),
                "",
            ]
        )

    lines.extend(
        [
            f"## {profile_meta.get('archetype_count', 0)} archetypes detected",
            "",
        ]
    )
    for name, arch in sorted((archetypes.get("archetypes") or {}).items()):
        canonical_entries = (canonicals.get("canonicals") or {}).get(name) or []
        first = canonical_entries[0] if canonical_entries else None
        witness = first.get("witness") if isinstance(first, dict) else None
        canonical_path = (
            witness.get("path") if isinstance(witness, dict) and witness.get("path") else "(none)"
        )
        display_paths = arch.get("paths_pattern_display") or arch.get("paths_pattern", "")
        lines.append(
            f"- **{name}** (cluster_size {arch.get('cluster_size', 0)}, "
            f"paths {display_paths}) — canonical: `{canonical_path}`"
        )

    lines.extend(["", "## Rules", ""])
    rules_block = (rules_data or {}).get("rules") if rules_data else None
    detected_tools = sorted(rules_block.keys()) if isinstance(rules_block, dict) else []
    if detected_tools:
        lines.append(
            f"_Auto-derived from {len(detected_tools)} tool config file(s): "
            f"{', '.join(f'`{t}`' for t in detected_tools)}._"
        )
        lines.append("")
        for tool in detected_tools:
            tool_block = rules_block[tool]
            if not isinstance(tool_block, dict):
                continue
            rule_count = count_config_rules(tool_block)
            lines.append(f"- **{tool}** — {rule_count} rule(s) extracted")
    else:
        lines.append(
            "_No tool-config rules detected._ The bootstrap looked for "
            "`eslint`, `tsconfig`, `prettier`, `rubocop`, and `.editorconfig` "
            "and found none of them. Auto-derived rules will appear here "
            "once those configs exist."
        )

    lines.extend(["", "## Idioms", ""])
    active_idioms = extract_idioms_section(idioms_text, "## active")
    if active_idioms:
        lines.append(
            "_The following idioms ship in this profile and will be injected "
            "into the model's context before each Edit/Write. Review carefully "
            "before granting trust._"
        )
        lines.append("")
        lines.append(active_idioms)
        lines.append("")
    else:
        lines.append("_No idioms captured yet. Run /chameleon-teach to record team conventions._")
        lines.append("")

    deprecated_idioms = extract_idioms_section(idioms_text, "## deprecated")
    if deprecated_idioms:
        lines.append("## Deprecated idioms")
        lines.append("")
        lines.append(
            "_The following idioms were retired by `/chameleon-teach`. They "
            "are kept here for audit history and are NOT injected into "
            "context._"
        )
        lines.append("")
        lines.append(deprecated_idioms)
        lines.append("")

    return "\n".join(lines)
