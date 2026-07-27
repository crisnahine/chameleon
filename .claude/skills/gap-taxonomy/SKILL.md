---
name: gap-taxonomy
description: Lens definitions and per-language/per-framework probes for auditing the chameleon plugin. Loaded by /loop and the gap-hunter agent. Use when hunting coverage gaps, vacuous-silence bugs, logic gaps, workflow gaps or inconsistencies across chameleon's supported languages and frameworks.
---

# Gap taxonomy — chameleon

Ten lenses. A lens that finds nothing is signal, not failure — provided it **ran** (see the
not-run rule in `/loop` Phase 3).

---

## L0 — Coverage matrix (run first, every round)

The lens this repo needs most, because its own doctrine says an unfired rule and a clean file are
indistinguishable downstream.

**Deterministic seed:** `python3 plugin/scripts/coverage_matrix.py --repo . --json`

**Probes:**
- Every `?` cell → open the construction site, decide confirmed-present vs confirmed-absent.
- Every rule whose observed languages are a **strict subset** of the other rules in the same
  function. `scan_dangerous_sinks` is the canonical case: it handles all three languages, so a
  rule emitted from only one branch inside it is asymmetric by omission, not by design.
- Every entry in `BLOCK_ELIGIBLE_RULES` cross-checked against `BLOCK_RULE_LANGUAGES` **and**
  against the implementation. A rule that is block-eligible, in `SECURITY_BLOCK_RULES` (hence
  calibration-exempt), and declared `None` (language-independent) but implemented for two of three
  languages is the worst cell in the matrix: certified active, never measured, and silent.
- Every advisory in `stop/advisories.py` and every lens in `stop/lenses/` — same question.
  Coverage discipline applied to block rules but not to advisories is a half-audit.
- `_classify_framework` returns exactly six frameworks. For each rule/advisory/principle that
  claims framework awareness, does it handle all six, or does it special-case Rails and fall
  through for the rest?
- Any framework named in code but never *returned by* `_classify_framework` is a claim with no
  detection behind it.

**Known-justified absences** (a construct that does not exist is not a gap): `then-without-catch`
has no Ruby/Python analogue; `jsx-presence-mismatch`, `default-export-kind-mismatch` and
`named-export-count-bucket-mismatch` are TS/JS syntax facts.

**Do not** treat a `?` as a finding without a read. That is the tool's own failure mode.

## L1 — Vacuous silence (this repo's signature bug)

Anywhere a caller cannot distinguish "checked and clean" from "never checked."

**Probes:**
- Every `return []`, `return None`, `return {}`, `return 0` on a failure path — can the caller
  tell it apart from a real empty result?
- Every `except: pass` / `except Exception: return X` — the fail-open is usually correct here
  (documented contract), but is the degraded state **named**? Grep for a paired
  `_emit_check_event`, `_log_swallowed`, `degraded_telemetry` write, or banner.
- Every status field a consumer branches on: does `ok` have an `untrusted` / `stub` /
  `not_scanned` sibling, or does the dead state fall into the `ok` branch? (This is exactly
  the `doctor.advisory_emission` bug that `advisory_suppressed` was added to fix.)
- Every bounded scan: is the cap reported (`truncated`, `sampled`, `scan_truncated`), so an
  absent row reads as "not scanned" rather than "clean"?
- Every exit code: does a could-not-run path return the same code as a ran-and-passed path?
- Every cached result keyed on `(mtime_ns, size)` — does a cache miss degrade to a wrong answer
  or to a recompute?

## L2 — Workflow & state machines

**Probes:**
- `enforcement.py` levels: L0→L1→L2. Enumerate transitions. Is there a path into L2 with no path
  out? `record_clean` decrements one level — can a file that violated 5× reach clean in bounded
  turns, or is escalation effectively one-way inside a session?
- `is_self_correction` (10s window) vs `correction_count_reset` (60s): what happens between
  10s and 60s? Is that band's behavior intended and tested?
