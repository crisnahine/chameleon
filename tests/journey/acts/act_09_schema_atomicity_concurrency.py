"""Act 9: Schema + atomicity + concurrency + monorepo (Phases 27, 28, 29, 30, 31, 32)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Schema migration, atomic-txn recovery, concurrent refresh, glob brace expansion,
git merge driver, and monorepo aggregation. Use absolute paths everywhere.

PHASE 27 - schema migration:
  emit checkpoint started phase 27

  STEP 1 - v0.3 schema migration or refusal:
    Use Bash to create a separate copy of the ts_basic fixture directory at
    working/ts_basic_v03_copy/. Plant a .chameleon/profile.json in that directory
    that has schema_version 3 (a v0.3 profile) with minimal valid fields:
      python3 -c "
      import json, pathlib
      d = pathlib.Path('working/ts_basic_v03_copy/.chameleon')
      d.mkdir(parents=True, exist_ok=True)
      profile = {'schema_version': 3, 'language': 'typescript', 'archetypes': []}
      (d / 'profile.json').write_text(json.dumps(profile))
      print('planted v0.3 profile')
      "
    Call chameleon-mcp::detect_repo with path=working/ts_basic_v03_copy.
    Report whether the response is a migration success or a refusal with a clear
    error envelope. Either outcome is acceptable - we are testing that chameleon
    handles it gracefully (no crash, no silent data loss).

  STEP 2 - v99 schema refusal:
    In working/ts_basic_v03_copy/, overwrite .chameleon/profile.json with
    schema_version 99:
      python3 -c "
      import json, pathlib
      d = pathlib.Path('working/ts_basic_v03_copy/.chameleon')
      d.mkdir(parents=True, exist_ok=True)
      profile = {'schema_version': 99, 'language': 'typescript', 'archetypes': []}
      (d / 'profile.json').write_text(json.dumps(profile))
      print('planted v99 profile')
      "
    Call chameleon-mcp::detect_repo with path=working/ts_basic_v03_copy.
    Verify the response contains an error envelope indicating unsupported schema
    version (look for 'unsupported_schema_version' or similar in the error field).

  STEP 3 - v0.5.8 missing clustering_algorithm_version:
    In working/ts_basic_v03_copy/, overwrite .chameleon/profile.json with a
    v0.5.8-era profile that omits clustering_algorithm_version:
      python3 -c "
      import json, pathlib
      d = pathlib.Path('working/ts_basic_v03_copy/.chameleon')
      d.mkdir(parents=True, exist_ok=True)
      profile = {
        'schema_version': 6,
        'language': 'typescript',
        'archetypes': [{'name': 'util', 'cluster_size': 5}]
      }
      (d / 'profile.json').write_text(json.dumps(profile))
      print('planted v0.5.8 profile (no clustering_algorithm_version)')
      "
    Call chameleon-mcp::get_archetype with path=working/ts_basic_v03_copy
    and any archetype name. Report whether pre-v0.5.9 detection works (the
    missing field should be handled gracefully, not crash).

  emit checkpoint completed phase 27

PHASE 28 - atomic-txn recovery:
  emit checkpoint started phase 28

  STEP 1 - dead-PID orphan cleanup:
    Use Bash to plant a partial transaction directory in working/ts_basic:
      python3 -c "
      import pathlib, json
      tmp = pathlib.Path('working/ts_basic/.chameleon/.tmp/abc123')
      tmp.mkdir(parents=True, exist_ok=True)
      # Partial profile (no COMMITTED sentinel)
      (tmp / 'profile.json').write_text(json.dumps({'schema_version': 7, 'partial': True}))
      # Sentinel pidfile with guaranteed-dead PID 99999
      (tmp / '.txn_pid').write_text('99999')
      print('planted orphan txn abc123 with dead PID 99999')
      "
    Call chameleon-mcp::bootstrap_repo with path=working/ts_basic (no force flag).
    Verify the orphan transaction abc123 was cleaned up: after the call, check
    that working/ts_basic/.chameleon/.tmp/abc123 no longer exists.
    Report the bootstrap result (expect already_bootstrapped or ok).

  STEP 2 - alive-PID no cleanup:
    Use Bash to plant a second transaction directory with the CURRENT runner PID:
      python3 -c "
      import os, pathlib, json
      tmp = pathlib.Path('working/ts_basic/.chameleon/.tmp/def456')
      tmp.mkdir(parents=True, exist_ok=True)
      (tmp / 'profile.json').write_text(json.dumps({'schema_version': 7, 'partial': True}))
      # Use current PID (alive) as the sentinel
      (tmp / '.txn_pid').write_text(str(os.getpid()))
      print(f'planted alive-PID txn def456 with PID {os.getpid()}')
      "
    Call chameleon-mcp::bootstrap_repo with path=working/ts_basic (no force flag).
    Verify working/ts_basic/.chameleon/.tmp/def456 STILL EXISTS after the call
    (alive PID means in-progress - should not be cleaned up).
    Clean up manually afterward:
      rm -rf working/ts_basic/.chameleon/.tmp/def456

  emit checkpoint completed phase 28

PHASE 29 - concurrent refresh:
  emit checkpoint started phase 29

  Use Bash to run two parallel refresh_repo calls on working/ts_basic via
  background subshells. Both calls invoke the chameleon_mcp tools module directly:

    python3 -c "
    import sys, os
    sys.path.insert(0, '.')
    # We will run two parallel refresh attempts
    import subprocess
    script = '''
import sys
sys.path.insert(0, '.')
os.environ.setdefault('CHAMELEON_PLUGIN_DATA', os.environ.get('CHAMELEON_PLUGIN_DATA', ''))
from chameleon_mcp import tools
import json
result = tools.refresh_repo({'path': sys.argv[1]})
print(json.dumps(result) if isinstance(result, dict) else str(result))
'''.strip()
    import tempfile, os
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(script)
        fname = f.name
    p1 = subprocess.Popen([sys.executable, fname, 'FIXTURE_PATH'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    p2 = subprocess.Popen([sys.executable, fname, 'FIXTURE_PATH'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    o1, _ = p1.communicate(timeout=60)
    o2, _ = p2.communicate(timeout=60)
    print('OUT1:', o1.decode(errors='replace'))
    print('OUT2:', o2.decode(errors='replace'))
    os.unlink(fname)
    " 2>&1

  Replace FIXTURE_PATH above with the absolute path to working/ts_basic.

  From the outputs, verify that at least one call returns a success envelope
  and at least one returns a failed envelope with an error message containing
  'in progress' or 'PID' (indicating flock contention). Report both outputs.

  emit checkpoint completed phase 29

PHASE 30 - glob brace expansion:
  emit checkpoint started phase 30

  STEP 1 - plant a brace-glob in profile.json:
    Use Bash to update working/ts_basic/.chameleon/profile.json to include a
    paths_glob with brace expansion:
      python3 -c "
      import json, pathlib
      p = pathlib.Path('working/ts_basic/.chameleon/profile.json')
      data = json.loads(p.read_text())
      data['discovery'] = data.get('discovery', {})
      data['discovery']['paths_glob'] = 'src/{components,hooks}/**/*.{ts,tsx}'
      p.write_text(json.dumps(data, indent=2))
      print('updated paths_glob to brace pattern')
      "
    Call chameleon-mcp::refresh_repo with path=working/ts_basic.
    Verify the refresh completed without error. Report whether the response
    mentions glob expansion or the 4 expanded patterns (components/ts,
    components/tsx, hooks/ts, hooks/tsx).

  STEP 2 - 512-pattern cap enforcement:
    Use Bash to construct a pathological glob with >512 brace combinations:
      python3 -c "
      # Generate a glob that expands to more than 512 patterns
      # {a,b,c,...} x {x,y,z,...} where each side has many branches
      parts_a = ','.join(f'dir{i}' for i in range(30))  # 30 dirs
      parts_b = ','.join(f'sub{i}' for i in range(20))  # 20 subdirs
      parts_c = ','.join(['ts', 'tsx', 'js', 'jsx'])   # 4 exts
      # 30 * 20 * 4 = 2400 combinations > 512 cap
      glob_str = 'src/{' + parts_a + '}/{' + parts_b + '}/**/*.{' + parts_c + '}'
      print(glob_str[:200], '...(truncated)')
      print(f'Expected expansions: 30 * 20 * 4 = {30*20*4} (> 512 cap)')
      import json, pathlib
      p = pathlib.Path('working/ts_basic/.chameleon/profile.json')
      data = json.loads(p.read_text())
      data['discovery'] = data.get('discovery', {})
      data['discovery']['paths_glob'] = glob_str
      p.write_text(json.dumps(data, indent=2))
      print('planted pathological glob')
      "
    Call chameleon-mcp::refresh_repo with path=working/ts_basic.
    Verify the call returns an error or warning indicating the 512-pattern cap
    was enforced (no silent truncation or crash). Report the response.

  STEP 3 - restore profile.json discovery to default:
    Use Bash to remove the discovery override from profile.json:
      python3 -c "
      import json, pathlib
      p = pathlib.Path('working/ts_basic/.chameleon/profile.json')
      data = json.loads(p.read_text())
      data.pop('discovery', None)
      p.write_text(json.dumps(data, indent=2))
      print('restored profile.json (removed discovery key)')
      "

  emit checkpoint completed phase 30

PHASE 31 - git merge driver:
  emit checkpoint started phase 31

  Use Bash to create two divergent branches of .chameleon/profile.json in
  working/ts_basic using the loopback origin, then run the merge driver:

  STEP 1 - create divergent branches:
    python3 -c "
    import subprocess, os, json, pathlib

    repo = pathlib.Path('working/ts_basic')
    profile_rel = '.chameleon/profile.json'
    profile_abs = repo / profile_rel

    # Read current profile
    data = json.loads(profile_abs.read_text())

    # Create branch-a: add an archetype entry
    subprocess.run(['git', '-C', str(repo), 'checkout', '-b', 'merge-branch-a'], check=True)
    a_data = dict(data)
    a_data['_branch_a_marker'] = 'branch-a-change'
    profile_abs.write_text(json.dumps(a_data, indent=2))
    subprocess.run(['git', '-C', str(repo), 'add', profile_rel], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'branch-a change'], check=True)
    sha_a = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()

    # Create branch-b from the same base
    subprocess.run(['git', '-C', str(repo), 'checkout', '-b', 'merge-branch-b', 'main'], check=True)
    b_data = dict(data)
    b_data['_branch_b_marker'] = 'branch-b-change'
    profile_abs.write_text(json.dumps(b_data, indent=2))
    subprocess.run(['git', '-C', str(repo), 'add', profile_rel], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'branch-b change'], check=True)
    sha_b = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()

    print(f'branch-a sha: {sha_a[:8]}  branch-b sha: {sha_b[:8]}')
    "

  STEP 2 - run the merge driver:
    Use Bash to run the chameleon merge driver on the two divergent profile files.
    The merge driver lives at scripts/chameleon-merge-driver.sh. Call it directly
    or call chameleon-mcp::merge_profiles with both branch versions as input:

    python3 -c "
    import subprocess, os, json, pathlib, tempfile

    repo = pathlib.Path('working/ts_basic')
    chameleon_root = pathlib.Path('.')

    # Get the two divergent profile.json contents
    branch_a_content = subprocess.check_output(
        ['git', '-C', str(repo), 'show', 'merge-branch-a:.chameleon/profile.json']
    ).decode()
    branch_b_content = subprocess.check_output(
        ['git', '-C', str(repo), 'show', 'merge-branch-b:.chameleon/profile.json']
    ).decode()
    base_content = subprocess.check_output(
        ['git', '-C', str(repo), 'show', 'main:.chameleon/profile.json']
    ).decode()

    print('base:', json.loads(base_content).get('schema_version'))
    print('branch-a has _branch_a_marker:', '_branch_a_marker' in branch_a_content)
    print('branch-b has _branch_b_marker:', '_branch_b_marker' in branch_b_content)
    print('Ready for merge_profiles call')
    "

    Call chameleon-mcp::merge_profiles with the two branch versions.
    Verify the merge result is a clean union containing both markers
    (_branch_a_marker and _branch_b_marker) with no conflict markers (<<<<,
    ====, >>>>).

  STEP 3 - restore working/ts_basic to main:
    Use Bash to check out main and clean up the branches:
      git -C working/ts_basic checkout main
      git -C working/ts_basic branch -D merge-branch-a merge-branch-b

  emit checkpoint completed phase 31

PHASE 32 - monorepo aggregation:
  emit checkpoint started phase 32

  STEP 1 - init the monorepo:
    Switch to working/ts_monorepo. If .chameleon/profile.json already exists
    (from Act 2), call chameleon-mcp::refresh_repo to update it. Otherwise
    run /chameleon-init to bootstrap.
    After init/refresh, verify:
      - working/ts_monorepo/.chameleon/profile.json exists
      - working/ts_monorepo/packages/api/.chameleon/profile.json exists (or if
        per-workspace bootstrap didn't run, note that)
      - working/ts_monorepo/packages/web/.chameleon/profile.json exists (or note)

  STEP 2 - verify (repo_id, repo_root) PK:
    Use Bash to inspect the index.db (if accessible):
      python3 -c "
      import os, sqlite3, pathlib
      data_dir = os.environ.get('CHAMELEON_PLUGIN_DATA', '')
      if not data_dir:
          print('CHAMELEON_PLUGIN_DATA not set')
      else:
          dbs = list(pathlib.Path(data_dir).rglob('index.db'))
          for db in dbs:
              print(f'Found index.db: {db}')
              try:
                  conn = sqlite3.connect(str(db))
                  tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
                  print('Tables:', [t[0] for t in tables])
                  conn.close()
              except Exception as e:
                  print(f'Error: {e}')
      "
    Report any index.db entries found with their (repo_id, repo_root) pairs.

  STEP 3 - verify workspaces field and aggregation formula:
    Read working/ts_monorepo/.chameleon/profile.json and check for a
    'workspaces' field. If present, verify the per-archetype cluster_size_total
    in the root profile matches the sum of cluster_size values across the
    matching workspace archetypes. Report the workspaces structure found.
    If the 'workspaces' field is absent, report that monorepo aggregation
    did not produce workspace metadata (acceptable for small fixtures that
    may not trigger per-workspace bootstrap in this version).

  emit checkpoint completed phase 32

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_09.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=50,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_canonical_excerpt",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__merge_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
            "mcp__plugin_chameleon_chameleon-mcp__trust_profile",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=1200,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[27, 28, 29, 30, 31, 32]
    )

    notes_extra: dict[int, str] = {}

    # Phase 27: transcript mentions schema_version migration or refusal
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        schema_signals = [
            r"schema_version",
            r"unsupported_schema",
            r"migration",
            r"v0\.3",
            r"v99",
            r"clustering_algorithm",
        ]
        found_signal = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in schema_signals
        )
        if not found_signal:
            notes_extra[27] = (
                "no schema migration/refusal signal found in transcript; "
                "schema tests may not have been exercised"
            )
    except Exception as exc:
        notes_extra[27] = f"transcript scan error for phase 27: {exc}"

    # Phase 28: orphan txn abc123 should be GONE after bootstrap
    tmp_abc123 = cwd / ".chameleon" / ".tmp" / "abc123"
    if tmp_abc123.exists():
        notes_extra[28] = (
            f"orphan txn dir {tmp_abc123} still exists after bootstrap_repo; "
            "dead-PID cleanup may not have fired"
        )

    # Phase 29: transcript shows failed.*PID envelope from one of the parallel calls
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        concurrent_signals = [
            r"in progress",
            r"PID\s+\d+",
            r"already.*running",
            r"flock",
            r"retry",
            r"status.*failed",
        ]
        found_concurrent = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in concurrent_signals
        )
        if not found_concurrent:
            notes_extra[29] = (
                "no flock/concurrent-refresh signal found in transcript; "
                "concurrent refresh contention may not have been exercised"
            )
    except Exception as exc:
        notes_extra[29] = f"transcript scan error for phase 29: {exc}"

    # Phase 30: glob expansion mentioned (4 patterns from {components,hooks} x {ts,tsx})
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        glob_signals = [
            r"components.*\.ts",
            r"hooks.*\.ts",
            r"brace",
            r"512",
            r"expansion",
            r"paths_glob",
        ]
        found_glob = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in glob_signals
        )
        if not found_glob:
            notes_extra[30] = (
                "no glob expansion signal found in transcript; "
                "brace expansion tests may not have been exercised"
            )
    except Exception as exc:
        notes_extra[30] = f"transcript scan error for phase 30: {exc}"

    # Phase 31: transcript shows 3-way merge success (no conflict markers)
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        merge_success_signals = [
            r"merge",
            r"branch.a",
            r"branch.b",
            r"union",
            r"_branch_[ab]_marker",
        ]
        found_merge = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in merge_success_signals
        )
        if not found_merge:
            notes_extra[31] = (
                "no merge driver signal found in transcript; "
                "3-way merge test may not have been exercised"
            )
        # Check that conflict markers did NOT appear in a merge result context
        conflict_markers = ["<<<<<<<", "=======", ">>>>>>>"]
        # Only flag if they appear near merge-related text
        for marker in conflict_markers:
            if marker in transcript_text:
                existing = notes_extra.get(31, "")
                notes_extra[31] = (
                    (existing + "; " if existing else "") +
                    f"conflict marker {marker!r} found in transcript; "
                    "3-way merge may have produced unresolved conflicts"
                ).strip("; ")
                break
    except Exception as exc:
        notes_extra[31] = f"transcript scan error for phase 31: {exc}"

    # Phase 32: working/ts_monorepo/.chameleon/profile.json exists with workspaces field
    ts_monorepo_profile = ctx.fixture("ts_monorepo") / ".chameleon" / "profile.json"
    try:
        expect.path_exists(32, ts_monorepo_profile)
    except expect.PhaseAssertionError as e:
        notes_extra[32] = str(e)

    # Apply cross-check findings to outcomes
    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")

    return ActResult(
        act_id="09_schema_atomicity_concurrency",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
