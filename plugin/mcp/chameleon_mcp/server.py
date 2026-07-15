"""FastMCP server entry point for chameleon-mcp.

Stdio transport. Long-lived process invoked by hooks via UNIX domain socket
(Phase 4 daemon model) or directly per-call (Phase 1C-2 fallback).

See docs/architecture.md sections:
- "MCP server (`chameleon-mcp`)" — full tool surface
- "Performance characteristics" — daemon model
- "Cluster signature function" — what tools rely on

Tool surface (v3 split): the 16 high-frequency conformance/comprehension tools
stay top-level; every lifecycle, review-engine, and telemetry operation routes
through one of three dispatcher tools (chameleon_lifecycle, chameleon_review,
chameleon_telemetry) whose `action` selects the underlying
chameleon_mcp.tools function. 19 registered tools total. The in-process
functions in tools.py are unchanged — hooks, the daemon socket protocol, and
the QA batteries keep importing them directly.

Known limitation (upstream): a ``tools/call`` whose arguments are nested past
pydantic-core's recursion cap (~200 levels) gets no JSON-RPC response — the
underlying mcp SDK drops it in its stream exception handler, so a client blocks
until its own timeout. No real client produces such input; documented rather
than patched here because the guard belongs in the SDK, not chameleon.
"""

import inspect

from mcp.server.fastmcp import FastMCP

from chameleon_mcp import __version__, tools

mcp = FastMCP("chameleon-mcp")

# Report the plugin build as serverInfo.version so a client can correlate the
# running server to the installed chameleon version. FastMCP's constructor does
# not expose the low-level Server's `version` (left None, so serverInfo falls
# back to the mcp SDK's own dist version, which identifies nothing about
# chameleon). Set it on the wrapped server, guarded: this is a cosmetic string,
# so a future SDK that renames the private attribute must NOT crash startup.
# __version__ is kept in sync across the six manifest files by bump-version.sh.
try:
    _low_server = getattr(mcp, "_mcp_server", None)
    if _low_server is not None and hasattr(_low_server, "version"):
        _low_server.version = __version__
