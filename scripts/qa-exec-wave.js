export const meta = {
  name: 'qa-exec-wave',
  description: 'Execute one wave (~500 cells) of the full chameleon matrix: drive each pending item per column with real invocations',
  phases: [{ title: 'Execute', detail: 'one agent per column drives its item batch' }],
}

// Absolute base paths come in via args at invocation time (workflow scripts have
// no process.env and no ~ expansion), so nothing personal is committed here. The
// placeholder default is only a shape hint; a real run must pass args.home.
const HOME = (args && args.home) || '/Users/you'
const WS = (args && args.ws) || HOME + '/Documents/Projects/chameleon-fullmatrix-qa'
const DEV = (args && args.dev) || HOME + '/Documents/Projects/chameleon'

const COLS = {
  C1: { repo: 'ts-plain', lang: 'typescript', fw: 'none', ext: 'ts', cmt: '//', dump: 'ts_dump.mjs' },
  C2: { repo: 'ts-nextjs', lang: 'typescript', fw: 'nextjs', ext: 'tsx', cmt: '//', dump: 'ts_dump.mjs' },
  C3: { repo: 'ts-nestjs', lang: 'typescript', fw: 'nestjs', ext: 'ts', cmt: '//', dump: 'ts_dump.mjs' },
  C4: { repo: 'rb-plain', lang: 'ruby', fw: 'none', ext: 'rb', cmt: '#', dump: 'prism_dump.rb' },
  C5: { repo: 'rb-rails', lang: 'ruby', fw: 'rails', ext: 'rb', cmt: '#', dump: 'prism_dump.rb' },
  C6: { repo: 'py-plain', lang: 'python', fw: 'none', ext: 'py', cmt: '#', dump: 'libcst_dump.py' },
  C7: { repo: 'py-django', lang: 'python', fw: 'django', ext: 'py', cmt: '#', dump: 'libcst_dump.py' },
  C8: { repo: 'py-drf', lang: 'python', fw: 'django+drf', ext: 'py', cmt: '#', dump: 'libcst_dump.py' },
  C9: { repo: 'py-flask', lang: 'python', fw: 'flask', ext: 'py', cmt: '#', dump: 'libcst_dump.py' },
  C10: { repo: 'py-fastapi', lang: 'python', fw: 'fastapi', ext: 'py', cmt: '#', dump: 'libcst_dump.py' },
}

const SCHEMA = {
  type: 'object',
  properties: {
    column: { type: 'string' },
    cells: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          item_id: { type: 'string' },
          status: { type: 'string', enum: ['PASS', 'FAIL', 'NA-ASSERTED', 'BLOCKED'] },
          evidence: { type: 'string', description: 'the real command/invocation run and its actual output, quoted' },
          correctness: { type: 'string' },
          effectiveness: { type: 'string' },
        },
        required: ['item_id', 'status', 'evidence'],
      },
    },
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: { title: { type: 'string' }, severity: { type: 'string' }, red_evidence: { type: 'string' } },
        required: ['title', 'severity'],
      },
    },
  },
  required: ['column', 'cells'],
}

const WAVE_SIZE = (args && args.waveSize) || 35
const OFFSET = (args && args.offset) || 0
const LIVE_VER = (args && args.liveVersion) || '4.4.43'
const LIVE_DIR = HOME + '/.claude/plugins/cache/chameleon/chameleon/' + LIVE_VER

phase('Execute')

