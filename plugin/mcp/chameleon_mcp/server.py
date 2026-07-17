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

Wire contract (v4.3): every registered tool sends its result as ONE compact
JSON text block — `structured_output=False`, no outputSchema, no
structuredContent duplication, no pretty-print indentation, and null-valued
fields dropped (absent == null for every documented field). The module-level
functions here still RETURN the plain dict (the `_wire_tool` decorator
registers a serializing wrapper with FastMCP and leaves the module symbol
untouched), so in-process callers and tests see dicts, while the model-facing
wire pays no formatting overhead. Dispatcher descriptions are kept under the
2KB ceiling Claude Code truncates tool descriptions at; the full per-action
signatures are available on demand via `action="help"` (generated from the
live tools.py signatures, so they can never drift).

Known limitation (upstream): a ``tools/call`` whose arguments are nested past
pydantic-core's recursion cap (~200 levels) gets no JSON-RPC response — the
underlying mcp SDK drops it in its stream exception handler, so a client blocks
until its own timeout. No real client produces such input; documented rather
than patched here because the guard belongs in the SDK, not chameleon.
"""

import functools
import inspect
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from chameleon_mcp import __version__, tools

# The only server text guaranteed in model context at session start once the
# client defers tool schemas (Claude Code defers MCP tools by default and
# loads them through tool search). Skill-style: says WHEN to search for these
# tools, then the honesty conventions every response shares. Keep under 2KB.
_INSTRUCTIONS = """\
chameleon derives each repo's own conventions and answers codebase-comprehension
queries from committed, trust-gated indexes (the comprehension reads are
offline, read-only, and never execute repo code).

Search for chameleon tools when the task involves: orienting on an unfamiliar
codebase (describe_codebase); finding a symbol/file (search_codebase); "who
calls this / what breaks if I change it" (get_callers, get_blast_radius,
query_symbol_importers, get_callees); per-file convention guidance
(get_pattern_context, get_canonical_excerpt, get_rules, lint_file); PR-review
facts (get_contract_breaks, get_crossfile_context, get_duplication_candidates,
chameleon_review); profile lifecycle — bootstrap/refresh/trust/teach
(chameleon_lifecycle); health/telemetry/post-incident replay
(chameleon_telemetry, explain_edit).

Response conventions: compact JSON; null-valued fields are OMITTED (absent ==
null). Only `found: true` is a real answer — an index-unavailable / untrusted
result means "run /chameleon-refresh or /chameleon-trust", never "no callers".
Absence of a caller edge is not proof of dead code (dynamic dispatch is
invisible to the snapshot). The three dispatcher tools take {action, params};
call any of them with action="help" for every action's full signature.
"""

mcp = FastMCP("chameleon-mcp", instructions=_INSTRUCTIONS)

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


# ---------------------------------------------------------------------------
# Wire layer
# ---------------------------------------------------------------------------

# Every comprehension/conformance read tool is side-effect-free and repeatable;
# announcing that lets a client skip per-call write-permission friction.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _strip_nones(value):
    """Drop null-valued dict fields recursively (absent == null on the wire).

    Tuples recurse like lists (json serializes both as arrays; the rules
    payload nests config dicts inside (source_key, config) tuples).
    """
    if isinstance(value, dict):
        return {k: _strip_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_strip_nones(v) for v in value]
    return value


def _wire(result: dict) -> str:
    """Serialize a tool result for the model: compact, null-free, one text block."""
    try:
        return json.dumps(
            _strip_nones(result), ensure_ascii=False, separators=(",", ":"), default=str
        )
    except Exception:
        # A result that cannot serialize is a chameleon bug; fail structured,
        # never crash the tool call.
        return json.dumps(
            {"api_version": "1", "data": {"status": "failed", "error": "unserializable result"}}
        )


def _wire_tool(annotations: ToolAnnotations | None = None):
    """Register `fn` with FastMCP behind a compact-JSON serializing wrapper.

    The wrapper is what FastMCP calls (returns `str`, `structured_output=False`,
    so the wire carries exactly one un-indented text block and the tool schema
    carries no outputSchema). The decorated module-level symbol stays the plain
    dict-returning function for in-process callers and tests.
    """

    def deco(fn):
        @functools.wraps(fn)
        def wire(*args, **kwargs) -> str:
            return _wire(fn(*args, **kwargs))

        wire.__annotations__ = dict(fn.__annotations__)
        wire.__annotations__["return"] = str
        mcp.tool(structured_output=False, annotations=annotations)(wire)
        return fn

    return deco


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Args:
        repo: repo_id from detect_repo
        file_path: absolute path to file

    Returns:
        archetype + alternatives + content_signal_match info
    """
    return tools.get_archetype(repo, file_path)