- Every "start" needs "finish", "cancel", "timeout": the job slot
  (`try_acquire_job_slot` / heartbeat / `JOB_HEARTBEAT_STALE_SECONDS`), the detached reviewer,
  the daemon, the bootstrap transaction.
- `launch_job` rolls back the slot claim on spawn failure — does every other early-return path
  between claim and launch also roll back?
- Multi-root Stop: a session that touches workspaces A and B where A blocks — does B's advisory
  pipeline still run, or does A's block short-circuit the loop?
- Mid-session cardinality change (single-root → multi-root) — `_stop_block_scope` was written for
  this; are there other counters that don't survive the flip?

## L3 — Edge cases & boundaries

**Probes:** for every input, what happens at
- a **fresh clone** (uniform mtimes — the bug that moved canonical selection to commit time),
  a **shallow clone** (no merge base — `GateUnusable`), a **worktree**, a **root commit**
  (`first_parent` returns None), a **detached HEAD**, an **empty repo**.
- a profile that is absent / torn / on a future `schema_version` / on schema v7 (must
  re-bootstrap at v8) / hand-edited / from a different repo.
- a monorepo whose workspaces share one git-remote-derived `repo_id`.
- zero archetypes, one archetype, an archetype with a single member, a sparse cluster (<5),
  a bimodal cluster at exactly 60/40.
- a file that is empty, minified, 10MB, non-UTF-8, a symlink, a Windows path, a path with a
  newline or a quote in it.
- clock: DST, a system clock that moves backwards (every `now - last_*` comparison).

## L4 — Error handling & failure modes

**Probes:**
- Every `subprocess.run` — timeout set? `returncode` checked? stderr captured and surfaced?
  (`git merge-base`, `git diff`, `git show`, `git log`, `claude -p`, the three extractor sidecars.)
- Every spawn: SIGKILL path, size cap on prompt and output, env inheritance
  (`CLAUDE_CONFIG_DIR` must pass through — an empty throwaway strips OAuth and the reviewer
  silently never fires).
- Batch partial failure: one unparseable file in a 1200-file calibration sample — does the whole
  pass die, or skip and report the skip?
- Lock contention: `save_state` skips rather than racing. Every other flock site — does it skip,
  block, or silently proceed?
- Retry on non-idempotent operations. Does anything retry a profile write or a ledger append?

## L5 — Data integrity & concurrency

**Probes:**
- Every atomic-write site: tmp name collision under parallel agents (per-PID+UUID is the pattern),
  `os.replace` vs `rename`, orphan tmp reaping, `chmod` after write.
- `_merge_states` is additive and monotonic — is every field it merges actually monotonic?
  A non-monotonic field merged with `max()` silently loses updates.
- Check-then-act: any `if not exists: create` without a lock or a unique constraint.
- `hash_profile` inputs: what is in the hashed surface vs deliberately out (`idioms.md` out,
  `idioms/*.json` in; `idiom-candidates/` out). Does anything newly written land on the wrong
  side? A new artifact inside `.chameleon/` that gets hashed will flip trust on every refresh.
- drift.db / sqlite: WAL mode, concurrent writers, schema migration on an old db.

## L6 — Contract & consistency drift

**Probes:**
- `_LIFECYCLE_ACTIONS` / `_REVIEW_ACTIONS` / `_TELEMETRY_ACTIONS` vs the functions that actually
  exist in `tools.py` + `_ACTION_MODULES`. `_resolve_action` is the single resolution site —
  is anything still doing its own `getattr(tools, ...)`?
- `action="help"` is generated from live signatures, so it cannot drift — verify nothing
  bypasses it, and that `_HELP_HIDDEN_PARAMS` still covers every test-only injection param.
- Version parity across the six manifests `bump-version.sh` claims to sync.
- Every threshold name in `_thresholds.py` vs every `threshold_int`/`threshold_float` call site —
  a typo'd name is a silent default.
