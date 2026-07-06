# Language & Framework Support Matrix

> The authoritative parity reference for chameleon's supported languages. The
> unit of support is the LANGUAGE: chameleon is framework-agnostic by default,
> learning each repo's own conventions from its structure (clustering, naming,
> signatures), so it works on any framework in a supported language. Off that one
> committed profile chameleon does both conformance (the per-edit convention
> guidance this matrix measures) and comprehension: `search_codebase`,
> `describe_codebase`, and `get_callees` (`comprehension.py`) answer 'where is X',
> 'what is this codebase', and 'what does this call' from the same derived
> artifacts. Where a
> framework's conventions are strong and well-known, a framework-aware layer
> sits ON TOP of that agnostic base for deeper guidance - currently Rails for
> Ruby, Django / DRF / Flask / FastAPI for Python, and Next.js / NestJS for
> TypeScript / JavaScript. (TypeScript / JavaScript now adds Next.js + NestJS
> framework awareness, with detection, naming roles, and framework-specific
> anti-hallucination, on top of its structural base.) **The goal: every
> supported language gets the same capability, with the same purpose, except
> where a capability is genuinely specific to a language or framework.** This doc
> is the basis for closing the gap.

Supported languages (the agnostic core works on any framework; the named
frameworks add a deeper, framework-aware layer on top):

- **TypeScript / JavaScript** - `.ts .tsx .js .jsx .mjs .cjs`, parsed with the TypeScript Compiler API (`ts_dump.mjs`). Agnostic across any TS/JS repo, with a deeper framework-aware layer for Next.js and NestJS.
- **Ruby** - `.rb`, parsed with Prism (`prism_dump.rb`). Agnostic across any Ruby repo, with a deeper framework-aware layer for Rails.
- **Python** - `.py`, parsed with libcst (`libcst_dump.py`), bundled with the plugin. Discovery globs `**/*.py` only (`_glob_for_extractor`); `.pyi` stubs are never discovered/clustered at bootstrap, but ARE honored downstream where a path is checked rather than discovered (signature contract-diff, phantom-import/-symbol probes, forward hydration, lint gates). Agnostic across any Python repo, with a deeper framework-aware layer for Django / DRF / Flask / FastAPI.

Legend: ✅ full · ⚠️ partial · ❌ missing (parity gap) · - n/a (legitimate exclusive)

## At a glance

- **210** capabilities mapped across the three languages, in 14 dimensions.
- **133** are at full parity today - all three languages ✅ (up from 108, and from 63 before the Python parity work).
- **Python**: **171** ✅ full · **10** ⚠️ partial · **3** ❌ missing · **26** - n/a.
- **TypeScript**: **177** ✅ full · **1** ⚠️ partial · **9** ❌ missing · **23** - n/a.
- **Ruby**: **161** ✅ full · **8** ⚠️ partial · **4** ❌ missing · **37** - n/a.
- Legitimate exclusives: **13** TypeScript, **12** Ruby (mostly its Rails-aware layer), **6** Python - capabilities that exist only where the language or its framework-aware layer warrants them.

Every remaining ❌ and ⚠️ is a documented language-specific exception or a named
roadmap follow-up: Ruby's Zeitwerk autoloading means it has no static import-of-named-symbol
(so the named-export cross-file rows are n/a there); TypeScript carries class heritage
on `class_contract` rather than a separate inheritance section; Python has no
language-level default/named export and its parsers fail fast (no graded
diagnostics count). The open follow-ups are Python cross-package resolution in
the WP-C5 cross-workspace existence index (TS/JS-only in v1; see dim 11) and Ruby
forward-definition hydration (a real gap, not n/a: the constant graph could drive
it; see dim 11). The cross-language gaps the original audit flagged - Python's
cross-file intelligence, the TS/Ruby security sinks, the TS extractor hardening + role
table, and the DRF/Django authz-guard - are all built and verified. See the roadmap
for what landed.

## The shared contract

Every supported language gets these, with the same purpose. This is the baseline
the matrix measures against - derivation, per-edit injection, and safety behave
identically regardless of language.

> The enumerated set below is the original all-three-✅ baseline (63 capabilities).
> Full parity is now **133** capabilities (see At a glance) - the parity work
> added 70 more rows to the all-✅ set than are listed here; the per-dimension tables
> are authoritative for the current state.