@_wire_tool(annotations=_READ_ONLY)
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
            "idioms": str (omitted when none),
            "meta": {"mtime_token": str, "computed_at": str}
          }
        }
    """
    return tools.get_pattern_context(file_path)


@_wire_tool(annotations=_READ_ONLY)
def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the canonical witness source for an archetype.

    The content is the witness file's source as committed (sanitized, not
    annotated), so its length tracks the witness — typically well under a
    thousand tokens, larger for a long canonical file.
    """
    return tools.get_canonical_excerpt(repo, archetype)


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def lint_file(repo: str, archetype: str, content: str, file_path: str | None = None) -> dict:
    """Validate file content against archetype's rules. Returns violations + canonical confidence.

    When file_path is provided, its extension is used for language detection
    instead of falling back to the witness extension.
    """
    return tools.lint_file(repo, archetype, content, file_path=file_path)


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def get_callers(repo: str, file_path: str, function_name: str) -> dict:
    """Who calls this function, from the committed calls snapshot (deterministic grades only).

    Reads the prebuilt calls_index.json artifact. Caller rows are GROUPED:
    each row is one (path, caller, grade, via) with every call line in
    `lines` (ascending); `via` appears when the edge was chased through
    re-export barrels, and barrel-chained edges keep separate rows. Grades are deterministic: same_file, import (TypeScript and
    Python), constant_receiver (Ruby), typed_property (TypeScript DI edges),
    module_attribute (Python ``from pkg import mod; mod.func()``).
    Dynamic/unsupported call paths are absent by design.

    Absence of callers is NOT evidence of dead code -- dynamic dispatch and
    callers added after the last bootstrap are invisible. Fails open with
    found=False on any ambiguity. Never fabricates a caller.
    """
    return tools.get_callers(repo, file_path, function_name)


@_wire_tool(annotations=_READ_ONLY)
def get_blast_radius(repo: str, file_path: str, function_name: str, depth: int = 0) -> dict:
    """Transitive callers of a function (change blast radius), from the calls snapshot.

    Walks the committed calls_index.json UPWARD from ``function_name`` in
    ``file_path`` and returns the bounded caller chains that reach it: "if I
    change this, what transitively calls it". Each chain starts at the
    function's FIRST caller and walks upward (caller -> caller's caller ...);
    the queried function appears once in module/function, never per chain.
    ``depth`` is the hop count;
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


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def describe_codebase(repo: str) -> dict:
    """A structural overview of the repo from its committed profile (comprehension).

    The "what is this codebase" answer: primary language and framework, the
    archetypes (kinds of files, each with size, summary, and canonical witness),
    file/symbol totals, and the god symbols (most-called production functions,
    test files excluded). All from committed artifacts, offline. Fails open with
    found=False on an unresolvable / untrusted repo.
    """
    return tools.describe_codebase(repo)


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def get_contract_breaks(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: deterministic caller-contract breaks for a branch diff vs base_ref.

    For each changed TS/Ruby/Python file, compares its callables' POSITIONAL
    parameter contract at base_ref vs HEAD and flags a NARROWING (new required
    positional, or optional->required) that has committed callers -- the
    deterministic signature-contract signal, surfaced as a citable tool result.
    Returns {status, findings:[{file, name, old/new_required_positional,
    caller_total, callers}]}. A second finding shape,
    kind="removed_export_still_imported" (both positional fields absent), flags
    an export the diff removed outright that indexed importers still reference
    -- the same existence-break class get_crossfile_context reports repo-wide,
    so cite it once across the two tools. git show + AST re-parse, no
    network/repo-exec; default-on; fails open to a no-signal result; never
    blocks. pr-review cites these as FIX.
    """
    return tools.get_contract_breaks(repo, base_ref)


@_wire_tool(annotations=_READ_ONLY)
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


@_wire_tool(annotations=_READ_ONLY)
def get_duplication_candidates(repo: str, file_path: str) -> dict:
    """Existing functions a file's new functions may re-implement under a new name.

    For each function defined in file_path, the bootstrap function catalog is
    prefiltered (signature shape + name-token overlap) to existing functions
    elsewhere in the repo that look like the same intent under a different name --
    the toDisplayDate vs formatDate case exact-name matching misses. Each
    candidate carries a short body excerpt read from disk. The response is
    budget-capped; when candidates or excerpts are dropped to fit, the result
    says so and names the file it kept working on.

    The tool only PREFILTERS; the LLM caller judges semantic equivalence against
    the candidate bodies. Duplication is a judgment call, so any finding raised
    from this is advisory FIX/NIT at most, never block-eligible.

    Returns {found, file, matches: [{function, candidates: [...]}]}. Fails open
    with found=False on any ambiguity (unresolvable/untrusted repo, missing
    catalog, unparsable file). Never fabricates a candidate.
    """
    return tools.get_duplication_candidates(repo, file_path)


