---
name: chameleon-pr-review
description: "Review a PR or branch diff against the repo's chameleon conventions, principles, and task requirements. Reports convention violations + logic gaps."
---

# PR Review with Chameleon Context

Review code changes against this codebase's actual conventions, principles, and (optionally) the task spec. Combines convention compliance with logic review.

## Input formats

```
/chameleon-pr-review                      → convention-only review of current branch vs main
/chameleon-pr-review EF-1234              → full review (conventions + Jira logic check)
/chameleon-pr-review <PR-URL>             → full review (conventions + linked Jira)
/chameleon-pr-review <PR-URL> EF-1234     → full review (explicit PR + ticket)
```

## Execution

Follow these steps in order. Do not skip steps.

### Step 1: Parse input

Determine what to review:
- **No args**: review current branch. Run `git diff main...HEAD --name-only` (or `production...HEAD` if main doesn't exist) to get changed files.
- **Jira key** (matches `[A-Z]+-\d+`): note it for Step 3.
- **PR URL** (contains `pullrequests` or `pull`): fetch the PR diff. For Bitbucket, use `bbcurl`. For GitHub, use `gh`.
- **Both**: use the PR diff and the Jira key.

If no changed files found, stop and tell the user.

### Step 2: Convention review

This is the core chameleon review. For EACH changed file:

**Skip these files** (false positives):
- Auto-generated files: `schema.rb`, `*.lock`, `*.generated.*`, vendored/third-party files
- Config/data files: `.yml`, `.json`, `.toml` unless the archetype specifically covers them
- Binary files, images, fonts

#### 2a. Get chameleon context

Call the `get_pattern_context` MCP tool with the file's absolute path:
```
get_pattern_context(file_path="/absolute/path/to/changed_file")
```

From the response, extract:
- `archetype.archetype` — which archetype this file matches
- `archetype.confidence_band` — how confident the match is
- `archetype.match_quality` — exact, ast, fallback, or none
- `canonical_excerpt.content` — the canonical witness code
- `repo.trust_state` — must be "trusted" for conventions to apply

If `trust_state` is not "trusted", warn and suggest `/chameleon-trust`.
If `match_quality` is "none" or "fallback", note it — the file may be in an uncovered area.

#### 2b. Run lint

Call the `lint_file` MCP tool:
```
lint_file(repo=<repo_id>, archetype=<archetype_name>, content=<file_content>, file_path=<abs_path>)
```

Collect ALL violations from the response. Each violation has `rule`, `severity`, `message`, `expected`, `actual`.

#### 2c. Check against canonical witness

Read the canonical witness content from Step 2a. Compare the changed file against it:
- Does the file follow the same structure as the witness?
- Does it use the same patterns the witness uses? (base classes, method shapes, import style, response format)
- Does it inherit/include/extend the same things the witness does?

The canonical witness IS the codebase's pattern. If the changed file diverges from it without good reason, flag as FIX.

**Use judgment for utility/helper files** — large multi-purpose files often have different shapes than the canonical. Don't blindly flag structural differences on files that serve a different purpose than the witness.

#### 2d. Check conventions

Load `.chameleon/conventions.json` from the repo root. Check per-archetype:

**Imports**: does the file use preferred imports? Does it import something the conventions say to avoid?

**Naming**: does the file follow the detected naming convention for declarations in this archetype? (prefix patterns, casing conventions)

**Inheritance**: does the file inherit/include/extend the dominant base class or mixin for this archetype?

**Method calls**: does the file use the common patterns for this archetype?

**Key exports**: is the author creating something that already exists in the key_exports list? Check for duplication.

#### 2e. Check principles

Read `.chameleon/principles.md` from the repo root. For each principle listed, check if the changed file violates it. Principles are auto-generated per repo — only the ones listed apply. Common principles:

- **Conventions override best practices** — does the file follow codebase patterns or generic patterns?
- **Match directory granularity** — is the file over-extracted or under-extracted vs siblings?
- **Match sibling test shape** — if this is a test file, do siblings have tests? If not, should this test exist?
- **One action, one job** — if this is an endpoint/action, does it combine data queries with file operations?
- **Use the wrapper** — does the file import a raw library when a project wrapper exists?
- **Prefer built-in idioms** — does the file use manual patterns when the language has a built-in idiom?

#### 2f. Check directory for existing similar code

For new files (not modifications), list sibling files in the same directory. Check if any existing file already provides what the new file is trying to do. Flag as NIT if potential duplication found.

### Step 3: Logic review (only when Jira ticket provided)

#### 3a. Gather task context

- **Jira ticket**: use the Atlassian MCP `getJiraIssue` tool. Read description, acceptance criteria, attachments.
- **Slack threads**: if the Jira ticket references Slack, or if a linked Slack thread exists, read it via Slack MCP `slack_read_thread`.
- **Attached docs**: if the ticket has attachments (screenshots, design docs), fetch what you can. List what you can't fetch and ask the user to paste them.

#### 3b. Check implementation completeness

For each requirement or acceptance criterion in the ticket:
- Is there corresponding code in the diff that implements it?
- If not, flag as BLOCK: "Requirement X has no implementation in this diff."

#### 3c. Check edge cases

For each changed file, consider:
- **Null/nil/undefined guards**: can any input be empty or missing? Is it handled?
- **Empty collections**: what happens when a query returns no results?
- **Authorization**: does the endpoint check permissions? Does it match the ticket's permission requirements?
- **Error handling**: are errors caught and returned in the project's standard format (check the canonical witness for the pattern)?
- **Race conditions**: for async or background operations, can two requests conflict?

Flag genuine risks as FIX. Don't flag hypothetical concerns.

#### 3d. Check spec compliance

Does the implementation match the spec exactly, or does it diverge?
- Different field names than the spec describes?
- Different endpoint shape than sibling endpoints use?
- Missing features the spec lists?
- Extra features the spec doesn't mention?

Flag divergences as FIX with the specific spec reference.

### Step 4: Output

Format the review as follows:

```
## Verdict: [APPROVE / APPROVE WITH NITS / NEEDS CHANGES / BLOCK]

Reviewed N files against chameleon conventions + [ticket KEY / branch diff].

### Convention findings (X issues)

**BLOCK:**
- `path/to/file:14` — [violation message from lint_file or canonical comparison]

**FIX:**
- `path/to/file:22` — [convention violation: what's wrong and what the codebase convention is]

**NIT:**
- `path/to/file` — Similar utility already exists in key_exports list

### Logic findings (Y issues) [only when ticket provided]

**BLOCK:**
- Acceptance criterion "X" has no implementation in this diff

**FIX:**
- No guard on potentially empty input — can cause runtime error
- Endpoint shape diverges from spec (spec says X, code does Y)

### Per-file details

#### `path/to/changed_file`
- Archetype: `name` (confidence: band, match: quality)
- Canonical witness: `path/to/witness`
- Violations: N (breakdown by severity)
- [details or "Follows conventions correctly."]
```

### Severity classification

| Severity | Meaning | Convention examples | Logic examples |
|----------|---------|-------------------|----------------|
| **BLOCK** | Must fix before merge | Missing base class/mixin the archetype requires | Missing requirement, race condition |
| **FIX** | Should fix | Wrong response pattern, missing naming convention | Missing null guard, spec divergence |
| **NIT** | Optional improvement | Potential duplication with existing utility | Minor inconsistency |

### Verdict rules

- **BLOCK**: any BLOCK finding → verdict is BLOCK
- **NEEDS CHANGES**: any FIX finding but no BLOCKs → NEEDS CHANGES
- **APPROVE WITH NITS**: only NIT findings → APPROVE WITH NITS
- **APPROVE**: zero findings → APPROVE

## Important

- Do NOT auto-fix code. Report only.
- Do NOT post comments to Bitbucket/GitHub. Show findings in chat only.
- Do NOT touch the Jira ticket (no comments, no status changes).
- When unsure if something is a violation, check the canonical witness. If the witness does the same thing, it's not a violation.
- Distinguish between violations the PR INTRODUCED vs pre-existing issues the PR didn't cause. Only flag PR-introduced issues.
- Skip auto-generated files: `schema.rb`, `*.lock`, `*.generated.*`, vendored files. These produce false positives.
- Large utility files (helpers, concerns, base classes) often have different shapes than the canonical — use judgment, not blind flagging.