except Exception:
    pass


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
    (Ruby), typed_property (TypeScript DI edges), module_attribute (Python
    ``from pkg import mod; mod.func()``). Dynamic/unsupported call paths are
    absent by design.

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
    Grades are deterministic (same_file, import, constant_receiver, typed_property, module_attribute).
    Absence of a caller is NOT dead code (dynamic dispatch / reflection / post-bootstrap
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
    {callee, file, grade} with the deterministic grades (same_file, import,
    constant_receiver, typed_property, module_attribute). Absence of a callee is
    NOT proof it calls nothing (dynamic dispatch is invisible). Fails open with
    found=False on any ambiguity.
    """
    return tools.get_callees(repo, file_path, function_name)


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
def explain_edit(repo: str, file_path: str) -> dict:
    """Replay what chameleon knew and did the last time a file was edited.

    The post-incident recovery read. Returns the most-recent per-edit decision
    log row for file_path (archetype, match_quality, confidence_band, violations
    raised, the block-eligible rules that stood, the resolved outcome) and a
    classification: advised (a rule fired but did not block -- including an
    archetype-independent secret/eval on a no-archetype file, which takes
    precedence over the match quality), coverage-gap (no archetype or
    fallback/none quality AND nothing fired), in-scope-miss (ast/exact match but
    nothing caught the defect), or blocked / overridden (the gate did block).
    found is False when no edit was ever logged.
    """
    return tools.explain_edit(repo, file_path)


# ---------------------------------------------------------------------------
# Dispatcher tools
#
# Every lifecycle, review-engine, and telemetry operation routes through one
# of the three dispatchers below. Each action maps 1:1 onto the same-named
# function in chameleon_mcp.tools, resolved by name at call time (so tests can
# monkeypatch tools.<fn> exactly as they could with the old flat wrappers).
# ---------------------------------------------------------------------------

_LIFECYCLE_ACTIONS = (
    "bootstrap_repo",
    "refresh_repo",
    "trust_profile",
    "list_profiles",
    "merge_profiles",
    "teach_profile",
    "teach_profile_structured",
    "teach_competing_import",
    "unteach_competing_import",
    "propose_archetype_renames",
    "apply_archetype_renames",
    "disable_session",
    "pause_session",
)

_REVIEW_ACTIONS = (
    "get_autopass_verdict",
    "refute_finding",
    "record_review_verdict",
    "record_finding_fate",
    "get_review_history",
    "scan_dependency_changes",
    "dep_audit",
)

_TELEMETRY_ACTIONS = (
    "get_status",
    "get_drift_status",
    "get_drift_antipatterns",
    "get_shadow_report",
    "get_override_audit",
    "get_longitudinal_signals",
    "get_finding_fate_stats",
    "get_shelved_findings",
    "get_idiom_coverage",
    "check_idiom_candidates",
    "list_idiom_candidates",
    "get_prose_rule_candidates",
    "daemon_status",
    "doctor",
)


def _dispatch(
    dispatcher: str,
    valid_actions: tuple[str, ...],
    action: str,
    params: dict | None,
) -> dict:
    """Route a dispatcher call to tools.<action>(**(params or {})).

    An unknown action returns the standard failed envelope (same shape
    tools.py builds via _envelope) listing the dispatcher's valid actions.
    Params that do not bind to the action's real signature return a
    structured error naming that signature -- never a crash. Errors raised
    INSIDE a tool propagate to FastMCP's normal tool-error handling, exactly
    as they did for the flat per-tool wrappers.
    """
    if action not in valid_actions:
        return tools._envelope(
            {
                "status": "failed",
                "error": (
                    f"unknown action {action!r} for {dispatcher}; "
                    f"valid actions: {', '.join(valid_actions)}"
                ),
                "valid_actions": list(valid_actions),
            }
        )
    fn = getattr(tools, action)
    if params is None:
        kwargs: dict = {}
    elif isinstance(params, dict):
        kwargs = dict(params)
    else:
        return tools._envelope(
            {
                "status": "failed",
                "error": (
                    f"params for {dispatcher} action {action!r} must be an object of "
                    f"keyword arguments, got {type(params).__name__}"
                ),
            }
        )
    try:
        inspect.signature(fn).bind(**kwargs)
    except TypeError as exc:
        return tools._envelope(
            {
                "status": "failed",
                "error": (
                    f"invalid params for {dispatcher} action {action!r}: {exc}; "
                    f"expected signature: {action}{inspect.signature(fn)}"
                ),
            }
        )
    return fn(**kwargs)


@mcp.tool()
def chameleon_lifecycle(action: str, params: dict | None = None) -> dict:
    """Profile lifecycle operations, routed by `action`; arguments go in `params`.

    Actions:
    - bootstrap_repo(path, paths_glob=None, force=False, production_ref=None):
      First-time analysis: AST scan + interview + atomic profile commit.
      force=true overwrites a committed profile; production_ref pins
      derivation to a production branch.
    - refresh_repo(repo, force=False): Re-analyze repo, detect drift, update
      profile. OS-level locked via flock.
    - trust_profile(repo, confirmation_token): Mark a committed profile as
      trusted for the current user (token: typed repo name or
      `yes-trust-<repo_id_short>`).
    - list_profiles(cursor=None, limit=100): List all known repos this user
      has touched. Cursor-paginated from day 1.
    - merge_profiles(repo, base, ours, theirs): Three-way merge: re-cluster
      from union of ours and theirs.
    - teach_profile(repo, feedback, archetype=None): Apply user-driven
      correction to profile (idiom, banned import, mandatory wrapper).
    - teach_profile_structured(repo, slug, rationale, example=None,
      counterexample=None, archetype=None, status="active", source=None):
      Structured-form idiom capture for /chameleon-teach. Validates the slug
      against `^[a-z][a-z0-9-]{2,63}$` and enforces a 50KB cap on rationale +
      example + counterexample combined; renders to idioms.md and delegates
      to teach_profile for the downstream protections. `source` records
      provenance (a doc path:line, a git ref, or a note) as a `Source:` line.
    - teach_competing_import(repo, archetype, preferred, over): Capture a
      wrapper-preference ("use `preferred`, not `over`") for an archetype.
    - unteach_competing_import(repo, archetype, preferred, over): Remove a
      taught wrapper-preference pair.
    - propose_archetype_renames(repo, top_n=8): Suggest better names for the
      top-N largest archetypes in the profile.
    - apply_archetype_renames(repo, renames): Apply an archetype rename
      mapping atomically.
    - disable_session(repo, session_id, force=False): Suppress chameleon
      advisory injections for this Claude Code session. The marker is
      HMAC-signed so an out-of-process attacker can't plant a forgery, and
      the repo must have a trust grant before it will write. A session_id
      that has never invoked another chameleon tool for this repo is REFUSED
      by default; pass force=true for legitimate first-time-disable from a
      brand-new session (the response still surfaces a warning).
    - pause_session(repo, minutes=15): Pause chameleon advisory injections
      for `minutes` minutes (default 15). Auto-expires.

    An unknown action returns a failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_lifecycle", _LIFECYCLE_ACTIONS, action, params)


@mcp.tool()
def chameleon_review(action: str, params: dict | None = None) -> dict:
    """PR-review engine operations, routed by `action`; arguments go in `params`.

    Actions:
    - get_autopass_verdict(repo, base_ref="main"): ADVISORY: is this branch's
      diff vs base_ref safe to auto-pass, or does it need a human? Returns
      {auto_pass_eligible, risk, reasons, facts, changed_files, typecheck}.
      Never gates -- it marks the routine slice safe to skip and routes the
      rest to a human with a reason: a grounded block finding, a
      security-sensitive surface, too large, high blast radius, a file
      outside the profiled archetypes, a removed guard line or in-diff
      chameleon-ignore directive, test weakening alongside live-source
      changes, or an unknown blast radius. Fails open toward "needs human".
    - refute_finding(repo, findings, base_ref="main"): Round-3: independently
      refute model-judgment review findings (one hardened no-tools refuter
      per finding; verdict per finding: confirmed = keep, refuted = drop,
      unverified = keep, labeled).
    - record_review_verdict(repo, verdict, findings_count=None,
      commit_sha=None, pr_id=None, complexity_tier=None): Append a
      /chameleon-pr-review verdict to the repo's signed review ledger AFTER
      the verdict is shown. `verdict` is the rendered verdict string,
      `findings_count` the total BLOCK+FIX+NIT count, `commit_sha` the
      reviewed HEAD, `pr_id` optional, `complexity_tier` the change's
      structural tier (easy/medium/hard/complex from get_autopass_verdict).
      Best-effort; tamper-evident, NOT forgery-proof and NOT CI-verifiable.
    - record_finding_fate(repo, fate, message, file=None, line=None,
      lens=None, confidence_at_emit=None, surface=None): Persist how a human
      adjudicated one review finding into the signed finding-fate ledger.
      `fate` is accepted / declined / converted (agree / push-back / convert
      normalize too). Only a 16-hex digest of `message` + `file` + `line` is
      stored, never the prose; `surface` labels the origin (pr-review /
      receiving / deep-work). Best-effort: never blocks the review.
    - get_review_history(repo, limit=None): Return the persisted PR-review
      verdict trail for a repo, newest first (each record HMAC re-checked).
    - scan_dependency_changes(repo, base_ref="main"): No-network supply-chain
      review of a branch's manifest/lockfile changes. Advisory only; pure
      diff parse, no install.
    - dep_audit(repo): Opt-in dependency / supply-chain audit (npm audit /
      bundler-audit); gated behind CHAMELEON_ALLOW_DEP_AUDIT=1 because it
      hits the network. Advisory only.

    An unknown action returns a failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_review", _REVIEW_ACTIONS, action, params)


@mcp.tool()
def chameleon_telemetry(action: str, params: dict | None = None) -> dict:
    """Status, drift, ledger, and health reads, routed by `action`; arguments go in `params`.

    Actions:
    - get_status(repo): Report enforcement mode + active/demoted block rules
      for a repo.
    - get_drift_status(repo): Report freshness, days_since_refresh,
      observed_drift_score for a repo.
    - get_drift_antipatterns(repo, archetype=None): Per-archetype
      recurring-violation signals from this repo's drift history.
    - get_shadow_report(repo, window_days=None): Per-rule would-block counts
      from the shadow log for the shadow->enforce decision.
    - get_override_audit(repo, window_days=None): Per-rule inline-override
      audit: how often each block rule gets chameleon-ignored.
    - get_longitudinal_signals(repo, window_days=None): Two honestly-labelled
      longitudinal health tracks for a repo.
    - get_finding_fate_stats(repo): Per-lens precision from the repo's
      finding-fate ledger (advisory).
    - get_shelved_findings(repo): Below-surface-bar findings currently
      shelved for a repo (severity/claim/file/recurrence per row) -- the
      /chameleon-status and /chameleon-explain browsing surface. Shelved,
      not delivered; recurs toward auto-promotion. Read-only.
    - get_idiom_coverage(repo): Map of guidance chameleon ALREADY captures
      for a repo. Read-only.
    - check_idiom_candidates(repo, candidates): Novelty gate for idiom
      candidates before they are taught. Read-only judging: each candidate is
      {slug, rationale, example?, counterexample?, archetype?}, at most 32
      per call. Per-candidate verdicts: `novel` (safe to teach), `duplicate`
      (slug already in idioms.md, text near-identical to an existing idiom,
      or repeats an earlier candidate in the batch), `covered` (restates an
      auto-derived principle, competing-import pair, naming/inheritance
      convention, or lint/format rule), or `invalid`; `quality_warnings`
      flags missing example/counterexample and thin rationales. Writes still
      go through teach_profile_structured (append-only).
    - list_idiom_candidates(repo): Unapproved idiom proposals the
      self-learning miner derived from real usage (title, rationale,
      evidence trail, occurrences, session_ids) -- the /chameleon-auto-idiom
      "learned from usage" surface. NOTHING here is adopted; a candidate
      becomes a real idiom only through the normal teach/auto-idiom approval
      path. Read-only.
    - get_prose_rule_candidates(repo): Doc-stated "use X not Y" rules,
      corroborated against the repo's own imports. PROPOSE-only, read-only.
    - daemon_status(): Return current chameleon-mcp daemon status. Read-only.
    - doctor(repo=None): Triage report for chameleon installation health
      (subsystem checks; pass `repo` as an absolute repo root to target the
      per-repo checks at that repo instead of the process cwd).

    An unknown action returns a failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_telemetry", _TELEMETRY_ACTIONS, action, params)


def main() -> None:
    """Entry point for `chameleon-mcp` CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
