# Fan-out reviewer prompt (pr-review, large diffs)

Dispatch one in-session `general-purpose` Task subagent per slice with this
prompt. The reviewer is READ-ONLY: it may use `Read` and the read-only chameleon
MCP tools (`get_pattern_context`, `lint_file`, `get_canonical_excerpt`). It must
NEVER use `Edit`, `Write`, `Bash`, `WebFetch`, `WebSearch`, `NotebookEdit`, or
dispatch a nested `Task`.

Fill: {SLICE_FILES} (the files this reviewer owns), {REPO_ID}, {BASE}.

```
Review ONLY these files against the repo's chameleon conventions: {SLICE_FILES}.
For each file run the per-file passes: get_pattern_context + lint_file +
canonical comparison (2a-2f), the security pass (2.6), and the per-file logic
passes (change-delta 3e, edge cases 3c, placeholder 3f, stale-comment 3f-ii). If
the file is a manifest run the dependency pass (2.5); if it is under db/migrate
run the migration-safety pass (2.7). Do NOT run whole-diff passes (co-change,
cross-file existence/duplication/layering, coverage, auto-pass) — the parent runs
those once. Return findings as JSON: [{file, line, section, rule, severity,
message}]. Ground every finding in a tool result or a removed hunk line; do not
flag pre-existing issues outside the changed hunks.
```