const results = await parallel(Object.keys(COLS).map(col => () => {
  const c = COLS[col]
  return agent(
    [
      `Full-matrix execution for the chameleon plugin, COLUMN ${col}: repo ${WS}/${c.repo}`,
      `(${c.lang} / ${c.fw}), plugin v${LIVE_VER} at ${LIVE_DIR}, already bootstrapped + TRUSTED.`,
      `MANDATORY isolation: export CHAMELEON_PLUGIN_DATA=${WS}/.chamdata/${col} on every call.`,
      `MCP: cd ${DEV}/plugin/mcp && .venv/bin/python -c "..." with sys.path.insert(0,"${LIVE_DIR}/mcp") FIRST.`,
      `Hooks: pipe real JSON to ${LIVE_DIR}/hooks/<name> with CLAUDE_PLUGIN_ROOT=${LIVE_DIR}.`,
      `Dump script for this language: ${LIVE_DIR}/scripts/${c.dump}. Inline comment syntax: ${c.cmt}`,
      '',
      `SELECT YOUR BATCH: read ${DEV}/tests/matrix/cells.jsonl, filter to rows where`,
      `column=="${col}" AND status is PENDING **or FAIL** (in file order), SKIP the first`,
      `${OFFSET}, then take the next ${WAVE_SIZE}. Those item_ids are your work list.`,
      `FAIL rows are re-driven deliberately: a fix has usually shipped since that row failed, so`,
      `it must be re-verified against the CURRENT plugin (v${LIVE_VER}) rather than left stale.`,
      `Sort FAIL rows FIRST so re-verification is never starved by the pending backlog. Run e.g.:`,
      `  python3 -c "import json;`,
      `  rows=[r for r in (json.loads(l) for l in open('${DEV}/tests/matrix/cells.jsonl'))`,
      `   if r['column']=='${col}' and r['status'] in ('PENDING','FAIL')];`,
      `  rows.sort(key=lambda r: 0 if r['status']=='FAIL' else 1);`,
      `  print('\\n'.join(r['item_id'] for r in rows[${OFFSET}:${OFFSET + WAVE_SIZE}]))"`,
      `A re-driven FAIL that now works is a PASS with fresh evidence; one that still fails stays`,
      `FAIL with UPDATED evidence naming what is still wrong on v${LIVE_VER}.`,
      '',
      `Drive EACH selected item with a REAL invocation appropriate to its surface, observe the`,
      `ACTUAL output, and return a verdict per item. Real-usage execution -- no mocks, no unit`,
      `tests, no simulation.`,
      '',
      `For each item_id, look up its full record (name, surface, evidence anchor, how_to_invoke) by`,
      `grepping ${DEV}/tests/matrix/inventory.jsonl for that exact item_id -- the record tells you`,
      `what the item is and where its behavior lives in the source. Then drive it.`,
      '',
      'HOW to drive each surface:',
      '- hooks/*: pipe a realistic JSON payload to the named hook executable and read its output.',
      '  For registration/wrapper/resolver items, verify the documented behavior of that hook path.',
      '- mcp-tools/*: call the tool function (or dispatcher action) with real args on this repo.',
      '- bootstrap/*: inspect the artifact/field the step produces under .chameleon/ (already',
      '  bootstrapped); for a derivation step, confirm its output field is present and correct.',
      '- enforcement/*: for a rule, craft a real violating snippet in this language and drive',
      '  posttool-verify / preflight to see if it fires (or, for a language-exclusive rule that does',
      '  NOT apply to ' + c.lang + ', assert it correctly does NOT fire -> NA-ASSERTED with evidence).',
      '- aux/*: run the statusline / dump script / daemon-status / telemetry action for real.',
      '- framework-layers/*: verify the framework-specific behavior in THIS framework (' + c.fw + ');',
      '  if the item is specific to a DIFFERENT framework, assert it is inert here -> NA-ASSERTED.',
      '',
      'VERDICT RULES:',
      '- PASS: driven for real, output correct AND effective. Quote the real output in evidence.',
      '- FAIL: driven, output wrong/incomplete/weak. Quote the failing evidence. Add a gap.',
      '- NA-ASSERTED: the item legitimately does not apply to this language/framework AND you',
      '  verified it correctly does NOT fire/apply (e.g. jsx-mismatch on Ruby, a Rails item on a',
      '  Python repo). Evidence must show the non-application, not just "skipped".',
      '- BLOCKED: genuinely cannot run on this host (e.g. a Windows-only run-hook.cmd path on macOS).',
      '  State exactly why. Do NOT mark BLOCKED to avoid work -- only for real host limits.',
      '',
      'EVERY cell needs real evidence (a quoted command + output). "Ran without error" is not a PASS',
      '-- judge correctness and effectiveness. Return one cell per selected item_id (all ' + WAVE_SIZE + ').',
      'KEEP EVIDENCE CONCISE: quote at most ~200 chars of the actual output per item (the key line',
      'that proves the verdict), not the whole dump -- long evidence bloats your context and stalls',
      'the run. Correctness/effectiveness: one short sentence each.',
      '',
      'CRITICAL RESILIENCE -- NEVER HALT MID-BATCH:',
      '- If ANY tool use is rejected/denied ("the user doesn\'t want to proceed with this tool use"),',
      '  do NOT stop and do NOT abandon the batch. That command hit a headless-mode permission gate,',
      '  not a real failure. Rephrase it once (a simpler/one-line form often passes); if it is still',
      '  denied, mark THAT ONE item BLOCKED (evidence: "command permission-gated in headless workflow")',
      '  and move on to the next item.',
      '- Avoid destructive shell (rm -rf, and rm -f on shared plugin-data paths) -- those are the',
      '  commands the gate denies. To test a fresh-state scenario (cleared cooldown / drift marker /',
      '  degraded interpreter), point CHAMELEON_PLUGIN_DATA at a NEW empty subdir for that one probe',
      '  instead of deleting marker files in the shared dir.',
      '- You MUST ALWAYS end by calling StructuredOutput with a verdict for ALL ' + WAVE_SIZE + ' selected',
      '  items. Returning nothing loses the entire batch. A batch of 30 PASS + 5 BLOCKED is a success;',
      '  halting after 15 with no StructuredOutput is a total loss.',
      '',
      'Leave the repo git-clean. Do NOT use absolute developer home paths in your evidence text',
      '(use ~ or a relative form) -- they break a CI guard.',
    ].join('\n'),
    { label: 'exec:' + col, phase: 'Execute', schema: SCHEMA }
  )
}))

const clean = results.filter(Boolean)
log('exec-wave: ' + clean.reduce((n, r) => n + (r.cells || []).length, 0) + ' cells, ' +
    clean.reduce((n, r) => n + (r.gaps || []).length, 0) + ' gaps')
return { results: clean }