- Every artifact filename constant vs what `hash_profile`, the loader, and the merge driver expect.
- Rule names as strings: `lint_engine` emits `rule="x"`, `violation_class` lists it,
  `commit_scope.PER_EDIT_SUPPRESSED` filters it, the skills reference it. All four must agree —
  grep each rule name across the repo and diff the sets.
- The wire contract: `_strip_nones` drops nulls, so `absent == null`. Any consumer treating a
  missing key as an error rather than null is drifted.

## L7 — Security boundary

**Probes:**
- Every file read — does it go through `safe_open`? The rule is MUST; grep for bare `open(`,
  `Path.read_text`, `read_bytes` on a repo-derived path.
- Every string crossing into `<chameleon-context>` — through `sanitize_for_chameleon_context`?
- Every attacker-reachable artifact — size cap before parse? (`_MAX_ENFORCEMENT_BYTES`,
  `GATE_MAX_FILE_BYTES`, `_MAX_PROFILE_META_BYTES` are the pattern; anything new needs one.)
- Every value loaded from a committed file used in arithmetic or as a dict key — coerced
  fail-open (`_coerce_nonneg_int` / `_coerce_block_map`), or does a poisoned string crash a
  later write and silently lose a session's accounting?
- The trust bridge: `_resolve_main_key` verifies the `.git` backref against a forged pointer.
  Any other place that infers a repo relationship from an on-disk marker?
- `chameleon-ignore` directive parsing: string-literal blanking per language. A new language or
  a new string form (Ruby `%W`, Python f-string nesting, TS template literal with `${}`) that
  the blanker misses is a directive-injection hole.

## L8 — Model-facing UX

The "user" here is partly the model reading injected context, partly the human reading a banner.

**Probes:**
- Every injected block against its token budget — SessionStart loses the **entire** injection on
  a 3s overrun, so anything added to that path must be cheap.
- Every advisory: does it dedupe across turns? (`cochange_shown`, the idiom self-review marker,
  the ledger one-shot resurface are the pattern — once is a nudge, repeats are nagging.)
- Every banner: can it fire on a healthy install? A README resolving no archetype is the correct
  answer, not a fault.
- Every message a human reads: does it name the fix (`/chameleon-trust`, `/chameleon-refresh`)?
- Authority in rendered text: is a `derived` frequency line rendered under an enforcement header?
  That is the 4.6.0 `ENFORCED_IMPORTS_HEADER` bug — check every other rendered section for it.
- Every skill in `plugin/skills/` — does its SKILL.md describe behavior the code still has?

## L9 — Test gaps

**Probes:**
- Every rule name with zero references under `tests/`.
- Every rule tested for one language only — the L0 matrix has a test-coverage twin, and a
  Python branch with no Python fixture is a rule that will silently rot.
- Every `not_run`-shaped path (exit 2, `GateUnusable`, `stub`, `untrusted`) — tested? These are
  the paths whose *absence* of a test is most dangerous, because they only fire when broken.
- Skipped/`xfail` tests and how long they've been skipped (`git blame`).
- Fixtures that constrain nothing — the 4.6.0 turn-depth eval scored zero violations in both
  arms because the tasks asked for work the fixtures did not constrain. Does each fixture
  actually make the drift possible?

## L10 — Ops & observability

**Probes:**
- Every hook path — can it exceed its timeout (`hooks.json`: 45s, Stop 60s)? What is emitted on
  overrun?
- Every degraded mode — recorded to `degraded_telemetry` / a check event / the attestation?
- CI that doesn't run what it claims (a lint job with `|| true`, a test path excluded by pattern).
- The daemon: reclaim on stale heartbeat, refuse on Windows, fall back in-process. Any path
  where a dead daemon reads as a working one?
- Log rotation and PII: does anything log a file's content, a secret hit's matched text, or a
  repo path outside the sanitized surface?

---

# Per-language probes

Chameleon supports exactly three. For **every** rule and advisory, ask all three columns.

