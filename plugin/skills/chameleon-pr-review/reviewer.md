# Fan-out reviewer dispatch (pr-review, large diffs)

The per-slice reviewer role is packaged as the plugin agent
`chameleon:pattern-reviewer` (defined in `agents/pattern-reviewer.md` at the
plugin root — the fixed role, per-file passes, tool limits, and output schema
all live there). Dispatch one in-session Task subagent per slice with
`subagent_type: "chameleon:pattern-reviewer"` and the prompt below. The agent
is READ-ONLY: it may use `Read`, `Grep`, `Glob`, and the read-only chameleon
MCP tools (`get_pattern_context`, `lint_file`, `get_canonical_excerpt`); its
definition never allows `Edit`, `Write`, `Bash`, `WebFetch`, `WebSearch`,
`NotebookEdit`, or a nested agent dispatch.

If the harness does not expose the `chameleon:pattern-reviewer` agent type
(plugin agents unavailable), read
`${CLAUDE_PLUGIN_ROOT}/agents/pattern-reviewer.md` and dispatch a
`general-purpose` subagent with that file's body (everything after the
frontmatter) prepended to the prompt below — one source of truth, never a
from-memory retelling of the role.

Fill: {SLICE_FILES} (the files this reviewer owns), {REPO_ID}, and {SLICE_HUNKS}
(each slice file's hunk map from Step 1a: the added/changed line ranges in the
post-change file, plus the removed `-` lines per hunk). {SLICE_HUNKS} is
mandatory — the reviewer has no Bash and cannot re-derive the diff, so without
it the change-delta pass (3e) has no removed lines to read and the hunk gate has
no ranges to check against; a slice dispatched without hunks reviews blind.

```
Review ONLY these files against the repo's chameleon conventions: {SLICE_FILES}.
The repo id is {REPO_ID}.
Their hunk maps (added/changed line ranges + removed lines per hunk) are:
{SLICE_HUNKS}
Run every per-file pass your role defines on each file and return the
manifest + findings JSON your role specifies.
```