**1. AST extraction & language detection** - Dump-script backend / parser; Interpreter resolution strategy; Unavailable-toolchain degradation; can_handle detection signals; Detection precedence ordering; Default file glob; Pipe-deadlock-safe IO + timeout/exit truncation marking; MAX_AST_NODES cap (50000; Python's libcst 165000); MAX_FILE_SIZE cap (1MB) + file_too_large; MAX_CALLABLE_SIGNATURES cap (200); MAX_CALL_SITES cap (2000) + honest truncation flag; Symlink refusal + read-error guard; Per-file crash isolation; ParsedFile.top_level_node_kinds; ParsedFile.sha_hint (xxhash64); extras.function_scopes (body-shape: span/depth/branch/param); extras.callable_signatures (name/kind/params/spans); callable_signatures.params structured shape (name/optional/kind); callable_signatures.enclosing_class; callable kind taxonomy; extras.call_sites (caller->callee edges); extras.call_sites_total / call_sites_truncated

**2. Archetype clustering & cluster signature** - ClusterKey tuple: path_pattern_bucket; ClusterKey tuple: top_level_node_kinds; ClusterKey tuple: default_export_kind; ClusterKey tuple: named_export_count_bucket; Directory-based path bucketing; Sparse-cluster handling: adaptive threshold + loose merge; Shape-fuzzy merge (_shape_fuzzy_merge); Bimodal-split detection

**3. Archetype naming & framework priors** - Dispatch order in _base_name_for; Disambiguation suffixes (_disambiguation_suffixes)

**4. Shape lint (dimension mismatches)** - Language detection / dispatch into shape extractor; Shape extractor backing parser; top-level-node-kinds-mismatch; named-export-count-bucket-mismatch; ast_query recalibration (witness regex-vs-regex)

**5. Security lint (sinks & secrets)** - eval-call (bare eval()); secret-detected-in-content; string/comment stripper (per language)

**8. Import & cross-file-importer lint** - import-preference-violation (banned/preferred import enforcement)

**10. Conventions derivation** - import conventions (preferred + competing); import ordering (external-vs-relative grouping); naming: file-naming (basename casing + compound suffix); body_shape (per-function complexity norms); callable_signatures (consensus param shapes)

**11. Cross-file intelligence (symbols / calls / contracts)** - Calls index - same_file grade (file-local caller edges); Nearby-collaborator signatures (per-edit, default-ON); Function catalog + duplication-candidate prefilter

**12. Framework awareness** - Test-runner command recognition

**13. Teach / idioms / counterexamples / class contracts** - teach_profile (free-form idiom capture); teach_profile_structured (structured idiom capture); teach_competing_import (wrapper-preference convention); unteach_competing_import; Class-body-contract derivation: required methods; Idiom novelty/coverage: covered-by-principle / naming / competing-import / lint...; Idiom merge (3-way union of idioms.md by slug/section)

**14. Enforcement, block-eligibility & calibration** - import-preference-violation block rule; secret-detected-in-content block rule (kind-gated hard-block); eval-call block rule (deterministic dangerous sink); Calibration language allowlist (which profiles calibrate at all); Override-feedback demotion / SECURITY_BLOCK_RULES exemption; Inline chameleon-ignore directive (block override) + comment syntax

## Capability matrix

### 1. AST extraction & language detection

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Dump-script backend / parser | ✅ | ✅ | ✅ |  |
| Interpreter resolution strategy | ✅ | ✅ | ✅ | TS is the only one that must self-provision deps (npm ci into a data dir, locks, staging-swap, prune); Ruby needs ruby on PATH; Python is the cheapest (reuses the MCP interpreter). Legitimate per-language difference, not a gap. |
| Unavailable-toolchain degradation | ✅ | ✅ | ✅ |  |
| can_handle detection signals | ✅ | ✅ | ✅ | Ruby is the narrowest: it only matches Gemfile/*.gemspec, with NO bare-*.rb-file fallback the way TS (BUG-010 .ts fallback) and Python (rglob *.py) both have. A Rails-less Ruby script tree or a Ruby workspace member with no Gemfile of its ... |
| Detection precedence ordering | ✅ | ✅ | ✅ | Python is deliberately last-precedence because its detection is the most liberal; a polyglot repo with both package.json and *.py resolves to TS. Intentional, not a defect. |
| Default file glob | ✅ | ✅ | ✅ | Only TS needs brace-expansion (_expand_glob, typescript.py:419) because it covers 6 extensions; Ruby/Python are single-extension. Fine. |
| Subprocess hardening (env scrub + neutral cwd) | ✅ | ✅ | ✅ | Implemented for all three. The TS extractor now drops `NODE_OPTIONS` and `NODE_REPL_EXTERNAL_MODULE` (the Node analogues of RUBYOPT / PYTHONSTARTUP) before spawning ts_dump.mjs, alongside its existing neutral cwd; `NODE_PATH` stays (load-bearing, points Node at the bundled node_modules). Matches the Ruby RUBYOPT/RUBYLIB and Python PYTHONPATH/PYTHONSTARTUP scrubs. Full parity. |
| Pipe-deadlock-safe IO + timeout/exit truncation marking | ✅ | ✅ | ✅ |  |
| MAX_AST_NODES cap (50000 TS/Ruby; 165000 Py) | ✅ | ✅ | ✅ | All three enforce an AST-node cap under the same MAX_FILE_SIZE bound - full parity on the capability. The value is language-split: TS/Ruby cap at 50000, Python's libcst at 165000 (scripts/libcst_dump.py:42) because CST node counts run ~3.3x the equivalent AST for the same source. |
| MAX_FILE_SIZE cap (1MB) + file_too_large | ✅ | ✅ | ✅ |  |
| MAX_CALLABLE_SIGNATURES cap (200) | ✅ | ✅ | ✅ |  |
| MAX_CALL_SITES cap (2000) + honest truncation flag | ✅ | ✅ | ✅ |  |
| Symlink refusal + read-error guard | ✅ | ✅ | ✅ |  |
| Per-file crash isolation | ✅ | ✅ | ✅ |  |
| ParsedFile.top_level_node_kinds | ✅ | ✅ | ✅ | Node-kind strings are language-specific by design (FunctionDeclaration vs DefNode vs FunctionDef); Python adds the documented SimpleStatementLine-unwrap step (its libcst-specific quirk). Field present and populated in all three. |
| ParsedFile.default_export_kind | ✅ | ⚠️ | ⚠️ | Ruby/Python have no language-level default export, so the field is repurposed as a 'sole top-level definition kind' heuristic. That is a deliberate normalization, but it means the field carries a different meaning for those two languages; ... |
| ParsedFile.named_export_count | ✅ | ⚠️ | ⚠️ | Like default_export_kind, this is a true export count only for TS; for Ruby/Python it is a proxy (top-level definition count) since neither language has explicit named exports. Acceptable normalization. |
| ParsedFile.import_specifiers (module,kind) | ✅ | ⚠️ | ✅ | Ruby is partial: it only recognizes require/require_relative/autoload calls as 'imports' (prism_dump.rb:22-40). Rails autoloading (Zeitwerk) means most Rails files have NO require statements at all, so import_specifiers is frequently empty... |
| ParsedFile.has_jsx | ✅ | - | - | _exclusive: typescript_ |
| ParsedFile.parse_diagnostics_count + too_many_parse_errors | ✅ | ✅ | ⚠️ (by design) | Settled limitation, not a closable gap. Both Python parsers fail fast: `cst.parse_module` raises `ParserSyntaxError` on the FIRST syntax error and the `ast.parse` fallback raises `SyntaxError` likewise - neither returns a partial tree with a diagnostics array, so only 0 (clean), 1 (ast-recovered marker), or a hard parse_error record are achievable. TS's `ts.createSourceFile` is error-recovering and exposes `parseDiagnostics` as an array, which has no libcst/ast analogue. A graded count is structurally impossible without an error-recovering Python parser; stays ⚠️ by design. |
| ParsedFile.sha_hint (xxhash64) | ✅ | ✅ | ✅ |  |
| extras.function_scopes (body-shape: span/depth/branch/param) | ✅ | ✅ | ✅ | Branch/nesting node sets are language-tuned (Ruby blocks raise depth but are not separate frames; Python match raises depth, cases do not; TS switch counted once + per-CaseClause branch). All three emit the same 6 metric keys, so the norma... |
| extras.callable_signatures (name/kind/params/spans) | ✅ | ✅ | ✅ |  |
| callable_signatures.params structured shape (name/optional/kind) | ✅ | ✅ | ✅ | Python/Ruby model keyword + keyword_rest kinds (their languages have kwargs); TS models a 'destructured' kind instead (its language has object/array binding patterns). Each covers its language's real param vocabulary. |
| callable_signatures.return_type (declared annotation) | ✅ | - | ✅ | Implemented. libcst_dump records node.returns into sig['return_type'] (`_enter_function`, libcst_dump.py), omitted when unannotated; consumed via the symbol-signature index build in the orchestrator. Mirrors ts_dump. Full parity. |
| callable_signatures/param declared type annotation | ✅ | - | ✅ | Implemented. `_param_type` reads p.annotation and `_param_shapes` attaches shape['type'] when present (libcst_dump.py), mirroring ts_dump. Feeds the signature-hydration index alongside return_type. Full parity. |
| callable_signatures.decorators | ✅ | - | ✅ | Implemented. ts_dump.mjs now attaches per-method `decorators` (via the existing `decoratorsOf`) to each method/accessor signature (@Get(), NestJS), omitted when empty - matching Python's per-def decorators. Full parity. |
| callable_signatures.enclosing_class | ✅ | ✅ | ✅ |  |
| callable_signatures.enclosing_class_path (qualified) | ✅ | ✅ | ✅ | Implemented. ts_dump.mjs now tracks a `namespaceStack` (ModuleDeclaration) and joins it with the named-class frames to emit a qualified `enclosing_class_path` (e.g. `Api.FooController`), omitted for plain functions and anonymous-class sentinels. calls_index keys on it (falling back to the lexical name only for genuinely old dumps); a top-level class yields the bare name, so existing keys are unchanged. Full parity. |
| callable_signatures.base_class | ✅ | ✅ | ✅ | Implemented. ts_dump.mjs now carries each named class's `extends` on its method signatures via a `classBaseStack` (omitted when the class has no base), so the class contract reads base from callable_signatures.base_class for TS as it already did for Ruby/Python. Full parity. |
| callable_signatures.is_default_export | ✅ | ⚠️ | ⚠️ | Only meaningful for TS; Ruby/Python hardcode false because neither has a default-export concept. Correct, since the field would be meaningless for them. |
| callable kind taxonomy | ✅ | ✅ | ✅ | Each taxonomy is language-shaped. The class-contract method set (_CONTRACT_METHOD_KINDS, conventions.py) now includes Python 'staticmethod'/'classmethod' alongside 'method', so a Python class whose recurring members are decorated static/classmethods keeps its contract. TS kinds remain outside the contract method set (a separate TS consideration). |
| extras.class_shapes (per-class base + decorators) | ✅ | ❌ | ✅ | Implemented. The dump emits class_shapes with both a `bases` list and a TS-shaped `extends` string plus decorators (libcst_dump.py class_shapes emission); the class_contract consumer reads `extends or bases[0]` (`_collect_contract_classes`, conventions.py). Consumer key-mismatch closed. Full parity. |
| class_shapes.implements (TS interfaces) | ✅ | - | - | _exclusive: typescript_ |
| extras.class_body_calls (receiverless DSL macros) | ❌ | ✅ | ❌ | _exclusive: ruby_ |
| extras.call_sites (caller->callee edges) | ✅ | ✅ | ✅ | Receiver kinds are language-shaped: TS adds new/super, Ruby adds constant (Foo::Bar dispatch), Python is the leanest (bare/self/member only - no 'new' since Python uses plain Class() calls, no super-kind classification). Python's lack of a... |
| extras.call_sites_total / call_sites_truncated | ✅ | ✅ | ✅ |  |
| extras.import_symbols (named-import binding rows) | ✅ | - | ✅ | Implemented. `from m import a as b` emits {name,local,module,line} (`_collect_from_import`, libcst_dump.py) and is consumed by the calls index (`build_calls_index`, calls_index.py) and the (typescript,python)-gated reverse index (`REVERSE_INDEXED_LANGUAGES`, symbol_index.py). Full parity. Ruby: n/a (Zeitwerk - no static import-of-named-symbol to bind; see Settled limitations). |
| extras.namespace_imports (import * as alias) | ✅ | - | ✅ | Implemented. Whole-module binds emit {alias,module,line} (`_collect_import`, libcst_dump.py), consumed by the ns_aliases map in `build_calls_index` (calls_index.py). Parity with TS `import * as`. Full parity. Ruby: n/a (Zeitwerk - no import-aliasing statement to record; see Settled limitations). |
| extras.named_export_names + export_set_open (phantom-symbol/exports i... | ✅ | - | ✅ | Implemented. _module_exports enumerates top-level def/class + assignment targets + re-exports, descends top-level if/try/with, adds __init__ siblings, and opens on `import *`/PEP-562 __getattr__ (`_module_exports`, libcst_dump.py). The build runs for (typescript,python) via the `build_exports_index` call in `_bootstrap_single` (bootstrap/orchestrator.py). Full parity. Ruby: n/a (Zeitwerk - no language-level named exports, constants autoload by convention; see Settled limitations). |
| extras.class_shapes.class_attrs (class-level config-attribute presence) | ❌ | ❌ | ✅ | Python-exclusive. _class_attr_names captures the target names of direct class-body assignments (permission_classes, queryset, serializer_class) and emits them as class_shapes[].class_attrs (libcst_dump.py); the class-contract consumer reads them for the DRF/Django authz-attribute signal (against _PY_AUTHZ_ATTRS, conventions.py). TS class_shapes carries name/extends/decorators/implements but no class-body attribute capture; Ruby expresses class-level config via class_body_calls (DSL macros) instead. |

### 2. Archetype clustering & cluster signature

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| ClusterKey tuple: path_pattern_bucket | ✅ | ✅ | ✅ |  |
| ClusterKey tuple: content_signal_match | ✅ | ⚠️ | ⚠️ | No Ruby- or Python-specific file-level lexical directive is recognized (e.g. Ruby '# frozen_string_literal: true' magic comment, Python '# -*- coding -*-' / 'from __future__'); both langs can only ever produce 'shebang' or 'none', so this ... |
| ClusterKey tuple: top_level_node_kinds | ✅ | ✅ | ✅ |  |
| ClusterKey tuple: default_export_kind | ✅ | ✅ | ✅ | Semantics differ by lang and are approximated for Ruby/Python (no real 'default export' exists): Ruby/Python infer it from a sole top-level class/func. The node-kind vocabulary also differs (ClassDeclaration vs ClassNode vs ClassDef), whic... |
| ClusterKey tuple: named_export_count_bucket | ✅ | ✅ | ✅ | Python's count is purely top-level class+def and ignores __all__ (the actual Python export convention), so a module that re-exports many names via __all__ buckets as 0. Ruby counts class/module/def equally. Both are coarse proxies vs TS's ... |
| ClusterKey tuple: import_module_set_hash (hash_import_set) | ❌ | ❌ | ❌ | Vestigial for every language: the function exists and the ClusterKey field exists, but compute_signature pins it to '' (see the hash_import_set comment in signatures.py: exact import sets made every service its own cluster, the single largest over-fragmentation source)... |
| ClusterKey tuple: jsx_present | ✅ | - | - | _exclusive: typescript_ |
| Directory-based path bucketing | ✅ | ✅ | ✅ |  |
| Python role-based path bucketing (python_role_for_path) | - | - | ✅ | _exclusive: python (Django/DRF/Flask/FastAPI)_ |
| Monorepo-workspace path bucketing | ✅ | ✅ | ✅ | Implemented. `_MONOREPO_WORKSPACE_ROOTS` now includes the Nx workspace root `libs` alongside `packages`/`apps`/`workspaces` (signatures.py), so a `libs/<pkg>/...` tree keeps its workspace name like a JS monorepo. `src` is deliberately excluded (it is the dominant single-package source root, not a workspace root). Python role files (models.py etc.) still short-circuit to role buckets before this branch by design. Full parity. |
| sub_bucket splitting (_split_by_sub_bucket) | ✅ | ✅ | ⚠️ | The suffix vocabulary is language-mixed: 'concerns' fires for Rails, 'spec' for Ruby/RSpec, '__tests__' for JS/TS, 'tests'/'test'/'base' general. Python role clusters are deliberately exempt (forced sub_bucket='') so the cross-app 'model' ... |
| Sparse-cluster handling: adaptive threshold + loose merge | ✅ | ✅ | ✅ | Loose-merge groups partly on jsx_present, which is always False for Ruby/Python, so that grouping dimension is a no-op for them; merge there reduces to (path_pattern_bucket) + Jaccard, which is the intended behavior and not a defect. |
| Shape-fuzzy merge (_shape_fuzzy_merge) | ✅ | ✅ | ✅ | Group key includes jsx_present (always False for Ruby/Python, inert there) and default_export_kind (lang-specific node-kind names). Functionally identical across langs; only the discriminating power of jsx_present is TS-only. |
| Bimodal-split detection | ✅ | ✅ | ✅ | Two of the four inspected dimensions are weak for Ruby/Python: jsx_present is constant False (never bimodal) and content_signal_match collapses to shebang/none, so Ruby/Python bimodal detection effectively runs on 2 live dimensions vs TS's... |
| Generated-file skip (is_likely_generated) | ✅ | ✅ | ✅ | Implemented. is_likely_generated now matches the bare 'generated by' marker on the lowercased first 200 bytes (discovery.py:556,561), so '# Generated by Django ...' migrations are skipped (the `is_likely_generated` gate in `cluster_files`, clustering.py). Content-based, language-agnostic. Full parity. |

### 3. Archetype naming & framework priors

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Rails prior table (_RAILS_PRIORS) | - | ✅ | - | _exclusive: ruby (Rails)_ |
| TS/JS prior table (_TS_PRIORS) | ✅ | - | - | _exclusive: typescript_ |
| Python role table (_PY_ROLE_NAMES / _python_prior_match) | - | - | ✅ | _exclusive: python (Django: model/view/admin/urls/app-config/signal/manager/queryset/consum..._ |
| Dispatch order in _base_name_for | ✅ | ✅ | ✅ | All three languages are dispatched, but Python is placed LAST of the three prior passes and the gate condition differs (see Python cluster gate row). |
| Per-language cluster gate (_is_ruby/_is_typescript/_is_python_cluster) | ✅ | ✅ | ✅ | Implemented. The Python gate now carries the same no-`.rb`-anywhere purity clause as TS (`_is_python_cluster AND not _is_ruby_cluster AND not any(.rb)`, naming.py `_base_name_for`), so a mixed cluster whose first member is `.py` but which holds a stray `.rb` no longer takes a Python prior name. Full parity. |
| Test cluster detection (_looks_like_test) | ✅ | ✅ | ✅ | Implemented. `_PY_TEST_BASENAME_RE` (naming.py) now also matches Django startapp's default bare `tests.py` / `test.py` (`tests?` added to the alternation) alongside `test_`-prefix, `_test`-suffix, and `conftest`, so a Django app's tests module clusters as `test` and reaches the test-quality pass. Full parity. |
| Language-agnostic _has() fallback chain | ✅ | ✅ | ⚠️ | Fires for Python too (extension-agnostic), so a Python file under services/ that missed the role table still gets 'service'. But this chain is Rails/TS-shaped (controllers, mailers, hooks+use, components+jsx); it gives Python no Python-spe... |
| AST-shape fallback (jsx component / class) | ✅ | ✅ | ✅ | Implemented. is_class_default includes 'ClassDef' (naming.py:630-635); a single-top-level-class Python cluster names 'class'/'class-<suffix>' (naming.py:718-727), like Ruby ClassNode and TS ClassDeclaration. 'FunctionDef' is correctly NOT added to is_arrow_default (jsx-gated), so a function cluster is not mis-named 'component'. |
| Disambiguation suffixes (_disambiguation_suffixes) | ✅ | ✅ | ✅ |  |
| Workspace-prefix stripping (_strip_workspace_prefix) | ✅ | - | - | _exclusive: typescript_ |

### 4. Shape lint (dimension mismatches)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Language detection / dispatch into shape extractor | ✅ | ✅ | ✅ |  |
| Shape extractor backing parser | ✅ | ✅ | ✅ | Python is strictly stronger here: a true AST (stdlib ast) vs TS/Ruby regex heuristics, so Python never mis-tokenizes the way the regex paths can (the module docstring lint_engine.py:18-32 lists the regex cons). TS/Ruby cannot get a real pa... |
| default-export-kind-mismatch | ✅ | ⚠️ | ⚠️ | TS captures 6 distinct export-default kinds; Ruby and Python collapse to a 'single dominant declaration' proxy with only 2 possible values each and None whenever the file has both a class and a function (Python) or more than one top-level ... |
| top-level-node-kinds-mismatch | ✅ | ✅ | ✅ | Ruby has the richest kind vocabulary (superclass-tagged ClassNode, IncludeCall, DSL-category DslCall via _DSL_CATEGORY lint_engine.py:833-850, normalized in _normalize_kind lint_engine.py:853-874). TS folds FunctionDeclaration/FirstStateme... |
| named-export-count-bucket-mismatch | ✅ | ✅ | ✅ | Semantics differ by language (TS = real named exports; Ruby = top-level class/module/def; Python = top-level class+func), but each is the sensible analogue and the bucketing is shared, so all three are functionally complete. Severity is in... |
| jsx-presence-mismatch | ✅ | - | - | _exclusive: typescript/jsx_ |
| content-signal-mismatch | ✅ | ⚠️ | ⚠️ | Of the four recognized signals, three (use_client, use_server, ts_pragma) are TS/JS-only; only shebang (#!) is language-universal. So for Ruby the rule can only ever fire on a #! line and never recognizes `# frozen_string_literal: true`, a... |
| Async/Del/Try kind normalization (extractor-vs-bootstrap agreement) | - | - | ✅ | _exclusive: python_ |
| ast_query recalibration (witness regex-vs-regex) | ✅ | ✅ | ✅ | For Python the witness snapshot comes from stdlib ast and the candidate from stdlib ast, so they agree exactly; for TS/Ruby it is regex-vs-regex. The mechanism is language-uniform. The core-only env fallback drops default_export_kind/jsx_p... |

### 5. Security lint (sinks & secrets)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| eval-call (bare eval()) | ✅ | ✅ | ✅ |  |
| eval-call (Python exec()) | - | - | ✅ | _exclusive: python_ |
| eval-call (Ruby string-arg *_eval) | - | ✅ | - | _exclusive: ruby_ |
| eval-call (Ruby send(:eval)) | - | ✅ | - | _exclusive: ruby_ |
| weak-hash | ✅ | ✅ | ✅ | Implemented. The sink gate includes python (_WEAK_HASH_RE scan in lint_engine.py); _WEAK_HASH_RE matches hashlib.md5/sha1, gated on a crypto context so a benign cache-key MD5 stays quiet. Advisory warning, like TS/Ruby. Full parity. |
| insecure-random | ✅ | ✅ | ✅ | Implemented for all three. Ruby `rand(...)` / `Random.rand` in a crypto context (token/salt/nonce within +/-200 chars) nudges to `SecureRandom` (lint_engine.py), the same context gate as Python `random.*` and TS `Math.random`; `SecureRandom` itself is the secure target and never flags. Advisory warning. Full parity. |
| sql-string-interpolation | - | ✅ | - | _exclusive: ruby_ |
| secret-detected-in-content | ✅ | ✅ | ✅ |  |
| string/comment stripper (per language) | ✅ | ✅ | ✅ | Python stripper is regex-based and does NOT model implicit string concatenation or nested f-string expressions; adequate for the eval/exec token scan it feeds but weaker than the TS/Ruby strippers' coverage of their respective string forms... |
| command-injection sink (os.system / subprocess shell=True) | - | ✅ | ✅ | Implemented for both. Python flags os.system/os.popen/subprocess(shell=True); Ruby flags `system`/`exec` (call shape confirmed from raw content), backticks, and `%x{}` (lint_engine.py). The backtick/`%x{}` arms run on raw content with comment + string-literal span suppression (a `#{}` inside a backtick reads as a comment in the stripped copy); `execute` (ActiveRecord) and string/comment mentions don't flag. Advisory warning. Full parity. |
| insecure-deserialization sink (pickle / yaml.load) | - | ✅ | ✅ | Implemented for both. Python flags pickle.load/loads and yaml.load (not yaml.safe_load); Ruby flags `Marshal.load` and `YAML.load` (the dot-anchored `load` leaves `YAML.safe_load` and `Marshal.dump` clean), run on the strings-stripped scan (lint_engine.py). The hook and tool paths pass language to scan_dangerous_sinks. Advisory warning. Full parity. |
| eval-call (Ruby paren-less Kernel#eval) | - | ✅ | - | _exclusive: ruby_ |

### 6. Style lint (indent / quote / line-length)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| scan_style_rules language gate | ✅ | ✅ | ✅ | Implemented. python is in the scan_style_rules gate (lint_engine.py), routed through its own string/comment stripper + quote tokenizer. A .py edit resolves language=python and reaches the style baseline. Full parity. |
| Indent style/width rule | ✅ | ✅ | ✅ | Implemented. `_read_python_format` now lifts ruff `[tool.ruff.format] indent-style` and top-level `[tool.ruff] indent-width` (tool_config.py), and `_declared_indent` has a Python branch reading them from the `python_format` section (lint_engine.py), so a ruff/black repo gets indent findings without an .editorconfig (the .editorconfig path still backstops). Mirrors prettier useTabs/tabWidth. Full parity. |
| Quote style rule | ✅ | ✅ | ✅ | Implemented. `_declared_quote` reads python_format.quote_style (lint_engine.py) and a Python-aware tokenizer (_PY_TOKEN_RE) leaves docstrings + f/r/b-strings alone. Source: ruff [tool.ruff.format] quote-style else black's default (tool_config.py:244). Full parity. |
| Max line length rule | ✅ | ✅ | ✅ | Implemented. `_declared_max_line_length` reads python_format.line_length (lint_engine.py), sourced ruff>black line-length, then flake8/pycodestyle max-line-length, then .editorconfig. Full parity. |
| Line-length AllowedPatterns / AllowedURI exemption | - | ✅ | - | n/a (mirrors TS n/a). rubocop's Layout/LineLength AllowedPatterns/AllowedURI is hard-gated to Ruby (`_line_length_allowed_patterns`, lint_engine.py); no Python formatter has a config-level per-line length exemption (flake8 noqa is inline, not config). Legitimate Ruby exclusive. |
| rubocop path Exclude (AllCops + per-cop) | - | ✅ | - | n/a (mirrors TS n/a). rubocop's AllCops/per-cop path Exclude is ruby-gated (`_rubocop_exclude_globs` / `_rubocop_excluded`, lint_engine.py); ruff/flake8 `exclude` is file-selection (which files are scanned), a different construct. Legitimate Ruby exclusive. |
| Formatter-config source: per-language reader at bootstrap | ✅ | ✅ | ✅ | Implemented. _read_python_format parses pyproject [tool.ruff]/[tool.black]/[tool.ruff.format] and setup.cfg/tox.ini/.flake8 into a python_format section (tool_config.py:212-284), persisted to rules.json in `_bootstrap_single` (bootstrap/orchestrator.py). Pure-parse, fails open. Lifts line_length + quote_style + indent (indent_style + indent_width). Full parity. |
| Per-file emission cap + summary tail | ✅ | ✅ | ✅ | Implemented automatically with the gate. The cap + summary-tail (the summary tail in `scan_style_rules`, default 20) is language-independent and fires for Python ('+N more (capped at 20)'). Full parity. |

### 7. Naming & inheritance lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| naming-convention-violation (identifier naming) | ✅ | ✅ | ✅ | Implemented (PKG-6). Python def names derive method_casing (snake_case), classes derive class_casing (PascalCase); _python_naming_violations (lint_engine.py) enforces both at >=60% consistency, dunder/underscore exempt. Constant-casing deliberately not derived (a lowercase module var is valid PEP 8). Full on the applicable casing set. |
| naming-convention-violation block-eligibility (per-repo calibration g... | ✅ | ✅ | ✅ | Implemented (PKG-6). python is in BLOCK_RULE_LANGUAGES for naming-convention-violation (violation_class.py:237) and the calibration signal check fires off the Python-derived method_casing/class_casing keys (`_naming_entry_drives_rule`, enforcement_calibration.py). No longer a vacuous always-clean calibration. |
| file-naming-convention-violation | ✅ | ✅ | ✅ | Implemented. The edit-time gate now concatenates `_PY_EXTENSIONS` (`_file_naming_violations`, lint_engine.py), so a .py/.pyi basename whose casing or compound suffix breaks the archetype's dominant pattern reaches the language-agnostic check. Both halves wired. Full parity. |
| inheritance-convention-violation | - | ✅ | ✅ | Implemented (now ruby+python, no longer ruby-exclusive). `_python_inheritance_violations` flags a top-level (indent 0) class whose bases fall outside the cohort's dominant/known bases (lint_engine.py); derived by `_python_inheritance_conventions` (conventions.py) at a >=60% floor; block-eligible (violation_class.py:244). Bare/nested/known-base classes exempt. |
| required-guard-convention | - | ✅ | ✅ | Implemented for Python (the DRF/Django analog of the Rails before_action guard). A view archetype where >=60% of files made an authz decision derives `authz_required`; the edit-time lint flags an unguarded view. PRESENCE-semantics: a `permission_classes`/`authentication_classes` assignment (any value, incl. AllowAny), a `@login_required`/`@permission_required` decorator, a LoginRequiredMixin/PermissionRequiredMixin base, or a known cohort base satisfies it. Advisory info, never block-eligible. |
| then-without-catch | ✅ | - | - | _exclusive: typescript/JavaScript_ |
| tautological-assertion | ✅ | ✅ | ✅ | Implemented for all three. `_RUBY_TAUTOLOGY_RE` flags RSpec `expect(<lit>).to eq/eql/be(<same>)` (parenthesized or bare matcher arg) and Minitest `assert_equal <lit>, <same>` over literal-vs-same-literal (lint_engine.py), the near-zero-FP shape; a real assertion (`expect(result).to eq(1)`) and distinct values don't fire. Advisory info. Full parity. |
| test-quality suite (skipped-test, real-sleep-in-test, random-in-test,... | ✅ | ✅ | ✅ | Implemented for Python. Python test files name into the 'test' archetype (naming.py:649) so the gate opens (`_test_quality_violations` via `lint_conventions`, lint_engine.py); dedicated Python regexes cover skipped/tautology/real-sleep/random/assertion-free + witness-gated unstubbed-network and unfrozen-clock. All advisory. Full parity. |

### 8. Import & cross-file-importer lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| import-preference-violation (banned/preferred import enforcement) | ✅ | ✅ | ✅ | Python has no constant/usage fallback equivalent to Ruby's _ruby_module_in_use constant-path branch (`_ruby_module_in_use`, lint_engine.py): a discouraged module used transitively without an explicit import statement is invisible. Minor, since Python (... |
| import-preference: string-embedded-import false-positive guard | ✅ | ✅ | ✅ | Implemented for all three. Ruby/Python now filter each import-statement match through `_import_keyword_is_real` against the length-preserving strings/comments-stripped copy (lint_engine.py): a `require`/`import`/`from` whose keyword sits inside a docstring, heredoc, or string literal is dropped before specifier extraction, while a real top-level import still flags. TS keeps its `_blank_string_embedded_imports` pre-blank. Full parity. |
| import-preference inline-ignore directive (// /  # chameleon-ignore) | ✅ | ✅ | ✅ | Implemented. The Python directive scan routes through _blank_python_strings (violation_class.py:96), so a real `# chameleon-ignore import-preference-violation` suppresses the violation while the same text in a docstring does not. Full parity. |
| phantom-import (relative import resolves to no file on disk) | ✅ | ✅ | ✅ | Implemented. Relative imports (from .x / from ..pkg) are resolved on disk (.py/.pyi/__init__ probe, dotted-level-to-parent-dir) and a typo'd module flags phantom-import (`lint_phantom_imports`, phantom_imports.py); strings blanked first. Block-eligible. Full parity. |
| phantom-symbol (named binding not exported by resolved module) | ✅ | - | ✅ | Implemented. A resolved module's named bindings are checked against its CLOSED export set from the Python exports index; an absent name flags phantom-symbol, an open set (import */__getattr__) is skipped. Covers BOTH relative (`from .m import x`) and absolute first-party (`from pkg.mod import x`, resolved against the repo's Python source roots) forms; an unresolvable absolute spec may be stdlib or a dependency and stays silent. Build/lookup keys byte-identical. Ruby: n/a (Zeitwerk - no static named imports; matches the dim-11 mark and Settled limitations). Full parity. |
| cross-file-importers (blast-radius advisory on rename) | ✅ | - | ✅ | Implemented. `from x import y` rows build reverse_index.json (Python resolver); lint_cross_file_imports reports the blast-radius advisory per exported name with indexed importers (phantom_imports.py). Honors `# chameleon-ignore`. Ruby: n/a at edit time (Zeitwerk; matches dim 11) - the importer question IS answered for Ruby by `query_symbol_importers` via the constant graph (see dim 11). Full parity. |
| removed-export-breaks-importers (existence break on export removal) | ✅ | - | ✅ | Implemented. A top-level def/class an importer still imports but the edited module no longer exports is flagged with importer file:line sites (phantom_imports.py), via the same Python reverse index + ast-computed current export set; skipped on an open set. Ruby: n/a at edit time (Zeitwerk; matches dim 11) - the Ruby analogue (a removed class/module whose referencers still name it) runs at the Stop backstop over the constant graph (see dim 11). Full parity. |
| tsconfig/jsconfig path-alias resolution (@/* , ~/* aliases) | ✅ | - | - | _exclusive: typescript_ |
| NodeNext/ESM .js->.ts specifier remap in phantom resolution | ✅ | - | - | _exclusive: typescript_ |
| non-code / bundler-query specifier skip in phantom resolution | ✅ | - | - | n/a (mirrors TS n/a). vite/webpack `?query`/`#fragment` specifiers are a JS/TS bundler concept with no Python import-syntax analogue (`_NON_CODE_EXT_RE`, phantom_imports.py). Not a Python gap. |
| off-pattern counterexample capture (import-preference injection partn... | ✅ | ✅ | ✅ | Implemented (PKG-8). `_import_of` has a Python unquoted from/import branch (counterexamples.py) and the repo scan resolves language per file; a taught competing import on a Python repo captures the real off-pattern line (comment/string-state-aware). Full parity. |

### 9. Test-quality lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| test-quality pass language gate | ✅ | ✅ | ✅ | Implemented. The gate includes python and feeds a python-stripped scan + witness strip (`_test_quality_violations`, lint_engine.py); a pytest/unittest file in a test archetype reaches every test-quality rule. Full parity. |
| skipped-test | ✅ | ✅ | ✅ | Implemented. `_PY_SKIPPED_TEST_RE` matches @pytest.mark.skip/skipif/xfail, unittest skip variants, and pytest.skip() (lint_engine.py). Verified firing. |
| tautological-assertion | ✅ | ✅ | ✅ | Implemented for all three. Python `_PY_TAUTOLOGY_RE` flags `assert <lit> == <same>` and assertEqual(<lit>,<same>); Ruby `_RUBY_TAUTOLOGY_RE` flags `expect(<lit>).to eq/be(<same>)` and `assert_equal <lit>, <same>` (lint_engine.py). Full parity. |
| real-sleep-in-test | ✅ | ✅ | ✅ | Implemented. time.sleep(<n>) / asyncio.sleep(<n>) in a test body flagged (`_PY_REAL_SLEEP_RE`, lint_engine.py). Verified firing. |
| random-in-test | ✅ | ✅ | ✅ | Implemented. random.*, numpy/np.random.*, secrets.*, uuid.uuid1/4 flagged (`_PY_TEST_RANDOM_RE`, lint_engine.py). Edge: np.random.seed(0), the deterministic fix, also matches (info-severity). |
| assertion-free-test | ✅ | ✅ | ✅ | Implemented. A `def test_*` block is spanned (_py_block_span); absence of a recognized assertion (bare assert, pytest.raises, self.assert*) flags assertion-free-test (`_test_quality_violations`, lint_engine.py). Verified fire + suppress. |
| unstubbed-network | ✅ | ✅ | ✅ | Implemented. Python HTTP-client call tokens (requests/httpx/urllib/aiohttp) + stub tokens (responses/respx/vcr/httpretty/requests_mock/aioresponses) wired into the witness-gated rule (`_NETWORK_CALL_TOKENS` / `_NETWORK_STUB_TOKENS`, lint_engine.py). |
| unfrozen-clock | ✅ | ✅ | ✅ | Implemented. Freeze tokens (freezegun/time_machine) + Python clock-read tokens (datetime.now/utcnow/today, time.time) wired into the witness-gated rule (`_CLOCK_READ_TOKENS` / `_CLOCK_FREEZE_TOKENS`, lint_engine.py). |
| witness assertion-helper self-calibration | ✅ | ✅ | ✅ | Implemented. The helper-vocabulary derivation is parameterized by language (`_witness_assert_helpers`, lint_engine.py), so a candidate wrapping asserts in the team's helper isn't mis-flagged assertion-free when a sibling witness uses it. Verified suppress-with-witness. |
| CHAMELEON_LINT_DIMENSIONS core/full toggle | - | - | - |  |
| test-path detection (_is_test_path) | ✅ | ✅ | ✅ | Implemented. _is_test_path matches test_*.py / *_test.py / conftest.py / Django's bare tests.py + any path with a tests/ component (conventions.py). The earlier bare-tests.py gap is closed by the `tests?` regex extension. |
| candidate-test-path derivation (_candidate_test_paths) | ✅ | ✅ | ✅ | Implemented. The Python block now also emits the dominant Django/pytest nested-package candidate `<dir>/tests/test_<stem>.py` (and `<dir>/tests/<stem>_test.py`) - a `tests/` package sibling to the source's own directory, the analogue of the TS `__tests__` sibling - alongside the co-located and root-mirrored candidates (conventions.py). Full parity. |
| test-pairing convention derivation + advisory | ✅ | ✅ | ✅ | Implemented. With the nested `<app>/tests/test_<stem>.py` candidate added, the derivation now pairs the dominant Django/pytest nested per-app `tests/` layout (verified: a 10-file `app/tests/test_*.py` cohort derives frequency 1.0), alongside co-located and root-mirrored layouts. Full parity. |
| test-archetype naming (_looks_like_test) | ✅ | ✅ | ✅ | Implemented. _looks_like_test recognizes co-located pytest files by basename + nested tests/ clusters + Django's bare tests.py (naming.py), so the cluster names 'test' and reaches the test-quality pass. The earlier Django tests.py-only-cluster gap is closed. |

### 10. Conventions derivation

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| import conventions (preferred + competing) | ✅ | ✅ | ✅ |  |
| import ordering (external-vs-relative grouping) | ✅ | ✅ | ✅ |  |
| naming: TS prefix conventions (interface/type/enum I-prefix) | ✅ | - | - | _exclusive: typescript_ |
| naming: Ruby casing conventions (method/class/constant casing) | - | ✅ | ⚠️ | Partial. Python derives method_casing (snake_case) + class_casing (PascalCase) via the language-agnostic casing path (`extract_declarations_from_content` -> `extract_naming_conventions`, conventions.py). Constant-casing is deliberately omitted (a lowercase module var is statically indistinguishable from a real UPPER constant, so a rule would false-flag valid PEP 8). 2 of 3 casing types; ⚠️ by design. |
| naming: file-naming (basename casing + compound suffix) | ✅ | ✅ | ✅ |  |
| inheritance (dominant base class + include mixins) | ❌ | ✅ | ✅ | Implemented (Python column). _python_inheritance_conventions (conventions.py) reads class_shapes[].bases (deduped, 'object' dropped) for dominant_base + known_bases, wired for ruby+python. No dominant_include is correct Python scope (a mixin is just another base). TS stays ❌. |
| method_calls (Rails DSL fingerprint) | - | ✅ | - | _exclusive: ruby_ |
| required_guards (controller before_action authz) | - | ✅ | - | _exclusive: ruby_ |
| class_contract (DSL macros / decorators / required methods / base) | ✅ | ✅ | ✅ | Implemented. The bases-vs-extends mismatch is closed (consumer reads bases[0], dump emits extends; _collect_contract_classes, conventions.py), staticmethod/classmethod are in _CONTRACT_METHOD_KINDS, data-model dunders shape-excluded. dsl_macros stay Ruby-only (a separate exclusive row). TS-parity (base+decorators+required-methods). Full. |
| key_exports (reuse / check-before-creating names) | ✅ | ✅ | ✅ | Implemented. The python branch reads libcst-enumerated named_export_names (top-level public def/class, __all__-aware) from extras, drops underscore/single-char, ranks by recurrence (`extract_key_exports`, conventions.py). Same reuse signal as TS/Ruby. Full parity. |
| body_shape (per-function complexity norms) | ✅ | ✅ | ✅ |  |
| callable_signatures (consensus param shapes) | ✅ | ✅ | ✅ |  |
| error_handling (try/catch vs rescue_from shape) | ✅ | ✅ | ✅ | Implemented. _PY_TRY_RE matches the colon form `try:` and records the fraction of archetype files doing structured error handling under try_catch (`_PY_TRY_RE` in `extract_error_handling_conventions`, conventions.py). The Ruby-only rescue_from shape is Ruby richness, not a Python gap. Full parity. |
| doc_coverage (documented-public-declaration fraction) | ✅ | ✅ | ✅ | Implemented with Python semantics: docstring detection scans DOWNWARD to the first body statement (_py_decl_has_docstring, conventions.py:339,467), measuring public def/class on a strings-stripped copy; wired through the orchestrator with extractor.language. Full parity. |
| test_pairing (source-to-test pairing rate + mapping) | ✅ | ✅ | ✅ | Implemented. `_is_test_path` recognizes test_*.py/*_test.py/conftest.py plus Django's bare `tests.py` (clean denominator), and `_candidate_test_paths` now emits the nested per-app `<app>/tests/test_<stem>.py` candidate alongside co-located and root-mirrored, so the dominant Django layout pairs. Full parity. |
| layering (repo-level forbidden cluster edges + import cycles) | ✅ | ✅ | ✅ | Implemented. _resolve_python translates dotted relative (.models, ..pkg) + absolute intra-repo specs to on-disk files (.py/.pyi/__init__, repo-root contained, fail-open external; import_graph.py:148-181), feeding the forbidden-edge + import-cycle build. Full parity. |

### 11. Cross-file intelligence (symbols / calls / contracts)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Exports index (named-symbol export set) | ✅ | - | ✅ | Python: full. libcst_dump emits named_export_names + export_set_open (descends top-level try/if/with, adds __init__ siblings, opens on import */__getattr__); build runs for (typescript,python) via the `build_exports_index` call in `_bootstrap_single` (bootstrap/orchestrator.py). Verified: a profiled repo writes a closed exports_index.json. Ruby: n/a. |
| Reverse index (importer graph: who imports a name) | ✅ | - | ✅ | Python: full. The reverse_index.json artifact and the edit-time advisory were already Python-correct; the `query_symbol_importers` tool and `get_crossfile_context` now dispatch `_python_current_export_names` (passing the absolute path so an `__init__.py` package's sibling re-exports are included) for `.py`/`.pyi` files instead of the TS-only `_current_export_names` (tools.py). The autopass blast router consumes the corrected tool unchanged. Ruby: n/a. |
| query_symbol_importers (who-references-this-name tool answer) | ✅ | ✅ | ✅ | The TOOL is at parity even though the reverse-index ARTIFACT is not: TS/Python read reverse_index.json (Python via _python_current_export_names for .py/.pyi), while Ruby dispatches `_ruby_constant_importers` (tools.py) over constant_index.json — each constant the edited file defines with its referencing files (the constant-reference blast radius); `broken` stays [] for Ruby (the constant index carries no method-level data). Full parity on the blast-radius question. |
| phantom-import (relative import target resolves to a file) | ✅ | ✅ | ✅ | Python: full. Relative imports are dot-walked to a base and probed against .py/.pyi/__init__ (`lint_phantom_imports`, phantom_imports.py); a typo'd relative module flags, strings blanked first. An absolute MODULE stays unverifiable without sys.path (may be stdlib/dependency; skipped by design) — its SYMBOLS are covered by the row below. Ruby: via require_relative. |
| phantom-symbol (imported name exists in target's exports) | ✅ | - | ✅ | Python: full. A resolved module's bindings are checked against its CLOSED Python export set; an absent binding flags, an open set is skipped. Relative AND absolute first-party forms (absolute specs resolve via the repo's Python source roots; an unresolvable spec stays silent). Verified end-to-end. Ruby: n/a. |
| cross-file-importers (edit-time blast-radius advisory) | ✅ | - | ✅ | Python: full at edit time. lint_cross_file_imports emits the 'N files import X' advisory per exported name with indexed importers, reading the Python export set via _python_current_export_names (`lint_cross_file_imports` / `_python_current_export_names`, phantom_imports.py) - not the TS regex. Ruby: n/a. |
| removed-export-breaks-importers (deterministic existence break) | ✅ | - | ✅ | Python: full at edit time. A def/class an indexed importer references but the module no longer exports is flagged with (importer,line) witnesses, suppressed on an open set (phantom_imports.py). The separate Stop-backstop advisory is no longer TS-gated: `_crossfile_existence_advisory_lines` (hook_helper.py) dispatches per language — TS regex export set, Python `_python_current_export_names`, and Ruby via the CONSTANT graph (a class/module constant_index records as defined only in the edited file, removed while referencers still name it, live-re-verified). Ruby: n/a on the named-export form (Zeitwerk), but the constant-removal analogue fires at Stop. An opt-in Stop-time BLOCK also exists for TS/Python (see dim 14). |
| Cross-workspace existence index (WP-C5 monorepo cross-package importer edges) | ✅ | - | ❌ | Default-ON, kill switch `CHAMELEON_CROSSWS_INDEX=0`. Closes the per-workspace reverse-index blind spot: each workspace's dropped `@scope/pkg` cross-package import specifiers are JOINed by the coordinator into a single cross_reverse_index.json in the PLUGIN DATA DIR (off the trust-hashed profile surface; `build_cross_reverse_index` / `load_cross_reverse_index`, symbol_index.py), consumed by a turn-end Stop ADVISORY (never a deny) with a live importer presence re-check (`_crossworkspace_existence_advisory_lines`, hook_helper.py). v1 resolves cross-package specifiers for TS/JS ONLY; Python cross-package resolution is a DOCUMENTED GAP (a real ❌ follow-up, not n/a — a .py edit is deliberately skipped so the doc matches the pipeline). Ruby: n/a (no named-import graph). |
| Calls index - same_file grade (file-local caller edges) | ✅ | ✅ | ✅ |  |
| Calls index - import grade (cross-file named/namespace-import call ed... | ✅ | - | ✅ | Python: full. `from .svc import run; run()` grades as an import edge (resolved against the target's closed export set) and `import a.b as x; x.f()` via the namespace alias (`build_calls_index` / `_resolved_module`, calls_index.py). The TS-only `new Foo()` grade is correctly excluded. Verified: import-grade cross-file edges in calls_index.json. |
| Calls index - constant_receiver grade (Ruby Const.method edges) | - | ✅ | - | _exclusive: ruby_ |
| Calls index - typed_property grade (TS `this.<prop>.<method>()` DI edges) | ✅ | - | - | _exclusive: typescript_. Resolves the dependency-injection / typed-field call shape through a class's declared property types (constructor parameter-properties + typed fields) to the concrete callee. FP-free: bare-identifier types only (generics/unions/untyped dropped), resolved against the ENCLOSING class of each call site (a sibling class's field type never leaks in), closed-export-set import resolution chased through barrels. Purely additive (no calls-index schema bump; existing call graphs keep working, refresh adds the DI edges). Python `self.attr` counterpart is a documented follow-up (Python DI is not idiomatic and needs __init__ assignment tracking). |
| Calls index - module_attribute grade (Python `mod.func()` module-attribute call edges) | - | - | ✅ | _exclusive: python_ (v2.50). `from pkg import mod; mod.func()` — the Python analog of `typed_property` / `constant_receiver`: the receiver must be a from-imported name whose `pkg.mod` specifier resolves to a real in-repo module FILE, and the member must be a callable defined at MODULE level in that file (a class-member-only name yields no edge — it would be an AttributeError through the module object). Additive like typed_property: a new value in VALID_GRADES (calls_index.py), no calls-index schema bump; refresh adds the edges. TS `import * as ns; ns.f()` already resolves through the import grade's ns_aliases; Ruby `Const.method` through constant_receiver. |
| get_callers / get_drift caller facts (tool read over calls index) | ✅ | ✅ | ✅ | Python: full. With the import grade built for Python, `get_callers` reads calls_index.json via `load_calls_index` (tools.py) - no TS export regex - and the judge's committed-callers grounding reads the same artifact (`caller_facts_for_diffs`, judge.py). Real import-grade Python callers. No longer starved. |
| get_callees (forward callees over the calls index) | ✅ | ✅ | ✅ | Forward counterpart of get_callers: inverts the committed calls_index to answer 'what does this function call', returning {callee, file, grade} over the deterministic same_file / import / constant_receiver grades (server.py, tools.py). All three read the same artifact. Full parity. |
| get_blast_radius (transitive / multi-hop callers) | ✅ | ✅ | ✅ | Walks calls_index UPWARD from a function and returns the bounded transitive caller chains - the 'if I change this, what transitively reaches it' question beyond one-hop get_callers - depth-clamped and fanout/total-node capped, the same reach the turn-end judge walks (blast_radius.py, server.py). Same calls snapshot for all three. Full parity. |
| Callable signatures index (per-symbol params/return/span) | ✅ | ⚠️ | ✅ | Python: full (typed). libcst_dump emits declared param `type` + `return_type` (omitted when unannotated), so Python signature rows carry params+types+return+span (the `build_symbol_signatures` call in `_bootstrap_single`, bootstrap/orchestrator.py). Verified: typed entries in symbol_signatures.json. Ruby stays ⚠️ (no static types). |
| Class/module NAME index + search (symbol_signatures.json `classes` section) | ✅ | ✅ | ✅ | v2.50, additive: a parallel `classes` map (rel -> classname -> {start_line, extends?, keyword?}) rides symbol_signatures.json with no schema bump — an old artifact simply has no `classes` key, so class search stays empty until a refresh. Ruby records the truthful `module`/`class` keyword and a block-form nested class is stored under its qualified constant path (the searchable identity; a leaf query still hits on the substring tier); TS/Python omit the keyword (all classes). `search_codebase` returns kind='class' rows (comprehension.py). Full parity. |
| Forward definition hydration (definitions of imported symbols for the... | ✅ | ❌ | ✅ | Python: full. `_parse_import_symbols` + `hydrate_imported_definitions` handle .py/.pyi: each named import resolves to its defining module and renders as a typed one-line signature (symbol_signatures.py). Verified: `add(x: int, y?: int): int - pkg/typed.py:1`. Ruby stays ❌ - a real gap, not n/a: the constant graph could drive hydration for Ruby. |
| Nearby-collaborator signatures (per-edit, default-ON) | ✅ | ✅ | ✅ | Python renders FULL typed signatures: the index stores declared param types + return_type (see the Callable-signatures-index row) and `render_imported_definition` (symbol_signatures.py) emits `param: type` / `: ret` whenever the stored type is present, plus caller-correct keyword syntax (`*` separator for Python keyword-only, `name:` for Ruby keywords). Ruby renders untyped param shapes - same limitation as the underlying signature index, not a separate gap. Default-ON for all three (kill switch `CHAMELEON_NEARBY_SIGNATURES=0`), ranked by call proximity. |
| Inbound-caller contracts (per-edit "who calls this file's exports", default-ON) | ✅ | ✅ | ✅ | The INBOUND counterpart of the row above: on a Tier-2 edit, `_inbound_contracts_section` (hook_helper.py) renders the edited file's own exports with the recorded call sites against them (cross-file-first ranking) and a "change a signature -> update these call sites in the same turn" directive — converting the most-detected defect class (cross-file staleness) into pre-edit prevention. Reads symbol_signatures.json + the reverse calls_index (mtime-cached, no live parse), so it is language-agnostic over whatever grades each language's calls index records; carries the honesty note (barrels/dynamic dispatch invisible to the snapshot). Kill switch `CHAMELEON_INBOUND_CALLERS=0`. Full parity. |
| Signature contract-diff / contract-breaks (narrowed positional contra... | ✅ | ✅ | ✅ | Python: full. The narrowing diff parses changed .py/.pyi via the Python extractor and counts required positionals PLUS — Python only — required KEYWORD-ONLY params (`_required_keyword_count`, signature_diff.py, v2.53): a `def f(*, x)` caller always passes `x` by keyword, so adding a required keyword-only arg now flags as a break exactly like a required positional. Optional keywords and **kwargs still do not flag, and Ruby required keywords stay deliberately excluded (the `_POSITIONAL_KINDS` guard — language is derived from the file extension). Joins to committed Python callers (signature_diff.py, tools.py). Full parity. |
| Function catalog + duplication-candidate prefilter | ✅ | ✅ | ✅ | Python: full on name-token + arity + exact body_hash, including the param-normalized body hash (body_hash_pnorm): _lang_from_path returns "python" for .py (function_catalog.py:208-209) and _block_param_names renames simple Python lambda params (function_catalog.py:247-258), so Python DOES get block/closure-param normalization (the orchestrator passes language=_lang_from_path at function_catalog.py:380). |
| Doctor advisory-emission health check (source-edit attribution) | ✅ | ✅ | ✅ | Python: full. .py/.pyi are in the doctor `_source_exts` set (tools.py), so a Python repo where archetype resolution silently stops firing triggers the 'advisories not firing' diagnostic, like TS/Ruby. Full parity. |

### 12. Framework awareness

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Framework role/archetype detection | ✅ | ✅ | ✅ | Implemented. `_TS_PRIORS` covers Next.js (app + pages router), Remix routes, and now NestJS/Angular filename-role suffixes (`*.controller.ts`/`*.resolver.ts`/`*.gateway.ts`/`*.service.ts`/`*.module.ts`/`*.guard.ts` via an empty-dir-chain + suffix predicate, framework-neutral names since NestJS and Angular share suffixes). A `route.ts` and a util no longer get the same treatment. Full parity with the Ruby Rails priors and Python role table. |
| Framework family classification (Rails vs Django vs Flask vs FastAPI) | ✅ | ✅ | ✅ | Implemented. `_classify_framework` resolves a discrete family - rails (Gemfile rails gem / config/application.rb), django (manage.py / Django dep), flask, fastapi (dep manifests), nextjs (next.config / next dep), nestjs (@nestjs/core dep) - from cheap markers + dependency manifests (no repo-code execution), failing open to None. No longer metadata-only: the orchestrator passes the classified family into `generate_principles`, and `_FRAMEWORK_PROTOCOL` (principles.py) emits a framework-specific anti-hallucination line into principles.md (injected at SessionStart) — the tag DRIVES model-facing guidance. Full parity. |
| Stored framework tag in profile | ✅ | ✅ | ✅ | Implemented. The classified family is persisted as an optional `framework` key in profile.json (no schema bump; old profiles load without it) and surfaced by `detect_repo`. Consumed downstream at bootstrap/refresh: the tag selects the `_FRAMEWORK_PROTOCOL` line `generate_principles` writes into principles.md (see the row above) — no longer persist-and-surface-only. Full parity. |
| Hybrid frontend handling (language_hint envelope) | ✅ | ✅ | ✅ | Implemented, both directions, persisted. A Python-primary repo with a recognized JS/TS frontend subtree (>=50 source files, vendored pruned) emits language_hint{primary:python, secondary_detected:typescript}; the reverse emits the mirror (`_bootstrap_single`, bootstrap/orchestrator.py). Reaches the persisted profile (summary.py:173). Full parity. |
| Companion-artifact co-change rules (framework pairings) | ✅ | ✅ | ✅ | Implemented for all three. TS now carries a framework-role pairing: cochange-nestjs-controller-module (`_COCHANGE_RULES`, cochange.py) fires when a new `*.controller.ts` is added without a `*.module.ts` companion in the change-set, framework-gated to repos whose package.json declares @nestjs (`_FRAMEWORK_DEP_MARKERS` / `_manifest_declares`, cochange.py) so a shared filename suffix never nags an Angular/Express/routing-controllers repo - alongside the existing cochange-prisma-migration + cochange-slice-store. Python's cochange-django-model-migration (`_COCHANGE_RULES`, cochange.py) fires on a new Django model without a migrations/*.py companion; Ruby has model->migration + controller->route. Advisory, turn-end, new-file-only. Full parity. |
| Test-runner command recognition | ✅ | ✅ | ✅ |  |
| Stale-test / test-pairing advisory eligibility | ✅ | ✅ | ✅ | Implemented. _normalize_language returns python so the stale-test loop no longer skips .py (cochange.py:69), and pytest/Django test conventions are covered by _candidate_test_paths + _is_test_path (conventions.py:556,534). Full parity on the co-located layout (nested-tests/ partial, see dim 9/10). |
| Authz / required-guard convention (before_action) | ❌ | ✅ | ✅ | Implemented for Python via the DRF/Django authz-guard derivation (permission_classes / @login_required / LoginRequiredMixin), the semantic analog of the Rails before_action guard. The Rails `before_action` callback shape itself stays Ruby-specific; TS has no derived equivalent. |
| Authz-base-class exemption (_RAILS_APP_ROOT_BASES) | - | ✅ | - | _exclusive: ruby_ |
| Inheritance-convention derivation (dominant_base / known_bases) | ❌ | ✅ | ✅ | Implemented. _python_inheritance_conventions (conventions.py) reads every declared base from class_shapes (deduped, 'object' excluded), emitting dominant_base/known_bases at the cohort floors. A Django models.Model / DRF APIView cohort forms a section. Full parity (TS stays ❌). |
| Class-contract decorator/base recognition (framework heritage) | ✅ | ✅ | ✅ | Implemented. The bases-vs-extends mismatch is fixed two ways (dump emits extends, consumer dual-reads bases; libcst_dump.py class_shapes emission, `_collect_contract_classes` in conventions.py), so a Python class's base reaches the contract. The base-only/method-less case (a pure-field model -> {}) is the cross-language contract gate, not a Python gap; that heritage is captured by the inheritance section. Full parity. |

### 13. Teach / idioms / counterexamples / class contracts

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| teach_profile (free-form idiom capture) | ✅ | ✅ | ✅ |  |
| teach_profile_structured (structured idiom capture) | ✅ | ✅ | ✅ |  |
| teach_competing_import (wrapper-preference convention) | ✅ | ✅ | ✅ |  |
| unteach_competing_import | ✅ | ✅ | ✅ |  |
| get_prose_rule_candidates (offline prose-rule miner) | ✅ | ✅ | ✅ | Mines repo docs (README, docs/) for a stated 'use X not Y' import rule and corroborates each candidate against the repo's ACTUAL imports before proposing it as a teachable idiom (prose_rules.py, server.py). Text + import scan, language-agnostic across the three. Offline, no repo-code execution. Full parity. |
| Per-edit counterexample capture (build counterexamples.json from a re... | ✅ | ✅ | ✅ | Implemented (PKG-8). `_import_of` has a Python unquoted-import branch (counterexamples.py) and the repo scan resolves detect_language per file (`capture_counterexamples_in_repo`), so a taught competing import captures the real off-pattern line; the unquoted form is gated off non-Python. Full parity. |
| Per-edit counterexample render ('do NOT write it this way' paired wit... | ✅ | ✅ | ✅ | Implemented. Render emits from stored data (`_counterexample_section`, hook_helper.py) and the edited file's language threads into the witness-vs-counterexample suppression (the `language` kwarg of `_counterexample_section`); with PKG-8 capture storing Python off-patterns, render works. Full parity. |
| Multi-off-pattern-per-archetype counterexamples (schema v2 list) | ✅ | ✅ | ✅ | Implemented by composition. The v2 per-archetype list machinery (normalize/capture/render) has no language branch and keys only on the `over` module string (`capture_counterexamples_in_repo`, counterexamples.py), so a Python archetype taught several competing imports keeps every off-pattern. Full parity. |
| Class-body-contract derivation: DSL macros | - | ✅ | - | _exclusive: ruby_ |
| Class-body-contract derivation: class decorators | ✅ | - | ✅ |  |
| Class-body-contract derivation: required methods | ✅ | ✅ | ✅ | Python's libcst_dump emits kind 'staticmethod'/'classmethod' for decorated methods, which ARE in _CONTRACT_METHOD_KINDS={method,singleton_method,staticmethod,classmethod} (conventions.py), so a Python class whose recurring members are decorated static/classmethods are recognized as contract methods. |
| Class-body-contract derivation: base class annotation | ✅ | ✅ | ✅ | Implemented (PKG-0). _collect_contract_classes (conventions.py) reads `extends` OR the first of `bases`, so a decorator-anchored, method-less Python class keeps its base annotation. The bases-vs-extends key mismatch is gone. Full parity. |
| Class-contract used as a base anchor for the cohort | ✅ | ✅ | ✅ | Implemented (same _collect_contract_classes fix). A Python cohort sharing a base anchors on it and carries it into the contract. The base-only-no-content {} case is cross-language design (Ruby/TS identical), not a Python gap. Full parity. |
| Standalone inheritance convention (dominant_base / known_bases sectio... | ❌ | ✅ | ✅ | Implemented (the '_exclusive: ruby_' note was stale). `extract_inheritance_conventions` dispatches Python to `_python_inheritance_conventions` (conventions.py), reading class_shapes[].bases for dominant_base/known_bases at the 60% floor; linted via _python_inheritance_violations. TS legitimately stays ❌ (heritage on class_contract, no section). |
| Idiom novelty/coverage: covered-by-principle / naming / competing-imp... | ✅ | ✅ | ✅ |  |
| Idiom novelty/coverage: covered-by-inheritance dedup | ❌ | ✅ | ✅ | Implemented (PKG-11, Python column). The dedup appends class_contract.base to the candidate bases (`_covered_reasons`, idiom_coverage.py), so a Python 'inherit from models.Model' idiom is deduped via the class_contract base even below the inheritance floor. TS stays ❌. |
| Idiom novelty/coverage: covered-by-class-contract content (DSL/requir... | ⚠️ | ✅ | ✅ | Implemented (PKG-11, Python column). The covered-by-class-contract reason is its own `if not is_ruby` branch (`_covered_reasons`, idiom_coverage.py) consuming the Python archetype's decorators/required-methods/DSL macros; a len>=3 guard prevents false dedupes on short tokens. |
| Idiom merge (3-way union of idioms.md by slug/section) | ✅ | ✅ | ✅ |  |

### 14. Enforcement, block-eligibility & calibration

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Block-eligible rule set (which rules may ever block) | ✅ | ✅ | ✅ | Implemented. All seven Python-applicable block rules fire on Python signal (phantom/naming/inheritance + import-preference, plus eval-call and secret-detected); BLOCK_RULE_LANGUAGES scopes the language-gated rules and calibration's lang_ok reads that allowlist (violation_class.py:230; lang_ok in `calibrate_block_rules`, enforcement_calibration.py), so the prior vacuous-active certification is gone. jsx-presence-mismatch is the one legitimately n/a rule. Full parity. |
| phantom-import block rule | ✅ | ✅ | ✅ | Implemented. python is in BLOCK_RULE_LANGUAGES['phantom-import'] (violation_class.py:234); the rule resolves a relative-import module to its file and flags unresolved ones + phantom-symbol for absent names (`lint_phantom_imports`, phantom_imports.py); calibration certifies on real signal; turn-end-deferred like TS/Ruby. tsconfig-alias / .js->.ts remap stay TS exclusives. |
| import-preference-violation block rule | ✅ | ✅ | ✅ |  |
| jsx-presence-mismatch block rule | ✅ | - | - | _exclusive: typescript_ |
| naming-convention-violation block rule | ✅ | ✅ | ✅ | Implemented for the applicable casing set: snake_case functions + PascalCase classes at >=0.60 consistency, dunder/underscore exempt (_python_naming_violations in lint_engine.py; BLOCK_RULE_LANGUAGES in violation_class.py). Constant-casing deliberately not derived, interface-prefix is TS-exclusive. Calibration certifies on real Python casing entries. |
| inheritance-convention-violation block rule | ❌ | ✅ | ✅ | Implemented (now ruby+python). inheritance-convention-violation is block-eligible for {ruby, python} (violation_class.py:244) and fires on real Python signal via _python_inheritance_violations, so calibration can certify it for Python. TS stays ❌ (class inheritance exists but no sibling rule is derived). |
| file-naming-convention-violation block rule | ✅ | ✅ | ✅ | Implemented, at parity with Ruby. The `_file_naming_violations` extension gate now includes `_PY_EXTENSIONS` (lint_engine.py), so a .py basename whose casing/compound-suffix breaks the dominant pattern is flagged (rule is None=language-independent). Fires rarely only because most modules are single lowercase words (no-signal), Python's filename distribution, not a shortfall. |
| secret-detected-in-content block rule (kind-gated hard-block) | ✅ | ✅ | ✅ |  |
| eval-call block rule (deterministic dangerous sink) | ✅ | ✅ | ✅ |  |
| removed-export-breaks-importers Stop block (opt-in crossfile existence deny) | ✅ | - | ✅ | v2.46, opt-in: `enforcement.crossfile_existence_block` (default false) on top of mode shadow/enforce. A named export the turn removed from an existing module that indexed importers still reference denies at Stop (never inline), FP-hardened by `_confirmed_crossfile_break_sites` (hook_helper.py): HEAD-scoped turn-introduced check + strict per-importer re-sourcing. Block scope is TS/Python `export` breaks ONLY; Ruby `constant` breaks (and the deleted-target/barrel cases) stay advisory-only by design — Ruby resolves constants globally, so "no other file defines it" cannot be cheaply proven at Stop, and under-block is the safe direction. Shadow logs would_block; overridable inline with `chameleon-ignore removed-export-breaks-importers`; counts against `stop_block_cap`. |
| Calibration language allowlist (which profiles calibrate at all) | ✅ | ✅ | ✅ | Allowlist parity is correct - Python is a first-class calibration language. The phantom-import and file-naming block rules now fire on real Python signal and calibration certifies them honestly - the active-but-inert footgun this note flagged before the parity work is closed (see those rows). |
| Override-feedback demotion / SECURITY_BLOCK_RULES exemption | ✅ | ✅ | ✅ |  |
| Inline chameleon-ignore directive (block override) + comment syntax | ✅ | ✅ | ✅ | _blank_string_literals (violation_class.py:64-99) now has a Python branch that blanks string bodies while preserving real # comments (where directives live), so a Python `# chameleon-ignore` is honored and the same text inside a docstring is not mis-read as author intent. The earlier _TS_STRING fall-through is gone. |

## Legitimate exclusives (the "except")

These are intentionally one-language: the capability only makes sense where its
language or framework provides the construct. Not gaps.

### TypeScript / JavaScript

- **ParsedFile.has_jsx** - Whether the file contains JSX/TSX elements.
- **class_shapes.implements (TS interfaces)** - Implemented-interface names on a class.
- **ClusterKey tuple: jsx_present** - Whether the file contains JSX/TSX, seventh tuple component.
- **TS/JS prior table (_TS_PRIORS)** - By far the largest vocabulary of the three tables; many names here (component, hook, layout, provider, context, route handlers) have no Ruby/Python prior equivalent because the underlying frameworks differ.
- **Workspace-prefix stripping (_strip_workspace_prefix)** - n/a for Ruby and Python: their detectors are not root-anchored. _has_dir_chain (naming.py:361-366) scans every segment offset and python_role_for_path scans the basename plus all parent dirs (signatures.py:205-210), so a role si...
- **jsx-presence-mismatch** - Correctly n/a: JSX is a TS/JS construct. Because both Ruby and Python witnesses and candidates are always jsx_present=False, recalibrate_ast_query (lint_engine.py:130) sets the expectation to False and lint's branches a...
- **then-without-catch** - Promise `.then()/.catch()` is a JS/TS-specific async construct. Ruby and Python have no equivalent thenable chain (Python asyncio uses await/try, Ruby uses blocks/begin-rescue), so genuinely n/a - no parity expectation.
- **tsconfig/jsconfig path-alias resolution (@/* , ~/* aliases)** - Resolves non-relative aliased import specifiers through nearest-tsconfig `paths`, treating a declared-but-unresolved alias as resolved (generated/build dirs).
- **NodeNext/ESM .js->.ts specifier remap in phantom resolution** - A `.js`/`.jsx`/`.mjs`/`.cjs` specifier is probed against the corresponding `.ts`/`.tsx`/`.mts`/`.cts` source on disk before flagging as phantom.
- **non-code / bundler-query specifier skip in phantom resolution** - Skips non-code extensions (css, svg, png, md, graphql, etc.) and strips vite/webpack `?react`/`?url`/`#frag` suffixes so asset imports never flag as phantom.
- **naming: TS prefix conventions (interface/type/enum I-prefix)** - Dominant single-letter prefix on interface/type/enum declaration names.
- **jsx-presence-mismatch block rule** - Errors when a file HAS JSX but its archetype is a non-JSX one (severity-gated: only the error 'has JSX' form blocks, the warning 'missing JSX' form stays advisory).
- **Calls index - typed_property grade (TS `this.<prop>.<method>()` DI edges)** - Resolves the dependency-injection / typed-field call shape through a class's declared property types to the concrete callee; needs static field types, which only TS carries. The Python `self.attr` counterpart is a documented follow-up (Python DI is not idiomatic and needs `__init__` assignment tracking); Ruby dispatches the equivalent intent through `constant_receiver`.

### Ruby (and its Rails-aware layer)

- **extras.class_body_calls (receiverless DSL macros)** - Class-body DSL macros are the Ruby/Rails pattern (ActiveInteraction, validates, has_many); TS/Python express the same intent differently (decorators, base classes), which class_shapes.decorators / class_shapes already c...
- **Rails prior table (_RAILS_PRIORS)** - Directory-chain prior table mapping app/controllers, app/models, app/services, db/migrate, config/initializers etc. to clean archetype names (controller, model, service, job, mailer, helper, policy, serializer, presente...
- **eval-call (Ruby string-arg *_eval)** - Flags instance_eval/class_eval/module_eval when the argument is a string/heredoc literal (block forms are exempt); warning severity.
- **eval-call (Ruby send(:eval))** - Flags send/public_send dynamically dispatching to :eval as the same arbitrary-code sink; error severity.
- **eval-call (Ruby paren-less Kernel#eval)** - Flags the paren-less `eval "x"` form (the `_RUBY_PARENLESS_EVAL_RE` arm); the bare-`eval()` rule requires the `(` and never matches it. Error severity.
- **sql-string-interpolation** - Scoped to ActiveRecord's #{}-into-SQL shape, which is a Rails-specific injection idiom. A general string-built-SQL detector for TS (knex/template-literal queries) or Python (f-string/`%`-formatted cursor.execute) would ...
- **Line-length AllowedPatterns / AllowedURI exemption** - ruff/flake8 do support per-line noqa and pycodestyle has noqa/URL leniency conventions, but this specific exemption shape (config-declared AllowedPatterns + AllowedURI) is a rubocop construct. If Python line-length is e...
- **rubocop path Exclude (AllCops + per-cop)** - This is legitimately rubocop-specific (it models rubocop's own Exclude glob semantics). TS arguably lacks a .prettierignore/.eslintignore equivalent path filter, but that is a separate TS gap, not a Python one. Python's...
- **method_calls (Rails DSL fingerprint)** - Legitimately Rails-specific. Python's analog (route/validation decorators, Django model Meta) flows through class_contract decorators instead, so this is not a true parity gap.
- **Calls index - constant_receiver grade (Ruby Const.method edges)** - Caller->callee edge where Const.method or Const.new dispatches to a singleton/instance method of a uniquely-defined fully-qualified class.
- **Authz-base-class exemption (_RAILS_APP_ROOT_BASES)** - Rails-specific: it exempts ApplicationController/ApplicationRecord-style app-root bases from Rails' required-guard/inheritance checks. Python now has its own inheritance-convention check (`_python_inheritance_violations`), but it needs no Rails-app-root exemption - it exempts the cohort's own known bases and bare/nested classes instead. A genuine Rails exclusive, not downstream of a missing Python derivation.
- **Class-body-contract derivation: DSL macros** - Derives the repo-specific class-body DSL macros (e.g. Rails acts_as_*, has_many beyond the allowlist) shared across a cohort anchored on a dominant base.

### Python

- **Python role-based path bucketing (python_role_for_path)** - Legitimately Python-exclusive: Rails encodes role in the directory chain (app/models/) which the directory bucket already captures, and TS has no equivalent filename-as-role convention. No parity gap.
- **Python role table (_PY_ROLE_NAMES / _python_prior_match)** - Django/DRF/Flask/FastAPI roles are filename-encoded (models.py), unlike Rails' directory-encoded roles, so the mechanism is fundamentally filename-driven, not chain-driven.
- **Async/Del/Try kind normalization (extractor-vs-bootstrap agreement)** - Python-specific because only Python uses two different parsers across the hot path (stdlib ast) and bootstrap (libcst) whose node vocabularies diverge on async/star forms. TS (ts_dump.mjs both sides via signature recali...
- **eval-call (Python exec())** - Legitimately Python-specific: TS has no bare code-eval exec, and Ruby's exec is a shell-process call (a different command-injection sink), so no parity gap on this rule itself.
- **extras.class_shapes.class_attrs (class-level config-attribute presence)** - Captures class-body assignment target names (permission_classes, queryset, serializer_class) on `class_shapes[].class_attrs`, feeding the DRF/Django authz-attribute signal. TS `class_shapes` carries no class-body attribute capture; Ruby expresses class-level config via `class_body_calls` (DSL macros) instead.
- **Calls index - module_attribute grade (`mod.func()` module-attribute call edges)** - `from pkg import mod; mod.func()` is a Python-specific dispatch shape: the receiver is a from-imported submodule object. TS's `import * as ns; ns.f()` already resolves through the import grade's ns_aliases and Ruby's `Const.method` through `constant_receiver`, so only Python needed a dedicated grade.

## Parity gaps & roadmap

Two waves landed here. First the Python parity program (PKG-0..PKG-11) brought
Python to within a short tail of TypeScript. Then the cross-language parity sweep
closed that tail plus the TypeScript and Ruby gaps the audit flagged, lifting
all-three-✅ parity from 108 to 130 capabilities; the v2.46-v2.53 feature wave since
(importer-query tool parity, class-name search, inbound-caller contracts) lifts it
to **133**. Every remaining ❌/⚠️ is a documented language-specific exception (see
Settled limitations below) or a named follow-up (WP-C5 Python cross-package
resolution; Ruby forward-definition hydration over the constant graph). The per-dimension
tables above are the source of truth for the current state.

### Closed since the original audit (verified implemented)

All P0 wiring bugs, the full P1 Python feature set, and the P2/P3 Python items are
done - each traced to its firing path and (where applicable) its calibration path:

- **P0 wiring bugs** - the `class_shapes` `bases`/`extends` key mismatch (the consumer dual-reads `extends or bases[0]`); the vacuous-active calibration footgun (`BLOCK_RULE_LANGUAGES` scopes the language-gated rules, calibration's `lang_ok` reads that allowlist); `file-naming-convention-violation` now enforced for `.py` (the `_PY_EXTENSIONS` gate fix); counterexample capture now matches Python `import x` / `from x import y`.
- **Cross-file intelligence (the headline P1)** - exports index, reverse index (artifact + edit-time advisory), phantom-import, phantom-symbol, cross-file-importers, removed-export-breaks-importers, calls-index import grade, `get_callers` caller facts, typed callable-signature index, forward definition hydration, signature contract-diff. Built for Python and run-verified end-to-end on real repos.
- **Test-quality suite** - the whole pass fires for Python (pytest/unittest): skipped, tautological, real-sleep, random, assertion-free, plus witness-gated unstubbed-network and unfrozen-clock, with `test_*.py` / `*_test.py` / `conftest.py` test-path detection.
- **Style baseline** - `_read_python_format` reads ruff/black/flake8 config (pure TOML/INI, no repo-code exec) for line-length + quote; the scan, the per-file cap, and the summary tail all fire for Python.
- **Naming + inheritance lint** - `naming-convention-violation` (snake_case defs / PascalCase classes, block-eligible + calibrated), `file-naming-convention-violation`, and `inheritance-convention-violation` (Django `models.Model` / DRF `APIView` cohorts) all run for Python.
- **Conventions** - `doc_coverage` (docstring-aware), `error_handling` (`try:`), `key_exports`, `inheritance`, `layering`, and `class_contract` all derive for Python.
- **Framework awareness (P2)** - the Django `models.py` -> `migrations/*.py` co-change rule; hybrid-frontend `language_hint` (both directions, persisted); stale-test eligibility; the Python inheritance-convention derivation.
- **Security sinks (P3, Python)** - `weak-hash`, `insecure-random`, command-injection (`os.system` / `subprocess(shell=True)`), and insecure-deserialization (`pickle` / `yaml.load`) all detect for Python (advisory).
- **Signature richness (P2)** - Python `return_type` + declared param type annotations are emitted by libcst_dump.
- **Enforcement** - all seven Python-applicable block rules fire on real Python signal and certify honestly under calibration.

### Closed in the cross-language parity sweep (verified implemented)

The remaining Python partials and the cross-language (TS / Ruby / all-language)
gaps the original audit flagged are all built, unit-tested, and validated:

- **Python - the short tail of partials** - `query_symbol_importers` + `get_crossfile_context` + the autopass blast router dispatch `_python_current_export_names` (importer graph no longer reports Python names broken); the nested `<app>/tests/test_<stem>.py` Django/pytest pairing candidate; ruff `[tool.ruff.format] indent-style` / `[tool.ruff] indent-width` lifted for the indent rule; the Python+Ruby string-embedded-import false-positive guard; Django's bare `tests.py` in `_looks_like_test` / `_is_test_path`; the cluster gate's not-`.rb`-anywhere purity clause; the Nx `libs` monorepo workspace root.
- **TypeScript** - the extractor now scrubs `NODE_OPTIONS` / `NODE_REPL_EXTERNAL_MODULE` (matching the Ruby/Python interpreter-option scrubs); per-method `decorators`, a qualified `enclosing_class_path` (namespace + class), and `base_class` are attached to callable signatures; the `_TS_PRIORS` table gained NestJS/Angular filename-role suffixes (`*.controller.ts` / `*.service.ts` / `*.module.ts` / `*.guard.ts` / `*.resolver.ts` / `*.gateway.ts`).
- **Ruby** - `insecure-random` (`rand` / `Random.rand` → `SecureRandom`), command-injection (`system` / `exec` / backticks / `%x{}`), insecure-deserialization (`Marshal.load` / `YAML.load`), and `tautological-assertion` (`expect(1).to eq(1)`, `assert_equal 1, 1`) - the mirrors of the Python sinks + tautology, all advisory.
- **DRF/Django authz-guard** - a presence-based view-cohort derivation (`permission_classes` / `@login_required` / `LoginRequiredMixin`) and the advisory `required-guard-convention` lint for Python, the semantic analog of the Rails `before_action` guard. An explicit `permission_classes = [AllowAny]` satisfies it (an authz decision was made), so it never second-guesses an intentionally-public view.
- **Framework-family classifier + stored tag (all languages)** - `_classify_framework` resolves rails / django / flask / fastapi / nextjs / nestjs from cheap markers + dependency manifests, persisted as an optional `framework` key in profile.json (no schema bump) and surfaced by `detect_repo`. Since then the tag has grown a consumer: it selects the framework-specific anti-hallucination line `generate_principles` writes into principles.md (`_FRAMEWORK_PROTOCOL`, principles.py).

### Settled limitations (stay ❌/⚠️/- by design - not closable parity gaps)

- **`parse_diagnostics_count` (Python ⚠️)** - both `libcst.parse_module` and the `ast.parse` fallback raise on the FIRST syntax error and return no diagnostics array, so only 0 / 1-recovered / hard-error are achievable; TS's `ts.createSourceFile` is error-recovering and exposes `parseDiagnostics` as a list. A graded count is structurally impossible without an error-recovering Python parser.
- **Ruby cross-file named-symbol intelligence (the Ruby cells on import_symbols / namespace_imports / named_export_names / phantom-symbol / cross-file-importers / removed-export)** - Ruby has no static import-of-named-symbol (Zeitwerk autoloads by convention, so most files carry no `require`), so there is no named-import graph to build; the affected rows are n/a for Ruby, not open gaps. The blast-radius question is still ANSWERED for Ruby through the constant graph: `query_symbol_importers` dispatches `_ruby_constant_importers` over constant_index.json, and the Stop backstop's existence advisory flags a removed class/module whose referencers still name it (see dim 11). Legitimate language difference.
- **TS inheritance section (TS ❌ on inheritance / inheritance-convention derivation + violation + idiom dedup)** - TypeScript carries class heritage on `class_contract` (base + decorators + required methods), not a separate inheritance section; the information is present, just in a different shape.
- **`import_module_set_hash`** - deliberately dead for all three languages (the exact import set over-fragmented clusters).
- **`class_body_calls`** - a Ruby/Rails class-body DSL idiom; TS/Python express the same intent through decorators (captured on `class_shapes` / `callable_signatures`).
- **`default_export_kind` / `named_export_count` / `is_default_export` / `default-export-kind-mismatch` / `content-signal-mismatch`** - Python (like Ruby) has no language-level default/named export or rich file-level directive, so these are correct normalizations.
- **Python constant-casing** - deliberately not derived (a lowercase module var is valid PEP 8, so a SCREAMING_SNAKE rule would false-flag it).
- **Python role-cluster sub-bucketing** - Python role clusters are intentionally exempt from `_split_by_sub_bucket` so the cross-app `model` / `view` archetypes stay unified.

---

_Audit basis: a full-codebase parity audit, re-verified against the code on
2026-06-29 (v2.38.4). Covers the cross-language parity sweep, the comprehension
layer (`search_codebase` / `describe_codebase` / `get_callees` in
`comprehension.py`, plus `get_blast_radius` and `get_prose_rule_candidates`), the
now-default-ON nearby-collaborator signatures, and the Next.js / NestJS framework
awareness for TypeScript / JavaScript (every change unit-tested). Row-level
refresh on 2026-07-07 (v2.54.0): the v2.46-v2.53 features folded in
(module_attribute calls-index grade, class/module name index + search,
inbound-caller contracts, WP-C5 cross-workspace existence index, opt-in crossfile
existence Stop block, Python keyword-only contract-diff, framework-tag-driven
principles, Ruby/Python Stop-backstop existence coverage), the At-a-glance tallies
retallied by script from the final tables, and drifted file:line references
replaced with symbol references (file + backticked symbol, each confirmed present
in the named file). Line numbers move with every release: verify a row by grepping
the named symbol, not by line number; the few line refs that remain were
re-checked against the current source at this refresh. Re-run and regenerate when the language pipelines
change; the per-dimension tables above are the authoritative current state._
