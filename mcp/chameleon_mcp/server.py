"""FastMCP server entry point for chameleon-mcp.

Stdio transport. Long-lived process invoked by hooks via UNIX domain socket
(Phase 4 daemon model) or directly per-call (Phase 1C-2 fallback).

See docs/architecture.md sections:
- "MCP server (`chameleon-mcp`)" — full tool surface
- "Performance characteristics" — daemon model
- "Cluster signature function" — what tools rely on

Known limitation (upstream): a ``tools/call`` whose arguments are nested past
pydantic-core's recursion cap (~200 levels) gets no JSON-RPC response — the
underlying mcp SDK drops it in its stream exception handler, so a client blocks
until its own timeout. No real client produces such input; documented rather
than patched here because the guard belongs in the SDK, not chameleon.
"""

from mcp.server.fastmcp import FastMCP

from chameleon_mcp import tools

mcp = FastMCP("chameleon-mcp")


@mcp.tool()
def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to.

    Args:
        file_path: absolute path to a file inside a repo (or repo root)

    Returns:
        {
          "api_version": "1",
          "data": {
            "repo_id": str,            # sha256 hex of git_remote_url or canonicalized abs path
            "repo_root": str,          # absolute path to repo root
            "profile_status": str,     # "no_repo" | "no_profile" | "profile_present" | "profile_corrupted" | "profile_unsupported_schema_version"
            "trust_state": str,        # "trusted" | "untrusted" | "stale" | "n/a"
          }
        }
    """
    return tools.detect_repo(file_path)


@mcp.tool()
def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Args:
        repo: repo_id from detect_repo
        file_path: absolute path to file

    Returns:
        archetype + alternatives + content_signal_match info
    """
    return tools.get_archetype(repo, file_path)


@mcp.tool()
def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical excerpt + rules + meta in one round trip.

    Replaces the v3-era 4-call dance (detect_repo → get_archetype →
    get_canonical_excerpt → get_rules) per Round 5 API Designer
    recommendation.

    Args:
        file_path: absolute path to file

    Returns:
        {
          "api_version": "1",
          "data": {
            "repo": {"id": str, "profile_status": str, "trust_state": str},
            "archetype": {"archetype": str, "alternatives": [str], "confidence_band": str,
                          "content_signal_match": str, "match_quality": str, "sub_buckets_count": int},
            "canonical_excerpt": {"content": str, "witness_path": str, "truncated": bool, "sha_hint": str,
                                   "missing": bool (only when the witness file is gone),
                                   "oversize": bool (only when the witness exceeds the size ceiling)},
            "rules": [(source_key, config_dict), ...],
            "idioms": str | None,
            "meta": {"mtime_token": str, "computed_at": str}
          }
        }
    """
    return tools.get_pattern_context(file_path)


@mcp.tool()
def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the canonical witness source for an archetype.

    The content is the witness file's source as committed (sanitized, not
    annotated), so its length tracks the witness — typically well under a
    thousand tokens, larger for a long canonical file.
    """
    return tools.get_canonical_excerpt(repo, archetype)


@mcp.tool()
def get_rules(repo: str, source: str | None = None) -> dict:
    """Return repo-global rules (eslint / rubocop / formatting / typescript), keyed by tool/source.

    `source` filters to a single source key (`"eslint"`, `"rubocop"`,
    `"formatting"`, `"typescript"`). Omit to return all. Passing an
    archetype name (e.g. `"component"`) returns a failed envelope —
    rules are SOURCE-scoped, not archetype-scoped. The legacy
    `archetype=` kwarg was removed from the MCP schema
    (still accepted by the in-process function with a deprecation
    field, but no longer advertised to callers).
    """
    return tools.get_rules(repo, source)


@mcp.tool()
def lint_file(repo: str, archetype: str, content: str, file_path: str | None = None) -> dict:
    """Validate file content against archetype's rules. Returns violations + canonical confidence.

    When file_path is provided, its extension is used for language detection
    instead of falling back to the witness extension.
    """
    return tools.lint_file(repo, archetype, content, file_path=file_path)


