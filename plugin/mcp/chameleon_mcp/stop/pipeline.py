"""Stop turn-end gate orchestration: ``RootContext`` (one workspace root's
slice of a Stop invocation), ``stop_gates`` (the per-root gate pipeline
``gate_one_root`` calls once per discovered workspace), multi-root discovery
(``discover_stop_roots``), per-root trust/suppression dispatch
(``gate_one_root``), and the session attestation writer
(``write_session_attestation``).

``stop_gates`` runs, in order: the ENFORCE/feature-flag kill switches, the
live re-lint of every candidate file the per-edit verify armed, the unresolved
hard-block decision, the crossfile-existence hard-block decision, then (when
neither blocked) the turn-end advisory pipeline -- ``_run_review_job``
(``stop/scheduler.py``'s route decision and, on a spawn, the detached review
job launch; async-first per spec section 3.1, so it emits an in-turn
``additionalContext`` block only under ``CHAMELEON_JUDGE_WAIT``), the
test-run reminder, and the deterministic advisories (stale-test, change-set
completeness, historical co-change, cross-file/cross-workspace existence,
test integrity, scope drift). It never emits; the caller (``gate_one_root`` /
``stop_backstop``) is the sole place a hook-output dict reaches stdout.

``discover_stop_roots`` globs every workspace root the session touched (each
per-edit hook writes state under the edited file's OWN workspace repo_id, not
the launch cwd), regrouping by ``find_repo_root`` so a coordinator-cwd session
still gates every touched workspace. ``gate_one_root`` applies per-workspace
trust/staleness/suppression before handing off to ``stop_gates``.
``write_session_attestation`` builds (via the still-hook_helper-resident
``_build_session_attestation``) and persists one signed Stop attestation per
distinct run-root.

Extracted verbatim from ``hook_helper.py``'s ``_stop_gates``/
``_discover_stop_roots``/``_gate_one_root``/``_write_session_attestation`` (the
call-sites of all four are frozen -- see their shims in ``hook_helper.py``).
The pre-async-first judge/multi-lens/duplication/idiom-gate machinery still
lives in ``hook_helper.py`` (uncalled from this pipeline; a later task deletes
it) alongside ``_build_session_attestation`` and every other hook_helper-
resident helper, including the ``stop/gates.py`` and ``stop/advisories.py``
extractions re-exported as hook_helper module attributes -- all resolved
late-bound via a deferred ``from chameleon_mcp import hook_helper as hh``
import inside each function, so a test that patches
``chameleon_mcp.hook_helper.<name>`` stays effective for a call made from this
module -- and so this module's own top-level imports stay stdlib-only,
mirroring hook_helper's own pattern of deferring every non-stdlib import to
call time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RootContext:
    """One workspace root's slice of a Stop invocation."""

    payload: dict
    repo_root: Path
    repo_id: str
    session_id: str | None
    is_subagent: bool
    repo_data: Path
    daemon_state: dict | None = None
    only_files: set[str] | None = None
    allow_model_spawn: bool = True


