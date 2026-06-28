# Fan-out reviewer prompt (pr-review, large diffs)

Dispatch one in-session `general-purpose` Task subagent per slice with this
prompt. The reviewer is READ-ONLY: it may use `Read` and the read-only chameleon
MCP tools (`get_pattern_context`, `lint_file`, `get_canonical_excerpt`). It must
NEVER use `Edit`, `Write`, `Bash`, `WebFetch`, `WebSearch`, `NotebookEdit`, or
dispatch a nested `Task`.

Fill: {SLICE_FILES} (the files this reviewer owns), {REPO_ID}.

```
Review ONLY these files against the repo's chameleon conventions: {SLICE_FILES}.
For each file run the per-file passes: get_pattern_context + lint_file +
canonical comparison (2a-2f), the security pass (2.6, including 2.6d — route the
deterministic lint sinks from the file's own lint_file output), and the per-file
logic passes (change-delta 3e, edge cases 3c, placeholder 3f, stale-comment
3f-ii); if it is under db/migrate run the migration-safety pass (2.7). Do NOT run
the dependency pass (2.5) or any whole-diff pass (co-change, cross-file
existence/duplication/layering/contract-break, coverage, auto-pass) — the parent
runs those once at synthesis, and you are not granted scan_dependency_changes or
the cross-file tools. Return findings as JSON: [{file, line, section, rule,
severity, message}]. Ground every finding in a tool result or a removed hunk
line; do not flag pre-existing issues outside the changed hunks.
```
