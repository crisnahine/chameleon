# Dogfood report: forem

- App path: `/Users/crisn/Documents/Projects/Testing Apps/forem`
- Total wall time: 8.7s
- PASS: 43
- FAIL: 0
- FINDING: 1
- NOTE: 8

## Phase 0 — Pre-flight survey
_Detect app shape before bootstrap._
Duration: 0.2s

- `[.]` **shape=rails-with-frontend** — file_count_approx=7835
- `[.]` **has_gemfile=True**
- `[.]` **has_pkgjson=True**
- `[.]` **has_monorepo=False**

## Phase 1 — Bootstrap from scratch
_Remove any .chameleon/, bootstrap_repo, inspect outputs._
Duration: 0.0s

- `[.]` **pre-existing .chameleon/ removed**
- `[+]` **bootstrap_repo status=success** — in 4.0s, archetypes=127, files=3514
- `[+]` **profile.json present**
- `[+]` **profile.json schema_version == 7 (v0.5.2)**
- `[+]` **language_hint emitted (hybrid repo detected)** — primary=ruby, secondary=None
- `[+]` **naming quality: 0/127 are cluster-<hash>**
- `[+]` **paths_pattern_display present on 127 archetypes (v0.5.2 Rails fix)**

## Phase 2 — Trust flow
_detect_repo -> trust_profile -> detect_repo._
Duration: 0.0s

- `[+]` **initial trust_state='untrusted' (pre-trust)**
- `[+]` **trust_profile granted**
- `[+]` **post-trust detect_repo trust_state=trusted**

## Phase 3 — v0.5.2 tools.py fixes
_7 bugs: repo unify, slug, list, drift, excerpt, $HOME, suspicious._
Duration: 0.1s

- `[!]` **Bug 1: pause_session(repo_id) accepts hex digest** — {"status": "success", "expires_at": "2026-05-11T12:25:37Z", "minutes": 1}
- `[+]` **Bug 2: 5 same-second teaches produced 5 unique slugs** — ['idiom-2026-05-11-1778502277-7dbb', 'idiom-2026-05-11-1778502277-98e4'] ...
- `[+]` **Bug 3: list_profiles carries repo_root + archetype_count**
- `[+]` **Bug 4: get_drift_status(path) resolves to repo_id hex** — repo_id=6724e47f92b1..., keys=['days_since_refresh', 'observed_drift_score', 'recommended_action', 'repo_id']
- `[+]` **Bug 5: get_canonical_excerpt returns typed error envelope**
- `[+]` **Bug 6: detect_repo traversal returns no_repo**
- `[+]` **Bug 7: teach_profile flags prompt-injection feedback**

## Phase 4 — Clustering + signatures
_extension bucket, monorepo bucket, content_signal, adaptive threshold._
Duration: 0.0s

- `[+]` **Bug 4-1: extension-aware bucket separates .tsx/.ts** — src/components:tsx vs src/components:ts
- `[+]` **Bug 4-2: monorepo bucket preserves workspace name** — packages/excalidraw/components vs packages/element/components
- `[+]` **Bug 4-3: content_signal_match_for detects use_client**
- `[+]` **Bug 4-3: content_signal_match_for=none on plain JS**
- `[+]` **Bug 4-3: content_signal_match_for detects shebang**
- `[.]` **Bug 4-4: adaptive threshold verified at unit level (52/52 in v0_5_2_clustering)**

## Phase 5 — Bootstrap fixes
_Sibling preservation, Rails priors, paths_pattern_display, db/schema.rb exclusion._
Duration: 3.5s

- `[+]` **Bug 5-1: .skip + team-notes.md survived atomic_profile_commit**
- `[.]` **Bug 5-2/5-3 verified in Phase 1 archetype inspection**
- `[.]` **Bug 5-4: db/schema.rb present in repo (excluded at discovery — unit-tested)**

## Phase 6 — Lint engine + idiom scoping
_GitHub PAT string-concat fold, idiom language scoping._
Duration: 0.0s

- `[+]` **Bug 6-1: scan_secrets flagged direct.ts**
- `[+]` **Bug 6-1: scan_secrets flagged concat.ts**
- `[+]` **Bug 6-1: scan_secrets flagged aws_concat.ts**
- `[+]` **Bug 6-1: scan_secrets clean on clean.ts**
- `[+]` **Bug 6-2: idiom filter keeps ruby + any, drops typescript**
- `[+]` **Bug 6-2: idiom filter keeps typescript + any, drops ruby**
- `[+]` **Bug 6-2: language_for_path('.rb') == 'ruby'**
- `[+]` **Bug 6-2: language_for_path('.ts') == 'typescript'**
- `[+]` **Bug 6-2: language_for_path('.md') == 'unknown'**

## Phase 7 — Each MCP tool end-to-end
_Exercise all 19 tools individually._
Duration: 0.5s

- `[+]` **get_pattern_context returned** — top_keys=['api_version', 'data'], data_keys=['archetype', 'canonical_excerpt', 'idioms', 'meta', 'repo', 'rules'], file=cover_image_uploader.rb
- `[+]` **get_archetype(class) returned** — content_signal=None
- `[+]` **get_canonical_excerpt(class) returned content** — 236 bytes
- `[+]` **get_rules(class) returned** — ['rules']
- `[+]` **lint_file returned** — violations=2
- `[+]` **propose_archetype_renames returned** — 0 proposals
- `[+]` **refresh_repo status='noop'** — strategy=None
- `[+]` **disable_session status='success'**

## Phase 8 — Real edit + drift recording
_Simulate 3 edits via preflight hook, check drift.db._
Duration: 0.3s

- `[+]` **preflight hook edit #1 returned 0** — stdout=3 bytes
- `[+]` **preflight hook edit #2 returned 0** — stdout=3 bytes
- `[+]` **preflight hook edit #3 returned 0** — stdout=3 bytes

## Phase 9 — Refresh (partial + full)
_refresh_repo with edits queued._
Duration: 0.2s

- `[+]` **refresh_repo status='noop'** — {"status": "noop", "reason": "no files changed since last refresh", "archetypes_detected": 127, "files_processed": 3514, "duration_ms": 0, "profile_path": "/Users/crisn/Documents/Projects/Testing Apps

## Phase 10 — Cleanup verification
_Check final state of .chameleon/._
Duration: 0.0s

- `[+]` **.chameleon/ exists with 10 files** — .idioms.lock, .skip, COMMITTED, archetypes.json, canonicals.json, idioms.md, profile.json, profile.summary.md, rules.json, team-notes.md