@_wire_tool(annotations=_READ_ONLY)
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
#
# Description budget: Claude Code truncates each tool description at 2KB, so
# each dispatcher docstring stays under that with one line per action; the
# full signatures come from `action="help"`, generated from tools.py at call
# time so they can never drift from the code.
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


# Test-only injection parameters (clock/root overrides) that exist on the
# tools.py signatures but are not part of the model-facing API; help must not
# advertise them.
_HELP_HIDDEN_PARAMS = frozenset({"now", "analysis_root"})


def _action_help(dispatcher: str, valid_actions: tuple[str, ...]) -> dict:
    """Build the on-demand action reference from the LIVE tools.py surface.

    Each entry is the action's real signature (inspect.signature, so a changed
    default or new parameter shows up here without any doc edit) plus the first
    paragraph of its tools.py docstring. This is the detail the dispatcher
    docstrings deliberately no longer carry.
    """
    entries = []
    for action in valid_actions:
        fn = getattr(tools, action, None)
        if not callable(fn):
            continue
        try:
            params = []
            for p in inspect.signature(fn).parameters.values():
                if p.name in _HELP_HIDDEN_PARAMS:
                    continue
                part = p.name
                ann = p.annotation
                if ann is not inspect.Parameter.empty:
                    # tools.py uses `from __future__ import annotations`, so
                    # annotations arrive as strings; render them unquoted.
                    part += f": {ann if isinstance(ann, str) else getattr(ann, '__name__', ann)}"
                if p.default is not inspect.Parameter.empty:
                    part += f"={p.default!r}"
                params.append(part)
            sig = f"({', '.join(params)})"
        except (TypeError, ValueError):
            sig = "(...)"
        doc = inspect.getdoc(fn) or ""
        summary = " ".join(doc.split("\n\n", 1)[0].split())
        entries.append({"action": f"{action}{sig}", "summary": summary[:400]})
    return tools._envelope({"status": "ok", "dispatcher": dispatcher, "actions": entries})


