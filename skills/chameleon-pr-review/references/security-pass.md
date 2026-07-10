# pr-review reference — Step 2.6: Security pass

### Step 2.6: Security pass (always, every changed source file)

Run this on every changed source file regardless of whether a Jira ticket was supplied. The convention review above checks shape; this pass checks for the security shapes a human reviewer watches for. It has three parts with three different confidence levels, and they are NOT equal. Keep them separate in the output and never collapse the weaker two into the secret part's confidence.

#### 2.6a. Secret escalation (BLOCK, deterministic kinds inside the diff only)

Read the `secret-detected-in-content` violations from each file's Step 2b `lint_file` response. These come from a secret scan that runs before the archetype match and before the trust gate, so they are present for every file you linted, trusted or not.

Two gates decide what a secret violation may do, and both are mandatory:

1. **Kind gate.** Escalate to **BLOCK** only violations whose `secret_hard` field is true. The `secret_hard` boolean is the authority — read it off the violation, do not re-derive it from a prefix list (the recognized-kind set grows, and a hand-list drifts out of date). It marks the deterministic, fixed-shape credential kinds the engine recognizes: AWS `AKIA`, GitHub `ghp_`, GitLab `glpat-`/`gldt-`/`glft-`/`glsoat-`/`glrt-`, Anthropic `sk-ant-`, Stripe `sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`, Slack `xox[baprs]-`, Google `AIza`, Azure `AccountKey=`, and PEM private keys — but trust the flag, not this parenthetical. Violations without `secret_hard` (40-char base64 runs, high-entropy hex, password assignments, JWT-shaped strings, entropy hits) match ordinary identifiers, git SHAs, and data blobs in real code at a rate that makes them verdict-poison; report them at most as a **NIT** labeled "low-precision secret heuristic, verify by eye", never as FIX or BLOCK, and never let them influence the verdict.
2. **Hunk gate.** `lint_file` scans the FULL file content, not the diff, so a hit is NOT in the change by construction. The reported line is the `at line N` token in the violation's `actual`/`message` string (e.g. `aws_access_key at line 40` -> line 40); every hard-kind secret carries one. A hard-kind secret whose reported line falls inside an added/changed hunk of this diff is a **BLOCK**. A hard-kind secret on a line the diff did not touch is pre-existing: report it in a separate "Pre-existing repo hygiene" note at the end of the review (it deserves rotation, but this PR did not introduce it), and do not let it affect the verdict.

For each secret BLOCK, cite the file and line, carry the violation's own message (it names the kind and tells the author to rotate it), and label it "verify this is not a live credential; if it is a test fixture, it is safe to keep" - a fixture key is overridden by the author, not silently dropped by this review.

#### 2.6b. Ruby controller authorization (advisory FIX, presence-only)

For Ruby controllers ONLY, compare the authorization-callback presence of the changed file against its canonical witness. The only signal the profile carries here is presence or absence of `before_action`-style callbacks; it does NOT map a callback to the action methods it guards, so this check cannot tell whether a specific new action is actually covered.

Raise a **FIX** (never BLOCK) when the canonical witness for this controller archetype declares `before_action` (or `prepend_before_action`) authorization callbacks and the changed controller declares none, AND the change adds a new public action method. Label it exactly as a heuristic: "presence-only check, cannot confirm the new action is covered. The witness controller declares before_action callbacks; this changed controller declares none. Authorization may still be inherited from a base controller." Do not claim a structured divergence; do not name which action is unguarded; do not cite a "witness authz divergence" as if the profile mapped callbacks to actions. It does not.

When the file's archetype carries a `required_guards` entry in `.chameleon/conventions.json` (`conventions.required_guards[<archetype>]`), cite the specific expected guard symbol rather than the generic "declares before_action callbacks" phrasing: name the guard (`before_action :authorize!`), the `sample_size` (how many of the archetype's controllers were measured) and the fixed 60% derivation floor the guard cleared, and the archetype. The entry carries `{required_guards, known_guards, sample_size}` and NO per-archetype frequency field, so cite the floor and the sample count, not an invented measured percentage. Check the archetype's `known_guards` list first; a guard the changed controller uses that is listed there is a legitimate variant and not a miss. The honesty label is unchanged: it is still presence-only and still "cannot confirm the new action is covered". The `required_guards` data names the expected guard; it does not map that guard to the action it covers, so it never reaches BLOCK and never claims a structured callback-to-action divergence.

Skip this check entirely for TypeScript and any non-Ruby file. There is no route/middleware/controller extraction for those languages, so there is no presence signal to compare and nothing honest to say.

#### 2.6c. Tainted input, SSRF, path traversal (advisory FIX, single-hunk scope)

Read each file's added (`+`) lines from the hunk map (Step 1a). Within a single file's hunk, look for these flows where request-controlled input reaches a dangerous sink:

- **Taint to sink**: a value read from request data (params, query string, request body, headers, an inbound argument) flows on an added line into `eval`/`constantize`/`send`/`system`/backticks/`%x`/a raw SQL string (Ruby), or `eval`/`Function`/a shell exec/a raw query (TS), with no sanitization between source and sink inside the hunk.
- **SSRF**: an added outbound HTTP call (`Net::HTTP`, `Faraday`, `HTTParty`, `open-uri`, `fetch`, `axios`, `http.get`) whose URL is built from request data rather than a constant or an allow-listed host.
- **Path traversal**: an added filesystem read/write (`File.read`/`File.open`/`Dir`/`fs.readFile`/`fs.createReadStream`/`require`) whose path is built from request data without a basename/allow-list check inside the hunk.

These are judgment calls, not witnessed facts. Cap every one at **FIX** (never BLOCK) and label each: "advisory, single-hunk scope; may miss a flow whose source and sink are in different files, and may be a false positive if the value was sanitized outside this hunk."

The cited tainted line MUST be inside the diff. If the source or the sink is not on an added/changed line in the hunk map, do not raise the finding: a flow you cannot point at inside the change is exactly the cross-file case this single-hunk pass cannot see, and reporting it would be a guess. These findings go through the Step 4 hunk gate like every other per-line finding.

Never let any 2.6b or 2.6c finding reach BLOCK, and do not claim they honor the integrity/calibration guarantee the same way a lint violation or a removed-guard hunk finding does. They are judgments; the secret finding (2.6a) and the deterministic lint sinks (2.6d below) are the witnessed facts in this pass.

#### 2.6d. Deterministic lint-sink and quality findings (witnessed facts)

Step 2b's `lint_file` already returns more than secrets and style. Beyond `secret-detected-in-content` (Step 2.6a), the `violations` list carries deterministic security-sink and test-quality rules the convention loop never routes — so route them here. Each violation is `{rule, severity, message, expected, actual}`; there is NO integer line field. The line, WHEN PRESENT, is the ` at line N` token inside `actual` (parse it with the same `at line N` rule the secret hunk gate uses in Step 2.6a). A violation also carries `ignored: true` when an inline `chameleon-ignore` directive already covers it — skip those.

These are WITNESSED facts (the engine's own deterministic rules), so they are refuter-EXEMPT (Step 4b): verify each inline by re-confirming the returned violation, never send it to `refute_finding`. Where this pass and the hand-rolled taint pass (Step 2.6c) fire on the SAME line, the deterministic hit here WINS — drop the 2.6c judgment for that line.

Two groups, with different scope and severity:

**Security sinks — pre-trust, every linted file, line-anchored → hunk-gated.** These fire before the trust gate and before the archetype match, so they are present for every file you linted (trusted or not), and each carries ` at line N` in `actual`. Run each through the Step 4a hunk gate; an out-of-hunk hit goes to the "Pre-existing repo hygiene" note like an out-of-hunk secret, never the verdict.
- `eval-call` (only the `severity: error` forms) → **BLOCK**. A code-execution sink introduced on an added line is the same tier as a secret: a witnessed structural fact in the diff. Cite the file, the parsed line, the rule, and carry the violation `message`. The error-severity `eval-call` forms are TS/Python `eval(`, Python `exec(`, and the Ruby paren-less `eval`/`send(:eval)`. RESPECT the returned `severity`: the engine DELIBERATELY emits `eval-call` at `severity: warning` for the Ruby string-argument `class_eval`/`instance_eval`/`module_eval` metaprogramming forms (an established Rails idiom it refuses to hard-block), so route a `warning`-severity `eval-call` at **FIX**, never BLOCK — do not escalate by rule name alone. `command-injection` is NOT block-eligible: the engine emits it at `severity: warning` only and keeps it out of `BLOCK_ELIGIBLE_RULES` (it is a `#{…}`-in-a-shell-string heuristic, not taint analysis, so a constant interpolation like `system "echo #{VERSION}"` would false-BLOCK), so it caps at **FIX** below — the same tier the receiving skill gives it. Route by the returned `severity`, not the rule name: `eval-call` at `error` is the ONLY deterministic sink that blocks.
- `command-injection`, `sql-string-interpolation` (Ruby only), `insecure-deserialization`, `weak-hash`, `insecure-random`, and any `warning`-severity `eval-call` → **FIX**. A witnessed dangerous pattern on an added line; cite the rule and parsed line.

**Test-quality / discipline — trusted path, whole-file, no line → NIT.** These run only when the profile is trusted (the test-discipline rules additionally require a `test`/`spec` archetype; `then-without-catch` fires for any TypeScript file). They carry NO line — they are whole-file advisories — so they are NOT hunk-gated; report each at **NIT** anchored to the file (they only ever run on a changed file you are already reviewing).
- `then-without-catch` (a `.then` with no `.catch`, i.e. an unhandled promise rejection), `unfrozen-clock`, `unstubbed-network`, `skipped-test`, `tautological-assertion`, `assertion-free-test`, `real-sleep-in-test`, `random-in-test` → **NIT**, citing the rule.

A clean file emits none of these; only route what `lint_file` actually returned, never a sink you reasoned about yourself (that is Step 2.6c's job, capped at FIX). Render them in the Security / quality findings output section.
