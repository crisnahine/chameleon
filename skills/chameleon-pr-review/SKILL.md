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

#### 2a. Get chameleon context

Call the `get_pattern_context` MCP tool with the file's absolute path:
```
get_pattern_context(file_path="/absolute/path/to/changed_file.rb")
```

From the response, extract:
- `archetype.archetype` — which archetype this file matches
- `archetype.confidence_band` — how confident the match is
- `archetype.match_quality` — exact, ast, fallback, or none
- `canonical_excerpt.content` — the canonical witness code
- `repo.trust_state` — must be "trusted" for conventions to apply

If `trust_state` is not "trusted", warn and suggest `/chameleon-trust`.

#### 2b. Run lint

Call the `lint_file` MCP tool:
```
lint_file(repo=<repo_id>, archetype=<archetype_name>, content=<file_content>, file_path=<abs_path>)
```

Collect ALL violations:
- `top-level-node-kinds-mismatch` → structural violation (BLOCK)
- `import-preference-violation` → wrong import used (FIX)
- `naming-convention-violation` → missing prefix convention (FIX)
- `inheritance-convention-violation` → wrong base class (FIX)
- `content-signal-mismatch` → missing directive like 'use client' (NIT)

#### 2c. Check against canonical witness

Read the canonical witness content from Step 2a. Compare the changed file against it:
- Does the file follow the same structure? (imports → class → methods)
- Does it use the same patterns? (render_data, ActiveInteraction, React.FC)
- Does it inherit/include the same base classes?

If the file diverges from the canonical pattern, flag as FIX.

#### 2d. Check conventions

Load `.chameleon/conventions.json` from the repo root. Check per-archetype:

**Imports**: does the file use preferred imports? Does it import something the conventions say to avoid?

**Naming**: for TypeScript files, do interfaces use the detected prefix (I-prefix, T-prefix)?

**Inheritance**: for Ruby files, does the class inherit the dominant base class for this archetype?

**Method calls**: does the file use the common DSL patterns for this archetype?

**Key exports**: is the author creating something that already exists in the key_exports list?

#### 2e. Check principles

Read `.chameleon/principles.md` from the repo root. For each principle, check if the changed file violates it:

1. **"conventions override general best practices"** — does the file follow codebase patterns or generic patterns from training data?
2. **"Match directory granularity"** — is the file over-extracted or under-extracted vs siblings?
3. **"Match sibling test shape"** — if this is a test file, do siblings have tests? If not, should this test exist?
4. **"One action, one job"** — if this is a controller/endpoint, does it combine data queries with downloads?
5. **"Use the wrapper, not the raw library"** — does the file import a raw library when a project wrapper exists?
6. **"Prefer built-in idioms"** — does the file use manual check-then-create when a language idiom exists?

Only check principles that are present in THIS repo's `principles.md` (they're auto-generated based on repo structure).

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
- **Nil/null guards**: can any input be nil? Is it handled?
- **Empty collections**: what happens when a query returns no results?
- **Authorization**: does the endpoint check permissions? Does it match the ticket's permission requirements?
- **Error handling**: are errors caught and returned in the project's standard format?
- **Race conditions**: for async operations, can two requests conflict?

Flag genuine risks as FIX. Don't flag hypothetical concerns.

#### 3d. Check spec compliance

Does the implementation match the spec exactly, or does it diverge?
- Different field names than the spec describes?
- Different endpoint shape (query params vs path segments)?
- Missing features the spec lists?
- Extra features the spec doesn't mention?

Flag divergences as FIX with the specific spec reference.

### Step 4: Output

Format the review as follows:

```
## Verdict: [APPROVE / APPROVE WITH NITS / NEEDS CHANGES / BLOCK]

Reviewed N files against chameleon conventions + [ticket EF-XXXX / branch diff].

### Convention findings (X issues)

**BLOCK:**
- `path/to/file.rb:14` — Missing Sidekiq::Throttled::Worker include (99% of workers have it)
- `path/to/file.rb:8` — Class inherits nothing, archetype expects ActiveInteraction::Base

**FIX:**
- `path/to/file.rb:22` — Uses `render json:` instead of `render_data` (canonical uses render_data)
- `path/to/types.ts:5` — Interface `UserProps` should be `IUserProps` (99% I-prefix convention)

**NIT:**
- `path/to/file.rb:1` — Missing `# frozen_string_literal: true`
- `path/to/new_util.ts` — Similar utility `formatCurrency` already exists in key_exports

### Logic findings (Y issues) [only when ticket provided]

**BLOCK:**
- Acceptance criterion "sync seller emails for active listings" has no implementation
- No nil guard on `listing.seller` — can be nil per schema, causes NoMethodError

**FIX:**
- Endpoint uses path segment `/listings/:id/ai_data` but codebase convention is query params `?listing_id=`
- Missing `else` branch in case statement — unhandled format silently returns 204

### Per-file details

#### `path/to/changed_file.rb`
- Archetype: `service` (confidence: high, match: ast)
- Canonical witness: `app/services/achievements/base_service.rb`
- Violations: 2 (1 FIX, 1 NIT)
- [details...]

#### `path/to/another_file.ts`
- Archetype: `component` (confidence: high, match: exact)
- Canonical witness: `src/components/Button.tsx`
- Violations: 0
- Follows conventions correctly.
```

### Severity classification

| Severity | Meaning | Convention examples | Logic examples |
|----------|---------|-------------------|----------------|
| **BLOCK** | Must fix before merge | Missing base class, wrong includes | Missing requirement, race condition |
| **FIX** | Should fix | Wrong render method, missing prefix | Missing nil guard, spec divergence |
| **NIT** | Optional improvement | Missing frozen_string_literal, potential duplication | Minor naming inconsistency |

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
- Skip auto-generated files: `db/schema.rb`, `*.lock`, `*.generated.*`, vendored files. These produce false positives.
- Skip config/data files: `.yml`, `.json`, `.toml` unless the archetype specifically covers them.
- Large utility controllers (helpers, concerns) often have different shapes than the canonical - use judgment, not blind flagging.