def _dispatch(
    dispatcher: str,
    valid_actions: tuple[str, ...],
    action: str,
    params: dict | None,
) -> dict:
    """Route a dispatcher call to tools.<action>(**(params or {})).

    ``action="help"`` returns the generated per-action signature reference.
    An unknown action returns the standard failed envelope (same shape
    tools.py builds via _envelope) listing the dispatcher's valid actions.
    Params that do not bind to the action's real signature return a
    structured error naming that signature -- never a crash. Errors raised
    INSIDE a tool propagate to FastMCP's normal tool-error handling, exactly
    as they did for the flat per-tool wrappers.
    """
    if action == "help":
        return _action_help(dispatcher, valid_actions)
    if action not in valid_actions:
        return tools._envelope(
            {
                "status": "failed",
                "error": (
                    f"unknown action {action!r} for {dispatcher}; "
                    f"valid actions: {', '.join(valid_actions)}, help"
                ),
                "valid_actions": [*valid_actions, "help"],
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


@_wire_tool()
def chameleon_lifecycle(action: str, params: dict | None = None) -> dict:
    """Profile lifecycle: bootstrap, refresh, trust, teach, idioms, rename, merge,
    disable/pause. Route by `action`; arguments go in `params` as keyword
    arguments. `action="help"` returns every action's full signature.

    Actions:
    - bootstrap_repo(path, ...): first-time repo analysis -> committed profile
      (force=true overwrites; production_ref pins derivation to a branch).
    - refresh_repo(repo, force=False): re-analyze + update profile (flock-locked).
    - trust_profile(repo, confirmation_token): approve a committed profile
      (token: typed repo name or `yes-trust-<repo_id_short>`).
    - list_profiles(cursor=None, limit=100): known repos, cursor-paginated.
    - merge_profiles(repo, base, ours, theirs): three-way profile merge.
    - teach_profile(repo, feedback, archetype=None): free-form correction
      (idiom, banned import, mandatory wrapper).
    - teach_profile_structured(repo, slug, rationale, ...): structured idiom
      capture; slug must match ^[a-z][a-z0-9-]{2,63}$; 50KB cap on
      rationale+example+counterexample combined; `source` records provenance.
    - teach_competing_import(repo, archetype, preferred, over) /
      unteach_competing_import(...): capture/remove "use preferred, not over".
    - propose_archetype_renames(repo, top_n=8) /
      apply_archetype_renames(repo, renames): better archetype names.
    - disable_session(repo, session_id, force=False): suppress advisories this
      session. Marker HMAC-signed; requires a trust grant; a session that never
      invoked another chameleon tool for this repo is REFUSED by default
      (force=true overrides, still warned).
    - pause_session(repo, minutes=15): pause advisories; HMAC-signed marker,
      auto-expires.

    Unknown action -> failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_lifecycle", _LIFECYCLE_ACTIONS, action, params)


@_wire_tool()
def chameleon_review(action: str, params: dict | None = None) -> dict:
    """PR-review engine: auto-pass routing, finding refutation, verdict + fate
    ledgers, dependency scans. Route by `action`; arguments go in `params`.
    `action="help"` returns every action's full signature.

    Actions:
    - get_autopass_verdict(repo, base_ref="main"): ADVISORY is-this-diff safe to
      auto-pass. Never gates; fails open toward needs-human with a reason
      (grounded block finding, security-sensitive surface, too large, high or
      unknown blast radius, file outside profiled archetypes, removed guard or
      in-diff chameleon-ignore, test weakening beside live-source changes).
    - refute_finding(repo, findings, base_ref="main"): round-3 independent
      refuter, one hardened no-tools spawn per finding (confirmed=keep,
      refuted=drop, unverified=keep labeled).
    - record_review_verdict(repo, verdict, findings_count=None, commit_sha=None,
      pr_id=None, complexity_tier=None): append a shown verdict to the signed
      review ledger (tamper-evident, NOT forgery-proof, not CI-verifiable).
    - record_finding_fate(repo, fate, message, file=None, line=None, lens=None,
      confidence_at_emit=None, surface=None): persist one human adjudication
      (fate: accepted / declined / converted); stores only a 16-hex digest of
      message+file+line, never the prose.
    - get_review_history(repo, limit=None): verdict trail, newest first, HMAC
      re-checked.
    - scan_dependency_changes(repo, base_ref="main"): no-network supply-chain
      review of manifest/lockfile diffs; advisory only.
    - dep_audit(repo): opt-in npm/bundler audit (network;
      CHAMELEON_ALLOW_DEP_AUDIT=1); advisory only.

    Unknown action -> failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_review", _REVIEW_ACTIONS, action, params)


@_wire_tool(annotations=_READ_ONLY)
def chameleon_telemetry(action: str, params: dict | None = None) -> dict:
    """Status, drift, ledger, idiom-candidate, and health reads (all read-only).
    Route by `action`; arguments go in `params`. `action="help"` returns every
    action's full signature.

    Actions:
    - get_status(repo): enforcement mode + active/demoted block rules.
    - get_drift_status(repo): freshness, days_since_refresh, drift score.
    - get_drift_antipatterns(repo, archetype=None): recurring-violation signals.
    - get_shadow_report(repo, window_days=None): per-rule would-block counts.
    - get_override_audit(repo, window_days=None): per-rule inline-override audit.
    - get_longitudinal_signals(repo, window_days=None): longitudinal health tracks.
    - get_finding_fate_stats(repo): per-lens precision from the finding-fate ledger.
    - get_shelved_findings(repo): below-bar findings currently shelved
      (recurrence counts toward auto-promotion).
    - get_idiom_coverage(repo): guidance chameleon ALREADY captures.
    - check_idiom_candidates(repo, candidates): novelty gate before teaching; at
      most 32 candidates per call, each {slug, rationale, example?,
      counterexample?, archetype?}; per-candidate verdicts `novel` /
      `duplicate` / `covered` / `invalid` plus quality_warnings.
    - list_idiom_candidates(repo): unapproved usage-mined idiom proposals
      (NOTHING auto-adopts; teach/auto-idiom approval is the only path in).
    - get_prose_rule_candidates(repo): doc-stated "use X not Y" rules
      corroborated against the repo's imports; propose-only.
    - daemon_status(): daemon health.
    - doctor(repo=None): installation triage report (pass an absolute repo root
      to target per-repo checks).

    Unknown action -> failed envelope listing the valid actions.
    """
    return _dispatch("chameleon_telemetry", _TELEMETRY_ACTIONS, action, params)


def main() -> None:
    """Entry point for `chameleon-mcp` CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