### typescript (`.ts .tsx .js .jsx .mjs .cjs`) — extractor `ts_dump.mjs`
- The heuristic lint's documented misses: `export {default} from "./x"`, JSX detection via
  `</(\w+)` on string-stripped content. Has any new rule inherited the same blind spot?
- `tsconfig` path aliases — `phantom-import` skips unmapped aliases (correctly). Monorepo
  `references` / `paths` inherited from a parent config?
- Node-unavailable degradation (`NodeUnavailableError`) — every caller handles it?
- Security sinks TS *should* have, checked against what it does: `child_process.exec` with
  interpolation, `vm.runInNewContext`, `node-serialize`, template-literal SQL (knex/pg raw),
  `crypto.createHash('md5')`.

### ruby (`.rb`) — extractor `prism_dump.rb`
- String forms the blanker must cover: heredocs (`<<~`, `<<-`, quoted delimiters), `%q %Q %w %W`,
  `?x` char literals, `=begin/=end` block comments.
- `#{}` interpolation is the SQL vector and survives string-blanking by design — is that
  exception scoped to the query-method anchor, or does a new rule copy the raw-content scan
  without the anchor?
- Rails-specific: `before_action` / `skip_before_action` / `only:` / `except:` scoping,
  `application_*.rb` abstract bases, concerns, `ApplicationRecord` inheritance chains.
- Missing sinks to check: `Digest::MD5`/`SHA1`, `Marshal.load`, `YAML.load`, backticks and
  `system()`/`exec()` with interpolation.

### python (`.py .pyi`) — extractor `libcst_dump.py`
- String forms: triple-quoted, f-string with nested quotes, r/b/rb prefixes, implicit
  concatenation across lines.
- Decorators as the authz signal (`_PY_AUTHZ_DECORATOR_RE`) — class-based views inherit auth
  from a mixin, which `_PY_AUTHZ_MIXIN_TAILS` handles; does it cover DRF `permission_classes`?
- `__init__.py` re-exports and namespace packages vs `phantom-import` resolution.
- Missing sinks to check: `hashlib.md5/sha1`, `pickle.loads`, `yaml.load` without `SafeLoader`,
  `subprocess(..., shell=True)`, `os.system`, `.raw()` / `.extra()` / cursor `%`-formatting.
- Async: an un-awaited coroutine is the `then-without-catch` analogue and has no rule.

# Per-framework probes

`_classify_framework` returns exactly six. For each, check whether a rule/advisory/principle that
claims framework awareness actually branches for it, or special-cases Rails and falls through.

| Framework | Guard idiom | Base/inheritance | Test idiom | Migration surface |
|---|---|---|---|---|
| rails | `before_action` | `ApplicationController/Record/Job` | RSpec `it/describe`, minitest | `db/migrate` |
| django | `@login_required`, `LoginRequiredMixin`, `permission_classes` | `models.Model`, `View` | `TestCase`, pytest-django | `migrations/` |
| flask | `@login_required`, before_request | Blueprint | pytest | Alembic |
| fastapi | `Depends(...)` | `BaseModel`, `APIRouter` | pytest + TestClient | Alembic |
| nextjs | middleware.ts, route handlers | — | jest/vitest, playwright | — |
| nestjs | `@UseGuards()` | `@Module`, `@Injectable` | jest | TypeORM/Prisma |

Two standing questions:
1. `required-guard-convention` is implemented for Rails (`before_action`) and Python views. Is
   there any NestJS `@UseGuards` or Next.js middleware equivalent, or does TS have no guard rule?
2. Anything named in code but never returned by `_classify_framework` (e.g. `vue`, `react` appear
   in `conventions.py`) — a coverage claim with no detection behind it.

---

# Tooling (suggest, never auto-install)

`plugin/scripts/coverage_matrix.py` (in-repo) · `ast-grep` (structural, all three languages) ·
`ripgrep` · `python3 -m ast` for exact attribution · `git log --name-only` with pathspec
exclusions · `lizard` (complexity — `tools.py` at 15k and `lint_engine.py` at 5.1k are where
this repo's otherwise strong module boundaries are thinnest).