def stop_gates(ctx: RootContext) -> dict:
    """Run the turn-end gates and return the hook-output dict (never emits).

    Mechanical extraction of stop_backstop's gate pipeline so the caller can
    write the session attestation at a single site after every gate finished
    and saved state. Ordering and blocking semantics are unchanged from when
    this body lived inline. CHAMELEON_ENFORCE=0 is checked here rather than
    before repo resolution so an enforce-off session still reaches the caller's
    attestation write with its env state recorded; it returns {} immediately,
    exactly as the old early return did. Fails open to {}.

    Multi-root (coordinator monorepo) fields on ``ctx``, all defaulting to the
    single-root behavior so the ordinary path is unchanged:

    - ``only_files``: when set, ``state.files`` is filtered to just these
      absolute paths right after load, scoping the candidate re-lint AND every
      advisory helper (they all read ``state.files``) to one workspace's edits.
      A shared-repo_id monorepo keeps ALL workspaces' files in one state file;
      scoping lets each workspace re-lint against its own profile. In this mode
      the internal saves use ``prune_missing=False`` so root-A's save cannot
      delete root-B's just-deleted entry before root-B's scoped pass records it.
    - ``allow_model_spawn``: when False, the scheduler route/launch
      (``_run_review_job`` -- the only remaining spawn site) is skipped
      entirely so the whole Stop pays for at most one detached review job
      across all roots; deterministic advisories still run.

    The multi-root caller short-circuits on the first blocking root (armed roots
    rank first), so ``stop_hook_blocks`` is incremented for exactly one root per
    Stop even when several workspaces share one ``repo_data`` -- the anti-loop
    cap cannot be double-spent.
    """
    repo_root = ctx.repo_root
    repo_id = ctx.repo_id
    session_id = ctx.session_id
    is_subagent = ctx.is_subagent
    repo_data = ctx.repo_data
    daemon_state = ctx.daemon_state
    only_files = ctx.only_files
    allow_model_spawn = ctx.allow_model_spawn
    try:
        from chameleon_mcp import hook_helper as hh
        from chameleon_mcp.enforcement import load_state, save_state

        if os.environ.get("CHAMELEON_ENFORCE") == "0":
            hh._emit_check_event(repo_id, session_id, "stop_relint", "skipped", "enforce_env_off")
            return {}

        from chameleon_mcp.profile.config import load_config_enforcement_only

        # Isolated enforcement read: an unrelated config-section typo must not
        # raise and silently disable the Stop backstop.
        cfg = load_config_enforcement_only(hh._enf_profile_dir(repo_root))
        if not cfg.stop_backstop:
            hh._emit_check_event(repo_id, session_id, "stop_relint", "skipped", "feature_disabled")
            return {}

        state = load_state(repo_data, session_id or "")
        # Multi-root scoping: keep only this workspace's files so the candidate
        # loop and every advisory helper (which all iterate state.files) see just
        # the edits that belong to repo_root. The session counters ride along on
        # the same loaded state; the additive save-merge preserves other roots'
        # entries. prune_missing must be off here (see the save below).
        if only_files is not None:
            state.files = {k: v for k, v in state.files.items() if k in only_files}
        _prune_on_save = only_files is None
        # Per-workspace block scope: computed in BOTH modes so the anti-loop cap
        # stays consistent across a mid-session single<->multi cardinality change.
        # ``_ws_scope`` (the once-per-session idiom marker discriminator) stays
        # None in single-root to keep the legacy marker filename + tests unchanged;
        # the block budget always keys by ``_block_scope``.
        _block_scope = hh._stop_block_scope(repo_root)
        _ws_scope: str | None = _block_scope if only_files is not None else None

        # Cap reached: the backstop must never BLOCK again this session (so an
        # unresolvable violation cannot trap the turn in a loop), but the turn-end
        # advisories still run below -- silencing them once the block budget was
        # spent was a coverage gap. cap_reached suppresses only the block, not the
        # advisory pipeline. The per-workspace budget means one dirty workspace's
        # blocks never downgrade a sibling's hard block to advisory.
        _block_count = hh._effective_stop_blocks(state, _block_scope)
        cap_reached = _block_count >= cfg.stop_block_cap
        if cap_reached:
            hh._emit_check_event(repo_id, session_id, "stop_relint", "skipped", "cap_reached")

        # A candidate (L2 with the cached flag set) is only blocked after a LIVE
        # re-lint confirms an enforceable hard violation still stands; the cached
        # flag alone can be stale (e.g. a phantom import resolved by a later edit
        # to the import target, leaving the importing file un-reverified). Files
        # that re-verify clean have their flag cleared and the state persisted, so
        # the heal sticks and they aren't re-checked next turn.
        #
        # Load the profile once for the whole pass: every candidate re-lints
        # against the same profile, so re-reading it per file just multiplies the
        # stat + witness recalibration cost across the candidate set.
        preloaded = None
        try:
            from chameleon_mcp.profile.loader import load_profile_dir
            from chameleon_mcp.tools import _effective_profile_dir

            preloaded = load_profile_dir(_effective_profile_dir(repo_root))
        except Exception:
            preloaded = None

        # Read the active block rules once for the whole pass. Each candidate
        # re-lint consults the same set, so reading enforcement.json per file just
        # multiplies the disk I/O across the candidate set.
        active_rules: set[str] | None = None
        try:
            from chameleon_mcp.enforcement_calibration import active_block_rules

            active_rules = active_block_rules(hh._enf_profile_dir(repo_root))
        except Exception:
            active_rules = None

        # Shared liveness flag for the per-file daemon fallback: once a daemon
        # call comes back empty, every later file skips the daemon and resolves
        # the archetype in-process, so a hung daemon cannot stack timeouts. The
        # caller shares the same flag with the attestation writer so the whole
        # Stop pays for at most one failed daemon probe.
        if daemon_state is None:
            daemon_state = {"available": True}

        unresolved: list[str] = []
        # path -> enforceable hard rules still standing, so the shadow would_block
        # row can attribute the backstop block to the specific rules per file.
        unresolved_rules: dict[str, list[str]] = {}
        # Files this turn recorded as edited that no longer exist: a module the
        # turn DELETED. It exports nothing now, so its importers' call sites are
        # broken -- the strongest existence break there is. The prune below drops
        # them from state (so it does not accumulate phantom paths), so capture
        # them here first and hand them to the crossfile advisory, which the loop
        # otherwise never sees (it iterates the surviving state.files).
        deleted_paths: list[str] = []
        cleared_any = False
        for path, fs in list(state.files.items()):
            p = Path(path)
            if not p.is_file():
                # The file was deleted since it was recorded; drop its entry so
                # state does not accumulate phantom paths across the session.
                deleted_paths.append(path)
                del state.files[path]
                cleared_any = True
                # Persist it: if THIS Stop short-circuits before the advisory
                # pipeline (the once-per-session idiom block), the prune above
                # would otherwise lose the deletion before its crossfile advisory
                # ever runs. Surfaced exactly once from the persisted set.
                hh._record_pending_deletions(repo_data, session_id, [path])
                continue
            # Any file the per-edit verifier armed (cached blockable flag) is a
            # re-check candidate, regardless of escalation level. The level gate
            # belongs inside the re-lint, not here: a deterministic content fact
            # (a leaked credential, a phantom import) refuses the turn on the
            # first edit, while an archetype-dependent rule still honors the L2
            # ladder. Filtering on L2 here disarmed the documented turn-end
            # refusal for a single-edit secret/phantom that sits at L0/L1.
            if not fs.blockable_unresolved:
                continue
            file_rules: list[str] = []
            verdict = hh._stop_file_still_blockable(
                repo_root,
                path,
                loaded=preloaded,
                active=active_rules,
                daemon_state=daemon_state,
                out_rules=file_rules,
                level=fs.level,
            )
            if verdict is None:
                # Re-verify could not run this turn (the file was unreadable);
                # keep the file armed and re-check next Stop rather than clearing
                # the flag on a violation that may still stand. Do not add it to
                # ``unresolved`` -- an unverifiable file must not block THIS turn.
                continue
            if verdict:
                unresolved.append(path)
                unresolved_rules[path] = file_rules
            else:
                fs.blockable_unresolved = False
                cleared_any = True

        if cleared_any:
            try:
                save_state(state, repo_data, session_id or "", prune_missing=_prune_on_save)
            except Exception:
                pass

        # The candidate re-lint completed (possibly over zero candidates):
        # record it so the attestation can attest the relint ran this Stop.
        hh._emit_check_event(repo_id, session_id, "stop_relint", "ran")

        def _run_advisories() -> dict:
            # Turn-end advisory pipeline, extracted so it runs in EVERY
            # non-blocking case -- a clean turn, a shadow turn, an off turn, and a
            # capped/shadow turn that had an unresolved violation the backstop did
            # not block. Silencing these advisories whenever a would-block file was
            # present (or the cap was spent) was a coverage gap.
            #
            # Async-first model review (spec section 3.1): ``stop/scheduler.py``
            # decides whether to launch ONE detached review job covering
            # whichever lenses (correctness/duplication/idiom) the repo's config
            # enables -- it replaces the old idiom-review interrupt, the
            # correctness-judge route/gate, the multi-lens pass, and the
            # standalone duplication gate outright; there is nothing left for a
            # five-boolean defer matrix to arbitrate. The idiom review is no
            # longer a guaranteed once-per-session interrupt: it is a scoped
            # detector lens inside the job (compliant turns hear nothing).
            # SubagentStop never schedules -- the scheduler itself refuses
            # (``RouteContext.is_subagent``), so this call is unconditional here.
            #
            # allow_model_spawn is False on every non-first root of a multi-root
            # Stop: the reviewer budget (one job across the whole 55s Stop) was
            # spent by the ranked-first root. Skip the route computation entirely
            # (its risk facts / blast-radius reads are the fixed cost we do not
            # want to pay per root) rather than force a non-spawning decision
            # through the scheduler. Deterministic advisories still run for this
            # root.

            # Finding->fix loop re-check (#9): re-check every PRIOR-Stop finding
            # scoped to this workspace and re-surface an unaddressed high-severity
            # one exactly once. Deliberately inside the advisory pipeline, not
            # before the block decision: marking a row ``resurfaced`` is an
            # irreversible terminal transition (``undelivered_findings`` never
            # returns it again, ``mark_delivered`` refuses it as a source state),
            # so it must happen ONLY on a turn that actually emits the resurface
            # line. A blocking Stop returns before this runs, leaving the finding
            # open to resurface on a later non-blocking Stop rather than burning
            # its one shot on a turn whose output is discarded. That protects the
            # BLOCKING root, but a multi-root Stop has one more failure mode: an
            # EARLIER non-blocking root's resurface line packs into ITS output,
            # and a LATER root then blocks -- the Stop caller (stop_backstop)
            # discards every non-blocking root's advisories on a block, which
            # would silently burn the earlier root's one-shot resurface for
            # nothing. ``compute_resurface`` is therefore the PURE recheck: it
            # still commits the ``addressed`` side inline (a finding whose file
            # changed or vanished is gone regardless of whether this turn's
            # output ever reaches the user), but leaves the terminal
            # ``resurfaced`` write to ``review_ledger.mark_resurfaced``, called
            # only by stop_backstop after the whole multi-root loop confirms no
            # later root blocked AND the ranked assembler actually packed this
            # root's resurface item. Gated by CHAMELEON_FINDING_LEDGER at the
            # call site (compute_resurface is unconditional). Fail-open to [].
            resurface_lines: list[str] = []
            resurface_match_keys: tuple[str, ...] = ()
            if repo_id and os.environ.get("CHAMELEON_FINDING_LEDGER") != "0":
                try:
                    from chameleon_mcp import review_ledger

                    resurface_result = review_ledger.compute_resurface(repo_id, repo_root)
                    resurface_lines = resurface_result.lines
                    resurface_match_keys = resurface_result.match_keys
                except Exception:
                    resurface_lines = []
                    resurface_match_keys = ()

            review_context: str | None = None
            # Under CHAMELEON_JUDGE_WAIT the review render's delivery is NOT
            # committed inside _run_review_job (see its docstring): these are
            # the findings its text represents, committed by the Stop caller
            # only if the block survives the ranked pack below AND no later
            # root blocks -- the same drop-safe two-phase the resurface path
            # uses, so a ceiling-dropped review block never retires a finding
            # the user never saw.
            review_delivered_keys: tuple[str, ...] = ()
            if allow_model_spawn:
                review_context, review_delivered_keys = _run_review_job(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                    repo_data=repo_data,
                    daemon_state=daemon_state,
                    is_subagent=is_subagent,
                )
            else:
                hh._emit_check_event(
                    repo_id, session_id, "review_job", "skipped", "multiroot_budget"
                )

            # Test-run reminder: real source edited, no passing test run seen
            # this session. Extracted into its own deterministic advisory (it
            # used to ride the idiom gate's once-per-session block); it fires
            # independently of enforcement.idiom_review and of whether the
            # review job above spawned. Top-level Stop only, mirroring the
            # is_subagent guard on scope-drift below -- a subagent's narrow task
            # is not the turn-ending point the reminder is aimed at; the parent
            # Stop still gets it.
            reminder_lines: list[str] = []
            if not is_subagent:
                reminder_lines = hh._test_run_reminder_lines(
                    repo_root=repo_root,
                    repo_id=repo_id,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                )

            # Stale-test advisory: a turn that edited a paired source but left its
            # existing test untouched gets a coverage nudge. Advisory only, folded
            # into the same Stop context the judge uses so a turn emits one block.
            stale_lines = hh._stale_test_advisory_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                preloaded=preloaded,
                daemon_state=daemon_state,
            )

            # Change-set completeness: a turn that created a new file whose
            # framework convention demands a companion (a model its migration, a
            # controller its route) but whose change-set carries none gets a
            # nudge. Advisory only, folded into the same Stop context.
            cochange_lines = hh._changeset_completeness_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                daemon_state=daemon_state,
                persist=lambda: save_state(state, repo_data, session_id or ""),
            )

            # Historical co-change (F7): a turn that edited a file whose git history
            # shows a usual partner, left untouched. Framework-agnostic complement
            # to the curated pairs above, read from the plugin-data index built at
            # bootstrap. Advisory only, folded into the same Stop context.
            cochist_lines = hh._cochange_history_advisory_lines(
                repo_root=repo_root,
                repo_id=repo_id,
                state=state,
                cfg=cfg,
                persist=lambda: save_state(state, repo_data, session_id or ""),
            )

            # Cross-file existence breaks: a turn that removed/renamed a TS export
            # other files still import by name left their call sites broken. Reuse
            # the persisted reverse index + a regex presence check (no parse at
            # Stop). Advisory only, folded into the same Stop context.
            # deleted_paths carries THIS turn's deletions plus any persisted from a
            # prior Stop that short-circuited (idiom block) before this pipeline ran;
            # dedup and mark surfaced afterwards so a deleted module is reported once.
            pending_del = hh._consume_pending_deletions(repo_data, session_id)
            all_deleted = list(dict.fromkeys(list(deleted_paths) + pending_del))
            crossfile_lines = hh._crossfile_existence_advisory_lines(
                repo_root=repo_root,
                state=state,
                cfg=cfg,
                deleted_paths=all_deleted,
            )
            hh._mark_pending_deletions_surfaced(repo_data, session_id, all_deleted)

            # WP-C5: cross-WORKSPACE existence breaks -- an export this workspace
            # file removed that a SIBLING workspace still imports (read from the
            # coordinator cross index in the plugin data dir). Advisory only.
            crossws_lines = hh._crossworkspace_existence_advisory_lines(
                repo_root=repo_root, state=state, cfg=cfg
            )

            # Turn-end test integrity: a turn that changed live source while
            # weakening tests (added skips, dropped assertions, deleted tests)
            # gets a deterministic advisory naming what was weakened. Zero model
            # spawn, folded into the same Stop context.
            testint_lines = hh._test_integrity_advisory_lines(
                repo_root=repo_root,
                repo_id=repo_id,
                session_id=session_id,
                state=state,
                cfg=cfg,
                repo_data=repo_data,
            )

            # Intent scope drift: when the session's captured request named specific
            # identifiers, flag any changed file that shares nothing with them as a
            # possibly-unrequested change. Advisory only; top-level Stop only (a
            # subagent is dispatched for a scoped sub-task, so its file set is
            # expected to differ from the parent request).
            scope_lines: list[str] = []
            if not is_subagent:
                scope_lines = hh._scope_drift_advisory_lines(
                    repo_root=repo_root,
                    repo_data=repo_data,
                    session_id=session_id,
                    state=state,
                    cfg=cfg,
                )

            # Ranked, budgeted single emission (spec section 6) in place of the
            # old unbudgeted "\n\n".join(context_blocks): every source above
            # becomes one EmissionItem at its rank tier, and
            # assemble_stop_context greedy-packs them under
            # STOP_RENDER_TOKEN_CEILING. header=None keeps the pre-ranking
            # behavior: each item is ALREADY a [🦎]-headered
            # <chameleon-context> block, so no extra top-level header is added
            # (the emission keeps its per-block headers, blocks blank-line
            # separated -- exactly the old additive join) and only the ranking
            # + ceiling + packed-key accounting are new. Ranking preserves the
            # old fixed source order (resurface, review, then the 8
            # deterministic advisories) since each tier is stable-sorted.
            #
            # Two items carry match_keys for the drop-safe two-phase commit the
            # caller (stop_backstop) runs AFTER the whole multi-root loop:
            # resurface (mark_resurfaced) and, under JUDGE_WAIT, review
            # (mark_delivered). Each is committed ONLY for the keys that packed
            # here (in ``packed_match_keys``) AND belong to a root the loop did
            # not later discard for a block -- a ceiling-dropped or
            # block-discarded item leaves its findings reachable next turn.
            from chameleon_mcp._thresholds import threshold_int
            from chameleon_mcp.stop.assemble import (
                PRIORITY_ADVISORY,
                PRIORITY_DELIVERED_UNVERIFIED,
                PRIORITY_RESURFACED,
                EmissionItem,
                assemble_stop_context,
            )

            items: list[EmissionItem] = []
            if resurface_lines:
                items.append(
                    EmissionItem(
                        priority=PRIORITY_RESURFACED,
                        text=(
                            "<chameleon-context>\n"
                            + "\n".join(resurface_lines)
                            + "\n</chameleon-context>"
                        ),
                        match_keys=resurface_match_keys,
                        droppable=False,
                    )
                )
            if review_context:
                # Already <chameleon-context>-wrapped (with its own single
                # [🦎] header) by judge_wait.wait_and_render. Its findings are
                # NOT yet marked delivered (deferred, see review_delivered_keys
                # above): they ride as this item's match_keys so the caller
                # commits mark_delivered only if this block actually packs.
                items.append(
                    EmissionItem(
                        priority=PRIORITY_DELIVERED_UNVERIFIED,
                        text=review_context,
                        match_keys=review_delivered_keys,
                    )
                )
            for lines in (
                reminder_lines,
                stale_lines,
                cochange_lines,
                cochist_lines,
                crossfile_lines,
                crossws_lines,
                testint_lines,
                scope_lines,
            ):
                if lines:
                    items.append(
                        EmissionItem(
                            priority=PRIORITY_ADVISORY,
                            text="<chameleon-context>\n"
                            + "\n".join(lines)
                            + "\n</chameleon-context>",
                        )
                    )

            try:
                assembled = assemble_stop_context(
                    items,
                    header=None,
                    ceiling_tokens=threshold_int("STOP_RENDER_TOKEN_CEILING"),
                )
            except Exception:
                # Fail-open: a packer bug must not erase every advisory this
                # pass already computed successfully above. Fall back to the
                # pre-ranked additive join (today's shape, minus ranking and
                # the ceiling) rather than losing the turn's advisories
                # outright. No resurface/delivery commit rides this path -- the
                # candidates stay pending, exactly as an omitted-for-space
                # item would.
                joined = "\n\n".join(it.text for it in items)
                if not joined:
                    return {}
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": joined,
                    }
                }
            if not assembled.text:
                return {}
            result: dict = {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": assembled.text,
                }
            }
            # Private side-channels, read and consumed only by stop_backstop's
            # multi-root loop -- never forwarded to the emitted hook output
            # (the caller extracts hookSpecificOutput.additionalContext
            # separately from this dict; a direct single-root emit never
            # reaches _emit with these keys still attached in production, since
            # stop_backstop always goes through the same root loop, which reads
            # and drops them). Each carries only the keys that actually packed.
            packed = set(assembled.packed_match_keys)
            resurface_committed = tuple(k for k in resurface_match_keys if k in packed)
            if resurface_committed:
                result["_resurface_committed_keys"] = resurface_committed
            review_committed = tuple(k for k in review_delivered_keys if k in packed)
            if review_committed:
                result["_review_delivered_keys"] = review_committed
            return result

        # Block decision first, THEN the advisory pipeline. An unresolved
        # violation only ever suppresses the Stop under enforce with cap budget
        # left; shadow, off, and a capped enforce turn never block. In every
        # non-blocking case the turn-end advisories still run below -- the
        # previous early returns silenced them whenever a would-block file was
        # present, which starved duplication / crossfile / scope-drift review.
        if unresolved:
            hard_block = cfg.mode == "enforce" and not cap_reached
            # Record the would-have-blocked signal for shadow (feeds the
            # promotion report) AND for a capped enforce turn (the block the cap
            # swallowed is still real signal). off stays fully silent: a
            # would_block row on an enforcement-off repo is itself misleading.
            if cfg.mode == "shadow" or (cap_reached and cfg.mode == "enforce"):
                try:
                    from chameleon_mcp.metrics import emit_hook_metric

                    # One would_block row per rule per unresolved file, so the
                    # shadow report attributes the backstop block to the specific
                    # rule and can sample the file for spot-check. A file that
                    # re-lints blockable but yielded no rule name still gets one
                    # row with a null rule so the file:line sample is not lost.
                    emitted_any = False
                    for path in unresolved:
                        file_rel = hh._repo_rel(repo_root, path)
                        rules = unresolved_rules.get(path) or [None]
                        for rule in rules:
                            emit_hook_metric(
                                "stop-backstop",
                                elapsed_ms=0,
                                repo_id=repo_id,
                                advisory_emitted=True,
                                would_block=True,
                                rule=rule,
                                file_rel=file_rel,
                            )
                            emitted_any = True
                    if not emitted_any:
                        emit_hook_metric(
                            "stop-backstop",
                            elapsed_ms=0,
                            repo_id=repo_id,
                            advisory_emitted=True,
                            would_block=True,
                        )
                except Exception:
                    pass

            if hard_block:
                from chameleon_mcp.sanitization import sanitize_for_chameleon_context

                names = ", ".join(
                    sanitize_for_chameleon_context(Path(p).name) for p in unresolved[:5]
                )
                more = f" (+{len(unresolved) - 5} more)" if len(unresolved) > 5 else ""
                # Name the actual failing rules in the reason and the ignore hint,
                # so the model can construct a working escape instead of typing the
                # literal placeholder `<rule>`. Distinct across the named files, in
                # first-seen order.
                distinct_rules = list(
                    dict.fromkeys(
                        r for p in unresolved[:5] for r in (unresolved_rules.get(p) or []) if r
                    )
                )
                hint_rule = distinct_rules[0] if distinct_rules else "<rule>"
                rules_clause = (
                    f" (rule{'s' if len(distinct_rules) > 1 else ''}: {', '.join(distinct_rules)})"
                    if distinct_rules
                    else ""
                )
                # The multi-root caller short-circuits on the FIRST blocking root
                # (armed roots rank first), so exactly one root reaches this
                # increment per Stop even when several workspaces share one
                # repo_data -- the anti-loop cap cannot be double-spent in a
                # single Stop. Always charge the per-workspace budget (both modes),
                # off the reconciled effective count, so one workspace never
                # exhausts a sibling's cap and a single<->multi flip cannot re-arm
                # a spent one.
                state.stop_hook_blocks_by_root[_block_scope] = _block_count + 1
                try:
                    save_state(state, repo_data, session_id or "")
                except Exception:
                    pass
                return {
                    "decision": "block",
                    "reason": (
                        f"chameleon: unresolved convention violations remain in "
                        f"{names}{more}{rules_clause}. Fix them before ending, or add "
                        f"{hh._ignore_hint(unresolved[:5], hint_rule)} on the offending line."
                    ),
                }

            if cfg.mode == "off":
                # off is advisory-only and stays fully silent, even on an
                # unresolved violation.
                return {}
            # shadow / capped enforce: fall through to the advisories below.

        # Cross-file existence BLOCK: a named export the turn removed from an
        # existing module that indexed importers still reference. Runs AFTER the
        # calibrated-lint block above (that gate wins) and only when it did not
        # block. Stop-only, never inline. Each break is re-verified live and
        # HEAD-scoped by _confirmed_crossfile_break_sites (F3 turn-introduced +
        # F2 strict target-sourcing), so a mid-turn fix, a bare-package repoint,
        # or a pre-existing HEAD break never reaches here. Mirrors the unresolved
        # branch: shadow / capped-enforce emit a would_block row (carrying the
        # session id) and fall through to the advisory; enforce hard-blocks.
        # Fail-open: any error leaves the advisory pass untouched.
        if cfg.mode in ("shadow", "enforce") and getattr(cfg, "crossfile_existence_block", False):
            try:
                cf_breaks: list = []
                # for_block bypasses the advisory feature flag: the deny is gated by
                # its own crossfile_existence_block flag, not the advisory nudge's.
                hh._crossfile_existence_advisory_lines(
                    repo_root=repo_root,
                    state=state,
                    cfg=cfg,
                    out_breaks=cf_breaks,
                    for_block=True,
                )
                cf_confirmed: list = []
                for rec in cf_breaks:
                    sites = hh._confirmed_crossfile_break_sites(rec)
                    if sites:
                        cf_confirmed.append((rec, sites))
                if cf_confirmed:
                    cf_count = hh._effective_stop_blocks(state, _block_scope)
                    cf_cap_reached = cf_count >= cfg.stop_block_cap
                    cf_hard = cfg.mode == "enforce" and not cf_cap_reached
                    if cfg.mode == "shadow" or (cf_cap_reached and cfg.mode == "enforce"):
                        try:
                            from chameleon_mcp.metrics import emit_hook_metric

                            for rec, _sites in cf_confirmed:
                                mod_abs = str(Path(rec["ws_root"]) / rec["target_key"])
                                emit_hook_metric(
                                    "stop-backstop",
                                    elapsed_ms=0,
                                    repo_id=repo_id,
                                    advisory_emitted=True,
                                    would_block=True,
                                    rule="removed-export-breaks-importers",
                                    file_rel=hh._repo_rel(repo_root, mod_abs),
                                    session_id=session_id,
                                )
                        except Exception:
                            pass
                    if cf_hard:
                        from chameleon_mcp.sanitization import (
                            sanitize_for_chameleon_context as _cf_s,
                        )

                        parts: list[str] = []
                        for rec, sites in cf_confirmed[:5]:
                            nm = _cf_s(str(rec.get("name")))
                            tgt = _cf_s(str(rec.get("target_key")))
                            shown = ", ".join(
                                _cf_s(f"{s}:{ln}" if ln is not None else s) for s, ln in sites[:5]
                            )
                            parts.append(f"'{nm}' (removed from {tgt}) still imported by {shown}")
                        more = f" (+{len(cf_confirmed) - 5} more)" if len(cf_confirmed) > 5 else ""
                        # Charge the per-workspace anti-loop budget, same as the
                        # unresolved branch, so a persistent break cannot loop.
                        state.stop_hook_blocks_by_root[_block_scope] = cf_count + 1
                        try:
                            save_state(state, repo_data, session_id or "")
                        except Exception:
                            pass
                        hint_files = [
                            str(
                                Path(cf_confirmed[0][0]["ws_root"])
                                / cf_confirmed[0][0]["target_key"]
                            )
                        ]
                        return {
                            "decision": "block",
                            "reason": (
                                "chameleon: you removed exports still imported elsewhere: "
                                + "; ".join(parts)
                                + more
                                + ". Restore the export or update the call sites before "
                                "ending, or add "
                                + hh._ignore_hint(hint_files, "removed-export-breaks-importers")
                                + " in the source you touched."
                            ),
                        }
            except Exception:
                pass

        return _run_advisories()
    except Exception as exc:
        hh._note_if_config_malformed(exc, repo_id, session_id, "stop_relint")
        return {}