@mcp.tool()
def query_symbol_importers(repo: str, file_path: str) -> dict:
    """Cross-file importers of a module (TS/JS + Python; Ruby via the constant
    graph) + which imports it now breaks.

    Reads the prebuilt reverse index (symbol -> importers) plus the module's
    current on-disk export set. Returns:
      - importers: per exported name, the files that import it (rename blast radius)
      - broken: names an importer still references that the module no longer
        exports (the deterministic existence break)
      - export_set_open: True when `export * from` makes the set unenumerable
        (broken is suppressed; importers still reported)

    Fails open with found=False on any ambiguity (unresolvable/untrusted repo,
    missing index, unreadable module). Never fabricates an importer.
    """
    return tools.query_symbol_importers(repo, file_path)


@mcp.tool()
def get_callers(repo: str, file_path: str, function_name: str) -> dict:
    """Who calls this function, from the committed calls snapshot (deterministic grades only).

    Reads the prebuilt calls_index.json artifact. Returns the recorded caller
    rows for ``function_name`` defined in the file at ``file_path``. Grades are
    deterministic: same_file, import (TypeScript and Python), constant_receiver
    (Ruby), typed_property (TypeScript DI edges)
    (Ruby only). Dynamic/unsupported call paths are absent by design.

    Absence of callers is NOT evidence of dead code -- dynamic dispatch and
    callers added after the last bootstrap are invisible. Fails open with
    found=False on any ambiguity. Never fabricates a caller.
    """
    return tools.get_callers(repo, file_path, function_name)


@mcp.tool()
def get_blast_radius(repo: str, file_path: str, function_name: str, depth: int = 0) -> dict:
    """Transitive callers of a function (change blast radius), from the calls snapshot.

    Walks the committed calls_index.json UPWARD from ``function_name`` in
    ``file_path`` and returns the bounded caller chains that reach it: "if I
    change this, what transitively calls it". Each chain is root first
    (function -> caller -> caller's caller ...). ``depth`` is the hop count;
    it defaults to the judge's transitive depth and is clamped to
    [1, BLAST_RADIUS_MAX_DEPTH], sharing the judge's fanout / total-node caps.

    This is the same conservative reach the turn-end correctness judge walks,
    surfaced so pr-review and the human can ask beyond one-hop get_callers.
    Grades are deterministic (same_file, import, constant_receiver, typed_property). Absence of a
    caller is NOT dead code (dynamic dispatch / reflection / post-bootstrap
    callers are invisible). Fails open with found=False on any ambiguity. Never
    fabricates a caller.
    """
    return tools.get_blast_radius(repo, file_path, function_name, depth)


@mcp.tool()
def search_codebase(repo: str, query: str, limit: int = 10) -> dict:
    """Find symbols by name or file from the committed profile (comprehension).

    The "where is X / find Y" query, answered off chameleon's own profile: walks
    the committed symbol index and returns matches for `query` ranked exact name >
    prefix > substring > all-tokens > file-path, with the more-called symbol
    breaking ties. Each result carries name, file, line, signature, and caller
    count. Read-only, offline, no repo-code execution. Fails open with
    found=False on an unresolvable / untrusted repo or empty query.
    """
    return tools.search_codebase(repo, query, limit)


@mcp.tool()
def describe_codebase(repo: str) -> dict:
    """A structural overview of the repo from its committed profile (comprehension).

    The "what is this codebase" answer: primary language and framework, the
    archetypes (kinds of files, each with size, summary, and canonical witness),
    file/symbol totals, and the god symbols (most-called production functions,
    test files excluded). All from committed artifacts, offline. Fails open with
    found=False on an unresolvable / untrusted repo.
    """
    return tools.describe_codebase(repo)


@mcp.tool()
def get_callees(repo: str, file_path: str, function_name: str) -> dict:
    """What a function calls (forward edges), from the committed calls snapshot.

    The forward counterpart to get_callers / get_blast_radius: inverts the reverse
    calls_index to answer "what does this function call". Each result is
    {callee, file, grade} with the three deterministic grades (same_file, import,
    constant_receiver). Absence of a callee is NOT proof it calls nothing
    (dynamic dispatch is invisible). Fails open with found=False on any ambiguity.
    """
    return tools.get_callees(repo, file_path, function_name)


