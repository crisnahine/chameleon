# Fan-out reviewer prompt (pr-review, large diffs)

Dispatch one in-session `general-purpose` Task subagent per slice with this
prompt. The reviewer is READ-ONLY: it may use `Read` and the read-only chameleon
MCP tools (`get_pattern_context`, `lint_file`, `get_canonical_excerpt`). It must
NEVER use `Edit`, `Write`, `Bash`, `WebFetch`, `WebSearch`, `NotebookEdit`, or
dispatch a nested `Task`.

Fill: {SLICE_FILES} (the files this reviewer owns), {REPO_ID}, and {SLICE_HUNKS}
(each slice file's hunk map from Step 1a: the added/changed line ranges in the
post-change file, plus the removed `-` lines per hunk). {SLICE_HUNKS} is
mandatory — the reviewer has no Bash and cannot re-derive the diff, so without
it the change-delta pass (3e) has no removed lines to read and the hunk gate has
no ranges to check against; a slice dispatched without hunks reviews blind.

```
Review ONLY these files against the repo's chameleon conventions: {SLICE_FILES}.
Their hunk maps (added/changed line ranges + removed lines per hunk) are:
{SLICE_HUNKS}
For each file run the per-file passes: get_pattern_context + lint_file +
canonical comparison (2a-2f), the security pass (2.6, including 2.6d — route the
deterministic lint sinks from the file's own lint_file output), and the per-file
logic passes (change-delta 3e — compare each hunk's added lines against its
removed lines from the hunk map above: removed guards/early returns/awaits,
inverted conditions, weakened error handling; edge cases 3c; placeholder 3f;
stale-comment 3f-ii); if it is under db/migrate run the migration-safety pass
(2.7). Do NOT run the dependency pass (2.5) or any whole-diff pass (co-change,
cross-file existence/duplication/layering/contract-break, coverage, auto-pass)
— the parent runs those once at synthesis, and you are not granted
scan_dependency_changes or the cross-file tools. Return JSON:
{"manifest": [{file, passes: [{pass, status, note}]}],
 "findings": [{file, line, section, rule, severity, message}]}
where each file's manifest lists every per-file pass (2a-2f, 2.6, 2.7, 3c, 3e,
3f, 3f-ii) with status "ran" (note: what was checked / K findings — for 3e: "N
hunks read, ... or CLEAN" plus ONE quoted removed line from the hunk map, or
"no removed lines"; for 3c: what inputs/queries were checked) or "skipped" with
the sanctioned reason (file deleted / binary / not a migration / not a source
file: manifest-lockfile, lint only / profile untrusted / archetype match none —
lint+security only / the skip condition the pass's own step defines, named).
Ground every finding in a tool result or a removed hunk line from the map above;
anchor every per-line finding inside an added/changed range (do not flag
pre-existing issues outside them).
```