def _shown_idiom_slugs(session_doc) -> tuple[str, ...]:
    """Idiom slugs this session has already shown the model (spec section
    10.1's Tier-2/memory-channel dedup must-keep), which the idiom lens
    excludes from its scoped set before deciding to spawn.

    ``session_doc.idioms_shown_slugs`` (``core.session_state.SessionDoc``) is
    the native slug set the per-edit Tier-2 block writes directly -- no
    title->slug translation needed, since the recording site itself resolves
    each rendered idiom's TITLE to its store slug before writing here.
    """
    slugs = {str(s) for s in (getattr(session_doc, "idioms_shown_slugs", None) or ()) if s}
    return tuple(sorted(slugs))


def _run_review_job(
    *,
    repo_root: Path,
    repo_id: str,
    session_id: str | None,
    state,
    cfg,
    repo_data: Path,
    daemon_state: dict | None,
    is_subagent: bool,
) -> tuple[str | None, tuple[str, ...]]:
    """Route, and maybe launch, this turn's detached review job.

    The async-first replacement (spec section 3.1) for the deleted
    correctness-judge route/gate, multi-lens pass, and standalone
    duplication gate: ONE scheduler decision, at most ONE detached job,
    covering whichever lenses (correctness/duplication/idiom) the repo's
    config enables -- ``stop/scheduler.py`` is "the only code allowed to
    spawn a model" (its own module docstring). SubagentStop never schedules;
    the scheduler itself refuses on ``RouteContext.is_subagent``, so this
    function does not special-case it either.

    Returns ``(review_context, delivered_match_keys)``. Under
    ``CHAMELEON_JUDGE_WAIT`` a non-None ``review_context`` is the in-turn
    render (already ``<chameleon-context>``-wrapped); its
    ``delivered_match_keys`` are the findings that render represents but that
    are NOT yet committed delivered -- the caller commits them only if the
    block survives the ranked Stop assembler and no later root blocks (so a
    ceiling-dropped review block never retires a finding the user never saw).
    Otherwise ``(None, ())``: an ordinary async turn emits no in-turn
    findings; they arrive at the next UserPromptSubmit or a later
    dead-session SessionStart. Fails open to ``(None, ())`` at every seam --
    an exception here costs this Stop's review, never the turn.
    """
    from chameleon_mcp import hook_helper as hh

    try:
        from chameleon_mcp.core.session_state import read_session_doc
        from chameleon_mcp.stop.scheduler import JobRequest, RouteContext

        session_doc = read_session_doc(repo_id, session_id or "")
        route_ctx = RouteContext(
            repo_root=repo_root,
            repo_id=repo_id,
            session_id=session_id,
            repo_data=repo_data,
            is_subagent=is_subagent,
            files=tuple(state.files.keys()),
            daemon_state=daemon_state,
        )
        decision = hh._scheduler_route(route_ctx, session_doc, cfg)
        if not decision.spawn:
            return (None, ())

        heartbeat = hh._scheduler_try_acquire_job_slot(repo_id, session_id or "")
        if heartbeat is None:
            # Another job already owns this session's one slot; not this
            # Stop's turn to review. Explicit event per spec section 8
            # ("every skipped stage ... replaced by an explicit check event").
            hh._emit_check_event(repo_id, session_id, "review_job", "skipped", "job_inflight")
            return (None, ())

        request = JobRequest(
            repo_root=repo_root,
            repo_id=repo_id,
            session_id=session_id or "",
            files=decision.files,
            intent_tokens=decision.intent_tokens,
            lens_names=decision.lens_names,
            model=decision.model or "sonnet",
            heartbeat_path=heartbeat,
            shown_idiom_slugs=_shown_idiom_slugs(session_doc),
            intent_excerpts=decision.intent_excerpts,
            scope_lines=decision.scope_lines,
        )
        # "spawned" records the DECISION (route reason, lens set) the instant
        # the slot is claimed -- before the detach attempt, mirroring the
        # pre-phase-3 gate's own ordering ("budget spent + event emitted
        # before the spawn runs," so a crash mid-launch is still on record).
        # It does not assert the job is confirmed running; a detach failure
        # right after this is a SEPARATE, additional disclosure below, never
        # a replacement for it -- a test asserting "a spawn was decided for
        # reason X" must not depend on the detach itself succeeding.
        hh._emit_check_event(
            repo_id,
            session_id,
            "review_job",
            "spawned",
            reason=decision.reason,
            detail={"lenses": list(decision.lens_names), "files": len(decision.files)},
        )

        try:
            launched = hh._scheduler_launch_job(request)
        except Exception:
            # launch_job's OWN cleanup (_cleanup_failed_launch) only runs for
            # the failure modes it anticipates (a request-file write failure,
            # an unsupported platform, or an OSError from subprocess.Popen);
            # anything else escaping it here would otherwise be swallowed by
            # this function's own outer except below with the slot still
            # claimed -- job_inflight set and the spend still charged -- for
            # the rest of the heartbeat-staleness window, wedging this
            # session's one-job-at-a-time budget on a launch that never
            # actually started. Release it here exactly as a normal failed
            # launch would.
            from chameleon_mcp.stop.scheduler import _release_job_slot

            _release_job_slot(repo_id, session_id or "")
            hh._emit_check_event(
                repo_id, session_id, "review_job", "degraded", "platform_unavailable"
            )
            return (None, ())
        if not launched:
            # Platform or spawn failure -- launch_job already rolled back the
            # slot claim (heartbeat unlinked, spend refunded); disclose the
            # failure rather than staying silent (spec section 3.1: "review
            # is skipped with an explicit check event").
            hh._emit_check_event(
                repo_id, session_id, "review_job", "degraded", "platform_unavailable"
            )
            return (None, ())

        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.core.budget import TurnBudget

        budget = TurnBudget.for_hook(
            total_seconds=float(threshold_int("JUDGE_WAIT_STOP_BUDGET_SECONDS")),
            token_ceiling=threshold_int("REVIEW_RENDER_TOKEN_CEILING"),
        )
        return hh._judge_wait_and_render(
            repo_id=repo_id,
            repo_data=repo_data,
            ws_root=repo_root,
            session_id=session_id or "",
            heartbeat_path=heartbeat,
            budget=budget,
        )
    except Exception:
        return (None, ())