@mcp.tool()
def get_autopass_verdict(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: is this branch's diff vs base_ref safe to auto-pass, or human?

    Classifies the change and returns {auto_pass_eligible, risk, reasons, facts,
    changed_files, typecheck}. Never gates -- it marks the routine slice safe to
    skip and routes the rest to a human with a reason (grounded block finding, a
    security-sensitive surface, too large, high blast radius, a file outside the
    profiled archetypes, a removed guard line or in-diff chameleon-ignore
    directive, test weakening alongside live-source changes, or an unknown blast
    radius when the cross-file index cannot answer). The typecheck field is
    three-state (unavailable / clean / errors): unavailable -- including the
    default opt-in-not-set case -- is recorded as a fact and never forces
    needs-human, while type errors on changed files do. facts also carries the
    deterministic test-integrity and content signals (deleted/weakened tests,
    added skip markers, assertion delta, removed guard lines, chameleon-ignore
    added in-diff). Fails open toward "needs human" when a signal can't be read.
    Blast radius covers TS/JS files; an unreadable fan-out on a covered file
    routes to a human, and non-TS files are gated by the other signals until
    Ruby cross-file parity ships. Still ADVISORY, never gates.
    """
    return tools.get_autopass_verdict(repo, base_ref)


@mcp.tool()
def get_contract_breaks(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: deterministic caller-contract breaks for a branch diff vs base_ref.

    For each changed TS/Ruby file, compares its callables' POSITIONAL parameter
    contract at base_ref vs HEAD and flags a NARROWING (new required positional,
    or optional->required) that has committed callers -- the deterministic
    signature-contract signal, surfaced as a citable tool result. Returns
    {status, findings:[{file, name, old/new_required_positional, caller_total,
    callers}]}. git show + AST re-parse, no network/repo-exec; default-on; fails
    open to a no-signal result; never blocks. pr-review cites these as FIX.
    """
    return tools.get_contract_breaks(repo, base_ref)


@mcp.tool()
def get_crossfile_context(repo: str) -> dict:
    """Cross-file existence breaks across a repo (TS/JS + Python via the
    reverse index; Ruby via the constant graph), for PR review.

    Scans the prebuilt reverse index and returns one finding per removed/renamed
    export an indexed importer still references -- the deterministic cross-file
    break class. Each finding carries high_confidence, true only when the
    resolution is unambiguous end to end (exact file match, closed export set,
    an importer that still names the binding). A consumer must relay ONLY
    high_confidence=true findings; lower-confidence rows are returned for
    transparency and must be dropped, never surfaced.

    Returns {found, findings: [{symbol, module, count, high_confidence, sites}]}.
    Fails open with found=False on any ambiguity (unresolvable/untrusted repo,
    missing index); never fabricates an importer edge.
    """
    return tools.get_crossfile_context(repo)


@mcp.tool()
def refute_finding(repo: str, findings: list, base_ref: str = "main") -> dict:
    """Round-3: independently refute model-judgment review findings.

    The pr-review / receiving skills call this ONCE with the batch of surviving
    model-judgment BLOCK/FIX findings (tool-grounded findings are verified inline,
    not sent here). The engine spawns one hardened, no-tools claude -p refuter per
    finding, capped + timed out, and returns a verdict per finding:
    confirmed (keep) | refuted (drop) | unverified (keep, labeled 'round 3
    unavailable'). A 'confirmed' verdict never authorizes an edit or a post.
    Default-ON; set CHAMELEON_REVIEW_REFUTER=0 to disable.
    """
    return tools.refute_finding(repo, findings, base_ref)


@mcp.tool()
def get_duplication_candidates(repo: str, file_path: str) -> dict:
    """Existing functions a file's new functions may re-implement under a new name.

    For each function defined in file_path, the bootstrap function catalog is
    prefiltered (signature shape + name-token overlap) to existing functions
    elsewhere in the repo that look like the same intent under a different name --
    the toDisplayDate vs formatDate case exact-name matching misses. Each
    candidate carries a short body excerpt read from disk.

    The tool only PREFILTERS; the LLM caller judges semantic equivalence against
    the candidate bodies. Duplication is a judgment call, so any finding raised
    from this is advisory FIX/NIT at most, never block-eligible.

    Returns {found, file, matches: [{function, candidates: [...]}]}. Fails open
    with found=False on any ambiguity (unresolvable/untrusted repo, missing
    catalog, unparsable file). Never fabricates a candidate.
    """
    return tools.get_duplication_candidates(repo, file_path)


@mcp.tool()
def get_drift_status(repo: str) -> dict:
    """Report freshness, days_since_refresh, observed_drift_score for a repo."""
    return tools.get_drift_status(repo)


@mcp.tool()
def get_status(repo: str) -> dict:
    """Report enforcement mode + active/demoted block rules for a repo."""
    return tools.get_status(repo)


@mcp.tool()
def get_drift_antipatterns(repo: str, archetype: str | None = None) -> dict:
    """Per-archetype recurring-violation signals from this repo's drift history.

    For each archetype where edits repeatedly bumped a convention or drifted
    off-pattern, returns the rule(s) and frequency so /chameleon-auto-idiom can
    propose a counterexample-bearing idiom (the deriver reads a flagged file for
    the actual wrong-way form; drift.db stores no code). Optionally filtered to one
    archetype. Fail-open: an unresolvable repo returns an empty result.
    """
    return tools.get_drift_antipatterns(repo, archetype)


@mcp.tool()
def get_shadow_report(repo: str, window_days: int | None = None) -> dict:
    """Per-rule would-block counts from the shadow log for the shadow->enforce decision.

    Aggregates the accumulating real-edit metrics (not the one-shot bootstrap
    calibration get_status returns). Per rule: would-block count, distinct
    files/sessions, advisory-only count, and a promotion verdict by count. Plus a
    sampled file:line list for human spot-check and a window_truncated flag when
    rotation dropped older rows. No false-positive fraction (the data has no
    outcome signal). window_days defaults to CHAMELEON_SHADOW_REPORT_WINDOW_DAYS.
    """
    return tools.get_shadow_report(repo, window_days)


@mcp.tool()
def get_override_audit(repo: str, window_days: int | None = None) -> dict:
    """Per-rule inline-override audit: how often each block rule gets chameleon-ignored.

    Reads the durable drift.db override history (not wiped by refresh) plus the
    would-block metrics. Per rule: override count, would-block count, bare-blanket
    share, distinct files/sessions, and an override rate (overrides / fired edits).
    A high rate flags a rule fighting the team -- reconcile via /chameleon-refresh
    (recalibrate) or /chameleon-teach (fix the convention). This is a contention
    signal, not a false-positive rate, and never auto-mutates enforcement.
    window_days defaults to CHAMELEON_OVERRIDE_AUDIT_WINDOW_DAYS.
    """
    return tools.get_override_audit(repo, window_days)


@mcp.tool()
def get_longitudinal_signals(repo: str, window_days: int | None = None) -> dict:
    """Two honestly-labelled longitudinal health tracks for a repo.

    Track 1, structural_conformance: the drift score (1 - mean structural-match
    confidence), relabelled and explicitly NOT a quality bar. Track 2,
    enforcement_outcomes: aggregate would-block and idiom-review rates over the
    window, counting how often chameleon's own shape/idiom rules fired. Both
    carry a blind_spots/disclaimer caveat -- neither track sees logic, dataflow,
    cross-file, or auth defects, so an all-zeros result is not a safety guarantee.
    window_days defaults to CHAMELEON_LONGITUDINAL_WINDOW_DAYS.
    """
    return tools.get_longitudinal_signals(repo, window_days)


@mcp.tool()
def get_review_history(repo: str, limit: int | None = None) -> dict:
    """Return the persisted PR-review verdict trail for a repo, newest first.

    Each /chameleon-pr-review run appends a signed record pinning the reviewed
    commit, the profile that reviewed it (profile_sha256 + generation +
    schema_version), trust state, verdict, findings by severity, engine version,
    and reviewer. Lets a lead see the trail and spot a BLOCK verdict that shipped
    anyway. Each record carries `verified` (HMAC re-check): tamper-evidence
    against a third local user, NOT forgery resistance against the reviewed
    developer (who holds the key) and NOT CI-verifiable. Honest audit log, not a
    merge gate. limit defaults to CHAMELEON_REVIEW_HISTORY_DEFAULT_LIMIT.
    """
    return tools.get_review_history(repo, limit)


@mcp.tool()
def record_review_verdict(
    repo: str,
    verdict: str,
    findings_count: int | None = None,
    commit_sha: str | None = None,
    pr_id: str | None = None,
    complexity_tier: str | None = None,
) -> dict:
    """Append a /chameleon-pr-review verdict to the repo's signed review ledger.

    The skill's final step. After the verdict is shown in chat, persist it so a
    lead can later audit which reviewed commits shipped (and whether any BLOCK
    merged anyway). Args: `verdict` (the rendered verdict string),
    `findings_count` (total BLOCK+FIX+NIT count), `commit_sha` (reviewed HEAD),
    optional `pr_id`, optional `complexity_tier` (the change's structural tier
    easy/medium/hard/complex from get_autopass_verdict, so per-tier review-clean
    rates are trackable over the ledger).

    Stamps profile provenance (profile_sha256 + generation + schema_version),
    trust state, engine version, and reviewer onto the record. Best-effort: a
    ledger failure returns recorded=False and never blocks the review.
    Tamper-evident, NOT forgery-proof and NOT CI-verifiable. Read back with
    get_review_history.
    """
    return tools.record_review_verdict(
        repo,
        verdict,
        findings_count=findings_count,
        commit_sha=commit_sha,
        pr_id=pr_id,
        complexity_tier=complexity_tier,
    )


@mcp.tool()
def explain_edit(repo: str, file_path: str) -> dict:
    """Replay what chameleon knew and did the last time a file was edited.

    The post-incident recovery read. Returns the most-recent per-edit decision
    log row for file_path (archetype, match_quality, confidence_band, violations
    raised, the block-eligible rules that stood, the resolved outcome) and a
    classification: coverage-gap (no archetype or fallback/none match quality),
    in-scope-miss (ast/exact match but nothing caught the defect), or blocked /
    overridden (the gate did fire). found is False when no edit was ever logged.
    """
    return tools.explain_edit(repo, file_path)


@mcp.tool()
def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile. OS-level locked via flock."""
    return tools.refresh_repo(repo, force)


@mcp.tool()
def bootstrap_repo(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    production_ref: str | None = None,
) -> dict:
    """First-time analysis: AST scan + interview + atomic profile commit.

    Pass force=true to overwrite a committed profile (BUG-026). Pass
    production_ref to pin derivation to a production branch (the init skill's
    confirmed answer to "which branch is production?"); omitted, the lock comes
    from persisted config or origin auto-detection.
    """
    return tools.bootstrap_repo(path, paths_glob, force, production_ref=production_ref)


@mcp.tool()
def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos this user has touched. Cursor-paginated from day 1."""
    return tools.list_profiles(cursor, limit)


@mcp.tool()
def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge: re-cluster from union of ours and theirs.

    Used by `.gitattributes` merge driver registration.
    See docs/architecture.md "SQLite schemas" → "merge_profiles algorithm" subsection.
    """
    return tools.merge_profiles(repo, base, ours, theirs)


@mcp.tool()
def teach_profile(repo: str, feedback: str) -> dict:
    """Apply user-driven correction to profile (idiom, banned import, mandatory wrapper).

    Renamed from `refine_profile` in v4 to align with `/chameleon-teach` slash command.
    """
    return tools.teach_profile(repo, feedback)


@mcp.tool()
def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user.

    Requires confirmation_token (typed repo name or `yes-trust-<repo_id_short>`)
    to defeat normalization-deviance.
    """
    return tools.trust_profile(repo, confirmation_token)


@mcp.tool()
def disable_session(
    repo: str,
    session_id: str,
    force: bool = False,
) -> dict:
    """Suppress chameleon advisory injections for this Claude Code session.

    Per /chameleon-disable. preflight-and-advise checks the
    `.session_disabled.<session_id>` marker before injecting; when present
    AND validly HMAC-signed, no <chameleon-context> is added.

    Defenses: the marker is HMAC-signed so an out-of-process
    attacker can't plant a forgery. The repo must have a trust grant
    before disable_session will write. Sessions whose session_id has never
    invoked another chameleon tool for this repo are REFUSED by default;
    pass `force=True` for legitimate first-time-disable cases from a
    brand-new session (the response still surfaces a warning).
    """
    return tools.disable_session(repo, session_id, force=force)


@mcp.tool()
def pause_session(repo: str, minutes: int = 15) -> dict:
    """Pause chameleon advisory injections for `minutes` minutes (default 15).

    Per /chameleon-pause-15m. Auto-expires when the timestamp passes.
    """
    return tools.pause_session(repo, minutes)


@mcp.tool()
def propose_archetype_renames(repo: str, top_n: int = 8) -> dict:
    """Suggest better names for the top-N largest archetypes in the profile.

    Drives the /chameleon-init interview's rename step. For each archetype
    the response carries the current name, cluster_size, canonical file,
    and 3-5 candidate alternative names derived from the canonical filename,
    paths_pattern tail, and top-level node kinds.

    The MCP is stateless — the chameleon-init skill collects user choices
    and submits them as a single mapping via apply_archetype_renames.
    """
    return tools.propose_archetype_renames(repo, top_n)


@mcp.tool()
def apply_archetype_renames(repo: str, renames: dict) -> dict:
    """Apply an archetype rename mapping atomically.

    Rewrites archetypes.json + canonicals.json + rules.json keys (where
    keyed by archetype) and regenerates profile.summary.md. Wrapped in
    atomic_profile_commit so a crash mid-write leaves the previous profile
    untouched.

    Returns {status, renames_applied, new_profile_sha256}.
    """
    return tools.apply_archetype_renames(repo, renames)


@mcp.tool()
def teach_profile_structured(
    repo: str,
    slug: str,
    rationale: str,
    example: str | None = None,
    counterexample: str | None = None,
    archetype: str | None = None,
    status: str = "active",
    source: str | None = None,
) -> dict:
    """Structured-form idiom capture for /chameleon-teach.

    Validates slug regex (`^[a-z][a-z0-9-]{2,63}$`), enforces a 50KB cap on
    rationale + example + counterexample combined, renders to idioms.md in
    the same format as free-form teach_profile, and delegates to
    teach_profile for the downstream protections (advisory lock,
    sanitization, placeholder strip).

    ``source`` records where the idiom came from (a doc path:line, a git ref,
    or a free-form note) as a ``Source:`` line in idioms.md, so an auto-derived
    or doc-grounded idiom is traceable back to its evidence at trust-review time.
    """
    return tools.teach_profile_structured(
        repo,
        slug=slug,
        rationale=rationale,
        example=example,
        counterexample=counterexample,
        archetype=archetype,
        status=status,
        source=source,
    )


@mcp.tool()
def teach_competing_import(
    repo: str,
    archetype: str,
    preferred: str,
    over: str,
) -> dict:
    """Capture a wrapper-preference ("use `preferred`, not `over`") for an archetype.

    Writes conventions.imports.<archetype>.competing, enabling the
    banned-raw-import / mandatory-wrapper convention + principle that AST
    analysis cannot infer (e.g. "import the project http wrapper, not raw
    axios"). In-place, flock-serialized single-file write.
    """
    return tools.teach_competing_import(
        repo,
        archetype=archetype,
        preferred=preferred,
        over=over,
    )


@mcp.tool()
def unteach_competing_import(
    repo: str,
    archetype: str,
    preferred: str,
    over: str,
) -> dict:
    """Remove a taught wrapper-preference pair ("use `preferred`, not `over`").

    The inverse of teach_competing_import: deletes the matching {preferred, over}
    entry from conventions.imports.<archetype>.competing so a pair taught in error
    stops driving the banned-import lint, without hand-editing conventions.json.
    In-place, flock-serialized single-file write; no-op when the pair is absent.
    """
    return tools.unteach_competing_import(
        repo,
        archetype=archetype,
        preferred=preferred,
        over=over,
    )


@mcp.tool()
def get_idiom_coverage(repo: str) -> dict:
    """Map of guidance chameleon ALREADY captures for a repo. Read-only.

    Drives /chameleon-auto-idiom: the skill reads this BEFORE drafting idiom
    candidates so it never proposes guidance chameleon already auto-derives
    or the team already taught. Returns existing idioms (active slugs with
    summaries + deprecated slugs), principle lines, structured conventions
    (preferred/competing imports, file-naming casing, inheritance bases,
    error-handling shape, non-empty convention kinds), lint sources, and
    archetype names. Fail-open: damaged artifacts skip their section
    (listed in checks_skipped); only a missing profile fails.
    """
    return tools.get_idiom_coverage(repo)


@mcp.tool()
def check_idiom_candidates(repo: str, candidates: list) -> dict:
    """Novelty gate for idiom candidates before they are taught. Read-only.

    Each candidate is {slug, rationale, example?, counterexample?, archetype?}
    (at most 32 per call). Per-candidate verdicts: ``novel`` (safe to teach),
    ``duplicate`` (slug already in idioms.md, text near-identical to an
    existing idiom, or repeats an earlier candidate in the same batch),
    ``covered`` (restates an auto-derived principle, competing-import pair,
    naming/inheritance convention, or lint/format rule), ``invalid``.
    ``quality_warnings`` flags missing example/counterexample and thin
    rationales. Judging only — writes still go through
    teach_profile_structured, which is append-only (existing idioms are
    never modified or removed by this flow).
    """
    return tools.check_idiom_candidates(repo, candidates)


@mcp.tool()
def get_prose_rule_candidates(repo: str) -> dict:
    """Doc-stated "use X not Y" rules, corroborated against the repo's own imports.

    Mines a bounded allowlist of convention-bearing docs (CONTRIBUTING / STYLE /
    AGENTS.md / docs) for import-preference rules AST analysis cannot infer, then
    tags each by how the code backs it: ``corroborated`` (teachable -- preferred
    imported, discouraged absent), ``contested`` (discouraged still imported; doc
    and code disagree), or ``unsupported`` (cannot verify). Each candidate carries
    its ``source`` provenance (doc-path:line).

    PROPOSE-ONLY and read-only: it never writes idioms.md. Feed a corroborated
    candidate to teach_competing_import on approval. Offline, no repo-code
    execution, bounded. Fails open with found=False on an unresolvable / untrusted
    repo.
    """
    return tools.get_prose_rule_candidates(repo)


@mcp.tool()
def daemon_status() -> dict:
    """Return current chameleon-mcp daemon status.

    Phase 4.5 long-lived daemon: returns alive flag, PID, socket path,
    uptime (seconds) and ISO 8601 last_request_at when the daemon
    answered a ping. Read-only — does not start/stop the daemon.
    """
    return tools.daemon_status()


@mcp.tool()
def doctor(repo: str | None = None) -> dict:
    """Triage report for chameleon installation health.

    Returns a structured envelope with subsystem checks. Each check has a
    status (ok | warn | error) and a brief detail string. Subsystems checked:
    python version, bash on PATH, timeout(1) on PATH, the MCP server launcher
    (uvx), plugin data dir writable, all 5 hook scripts present and
    executable, HMAC key sane,
    daemon liveness, last 5 hook error log lines, per-known-repo
    profile/trust state.

    Pass `repo` (an absolute repo root) to target the per-repo checks
    (config_json, production_ref, and the profile_artifacts /
    judge_spawn_health / advisory_emission dead-install detectors) at that
    repo instead of the process cwd; omit it for the cwd-scoped default.

    Use /chameleon-doctor or inspect `data.overall` to get the overall
    health status.
    """
    return tools.doctor(repo=repo)


@mcp.tool()
def dep_audit(repo: str) -> dict:
    """Opt-in dependency / supply-chain audit (npm audit / bundler-audit). Advisory only.

    Runs the ecosystem auditors whose manifests exist in the repo root and returns
    a structured advisory summary. Gated behind CHAMELEON_ALLOW_DEP_AUDIT=1 because
    it hits the network; refuses with a clear message otherwise. Fails open to an
    "unavailable" per-ecosystem result when a binary or the network is absent.
    Never blocks anything.
    """
    return tools.dep_audit(repo)


@mcp.tool()
def scan_dependency_changes(repo: str, base_ref: str = "main") -> dict:
    """No-network supply-chain review of a branch's manifest/lockfile changes. Advisory only.

    Parses the base_ref...HEAD diff of changed package manifests/lockfiles for the
    deterministic pr-review Step 2.5 signals: new install-lifecycle script (FIX),
    lockfile entry resolving from a non-registry host (FIX), non-registry
    dependency source (FIX), and new direct dependency (NIT). Each finding cites
    the exact added line. PURE PARSE: no network, no install (that is dep_audit's
    opt-in job); default-on. Fails open to a no-signal result; never blocks.
    """
    return tools.scan_dependency_changes(repo, base_ref)


def main() -> None:
    """Entry point for `chameleon-mcp` CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