def discover_stop_roots(cwd: Path, session_id) -> list[dict]:
    """Every workspace root whose enforcement state was touched this session.

    Closes the coordinator-root dead spot: a session launched at a monorepo
    coordinator (its cwd resolves to a profile-less git root, or edits landed in
    sibling repos) has its per-edit state written under EACH edited file's own
    workspace repo_id, not the cwd's. This globs the session-keyed state files
    across every repo_id dir and regroups their recorded files by each file's
    OWN ``find_repo_root``, so the Stop can gate every touched workspace against
    its own profile instead of the one cwd resolves to.

    Each state file's parent dir NAME is the authoritative repo_id (the dir the
    armed state actually lives in). It is NOT recomputed via
    ``_compute_repo_id(ws_root)`` -- a workspace's live git identity can shift
    between the posttool write and the Stop (a remote added mid-session, a
    transient ``git remote`` failure), which would point the gate at a different,
    empty state dir and silently miss the armed block.

    Returns an ordered list of dicts (armed-bearing roots first, then by
    descending touched-file count, then path -- a deterministic tiebreak so the
    single model-spawn budget and any replay are stable), each:
    ``{"ws_root", "repo_id", "repo_data", "files": set[str], "has_armed": bool}``.
    Fails open to the cwd root alone (or []) on any error.
    """
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.optouts import _safe_session_marker
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.tools import _compute_repo_id

    groups: dict[str, dict] = {}

    def _add(ws_root: Path, repo_id: str, repo_data: Path, *, path=None, armed=False):
        try:
            ws_key = str(ws_root.resolve())
        except OSError:
            ws_key = str(ws_root)
        # Key by (repo_data, ws_root), NOT ws_root alone: if a workspace's git
        # identity shifts mid-session (an origin remote added, a transient git
        # failure changing the _compute_repo_id fallback), the same ws_root has
        # armed state under TWO repo_data dirs. Keying by ws_root alone would
        # collapse them and gate only the first dir's state, silently missing the
        # other's armed block. A (repo_data, ws_root) key gates each contributing
        # state file so every armed entry is re-linted. Normal topologies (one
        # repo_data per ws_root) produce one group either way.
        key = f"{repo_data}\x00{ws_key}"
        g = groups.get(key)
        if g is None:
            g = {
                "ws_root": ws_root,
                "repo_id": repo_id,
                "repo_data": repo_data,
                "files": set(),
                "has_armed": False,
            }
            groups[key] = g
        if path is not None:
            g["files"].add(path)
        if armed:
            g["has_armed"] = True

    marker = _safe_session_marker(session_id)
    # A degenerate empty/None session_id collapses to the shared "unknown" marker.
    # Globbing that bucket would pull in unrelated repos' leftover "unknown" state
    # (state files are never reaped), so restrict discovery to the cwd root only.
    if marker != "unknown":
        from chameleon_mcp.enforcement import load_state

        try:
            # Sorted so the discovered order (and, with the (repo_data, ws_root)
            # keying above, which group a ws_root's files land in) is stable
            # rather than filesystem glob-order dependent.
            state_files = sorted(hh._plugin_data_dir().glob(f"*/.enforcement.{marker}.json"))
        except OSError:
            state_files = []
        for sf in state_files:
            repo_data = sf.parent
            repo_id = repo_data.name
            try:
                st = load_state(repo_data, session_id or "")
            except Exception:
                continue
            for path, fs in st.files.items():
                try:
                    ws = find_repo_root(Path(path))
                except Exception:
                    ws = None
                if ws is None:
                    continue
                _add(
                    ws,
                    repo_id,
                    repo_data,
                    path=path,
                    armed=bool(getattr(fs, "blockable_unresolved", False)),
                )

    # Always include the cwd root if it resolves + carries a profile, so the
    # idiom review and attestation run for the primary repo even with zero armed
    # files (today's behavior). A cwd root already grouped from a state file keeps
    # that state file's authoritative repo_id.
    try:
        cwd_root = find_repo_root(cwd)
    except Exception:
        cwd_root = None
    if cwd_root is not None:
        try:
            cwd_id = _compute_repo_id(cwd_root)
            _add(cwd_root, cwd_id, hh._plugin_data_dir() / cwd_id)
        except Exception:
            pass

    ordered = sorted(
        groups.values(),
        key=lambda g: (0 if g["has_armed"] else 1, -len(g["files"]), str(g["ws_root"])),
    )
    try:
        from chameleon_mcp._thresholds import threshold_int

        cap = threshold_int("STOP_MAX_ROOTS")
    except Exception:
        cap = 16
    if len(ordered) > cap:
        # No silent truncation of ENFORCEMENT: armed roots rank first, so the cap
        # normally drops only advisory-only roots. But a session touching more
        # than `cap` ARMED workspaces would leave the overflow ungated -- record a
        # check event so a green Stop never reads as "every workspace was checked"
        # when it was not. Best-effort; a telemetry failure must not break the Stop.
        dropped = [g for g in ordered[cap:] if g["has_armed"]]
        if dropped:
            try:
                hh._emit_check_event(
                    dropped[0]["repo_id"],
                    session_id,
                    "stop_relint",
                    "skipped",
                    f"multiroot_cap_dropped_{len(dropped)}_armed",
                )
            except Exception:
                pass
    return ordered[:cap]


def gate_one_root(
    *,
    payload: dict,
    root: dict,
    session_id,
    is_subagent: bool,
    daemon_state: dict,
    only_files: set[str] | None,
    allow_model_spawn: bool,
) -> dict:
    """Trust / suppression / stale gates + ``stop_gates`` for one workspace.

    Returns ``{"output", "attest", "gated", "suppressed_reason"}``. ``gated`` is
    False for an untrusted or stale grant -- that root is skipped entirely and,
    matching today's single-root behavior, writes no attestation. A suppressed
    (paused / session-disabled) root skips the gates (output {}) but still
    attests, because the disable window is the scrutiny-relevant fact.
    """
    from chameleon_mcp import hook_helper as hh
    from chameleon_mcp.optouts import is_chameleon_suppressed
    from chameleon_mcp.profile.trust import profile_diverged_from_grant, trust_state_for

    ws_root = root["ws_root"]
    repo_id = root["repo_id"]
    repo_data = root["repo_data"]

    rec = trust_state_for(repo_id)
    # Per-root trust, never unioned: a grant on one workspace (or the coordinator)
    # does not vouch for another workspace's unreviewed profile. grants_root
    # resolves membership correctly even under a monorepo-shared repo_id.
    if rec is None or not rec.grants_root(ws_root):
        return {"output": {}, "attest": False, "gated": False, "suppressed_reason": None}
    if profile_diverged_from_grant(rec, ws_root, hh._enf_profile_dir(ws_root)):
        return {"output": {}, "attest": False, "gated": False, "suppressed_reason": None}

    suppressed_reason = is_chameleon_suppressed(ws_root, repo_id, session_id)
    if suppressed_reason is not None:
        hh._emit_check_event(repo_id, session_id, "stop_relint", "skipped", "suppressed")
        return {
            "output": {},
            "attest": True,
            "gated": True,
            "suppressed_reason": suppressed_reason,
        }

    try:
        output = hh._stop_gates(
            payload=payload,
            repo_root=ws_root,
            repo_id=repo_id,
            session_id=session_id,
            is_subagent=is_subagent,
            repo_data=repo_data,
            daemon_state=daemon_state,
            only_files=only_files,
            allow_model_spawn=allow_model_spawn,
        )
    except Exception:
        output = {}
    return {"output": output, "attest": True, "gated": True, "suppressed_reason": None}


def write_session_attestation(
    *,
    repo_root: Path,
    repo_id: str,
    session_id,
    repo_data: Path,
    suppressed_reason: str | None,
    daemon_state: dict | None = None,
) -> None:
    """Build and persist this session's Stop attestation. Strictly fail-open.

    The attestation is self-signed and raise-only: nothing recorded in it may
    ever lower scrutiny anywhere downstream; it exists only to RAISE gate depth
    and make post-incident replay honest. It must never change the turn
    outcome, so any exception is swallowed here (and again by the caller).
    """
    try:
        from chameleon_mcp import hook_helper as hh

        payload = hh._build_session_attestation(
            repo_root=repo_root,
            repo_id=repo_id,
            session_id=session_id,
            repo_data=repo_data,
            suppressed_reason=suppressed_reason,
            daemon_state=daemon_state,
        )
        from chameleon_mcp.review_ledger import record_session_attestation

        record_session_attestation(repo_id, payload)
    except Exception:
        pass
