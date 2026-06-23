# Language & Framework Support Matrix

> The authoritative parity reference for chameleon's supported languages and
> frameworks. **The goal: every supported language gets the same capability,
> with the same purpose, except where a capability is genuinely specific to a
> language or framework.** This doc is the basis for closing the gap.

Supported languages:

- **TypeScript / JavaScript** — `.ts .tsx .js .jsx .mjs .cjs`, parsed with the TypeScript Compiler API (`ts_dump.mjs`).
- **Ruby (on Rails)** — `.rb`, parsed with Prism (`prism_dump.rb`).
- **Python (Django / Flask / FastAPI)** — `.py .pyi`, parsed with libcst (`libcst_dump.py`), bundled with the plugin.

Legend: ✅ full · ⚠️ partial · ❌ missing (parity gap) · — n/a (legitimate exclusive)

## At a glance

- **198** capabilities mapped across the three languages, in 14 dimensions.
- **63** are at full parity today (the shared contract below).
- **83** are verified parity gaps a language should close (the roadmap below).
- Legitimate exclusives: **12** TypeScript, **17** Ruby/Rails, **4** Python — capabilities that exist only where the language/framework warrants them.

The headline asymmetry: **Python is structurally the closest language to TypeScript
(named imports, enumerable exports, type annotations) yet currently gets the least
cross-file coverage.** Most P1 gaps below close that.

## The shared contract

Every supported language gets these, with the same purpose. This is the baseline
the matrix measures against — derivation, per-edit injection, and safety behave
identically regardless of language.

**1. AST extraction & language detection** — Dump-script backend / parser; Interpreter resolution strategy; Unavailable-toolchain degradation; can_handle detection signals; Detection precedence ordering; Default file glob; Pipe-deadlock-safe IO + timeout/exit truncation marking; MAX_AST_NODES cap (50000); MAX_FILE_SIZE cap (1MB) + file_too_large; MAX_CALLABLE_SIGNATURES cap (200); MAX_CALL_SITES cap (2000) + honest truncation flag; Symlink refusal + read-error guard; Per-file crash isolation; ParsedFile.top_level_node_kinds; ParsedFile.sha_hint (xxhash64); extras.function_scopes (body-shape: span/depth/branch/param); extras.callable_signatures (name/kind/params/spans); callable_signatures.params structured shape (name/optional/kind); callable_signatures.enclosing_class; callable kind taxonomy; extras.call_sites (caller->callee edges); extras.call_sites_total / call_sites_truncated

**2. Archetype clustering & cluster signature** — ClusterKey tuple: path_pattern_bucket; ClusterKey tuple: top_level_node_kinds; ClusterKey tuple: default_export_kind; ClusterKey tuple: named_export_count_bucket; Directory-based path bucketing; Sparse-cluster handling: adaptive threshold + loose merge; Shape-fuzzy merge (_shape_fuzzy_merge); Bimodal-split detection

**3. Archetype naming & framework priors** — Dispatch order in _base_name_for; Disambiguation suffixes (_disambiguation_suffixes)

**4. Shape lint (dimension mismatches)** — Language detection / dispatch into shape extractor; Shape extractor backing parser; top-level-node-kinds-mismatch; named-export-count-bucket-mismatch; ast_query recalibration (witness regex-vs-regex)

**5. Security lint (sinks & secrets)** — eval-call (bare eval()); secret-detected-in-content; string/comment stripper (per language)

**8. Import & cross-file-importer lint** — import-preference-violation (banned/preferred import enforcement)

**10. Conventions derivation** — import conventions (preferred + competing); import ordering (external-vs-relative grouping); naming: file-naming (basename casing + compound suffix); body_shape (per-function complexity norms); callable_signatures (consensus param shapes)

**11. Cross-file intelligence (symbols / calls / contracts)** — Calls index — same_file grade (file-local caller edges); Nearby-collaborator signatures (per-edit, experimental); Function catalog + duplication-candidate prefilter

**12. Framework awareness** — Test-runner command recognition

**13. Teach / idioms / counterexamples / class contracts** — teach_profile (free-form idiom capture); teach_profile_structured (structured idiom capture); teach_competing_import (wrapper-preference convention); unteach_competing_import; Class-body-contract derivation: required methods; Idiom novelty/coverage: covered-by-principle / naming / competing-import / lint…; Idiom merge (3-way union of idioms.md by slug/section)

**14. Enforcement, block-eligibility & calibration** — import-preference-violation block rule; secret-detected-in-content block rule (kind-gated hard-block); eval-call block rule (deterministic dangerous sink); Calibration language allowlist (which profiles calibrate at all); Override-feedback demotion / SECURITY_BLOCK_RULES exemption; Inline chameleon-ignore directive (block override) + comment syntax

## Capability matrix

### 1. AST extraction & language detection

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Dump-script backend / parser | ✅ | ✅ | ✅ |  |
| Interpreter resolution strategy | ✅ | ✅ | ✅ | TS is the only one that must self-provision deps (npm ci into a data dir, locks, staging-swap, prune); Ruby needs ruby on PATH; Python is the cheapest (reuses the MCP interpreter). Legitimate per-language difference, not a gap. |
| Unavailable-toolchain degradation | ✅ | ✅ | ✅ |  |
| can_handle detection signals | ✅ | ✅ | ✅ | Ruby is the narrowest: it only matches Gemfile/*.gemspec, with NO bare-*.rb-file fallback the way TS (BUG-010 .ts fallback) and Python (rglob *.py) both have. A Rails-less Ruby script tree or a Ruby workspace member with no Gemfile of its … |
| Detection precedence ordering | ✅ | ✅ | ✅ | Python is deliberately last-precedence because its detection is the most liberal; a polyglot repo with both package.json and *.py resolves to TS. Intentional, not a defect. |
| Default file glob | ✅ | ✅ | ✅ | Only TS needs brace-expansion (_expand_glob, typescript.py:412) because it covers 6 extensions; Ruby/Python are single-extension. Fine. |
| Subprocess hardening (env scrub + neutral cwd) | ⚠️ | ✅ | ✅ | TS sets a neutral cwd but does not strip NODE_OPTIONS (the Node analogue of RUBYOPT/PYTHONSTARTUP), so a poisoned NODE_OPTIONS=--require ... in the environment could preload code before ts_dump.mjs. Ruby and Python both explicitly scrub th… |
| Pipe-deadlock-safe IO + timeout/exit truncation marking | ✅ | ✅ | ✅ |  |
| MAX_AST_NODES cap (50000) | ✅ | ✅ | ✅ |  |
| MAX_FILE_SIZE cap (1MB) + file_too_large | ✅ | ✅ | ✅ |  |
| MAX_CALLABLE_SIGNATURES cap (200) | ✅ | ✅ | ✅ |  |
| MAX_CALL_SITES cap (2000) + honest truncation flag | ✅ | ✅ | ✅ |  |
| Symlink refusal + read-error guard | ✅ | ✅ | ✅ |  |
| Per-file crash isolation | ✅ | ✅ | ✅ |  |
| ParsedFile.top_level_node_kinds | ✅ | ✅ | ✅ | Node-kind strings are language-specific by design (FunctionDeclaration vs DefNode vs FunctionDef); Python adds the documented SimpleStatementLine-unwrap step (its libcst-specific quirk). Field present and populated in all three. |
| ParsedFile.default_export_kind | ✅ | ⚠️ | ⚠️ | Ruby/Python have no language-level default export, so the field is repurposed as a 'sole top-level definition kind' heuristic. That is a deliberate normalization, but it means the field carries a different meaning for those two languages; … |
| ParsedFile.named_export_count | ✅ | ⚠️ | ⚠️ | Like default_export_kind, this is a true export count only for TS; for Ruby/Python it is a proxy (top-level definition count) since neither language has explicit named exports. Acceptable normalization. |
| ParsedFile.import_specifiers (module,kind) | ✅ | ⚠️ | ✅ | Ruby is partial: it only recognizes require/require_relative/autoload calls as 'imports' (prism_dump.rb:22-40). Rails autoloading (Zeitwerk) means most Rails files have NO require statements at all, so import_specifiers is frequently empty… |
| ParsedFile.has_jsx | ✅ | — | — | _exclusive: typescript_ |
| ParsedFile.parse_diagnostics_count + too_many_parse_errors | ✅ | ✅ | ⚠️ | Python is all-or-nothing: libcst raises on the first syntax error rather than recovering and counting, so there is no graded diagnostics count and the >20 skip path never engages. Functionally fine (a syntactically broken Python file is dr… |
| ParsedFile.sha_hint (xxhash64) | ✅ | ✅ | ✅ |  |
| extras.function_scopes (body-shape: span/depth/branch/param) | ✅ | ✅ | ✅ | Branch/nesting node sets are language-tuned (Ruby blocks raise depth but are not separate frames; Python match raises depth, cases do not; TS switch counted once + per-CaseClause branch). All three emit the same 6 metric keys, so the norma… |
| extras.callable_signatures (name/kind/params/spans) | ✅ | ✅ | ✅ |  |
| callable_signatures.params structured shape (name/optional/kind) | ✅ | ✅ | ✅ | Python/Ruby model keyword + keyword_rest kinds (their languages have kwargs); TS models a 'destructured' kind instead (its language has object/array binding patterns). Each covers its language's real param vocabulary. |
| callable_signatures.return_type (declared annotation) | ✅ | — | ⚠️ | Python supports return annotations (`def f() -> int`) and libcst exposes node.returns, but libcst_dump.py never records them, while ts_dump.mjs does. For definition-hydration parity Python arguably SHOULD emit return_type (and param `type`… |
| callable_signatures/param declared type annotation | ✅ | — | ❌ | Python type hints on params (`x: int`) are available via libcst (p.annotation) but never extracted, whereas TS records them. For Python this is a real missing field vs TS for definition-hydration / contract richness; should emit param type… |
| callable_signatures.decorators | ❌ | — | ✅ | TS supports method/accessor decorators (@Get(), NestJS) and the dump computes decoratorsOf for classes but never attaches per-method decorators to callable_signatures, while Python does (staticmethod/classmethod/route decorators per def). … |
| callable_signatures.enclosing_class | ✅ | ✅ | ✅ |  |
| callable_signatures.enclosing_class_path (qualified) | ❌ | ✅ | ✅ | TS records only the lexical enclosing_class, not a qualified path; calls_index.py:152 explicitly falls back to enclosing_class for 'old dumps, TS'. TS classes can be namespace/module-nested too, so for unambiguous cross-namespace class key… |
| callable_signatures.base_class | ❌ | ✅ | ✅ | For TS the base is only on class_shapes.extends, not on the method header; for Ruby/Python it is on both. The class contract reads base from callable_signatures.base_class (conventions.py:1057) AND class_shapes — see the class_shapes row f… |
| callable_signatures.is_default_export | ✅ | ⚠️ | ⚠️ | Only meaningful for TS; Ruby/Python hardcode false because neither has a default-export concept. Correct, since the field would be meaningless for them. |
| callable kind taxonomy | ✅ | ✅ | ✅ | Each taxonomy is language-shaped. Consequence: the class-contract method set (conventions.py:1006 _CONTRACT_METHOD_KINDS={'method','singleton_method'}) accepts Python 'method' but EXCLUDES Python 'staticmethod'/'classmethod' and ALL TS kin… |
| extras.class_shapes (per-class base + decorators) | ✅ | ❌ | ⚠️ | Python emits class_shapes but with a `bases` LIST key, while the consumer conventions.py:1038-1040 reads `shape.get('extends')` (a TS-only string key). So Python's class-level base is NOT picked up from class_shapes; it only reaches the co… |
| class_shapes.implements (TS interfaces) | ✅ | — | — | _exclusive: typescript_ |
| extras.class_body_calls (receiverless DSL macros) | ❌ | ✅ | ❌ | _exclusive: ruby_ |
| extras.call_sites (caller->callee edges) | ✅ | ✅ | ✅ | Receiver kinds are language-shaped: TS adds new/super, Ruby adds constant (Foo::Bar dispatch), Python is the leanest (bare/self/member only — no 'new' since Python uses plain Class() calls, no super-kind classification). Python's lack of a… |
| extras.call_sites_total / call_sites_truncated | ✅ | ✅ | ✅ |  |
| extras.import_symbols (named-import binding rows) | ✅ | ❌ | ❌ | Python `from m import a as b` is exactly the named-import shape import_symbols models, and Python COULD emit it (libcst ImportFrom gives name/asname/module), but libcst_dump.py only emits coarse import_specifiers. So the cross-file symbol-… |
| extras.namespace_imports (import * as alias) | ✅ | ❌ | ❌ | Python `import x.y as z` / `import x` namespace binds are analogous and resolvable from libcst, but are not emitted, so namespace-aliased call edges are not resolved for Python. Should emit for parity with TS; Ruby has no direct analogue (… |
| extras.named_export_names + export_set_open (phantom-symbol/exports i… | ✅ | ❌ | ❌ | Python's public export set is statically enumerable (top-level def/class names, __all__), so the phantom-symbol check COULD work for Python, but no named_export_names is emitted, leaving the exports index empty and the hallucinated-import … |

### 2. Archetype clustering & cluster signature

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| ClusterKey tuple: path_pattern_bucket | ✅ | ✅ | ✅ |  |
| ClusterKey tuple: content_signal_match | ✅ | ⚠️ | ⚠️ | No Ruby- or Python-specific file-level lexical directive is recognized (e.g. Ruby '# frozen_string_literal: true' magic comment, Python '# -*- coding -*-' / 'from __future__'); both langs can only ever produce 'shebang' or 'none', so this … |
| ClusterKey tuple: top_level_node_kinds | ✅ | ✅ | ✅ |  |
| ClusterKey tuple: default_export_kind | ✅ | ✅ | ✅ | Semantics differ by lang and are approximated for Ruby/Python (no real 'default export' exists): Ruby/Python infer it from a sole top-level class/func. The node-kind vocabulary also differs (ClassDeclaration vs ClassNode vs ClassDef), whic… |
| ClusterKey tuple: named_export_count_bucket | ✅ | ✅ | ✅ | Python's count is purely top-level class+def and ignores __all__ (the actual Python export convention), so a module that re-exports many names via __all__ buckets as 0. Ruby counts class/module/def equally. Both are coarse proxies vs TS's … |
| ClusterKey tuple: import_module_set_hash (hash_import_set) | ❌ | ❌ | ❌ | Vestigial for every language: the function exists and the ClusterKey field exists, but compute_signature pins it to '' (comment at 388-392: exact import sets made every service its own cluster, the single largest over-fragmentation source)… |
| ClusterKey tuple: jsx_present | ✅ | — | — | _exclusive: typescript_ |
| Directory-based path bucketing | ✅ | ✅ | ✅ |  |
| Python role-based path bucketing (python_role_for_path) | — | — | ✅ | _exclusive: python (Django/DRF/Flask/FastAPI)_ |
| Monorepo-workspace path bucketing | ✅ | ✅ | ⚠️ | Python role files (models.py etc.) short-circuit before this branch and are bucketed by role regardless of monorepo layout, so workspace disambiguation applies only to non-role .py files. The workspace roots are also JS-centric ('packages'… |
| sub_bucket splitting (_split_by_sub_bucket) | ✅ | ✅ | ⚠️ | The suffix vocabulary is language-mixed: 'concerns' fires for Rails, 'spec' for Ruby/RSpec, '__tests__' for JS/TS, 'tests'/'test'/'base' general. Python role clusters are deliberately exempt (forced sub_bucket='') so the cross-app 'model' … |
| Sparse-cluster handling: adaptive threshold + loose merge | ✅ | ✅ | ✅ | Loose-merge groups partly on jsx_present, which is always False for Ruby/Python, so that grouping dimension is a no-op for them; merge there reduces to (path_pattern_bucket) + Jaccard, which is the intended behavior and not a defect. |
| Shape-fuzzy merge (_shape_fuzzy_merge) | ✅ | ✅ | ✅ | Group key includes jsx_present (always False for Ruby/Python, inert there) and default_export_kind (lang-specific node-kind names). Functionally identical across langs; only the discriminating power of jsx_present is TS-only. |
| Bimodal-split detection | ✅ | ✅ | ✅ | Two of the four inspected dimensions are weak for Ruby/Python: jsx_present is constant False (never bimodal) and content_signal_match collapses to shebang/none, so Ruby/Python bimodal detection effectively runs on 2 live dimensions vs TS's… |
| Generated-file skip (is_likely_generated) | ✅ | ⚠️ | ⚠️ | Django migrations start with '# Generated by Django <ver> on <date>' which matches NONE of the markers (requires 'code generated by' or 'this file was generated', not bare 'generated by'), so Django-generated migrations are NOT skipped and… |

### 3. Archetype naming & framework priors

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Rails prior table (_RAILS_PRIORS) | — | ✅ | — | _exclusive: ruby (Rails)_ |
| TS/JS prior table (_TS_PRIORS) | ✅ | — | — | _exclusive: typescript_ |
| Python role table (_PY_ROLE_NAMES / _python_prior_match) | — | — | ✅ | _exclusive: python (Django: model/view/admin/urls/app-config/signal/manager/queryset/consum…_ |
| Dispatch order in _base_name_for | ✅ | ✅ | ✅ | All three languages are dispatched, but Python is placed LAST of the three prior passes and the gate condition differs (see Python cluster gate row). |
| Per-language cluster gate (_is_ruby/_is_typescript/_is_python_cluster) | ✅ | ✅ | ⚠️ | Python gate is weaker than TS. TS requires _is_typescript_cluster AND not _is_ruby_cluster AND not any(.rb) anywhere (full mixed-cluster purity, :621-625); Python only checks _is_python_cluster AND not _is_ruby_cluster (:630), with no not-… |
| Test cluster detection (_looks_like_test) | ✅ | ✅ | ⚠️ | Python has NO file-suffix test signal. _TEST_FILE_SUFFIXES lists Ruby (_spec.rb/_test.rb) and TS/JS (.test/.spec.*) but nothing for test_*.py / *_test.py / conftest.py, and python_role_for_path deliberately returns None for tests (verified… |
| Language-agnostic _has() fallback chain | ✅ | ✅ | ⚠️ | Fires for Python too (extension-agnostic), so a Python file under services/ that missed the role table still gets 'service'. But this chain is Rails/TS-shaped (controllers, mailers, hooks+use, components+jsx); it gives Python no Python-spe… |
| AST-shape fallback (jsx component / class) | ✅ | ✅ | ❌ | Dead for Python. libcst_dump emits default_export_kind='ClassDef'/'FunctionDef', but the is_class_default set only matches ClassNode/ClassDeclaration/ModuleNode and is_arrow_default only ArrowFunction/FunctionExpression. So a plain domain/… |
| Disambiguation suffixes (_disambiguation_suffixes) | ✅ | ✅ | ✅ |  |
| Workspace-prefix stripping (_strip_workspace_prefix) | ✅ | — | — | _exclusive: typescript_ |

### 4. Shape lint (dimension mismatches)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Language detection / dispatch into shape extractor | ✅ | ✅ | ✅ |  |
| Shape extractor backing parser | ✅ | ✅ | ✅ | Python is strictly stronger here: a true AST (stdlib ast) vs TS/Ruby regex heuristics, so Python never mis-tokenizes the way the regex paths can (the module docstring lint_engine.py:18-32 lists the regex cons). TS/Ruby cannot get a real pa… |
| default-export-kind-mismatch | ✅ | ⚠️ | ⚠️ | TS captures 6 distinct export-default kinds; Ruby and Python collapse to a 'single dominant declaration' proxy with only 2 possible values each and None whenever the file has both a class and a function (Python) or more than one top-level … |
| top-level-node-kinds-mismatch | ✅ | ✅ | ✅ | Ruby has the richest kind vocabulary (superclass-tagged ClassNode, IncludeCall, DSL-category DslCall via _DSL_CATEGORY lint_engine.py:750-767, normalized in _normalize_kind lint_engine.py:770-791). TS folds FunctionDeclaration/FirstStateme… |
| named-export-count-bucket-mismatch | ✅ | ✅ | ✅ | Semantics differ by language (TS = real named exports; Ruby = top-level class/module/def; Python = top-level class+func), but each is the sensible analogue and the bucketing is shared, so all three are functionally complete. Severity is in… |
| jsx-presence-mismatch | ✅ | — | — | _exclusive: typescript/jsx_ |
| content-signal-mismatch | ✅ | ⚠️ | ⚠️ | Of the four recognized signals, three (use_client, use_server, ts_pragma) are TS/JS-only; only shebang (#!) is language-universal. So for Ruby the rule can only ever fire on a #! line and never recognizes `# frozen_string_literal: true`, a… |
| Async/Del/Try kind normalization (extractor-vs-bootstrap agreement) | — | — | ✅ | _exclusive: python_ |
| ast_query recalibration (witness regex-vs-regex) | ✅ | ✅ | ✅ | For Python the witness snapshot comes from stdlib ast and the candidate from stdlib ast, so they agree exactly; for TS/Ruby it is regex-vs-regex. The mechanism is language-uniform. The core-only env fallback drops default_export_kind/jsx_p… |

### 5. Security lint (sinks & secrets)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| eval-call (bare eval()) | ✅ | ✅ | ✅ |  |
| eval-call (Python exec()) | — | — | ✅ | _exclusive: python_ |
| eval-call (Ruby string-arg *_eval) | — | ✅ | — | _exclusive: ruby_ |
| eval-call (Ruby send(:eval)) | — | ✅ | — | _exclusive: ruby_ |
| weak-hash | ✅ | ✅ | ❌ | Python should have this. hashlib.md5/hashlib.sha1 are the canonical weak-digest sinks, and _WEAK_HASH_RE would already match `hashlib.md5(` (it is a plain case-insensitive token match) — the ONLY thing excluding Python is the language gate… |
| insecure-random | ✅ | ❌ | ❌ | Ruby and Python have direct equivalents with no detection. Ruby: rand()/Random.rand vs SecureRandom (the _RUBY_RANDOM_RE at line 2366 is the unrelated test-flakiness scan, not this security rule). Python: random.random/randint vs the secre… |
| sql-string-interpolation | — | ✅ | — | _exclusive: ruby_ |
| secret-detected-in-content | ✅ | ✅ | ✅ |  |
| string/comment stripper (per language) | ✅ | ✅ | ✅ | Python stripper is regex-based and does NOT model implicit string concatenation or nested f-string expressions; adequate for the eval/exec token scan it feeds but weaker than the TS/Ruby strippers' coverage of their respective string forms… |
| command-injection sink (os.system / subprocess shell=True) | — | ❌ | ❌ | Python should have this: os.system, subprocess.* with shell=True, and os.popen are headline Python RCE sinks the task names explicitly. Ruby has the mirror gap (Kernel#system/exec, backticks, %x{}) — neither language detects shell command … |
| insecure-deserialization sink (pickle / yaml.load) | — | ❌ | ❌ | Python should have this: pickle.load/loads and yaml.load (non-SafeLoader) are classic Python code-execution sinks named in the task. Ruby's analogue (Marshal.load, YAML.load/unsafe_load) is also undetected. The Python sink scan currently c… |

### 6. Style lint (indent / quote / line-length)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| scan_style_rules language gate | ✅ | ✅ | ❌ | Python is excluded wholesale by a hardcoded allow-list. Since .editorconfig is a language-agnostic source already read for any repo and present in rules.json (orchestrator.py:1949), Python files in a repo with an .editorconfig declaring in… |
| Indent style/width rule | ✅ | ✅ | ❌ | Python repos overwhelmingly standardize on 4-space indentation (PEP 8) enforced by black/ruff; an edit that introduces a tab or mis-width is exactly the kind of nudge this rule gives TS/Ruby. The editorconfig fallback already exists and ap… |
| Quote style rule | ✅ | ✅ | ❌ | black normalizes to double quotes and ruff (flake8-quotes / format) enforces a quote style, so Python has a genuine declared-quote preference chameleon could read from pyproject [tool.black]/[tool.ruff]. Both the python config source AND a… |
| Max line length rule | ✅ | ✅ | ❌ | Python line length is a first-class, near-universal config: black line-length (default 88), ruff line-length, flake8/pycodestyle max-line-length (default 79), all in pyproject.toml/setup.cfg/tox.ini/.flake8. This is the strongest Python st… |
| Line-length AllowedPatterns / AllowedURI exemption | — | ✅ | ❌ | _exclusive: ruby_ |
| rubocop path Exclude (AllCops + per-cop) | — | ✅ | ❌ | _exclusive: ruby_ |
| Formatter-config source: per-language reader at bootstrap | ✅ | ✅ | ❌ | Python is the only one of the three supported languages with zero formatter-config source wired. black, ruff, and flake8 declare line-length, quote style, and (implicitly) 4-space indent in pyproject.toml/setup.cfg/.flake8/tox.ini — all pa… |
| Per-file emission cap + summary tail | ✅ | ✅ | ❌ | Cap logic is language-independent and would apply to Python automatically the moment the gate opens; no separate work needed beyond enabling the gate. |

### 7. Naming & inheritance lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| naming-convention-violation (identifier naming) | ✅ | ✅ | ❌ | Python lacks an identifier-naming lint entirely. PEP 8 defines strong, near-universal Python naming (snake_case functions/methods/modules, PascalCase classes, SCREAMING_SNAKE constants) — exactly the convention class chameleon already deri… |
| naming-convention-violation block-eligibility (per-repo calibration g… | ✅ | ✅ | ❌ | Follows directly from the missing Python naming lint: with no Python naming derivation/scan, listing Python here would be a vacuous (always-clean) calibration. The omission is correct given the gap, not an independent defect. |
| file-naming-convention-violation | ✅ | ✅ | ⚠️ | Python file-naming is half-wired: the convention is DERIVED (conventions.py:1813 is language-agnostic and tallies .py basenames) but never ENFORCED, because the edit-time gate at lint_engine.py:2829 hardcodes TS+Ruby extensions and omits _… |
| inheritance-convention-violation | — | ✅ | ❌ | _exclusive: ruby_ |
| required-guard-convention | — | ✅ | ❌ | _exclusive: ruby (Rails)_ |
| then-without-catch | ✅ | — | — | _exclusive: typescript/JavaScript_ |
| tautological-assertion | ✅ | ❌ | — | Ruby arguably should have a tautological-assertion check: RSpec/minitest can write the same dead self-comparison (`expect(true).to eq(true)`, `assert_equal 1, 1`) and the rest of the test-quality suite already covers Ruby. The TS branch is… |
| test-quality suite (skipped-test, real-sleep-in-test, random-in-test,… | ✅ | ✅ | ❌ | Whole test-quality pass is off for Python (gate at lint_engine.py:3247 lists only typescript/ruby). pytest/unittest tests can be skipped (@pytest.mark.skip), sleep (time.sleep), use random, or assert nothing — the same smells the rule mode… |

### 8. Import & cross-file-importer lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| import-preference-violation (banned/preferred import enforcement) | ✅ | ✅ | ✅ | Python has no constant/usage fallback equivalent to Ruby's _ruby_module_in_use constant-path branch (lint_engine.py:2247-2249): a discouraged module used transitively without an explicit import statement is invisible. Minor, since Python (… |
| import-preference: string-embedded-import false-positive guard | ✅ | ❌ | ❌ | Ruby and Python both lack the false-positive protection TS has for this exact rule. A competing import quoted inside a docstring, heredoc, or example string falsely flags. The guard (_blank_string_embedded_imports) is TS-coupled (uses _TS_… |
| import-preference inline-ignore directive (// /  # chameleon-ignore) | ✅ | ✅ | ⚠️ | Python is routed through the TS string stripper (_TS_STRING, which knows `"`/`'`/backtick, not Python triple-quotes or f/r/b prefixes). It only blanks Python docstrings incidentally (a `"""` degenerates into `""` + `"..."`), and backtick i… |
| phantom-import (relative import resolves to no file on disk) | ✅ | ✅ | ❌ | Python relative imports (`from .models import X`, `from ..pkg import y`) are resolvable on disk and a phantom check is buildable (a `.py`/`__init__.py` suffix probe parallel to _RUBY_SUFFIXES, mapping dotted relative levels to parent dirs)… |
| phantom-symbol (named binding not exported by resolved module) | ✅ | ❌ | ❌ | Ruby lacks a stable exported-symbol surface (constants/methods are open via metaprogramming/autoload), so none is defensible there. Python DOES have an enumerable importable surface (top-level defs/classes/assignments, and `__all__`); a ph… |
| cross-file-importers (blast-radius advisory on rename) | ✅ | ❌ | ❌ | Hard-gated TS-only at phantom_imports.py:923 and structurally backed only by the TS-fed reverse index. Python import graphs (`from mod import name`) are fully enumerable and a reverse index is buildable; Ruby is harder (autoload/constant r… |
| removed-export-breaks-importers (existence break on export removal) | ✅ | ❌ | ❌ | Same gap and same rationale as cross-file-importers: buildable for Python (enumerable `from x import y` references + a Python-fed reverse index), weak for Ruby (autoload). Currently TS-only by both the language gate and the absence of Ruby… |
| tsconfig/jsconfig path-alias resolution (@/* , ~/* aliases) | ✅ | — | — | _exclusive: typescript_ |
| NodeNext/ESM .js->.ts specifier remap in phantom resolution | ✅ | — | — | _exclusive: typescript_ |
| non-code / bundler-query specifier skip in phantom resolution | ✅ | — | ❌ | _exclusive: typescript_ |
| off-pattern counterexample capture (import-preference injection partn… | ✅ | ✅ | ❌ | Clear parity gap (not legitimate exclusivity): import-preference-violation itself fires for Python (verified), so a Python team that teaches 'prefer mylib.http over requests' gets the violation but NEVER the paired counterexample witness t… |

### 9. Test-quality lint

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| test-quality pass language gate | ✅ | ✅ | ❌ | Python is excluded by a hard `language in ("typescript","ruby")` check even though detect_language recognizes .py. A pytest test file (correctly archetype-named "test" when under tests/) never reaches any test-quality rule. For parity Pyth… |
| skipped-test | ✅ | ✅ | ❌ | No Python equivalent. pytest's @pytest.mark.skip / @pytest.mark.skipif / @pytest.mark.xfail / pytest.skip()/unittest @skip are the direct analogues and would carry the same "disabled test asserts nothing" signal; Python gets none. |
| tautological-assertion | ✅ | ❌ | ❌ | Genuinely TS-only in code (guarded by an explicit typescript check), but the concept is not TS-specific: Ruby `expect(true).to eq(true)` and Python `assert True == True` / `assertEqual(1,1)` are equally tautological. Both Ruby and Python l… |
| real-sleep-in-test | ✅ | ✅ | ❌ | No Python equivalent. `time.sleep(<n>)` / `asyncio.sleep(<n>)` in a pytest body is the same anti-pattern; Python gets no rule. |
| random-in-test | ✅ | ✅ | ❌ | No Python equivalent. `random.random()` / `random.randint()` / `os.urandom` / `uuid.uuid4()` / `secrets.*` in a test are the analogues; Python gets none. |
| assertion-free-test | ✅ | ✅ | ❌ | No Python assertion regex family and no Python test-block scanner. Python's bare `assert`, `pytest.raises`, `self.assertEqual/assertTrue/assertRaises`, and `assert ... ==` are recognizable; without them an assertion-free pytest test cannot… |
| unstubbed-network | ✅ | ✅ | ❌ | Token sets cover only TS and Ruby networking + stub libs. Python's requests/httpx/urllib/aiohttp (call tokens) and responses/respx/httpretty/vcrpy (stub tokens) are absent, so even if Python reached the pass this rule could never fire. |
| unfrozen-clock | ✅ | ✅ | ❌ | Freeze tokens (freezegun freeze_time, time-machine) and read tokens (datetime.now/utcnow, time.time, date.today) for Python are absent. Note freeze_time appears but is the Rails/Ruby timecop helper, not necessarily Python's freezegun decor… |
| witness assertion-helper self-calibration | ✅ | ✅ | ❌ | The helper-name heuristic is language-neutral in spirit but wired only into the TS/Ruby assertion path; Python has no witness path because the whole pass is gated off. |
| CHAMELEON_LINT_DIMENSIONS core/full toggle | — | — | — |  |
| test-path detection (_is_test_path) | ✅ | ✅ | ⚠️ | Python recognition is dir-only (catches files under tests/, but a co-located test_foo.py outside a tests dir is treated as source). Needs a Python test-basename pattern: ^test_.*\.py$ / .*_test\.py$ / conftest\.py. |
| candidate-test-path derivation (_candidate_test_paths) | ✅ | ✅ | ❌ | Python produces wrong candidates (pytest uses test_<stem>.py co-located or tests/test_<stem>.py mirrored, NOT <stem>.test.py). So extract_test_pairing_conventions is effectively inert/incorrect for Python — pairing will almost never match … |
| test-pairing convention derivation + advisory | ✅ | ✅ | ⚠️ | Mechanically reachable for Python but functionally broken: the candidate paths it checks (.test.py/.spec.py) don't exist in pytest layouts, so the pairing rate underreports to ~0 and the rule likely drops below the dominance floor (returns… |
| test-archetype naming (_looks_like_test) | ✅ | ✅ | ⚠️ | A Python test under a tests/ directory is correctly archetype-named "test" (so it WOULD satisfy the 3246 startswith gate) — but is then still blocked by the `language in ("typescript","ruby")` clause at 3247. Co-located pytest files (test_… |

### 10. Conventions derivation

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| import conventions (preferred + competing) | ✅ | ✅ | ✅ |  |
| import ordering (external-vs-relative grouping) | ✅ | ✅ | ✅ |  |
| naming: TS prefix conventions (interface/type/enum I-prefix) | ✅ | — | — | _exclusive: typescript_ |
| naming: Ruby casing conventions (method/class/constant casing) | — | ✅ | ❌ | Python SHOULD have casing naming (PEP8: snake_case functions, PascalCase classes, UPPER constants) — the construct exists and is highly conventional, but extract_declarations_from_content has no python branch (returns {} at line 280), so c… |
| naming: file-naming (basename casing + compound suffix) | ✅ | ✅ | ✅ |  |
| inheritance (dominant base class + include mixins) | ❌ | ✅ | ❌ | TS and Python both HAVE base classes (TS `extends`, Python `class Foo(Base)`); a dedicated inheritance section is not derived for them. Mitigated: heritage is partially recovered for both via class_contract (TS `extends`, Python via callab… |
| method_calls (Rails DSL fingerprint) | — | ✅ | — | _exclusive: ruby_ |
| required_guards (controller before_action authz) | — | ✅ | — | _exclusive: ruby_ |
| class_contract (DSL macros / decorators / required methods / base) | ✅ | ✅ | ⚠️ | Python class_contract is weaker than TS/Ruby: (1) base is lost from class_shapes because the dump emits `bases` not `extends`, recovered only if a plain-`method`-kind method exists to carry base_class; (2) Python staticmethod/classmethod m… |
| key_exports (reuse / check-before-creating names) | ✅ | ✅ | ❌ | Python SHOULD have key_exports — top-level def/class names (or __all__) are the obvious reuse signal — but extract_key_exports has no python branch, so the REUSE block is empty for Python repos. |
| body_shape (per-function complexity norms) | ✅ | ✅ | ✅ |  |
| callable_signatures (consensus param shapes) | ✅ | ✅ | ✅ |  |
| error_handling (try/catch vs rescue_from shape) | ✅ | ✅ | ❌ | Python SHOULD have error_handling — try/except is core Python — but the non-ruby branch tests for the C-style `try {` brace which Python never uses, so error_handling is silently always empty for Python. Needs a python branch matching `try… |
| doc_coverage (documented-public-declaration fraction) | ✅ | ✅ | ❌ | Python SHOULD have doc_coverage — docstrings (the line after a def/class) are the canonical Python doc form — but compute_doc_coverage_from_content has no python branch, returning (0,0), so the DOC COVERAGE block is never derived for Pytho… |
| test_pairing (source-to-test pairing rate + mapping) | ✅ | ✅ | ❌ | Python SHOULD have test_pairing — pytest is universal — but _candidate_test_paths emits only TS-shaped candidates for non-ruby langs (foo.test.py, __tests__/...) and _is_test_path doesn't recognize the `test_*.py` / `*_test.py` basename, s… |
| layering (repo-level forbidden cluster edges + import cycles) | ✅ | ✅ | ⚠️ | Python layering depends on whether import_graph.py resolves dotted relative imports (`.foo`, `..pkg`) to file paths the same way it resolves TS path imports; verify in import_graph.py before treating Python layering as full — flagged parti… |

### 11. Cross-file intelligence (symbols / calls / contracts)

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Exports index (named-symbol export set) | ✅ | — | ❌ | Python: Python has explicit module-level exports (top-level def/class, __all__) so an export set IS enumerable, but libcst_dump never emits named_export_names and the build is hard-gated to typescript, so Python gets no exports index. Ruby… |
| Reverse index (importer graph: who imports a name) | ✅ | — | ❌ | Python: `from x import y` is a named import structurally identical to TS's named import — the basis for a reverse index exists — but libcst emits no import_symbols rows and the resolver is TS-suffix-only, so Python has zero importer-graph … |
| phantom-import (relative import target resolves to a file) | ✅ | ✅ | ❌ | Python: relative imports (`from .sibling import x`, `from . import y`) resolve to concrete on-disk files just like TS relative specifiers and Ruby require_relative, so a typo'd/invented relative module is a checkable phantom-import — but l… |
| phantom-symbol (imported name exists in target's exports) | ✅ | — | ❌ | Python: `from x import y` names a specific binding that either exists in x or not — exactly the phantom-symbol case — but it depends on the exports index (not built for Python) and the TS-only symbol-check branch. Ruby: n/a (no named-symbo… |
| cross-file-importers (edit-time blast-radius advisory) | ✅ | — | ❌ | Python: the reverse index would make this computable for Python imports, but neither the reverse index nor the TS-only lint branch exists for Python. Ruby: n/a. |
| removed-export-breaks-importers (deterministic existence break) | ✅ | — | ❌ | Python: a removed `def`/`class` that an importer still does `from mod import name` on is a deterministic break in Python too — but it rides on the (absent) reverse index + exports set. Ruby: n/a (constants autoload, no named-import existen… |
| Calls index — same_file grade (file-local caller edges) | ✅ | ✅ | ✅ |  |
| Calls index — import grade (cross-file named/namespace-import call ed… | ✅ | — | ❌ | Python: `from mod import fn; fn()` and `import mod; mod.fn()` are exactly the named/namespace-import call the TS import grade captures, and libcst already emits the `member` call site — but the grade is gated to typescript only, so Python … |
| Calls index — constant_receiver grade (Ruby Const.method edges) | — | ✅ | — | _exclusive: ruby_ |
| get_callers / get_drift caller facts (tool read over calls index) | ✅ | ✅ | ⚠️ | Python: the tool works but is starved — only same_file edges exist for Python, so the cross-file caller facts (the whole point of the judge's reverse-caller grounding) are empty for Python. Lift requires extending the import grade to Pytho… |
| Callable signatures index (per-symbol params/return/span) | ✅ | ⚠️ | ⚠️ | Python: Python supports type annotations (`def f(x: int) -> str:`) that libcst can read, but libcst_dump emits no param `type` or `return_type`, so Python signature rows are param-shape + span only — strictly weaker than they could be. Rub… |
| Forward definition hydration (definitions of imported symbols for the… | ✅ | ❌ | ❌ | Python: Python named imports could be resolved to defining files and hydrated (the signature index already holds Python signatures), but _parse_import_symbols is TS-extension-gated and uses the TS resolver, so Python gets no forward hydrat… |
| Nearby-collaborator signatures (per-edit, experimental) | ✅ | ✅ | ✅ | Python renders param shapes but no types/returns (none stored), so the rendered signature is thinner than TS's typed one — same limitation as the underlying signature index, not a separate gap. Default-OFF for all three pending an A/B. |
| Signature contract-diff / contract-breaks (narrowed positional contra… | ✅ | ✅ | ❌ | Python: Python has required positional params and the diff logic (_required_positional_count, _POSITIONAL_KINDS) would work on libcst's param kinds (positional/optional/keyword/keyword_rest/rest) — and libcst emits start/end spans for re-p… |
| Function catalog + duplication-candidate prefilter | ✅ | ✅ | ✅ | Python: full on name-token + arity + exact body_hash. Minor: the param-normalized body hash (body_hash_pnorm) skips block/closure-param renaming for Python because _lang_from_path returns None for .py (function_catalog.py:200-207 handles o… |
| Doctor advisory-emission health check (source-edit attribution) | ✅ | ✅ | ❌ | Python: a Python repo where archetype resolution silently breaks gets no doctor 'advisories not firing' warning, because .py is excluded from _source_exts in the doctor check. Low-severity diagnostic-only gap, but a real per-language incon… |

### 12. Framework awareness

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Framework role/archetype detection | ⚠️ | ✅ | ✅ | TypeScript has no framework role table (no Next.js/NestJS/Remix role recognition); files are bucketed by directory only, so a Next.js app/route.ts and a util get the same treatment. TS arguably should have role priors like the other two. |
| Framework family classification (Rails vs Django vs Flask vs FastAPI) | ❌ | ⚠️ | ⚠️ | No language ever resolves a single framework name. Python lumps Django+DRF+Flask+FastAPI into one role table and Rails is the only one with an explicit project marker. A discrete framework classification (used to gate framework-specific ru… |
| Stored framework tag in profile | ❌ | ❌ | ❌ | Framework awareness is always re-inferred from path/role predicates at use-time, never stored, for all three languages. A persisted framework tag would let downstream checks gate on framework without re-running the path heuristics; none ex… |
| Hybrid frontend handling (language_hint envelope) | ✅ | ✅ | ❌ | Python has no hybrid-frontend handling. Django+React, FastAPI+Vue, and Flask+JS repos are extremely common, yet a Python-primary repo with a JS sidecar (or a TS-primary repo with a Python backend) gets no language_hint and no second-bootst… |
| Companion-artifact co-change rules (framework pairings) | ⚠️ | ✅ | ❌ | Django's model->migration (makemigrations) is a near-mandatory pairing yet has no CoChangeRule, and Python is double-excluded: _normalize_language returns None so Python files never reach the rule loop at all. A cochange-model-migration an… |
| Test-runner command recognition | ✅ | ✅ | ✅ |  |
| Stale-test / test-pairing advisory eligibility | ✅ | ✅ | ❌ | Python source files get no stale-test advisory because _normalize_language has no python branch, even though test_pairing IS derived for Python at bootstrap. Adding python to _normalize_language (and Python test-path candidate derivation) … |
| Authz / required-guard convention (before_action) | ❌ | ✅ | ❌ | _exclusive: ruby_ |
| Authz-base-class exemption (_RAILS_APP_ROOT_BASES) | — | ✅ | — | _exclusive: ruby_ |
| Inheritance-convention derivation (dominant_base / known_bases) | ❌ | ✅ | ❌ | Python has NO inheritance-convention derivation at all, which compounds the class_contract base-capture gap below (Ruby has the inheritance section as an independent base signal; Python has neither). A Django models.Model / DRF APIView / F… |
| Class-contract decorator/base recognition (framework heritage) | ✅ | ✅ | ⚠️ | Python decorator + required-method contracts work (DRF @action, @api_view, FastAPI route decorators, @dataclass recognized), but the class_shapes base anchor never fills for Python (key mismatch "bases" vs "extends"); base only survives vi… |

### 13. Teach / idioms / counterexamples / class contracts

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| teach_profile (free-form idiom capture) | ✅ | ✅ | ✅ |  |
| teach_profile_structured (structured idiom capture) | ✅ | ✅ | ✅ |  |
| teach_competing_import (wrapper-preference convention) | ✅ | ✅ | ✅ |  |
| unteach_competing_import | ✅ | ✅ | ✅ |  |
| Per-edit counterexample capture (build counterexamples.json from a re… | ✅ | ✅ | ❌ | Python: counterexample capture is a complete no-op. teach_competing_import for a Python repo writes the convention fine but the off-pattern is never captured, so the per-edit negative example never fires. To reach parity _import_of needs a… |
| Per-edit counterexample render ('do NOT write it this way' paired wit… | ✅ | ✅ | ❌ | Python renders nothing not because of a render gate but because capture stored nothing. Fixing the capture regex fixes render automatically; the render side itself needs no Python work. |
| Multi-off-pattern-per-archetype counterexamples (schema v2 list) | ✅ | ✅ | ❌ | Same root cause as capture: unreachable for Python until _import_of matches Python import syntax. |
| Class-body-contract derivation: DSL macros | — | ✅ | — | _exclusive: ruby_ |
| Class-body-contract derivation: class decorators | ✅ | — | ✅ |  |
| Class-body-contract derivation: required methods | ✅ | ✅ | ✅ | Python's libcst_dump emits kind 'staticmethod'/'classmethod' for decorated methods, which are NOT in _CONTRACT_METHOD_KINDS={method,singleton_method} (conventions.py:1006), so a Python class whose recurring members are all @staticmethod/@c… |
| Class-body-contract derivation: base class annotation | ✅ | ✅ | ⚠️ | Python-only delta: a decorator-anchored, method-less class loses its base annotation because _collect_contract_classes reads class_shapes['extends'] but libcst emits 'bases'. The fix is one line: read shape.get('extends') or (shape.get('ba… |
| Class-contract used as a base anchor for the cohort | ✅ | ✅ | ⚠️ | Same root cause as the base-annotation row (bases-vs-extends key mismatch). A Python archetype anchored purely on a shared base but with method-less classes (e.g. all-field Django models) won't form a base-anchored cohort; a decorator anch… |
| Standalone inheritance convention (dominant_base / known_bases sectio… | ❌ | ✅ | ❌ | _exclusive: ruby_ |
| Idiom novelty/coverage: covered-by-principle / naming / competing-imp… | ✅ | ✅ | ✅ |  |
| Idiom novelty/coverage: covered-by-inheritance dedup | ❌ | ✅ | ❌ | Asymmetry: a 'must inherit from BaseService' idiom that Ruby dedups as covered stays 'novel' for TS/Python, letting a redundant idiom land. To reach parity the dedup should also consult class_contract.base for non-Ruby languages (idiom_cov… |
| Idiom novelty/coverage: covered-by-class-contract content (DSL/requir… | ⚠️ | ✅ | ⚠️ | TS/Python class_contract data exists and is loaded into the coverage map (build_coverage:629), but the covered-reason that would consume it is dead for non-Ruby because it is nested under the Ruby-only inheritance branch. |
| Idiom merge (3-way union of idioms.md by slug/section) | ✅ | ✅ | ✅ |  |

### 14. Enforcement, block-eligibility & calibration

| Capability | TS | Ruby | Py | Notes |
|---|:--:|:--:|:--:|---|
| Block-eligible rule set (which rules may ever block) | ✅ | ✅ | ⚠️ | Python ends up with effectively 3 working block rules (import-preference, secret, eval) out of 8, vs TS ~5 and Ruby ~6, partly by design (jsx/inheritance) but partly from two inert-for-Python rules that the capability table still treats as… |
| phantom-import block rule | ✅ | ✅ | ❌ | Real gap: phantom-import is the flagship deterministic safety rule and Python repos (relative imports, missing modules) would benefit, yet it never fires for .py. Worse, because BLOCK_RULE_LANGUAGES says None (not {typescript,ruby}), calib… |
| import-preference-violation block rule | ✅ | ✅ | ✅ |  |
| jsx-presence-mismatch block rule | ✅ | — | — | _exclusive: typescript_ |
| naming-convention-violation block rule | ✅ | ✅ | ❌ | Arguable gap: Python has strong naming conventions (PEP8 snake_case functions, PascalCase classes, UPPER constants) that the rule shape could enforce, and the language is correctly excluded today only because no derivation+lint branch was … |
| inheritance-convention-violation block rule | ❌ | ✅ | — | _exclusive: ruby_ |
| file-naming-convention-violation block rule | ✅ | ✅ | ❌ | Concrete wiring bug, highest-confidence Python gap in this dimension: file_naming is derived for Python archetypes and the rule advertises itself language-independent (None), so calibration computes lang_ok=True and a vacuous 0.0 fp_rate (… |
| secret-detected-in-content block rule (kind-gated hard-block) | ✅ | ✅ | ✅ |  |
| eval-call block rule (deterministic dangerous sink) | ✅ | ✅ | ✅ |  |
| Calibration language allowlist (which profiles calibrate at all) | ✅ | ✅ | ✅ | Allowlist parity is correct — Python is a first-class calibration language. The downstream gaps (phantom-import, file-naming reporting active-but-inert for Python) are not caused by this allowlist; they are caused by those two rules being … |
| Override-feedback demotion / SECURITY_BLOCK_RULES exemption | ✅ | ✅ | ✅ |  |
| Inline chameleon-ignore directive (block override) + comment syntax | ✅ | ✅ | ✅ | Minor: _blank_string_literals (violation_class.py:64-98) has explicit ruby and TS branches but falls Python through the `_TS_STRING.sub` else path. Python single/double/triple-quoted strings differ from TS (triple-quote docstrings, no back… |

## Legitimate exclusives (the "except")

These are intentionally one-language: the capability only makes sense where its
language or framework provides the construct. Not gaps.

### TypeScript / JavaScript

- **ParsedFile.has_jsx** — Whether the file contains JSX/TSX elements.
- **class_shapes.implements (TS interfaces)** — Implemented-interface names on a class.
- **ClusterKey tuple: jsx_present** — Whether the file contains JSX/TSX, seventh tuple component.
- **TS/JS prior table (_TS_PRIORS)** — By far the largest vocabulary of the three tables; many names here (component, hook, layout, provider, context, route handlers) have no Ruby/Python prior equivalent because the underlying frameworks differ.
- **Workspace-prefix stripping (_strip_workspace_prefix)** — n/a for Ruby and Python: their detectors are not root-anchored. _has_dir_chain (:339-356) scans every segment offset and python_role_for_path scans the basename plus all parent dirs (signatures.py:201-203), so a role si…
- **jsx-presence-mismatch** — Correctly n/a: JSX is a TS/JS construct. Because both Ruby and Python witnesses and candidates are always jsx_present=False, recalibrate_ast_query (lint_engine.py:130) sets the expectation to False and lint's branches a…
- **then-without-catch** — Promise `.then()/.catch()` is a JS/TS-specific async construct. Ruby and Python have no equivalent thenable chain (Python asyncio uses await/try, Ruby uses blocks/begin-rescue), so genuinely n/a — no parity expectation.
- **tsconfig/jsconfig path-alias resolution (@/* , ~/* aliases)** — Resolves non-relative aliased import specifiers through nearest-tsconfig `paths`, treating a declared-but-unresolved alias as resolved (generated/build dirs).
- **NodeNext/ESM .js->.ts specifier remap in phantom resolution** — A `.js`/`.jsx`/`.mjs`/`.cjs` specifier is probed against the corresponding `.ts`/`.tsx`/`.mts`/`.cts` source on disk before flagging as phantom.
- **non-code / bundler-query specifier skip in phantom resolution** — Skips non-code extensions (css, svg, png, md, graphql, etc.) and strips vite/webpack `?react`/`?url`/`#frag` suffixes so asset imports never flag as phantom.
- **naming: TS prefix conventions (interface/type/enum I-prefix)** — Dominant single-letter prefix on interface/type/enum declaration names.
- **jsx-presence-mismatch block rule** — Errors when a file HAS JSX but its archetype is a non-JSX one (severity-gated: only the error 'has JSX' form blocks, the warning 'missing JSX' form stays advisory).

### Ruby / Rails

- **extras.class_body_calls (receiverless DSL macros)** — Class-body DSL macros are the Ruby/Rails pattern (ActiveInteraction, validates, has_many); TS/Python express the same intent differently (decorators, base classes), which class_shapes.decorators / class_shapes already c…
- **Rails prior table (_RAILS_PRIORS)** — Directory-chain prior table mapping app/controllers, app/models, app/services, db/migrate, config/initializers etc. to clean archetype names (controller, model, service, job, mailer, helper, policy, serializer, presente…
- **eval-call (Ruby string-arg *_eval)** — Flags instance_eval/class_eval/module_eval when the argument is a string/heredoc literal (block forms are exempt); warning severity.
- **eval-call (Ruby send(:eval))** — Flags send/public_send dynamically dispatching to :eval as the same arbitrary-code sink; error severity.
- **sql-string-interpolation** — Scoped to ActiveRecord's #{}-into-SQL shape, which is a Rails-specific injection idiom. A general string-built-SQL detector for TS (knex/template-literal queries) or Python (f-string/`%`-formatted cursor.execute) would …
- **Line-length AllowedPatterns / AllowedURI exemption** — ruff/flake8 do support per-line noqa and pycodestyle has noqa/URL leniency conventions, but this specific exemption shape (config-declared AllowedPatterns + AllowedURI) is a rubocop construct. If Python line-length is e…
- **rubocop path Exclude (AllCops + per-cop)** — This is legitimately rubocop-specific (it models rubocop's own Exclude glob semantics). TS arguably lacks a .prettierignore/.eslintignore equivalent path filter, but that is a separate TS gap, not a Python one. Python's…
- **inheritance-convention-violation** — Python uses `class X(Base):` inheritance heavily (Django models -> models.Model, DRF views -> APIView/GenericViewSet, Flask/SQLAlchemy base classes) — a genuine archetype-dominant-base convention exists and is not model…
- **required-guard-convention** — Tightly bound to the Rails ActionController before_action callback idiom, which has no direct cross-language analog. Django middleware/decorators (@login_required, permission_classes) and Flask before_request are concep…
- **method_calls (Rails DSL fingerprint)** — Legitimately Rails-specific. Python's analog (route/validation decorators, Django model Meta) flows through class_contract decorators instead, so this is not a true parity gap.
- **required_guards (controller before_action authz)** — Legitimately Rails-specific (before_action callbacks). No equivalent derived for TS/Python and none is idiomatic there.
- **Calls index — constant_receiver grade (Ruby Const.method edges)** — Caller->callee edge where Const.method or Const.new dispatches to a singleton/instance method of a uniquely-defined fully-qualified class.
- **Authz / required-guard convention (before_action)** — Rails before_action authz is legitimately Ruby-specific, but the SEMANTIC (a base-class/decorator that enforces auth on every member) has direct Python analogs: DRF permission_classes, a LoginRequiredMixin base, or a Fa…
- **Authz-base-class exemption (_RAILS_APP_ROOT_BASES)** — n/a for TS/Python because neither has an inheritance-convention check to exempt roots from (see next row). Not a gap on its own; it is downstream of the missing inheritance derivation.
- **Class-body-contract derivation: DSL macros** — Derives the repo-specific class-body DSL macros (e.g. Rails acts_as_*, has_many beyond the allowlist) shared across a cohort anchored on a dominant base.
- **Standalone inheritance convention (dominant_base / known_bases section)** — Intentional for the lint (comment at 1819-1821 notes running it on TS emits bogus lines), but it has a downstream parity cost on idiom dedup — see the idiom-coverage row. TS/Python heritage is partly carried by class_co…
- **inheritance-convention-violation block rule** — TS shows 'none' rather than 'n/a' because class inheritance exists in TS and a sibling rule could in principle exist; but it is legitimately Ruby/Rails-shaped (single-base, ApplicationX roots), so the gap is intentional…

### Python

- **Python role-based path bucketing (python_role_for_path)** — Legitimately Python-exclusive: Rails encodes role in the directory chain (app/models/) which the directory bucket already captures, and TS has no equivalent filename-as-role convention. No parity gap.
- **Python role table (_PY_ROLE_NAMES / _python_prior_match)** — Django/DRF/Flask/FastAPI roles are filename-encoded (models.py), unlike Rails' directory-encoded roles, so the mechanism is fundamentally filename-driven, not chain-driven.
- **Async/Del/Try kind normalization (extractor-vs-bootstrap agreement)** — Python-specific because only Python uses two different parsers across the hot path (stdlib ast) and bootstrap (libcst) whose node vocabularies diverge on async/star forms. TS (ts_dump.mjs both sides via signature recali…
- **eval-call (Python exec())** — Legitimately Python-specific: TS has no bare code-eval exec, and Ruby's exec is a shell-process call (a different command-injection sink), so no parity gap on this rule itself.

## Parity gaps & roadmap

Verified against the code (each was adversarially re-checked). Grouped by
dimension below; the priority tiers are the suggested order to perfect parity.

### Priority tiers

**P0 — wiring bugs (silently wrong today, fix first):**

- **`class_shapes` `bases` vs `extends` key mismatch** — the libcst dump emits a class's bases under `bases` (a list), but `conventions.py:1038` reads `extends` (TS's string key), so a Python class's base is dropped from its class-contract unless a plain instance method happens to carry it. One-line fix: read `shape.get("extends") or (shape.get("bases") or [None])[0]`.
- **Vacuous-active calibration footgun** — `phantom-import` and `file-naming-convention-violation` are declared language-independent (`None`) in `BLOCK_RULE_LANGUAGES`, but neither lint ever fires for `.py`. Calibration therefore certifies them "active" at `fp_rate 0.0` for a Python profile while they can never actually flag a Python file. Fix: either add Python branches, or scope those rules to `{typescript, ruby}` so they honestly report inert.
- **`file-naming-convention-violation` derived but not enforced for Python** — the convention is derived for `.py` archetypes, but the edit-time gate at `lint_engine.py:2829` omits `_PY_EXTENSIONS`, so it is dead. One-line fix.
- **Counterexample capture is a no-op for Python** — `teach_competing_import` writes the convention and the import-preference lint fires, but `_import_of`'s quoted-specifier assumption never matches Python `import x` / `from x import y`, so the per-edit "do NOT write it this way" witness never appears.

**P1 — high-value Python feature parity:**

- **Cross-file intelligence** — exports index, reverse (importer) index, phantom-import, phantom-symbol, cross-file-importers, removed-export-breaks, calls-index import grade, forward definition hydration, signature contract-diff. All TS-only today; Python's `from x import y` is the direct analog and unlocks all of them.
- **Test-quality suite** — entirely off for Python; pytest skip/sleep/random/assertion-free/network/clock + correct `test_*.py` / `*_test.py` / `conftest.py` test-path detection and pairing.
- **Style baseline** — entirely off for Python; read black / ruff / flake8 config (pure TOML/INI, no repo-code exec) for line-length, quote, indent.
- **`naming-convention-violation`** — PEP 8 snake_case functions, PascalCase classes, SCREAMING_SNAKE constants (mirror the Ruby casing path).
- **Conventions** — `doc_coverage` (docstrings), `error_handling` (`try:`), `key_exports` (top-level public names) all have no Python branch.

**P2 — framework awareness & richness:**

- Django `models.py` → `migrations/*.py` co-change rule; hybrid frontend (Django+React / FastAPI+Vue) `language_hint`; stale-test advisory eligibility; Python inheritance-convention (Django `models.Model`, DRF `APIView`).
- Signature richness: emit Python `return_type` + param type annotations (libcst exposes them).

**P3 — cross-language security & TypeScript hardening:**

- Sinks missing in **both** Ruby and Python: command-injection (`os.system`/`subprocess(shell=True)`; Ruby backticks/`system`), insecure-deserialization (`pickle`/`yaml.load`; Ruby `Marshal.load`), insecure-random. `weak-hash` is a one-line gate-widen for Python.
- TypeScript: scrub `NODE_OPTIONS` at the extractor boundary (Ruby/Python already scrub their interpreter-option vars); attach per-method decorators / `enclosing_class_path` / `base_class` to TS callable signatures; add a TS framework role table (Next.js/NestJS).

### Verified gaps by dimension

#### 1. AST extraction & language detection

- **Subprocess hardening (env scrub + neutral cwd)** (TS ⚠️ · Ruby ✅ · Py ✅) — Close the gap (low-severity parity/consistency hardening, not an RCE fix — the untrusted repo cannot set the MCP server's NODE_OPTIONS; the Ruby/Python comments correctly call this "hardening, not a live hole"). Add one line after env=os.environ.copy() in typescript.py (~line 351): env.pop("NODE_OPTIONS", None). This …
- **callable_signatures.return_type (declared annotation)** (TS ✅ · Ruby — · Py ⚠️) — Close the gap (small, additive, no downstream change). In libcst_dump.py `_enter_function`, read `node.returns` (the `-> T` annotation) and emit its text into the signature dict, e.g.: `return_type = cst.Module([]).code_for_node(node.returns.annotation).strip() if node.returns is not None else None` wrapped in try/exc…
- **callable_signatures/param declared type annotation** (TS ✅ · Ruby — · Py ❌) — Close it for Python (true TS-vs-Python parity gap, not a language limitation). Ruby n/a stands (untyped language, no annotation node). For Python: in libcst_dump.py::_param_shapes add `shape["type"] = code_for_node(p.annotation.annotation)` when `p.annotation is not None` (best-effort declared text, mirroring TS getTe…
- **callable_signatures.decorators** (TS ❌ · Ruby — · Py ✅) — Gap is real for TS (TS=none vs Python=full); Ruby's n/a is a genuine language exclusive. The TS leg is NOT a legitimate exclusive — TS has first-class method/class decorators (NestJS @Get/@Injectable, Angular, TypeORM @Entity, class-validator). Fix is a one-line symmetric change: add `decorators: decoratorsOf(node),` …
- **callable_signatures.enclosing_class_path (qualified)** (TS ❌ · Ruby ✅ · Py ✅) — No action required today, but NOT because it is a legitimate exclusive. The gap is real (TS emits no enclosing_class_path; verified three ways) and the capability is NOT language-specific (Python implements the identical field, and TS has class-callable static methods like Cls.staticMethod() that a constant-receiver-s…
- **callable_signatures.base_class** (TS ❌ · Ruby ✅ · Py ✅) — Close the gap — small localized thread-through, not new extraction. The functional loss is narrow: only the `overrides_base` advisory hint in `_callable_signature_index` (conventions.py:1731-1761) is unavailable for TS. The class-contract `base` is already at parity for TS via class_shapes.extends (conventions.py:1038…
- **extras.class_shapes (per-class base + decorators)** (TS ✅ · Ruby ❌ · Py ⚠️) — Real, fixable parity gap — close it. Python's class_shapes carries the base under key "bases" (a list), but the sole consumer conventions.py:1038 reads "extends" (TS's string key), so Python's base is silently dropped for any class without an instance method (Django models, Pydantic/SQLAlchemy declarative models, DRF …
- **extras.import_symbols (named-import binding rows)** (TS ✅ · Ruby ❌ · Py ❌) — Close the gap for Python only; leave Ruby as a legitimate exclusive. Python from-imports create real named bindings (`from m import a`, `from m import a as b`) on a known line -- the exact {name,local,module,line} shape collectImportSymbols produces for TS -- and libcst_dump.py:113-120 already walks ImportFrom and com…
- **extras.namespace_imports (import * as alias)** (TS ✅ · Ruby ❌ · Py ❌) — Split by language. Ruby = no action - legitimate exclusive: `import * as alias` has no Ruby analogue, and Ruby reaches the same cross-file call-edge goal through the constant-receiver path (calls_index.py:301-332), so it is at parity by a different, idiomatic mechanism. Python = real gap, build it: Python's idiomatic …
- **extras.named_export_names + export_set_open (phantom-symbol/exports index)** (TS ✅ · Ruby ❌ · Py ❌) — Close for Python; leave Ruby as legitimate near-exclusive. PYTHON is a real gap: `from .mod import name` is the direct static analog of TS `import { name } from './mod'`, and a hallucinated `from .utils import compute_thing` is the identical failure class the TS phantom-symbol check catches. Dynamism (__all__, star re…

#### 2. Archetype clustering & cluster signature

- **ClusterKey tuple: import_module_set_hash (hash_import_set)** (TS ❌ · Ruby ❌ · Py ❌) — No action for parity — the three languages are at perfect parity (all "none"), and this is NOT a language/framework exclusive but a deliberate, documented design decision uniform across TS/Ruby/Python. The import-module-set hash was intentionally removed from the live cluster key because including the exact import set…

#### 3. Archetype naming & framework priors

- **Test cluster detection (_looks_like_test)** (TS ✅ · Ruby ✅ · Py ⚠️) — Real Python parity gap, close it. Python tests are detected ONLY when under a tests/test/spec directory (signal a/c fires on the dir token). When colocated by filename — pytest `test_views.py` (prefix), `*_test.py` (suffix), Django startapp `myapp/tests.py`, `conftest.py` — all three signals miss: no test dir token (a…
- **AST-shape fallback (jsx component / class)** (TS ✅ · Ruby ✅ · Py ❌) — Close it with a one-line fix: add "ClassDef" to the is_class_default set at naming.py:596-600. Python clusters always have not jsx_present (libcst_dump.py:377), so line 679 would then name a single-top-level-class Python cluster "class"/"class-<suffix>" exactly as Ruby gets via ClassNode/ModuleNode. Do NOT add "Functi…

#### 5. Security lint (sinks & secrets)

- **weak-hash** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap with a one-line change: add "python" to the tuple at lint_engine.py:1586 (`if language in ("typescript", "ruby", "python")`). No new regex or helper needed — `_WEAK_HASH_RE`, `_sink_security_context`, and `_strip_python_strings_and_comments` already exist and were proven to fire on idiomatic `hashlib.md5…
- **insecure-random** (TS ✅ · Ruby ❌ · Py ❌) — Close the parity gap for both Ruby and Python (low priority — advisory-only warning, never block-eligible, so it is a missing nudge, not a missing block). The security concern (non-crypto PRNG in a token/salt/nonce context) is language-general; only the `Math.random(` regex is TS-specific syntax. Build: (1) Ruby — a `…
- **command-injection sink (os.system / subprocess shell=True)** (TS — · Ruby ❌ · Py ❌) — Close the gap (low effort, all three languages). The cited per-language statuses are correct except TS — TS should be `none`, not `n/a`: JS/Node has a real, applicable command-exec sink (child_process.exec/execSync(cmd), the canonical Node shell-injection vector), so the capability applies to TS just as much as to Pyt…
- **insecure-deserialization sink (pickle / yaml.load)** (TS — · Ruby ❌ · Py ❌) — Gap is real and spans all THREE languages, not two - correct the claim's TS=n/a to TS=none. The claim marks Ruby=none (applicable-but-unimplemented) on the strength of library/stdlib sinks (Marshal.load, YAML.load); by that same standard JS/TS has direct library analogs (js-yaml v3 load() = the literal "yaml.load with…

#### 6. Style lint (indent / quote / line-length)

- **scan_style_rules language gate** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap, but as a two-part change — widening the gate alone yields almost nothing. (1) Widen lint_engine.py:2032 to include "python" and wire the python stripper: in the language branch (2056-2061) add an elif python that uses _strip_python_strings_and_comments (already built, used by scan_dangerous_sinks) and a…
- **Indent style/width rule** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap — it is a true parity miss, not a legitimate exclusive. The shared .editorconfig fallback in _declared_indent/_declared_max_line_length is language-neutral, so a Python repo with .editorconfig indent_style/indent_size/max_line_length is silently skipped while an identical TS/Ruby repo gets findings; Pyth…
- **Quote style rule** (TS ✅ · Ruby ✅ · Py ❌) — Build it — close the parity gap (the whole style baseline is gated off Python, not just quote). Steps: (1) in bootstrap/tool_config.py, parse `[tool.ruff.format] quote-style` (single\|double\|preserve) and `[tool.black] skip-string-normalization` from pyproject.toml into a Python formatting section; (2) add a `python`…
- **Max line length rule** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap — it is a true parity miss, not a legitimate exclusive. Max line length is a universal formatter concern and Python has standard declared sources directly analogous to prettier printWidth / rubocop Layout/LineLength.Max: ruff `line-length` ([tool.ruff] in pyproject.toml), black `line-length` ([tool.black…
- **Formatter-config source: per-language reader at bootstrap** (TS ✅ · Ruby ✅ · Py ❌) — Build it — true parity gap, not an exclusive. black/ruff/flake8 are the direct Python analogs of prettier/rubocop, and Python repos get ZERO indent/quote/line-length style-baseline feedback today (scan_style_rules:2032 excludes Python before the editorconfig fallback even runs). Three sites, in order: (1) tool_config.…
- **Per-file emission cap + summary tail** (TS ✅ · Ruby ✅ · Py ❌) — Build Python declared-formatter-config style scanning in scan_style_rules; the per-file cap + summary tail come free once the scan runs for Python (the cap CANNOT be built independently — it is the shared tail of the function). Three steps: (1) add "python" to the :2032 gate — this alone enables the existing language-…

#### 7. Naming & inheritance lint

- **naming-convention-violation (identifier naming)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap -- true parity gap, not a legitimate exclusive. PEP 8 mandates exactly the dimensions Ruby already enforces: snake_case functions/methods, PascalCase (CapWords) classes, SCREAMING_SNAKE constants. Python's omission from BLOCK_RULE_LANGUAGES is a downstream consequence of the derivation gap (violation_cla…
- **naming-convention-violation block-eligibility (per-repo calibration gate)** (TS ✅ · Ruby ✅ · Py ❌) — Build Python naming-convention-violation parity, modeling on the Ruby path (NOT the TS interface_prefix path, which is genuinely TS-specific): (1) Add a Python casing branch to extract_declarations_from_content (conventions.py:280, the current empty return) emitting function/method names (snake_case), class names (Pas…
- **file-naming-convention-violation** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap. The gate's own comment (lint_engine.py:2825-2828) states its purpose: judge "only files of a profiled language," skipping Makefile/README/config. Python IS now a profiled language, so excluding .py contradicts the gate's stated intent — this is the gate not being updated when Python landed, not a delibe…
- **tautological-assertion** (TS ✅ · Ruby ❌ · Py —) — Close the gap. The cited Python status "n/a" is wrong: a Python file under tests/ gets a test/spec archetype (signatures.py:176, conventions.py:454), so the rule applies — correct Python to `none`. Add a self-comparing-assertion regex per language inside _test_quality_violations and widen the call-site gate at lint_en…
- **test-quality suite (skipped-test, real-sleep-in-test, random-in-test, assertion…** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap: add a full Python branch to the test-quality suite. Do NOT just add "python" to the gate at lint_engine.py:3247 -- the if/else in _test_quality_violations (2585-2594) and _assertion_free_violations (2679-2686) treats `else` as Ruby, so a bare gate flip routes Python into Ruby regexes that go silently in…

#### 8. Import & cross-file-importer lint

- **import-preference: string-embedded-import false-positive guard** (TS ✅ · Ruby ❌ · Py ❌) — Close the gap. It's a false-positive bug, not a legitimate exclusive — a code snippet stored in a string is not a real competing import in any of the three languages. Fixes are per-language and differ in shape: (1) PYTHON (one-line, reuse-only): at lint_engine.py:3079 extract specs from _strip_python_strings_and_comme…
- **import-preference inline-ignore directive (// /  # chameleon-ignore)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap. Add a Python branch to `_blank_string_literals` (violation_class.py:64-98) that blanks string literals ONLY (preserve comments — directives live in comments). Do NOT call the existing `_strip_python_strings_and_comments` / `_PY_STRING_OR_COMMENT`: that regex includes the `\#[^\n]*` line-comment alternat…
- **phantom-import (relative import resolves to no file on disk)** (TS ✅ · Ruby ✅ · Py ❌) — Build a Python branch — TS=full, Ruby=full, Python=none is a real, non-legitimate parity gap. Python has first-class relative imports (`from .mod import x`, `from ..pkg import y`) that map to disk exactly like TS relative specifiers and Ruby require_relative, and the same LLM-hallucination surface (inventing `from .ut…
- **phantom-symbol (named binding not exported by resolved module)** (TS ✅ · Ruby ❌ · Py ❌) — Close as two separate builds, not one. Ruby (highest value, genuine parity gap): teach prism_dump.rb to emit a per-file named-export set (top-level constants, module/class names, and public def names the file defines) into a `named_export_names`/`export_set_open`-style extra; have extractors/ruby.py forward it into th…
- **cross-file-importers (blast-radius advisory on rename)** (TS ✅ · Ruby ❌ · Py ❌) — Close the Python gap; flag Ruby as a mechanism-mismatch with a Ruby-shaped equivalent. PYTHON (true closeable gap, from-import is the direct analog of TS named imports): (1) in libcst_dump.py:_import_specifier (96-121) capture the imported NAME per from-import alias instead of collapsing to (module,'named'); emit impo…
- **removed-export-breaks-importers (existence break on export removal)** (TS ✅ · Ruby ❌ · Py ❌) — Split by language. PYTHON: close the gap — `from module import name` raises ImportError ("cannot import name 'name'") at load time, the identical deterministic existence break TS named imports have, so the capability genuinely applies and was simply not built. Build: (1) emit per-name import rows {name, module, line} …
- **off-pattern counterexample capture (import-preference injection partner)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap — true parity miss, not an exclusive. Import preference ("use the project http wrapper, not raw requests/httpx/urllib") is a real Django/Flask/FastAPI convention and Python's preferred-import side already works; only the counterexample-capture matcher is quote-shaped. Extend _import_of (counterexamples.p…

#### 9. Test-quality lint

- **test-quality pass language gate** (TS ✅ · Ruby ✅ · Py ❌) — Close it — true parity gap, not a legitimate exclusive. All six test-quality rules (skipped-test, tautological-assertion, real-sleep-in-test, random-in-test, assertion-free-test, unstubbed-network, unfrozen-clock) are universal testing anti-patterns with direct Python analogs. Build: (1) widen the lint_engine.py:3247 …
- **skipped-test** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. Python (pytest/unittest) has first-class skip markers with a low-FP regex signature: @pytest.mark.skip / @pytest.mark.skipif / @pytest.mark.xfail, pytest.skip(...), @unittest.skip / @unittest.skipIf / @unittest.skipUnless, @unittest.expectedFailure, and the assertEqual-style assertion vocabulary -- so t…
- **tautological-assertion** (TS ✅ · Ruby ❌ · Py ❌) — Close the gap for both languages; tautological self-comparison is a recognized test smell in RSpec (expect(1).to eq(1)) and pytest/unittest (assert 1 == 1, assert True == True, self.assertEqual(1, 1)), so it is not a TS exclusive. Two unequal lifts. RUBY (cheap): add a _RUBY_TAUTOLOGY_RE for RSpec self-comparison (exp…
- **real-sleep-in-test** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. (1) Add `_PY_REAL_SLEEP_RE` matching `time.sleep(<num>)` and `asyncio.sleep(<num>)` with a `(?<![.\w])`-style boundary like the TS/Ruby patterns. (2) Add "python" to the language gate at lint_engine.py:3247 and add a python branch in `_test_quality_violations` (lint_engine.py:2585-2594) to select the ne…
- **random-in-test** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. Python's random.random/randint/choice, numpy.random.*, and secrets.* (OS-seeded, the direct analog of Ruby SecureRandom) make a test's assertions just as seed-dependent as Math.random, so this is a real parity gap, not a language exclusive. Four touch points, all in lint_engine.py: (1) define _PY_RANDOM…
- **assertion-free-test** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap — true parity gap, not an exclusive (assertion-free testing is universal; pytest/unittest tests need this lint as much as jest/RSpec). Build, in order: (1) add `python` to the gate at lint_engine.py:3247; (2) add `_PY_ASSERTION_RE` covering bare pytest `assert`, unittest camelCase methods (`self.assertEq…
- **unstubbed-network** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap (it is a true parity gap, not an exclusive — network stubbing in tests is universal and Python has mature equivalents: responses, respx, vcrpy, requests-mock, httpretty, aioresponses). Two parts required, do NOT just flip the gate: (1) Add Python tokens — stub: responses, respx, vcr/VCR (vcrpy reuses VCR…
- **unfrozen-clock** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap (three-part change, not one): (1) add Python freeze tokens (freezegun, freeze_time, time_machine, travel) to _CLOCK_FREEZE_TOKENS and Python clock-read tokens (datetime.now, datetime.utcnow, datetime.today, date.today, time.time, time.monotonic) to _CLOCK_READ_TOKENS — substring `in` checks, so these are…
- **witness assertion-helper self-calibration** (TS ✅ · Ruby ✅ · Py ❌) — Close the parity gap. Add a Python arm to the test-quality lint family: (1) `_PY_ASSERTION_RE` covering bare `assert`, `pytest.raises`/`pytest.warns`, and unittest `self.assert*`/`self.assertRaises`; (2) a `_PY_TEST_BLOCK_RE` for pytest functions (`def test_*(`) and unittest methods (`def test_*(self`), with an indent…
- **test-path detection (_is_test_path)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap. (1) Add `_PYTHON_TEST_BASENAME_RE = re.compile(r"(^test_.+\|.+_test)\.py$")` plus an explicit `conftest.py` (and `conftest.pyi`) check, and give `_is_test_path` a `language == "python"` branch (conventions.py:448-452). (2) Add a Python arm to `_candidate_test_paths` (494-509): co-located `test_{stem}.py…
- **candidate-test-path derivation (_candidate_test_paths)** (TS ✅ · Ruby ✅ · Py ❌) — Real parity gap, close it. Add a `language == "python"` branch to _candidate_test_paths (conventions.py:457) emitting the real conventions: co-located test_<stem>.py and <stem>_test.py; mirrored tests/ (and test/) roots producing tests/<...>/test_<stem>.py with source-root (src/app/lib) swap, mirroring the Ruby/TS str…
- **test-pairing convention derivation + advisory** (TS ✅ · Ruby ✅ · Py ⚠️) — Real parity gap, close it. Add a `if language == "python"` branch to both helpers in conventions.py. (1) `_is_test_path` (438): match Python test basenames — `^test_.*\.py$`, `.*_test\.py$`, and `conftest.py` — in addition to the existing test-dir-component check, so co-located `test_foo.py` isn't miscounted as source…
- **test-archetype naming (_looks_like_test)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap with a Python-aware predicate in signal (b) of _looks_like_test, NOT a suffix-list append. Adding "_test.py"/".test.py" to _TEST_FILE_SUFFIXES only catches the minority suffix form; pytest's dominant convention is the test_ PREFIX (test_foo.py), and signal (b) uses str.endswith, which structurally cannot…

#### 10. Conventions derivation

- **naming: Ruby casing conventions (method/class/constant casing)** (TS — · Ruby ✅ · Py ❌) — Real gap, priority = Python. The cited claim is correct that Ruby=full and Python=none, but TS is none not n/a: casing applies to TS as a language (PascalCase classes/camelCase functions are canonical, enforced by @typescript-eslint/naming-convention), yet the TS branch extracts only interface/type/enum for prefix rul…
- **inheritance (dominant base class + include mixins)** (TS ❌ · Ruby ✅ · Py ❌) — Close the part that is a true parity gap, leave the Ruby-exclusive parts alone. (1) Do NOT port the whole inheritance section to Python: `include`-mixin detection (dominant_include) and `::`-namespace base_family grouping are legitimately Ruby/Rails-specific and would emit noise on Python (the design comment at conven…
- **class_contract (DSL macros / decorators / required methods / base)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap with two small additive fixes (no behavior change for TS/Ruby): 1. conventions.py:1038 — fall back to bases for languages whose class_shapes emit `bases` not `extends`: `ext = shape.get('extends') or (shape.get('bases') or [None])[0]`. Recovers the base anchor for field-only Python classes (Pydantic mode…
- **key_exports (reuse / check-before-creating names)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap: add a Python branch to extract_key_exports that reads ParsedFile extras (not regex, matching how extract_callable_signatures and extract_class_contract_conventions already source Python names). Emit only top-level PUBLIC names: top-level classes from class_shapes[].name, top-level functions from callabl…
- **error_handling (try/catch vs rescue_from shape)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap: add a Python branch to extract_error_handling_conventions. Minimal floor fix is `_PY_TRY_RE = re.compile(r"^\s*try\s*:", re.MULTILINE)` counted under the `try_catch` key, same as the TS path, so the "fraction of archetype files doing structured error handling" metric works for Django/DRF/Flask/FastAPI v…
- **doc_coverage (documented-public-declaration fraction)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap: add Python doc_coverage detection. CRITICAL — do NOT copy the TS/Ruby upward-scan: those scan lines ABOVE the declaration (_ts_decl_has_leading_doc i=decl_index-1; _ruby_def_has_leading_doc i=def_index-1), but a Python docstring is the FIRST STATEMENT INSIDE the body (below the def/class line), so an up…
- **test_pairing (source-to-test pairing rate + mapping)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap: add a Python branch to BOTH functions (fixing only _candidate_test_paths leaves the exclusion side broken — a co-located test_foo.py would be miscounted as a source file, polluting the denominator). 1) _is_test_path: add _PYTHON_TEST_BASENAME_RE = ^(test_.*\|.*_test)\.py$ and match it under a language==…
- **layering (repo-level forbidden cluster edges + import cycles)** (TS ✅ · Ruby ✅ · Py ⚠️) — Build a _resolve_python(spec, from_file, repo_root) in import_graph.py and add a language=="python" branch to _resolve_import_archetype. It must (1) translate a dotted relative spec to a filesystem path: ".models" -> from_file.parent/"models", ".." or "..pkg" -> walk up one package level per extra leading dot, "." -> …

#### 11. Cross-file intelligence (symbols / calls / contracts)

- **Exports index (named-symbol export set)** (TS ✅ · Ruby — · Py ❌) — Build a Python exports index — real parity gap, not a legitimate exclusive. The CAPABILITY (catch `from module import does_not_exist`) is shared by every language with named imports; Python's `from x import y` is its dominant import form, so the same phantom-symbol class TS catches is a live, common error class Python…
- **Reverse index (importer graph: who imports a name)** (TS ✅ · Ruby — · Py ❌) — Close the Python parity gap (Python is NOT a legitimate exclusive — unlike Ruby's path-based require, Python's `from m import name` is a true named-import model directly analogous to TS, so a reverse index is buildable and gives the same rename-blast-radius benefit). Work: (1) extend libcst_dump _import_specifier (or …
- **phantom-import (relative import target resolves to a file)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap — true parity gap, not an exclusive. Python has explicit relative imports (`from . import x`, `from .mod import y`, `from ..pkg import z`) that resolve to files on disk exactly like Ruby's require_relative, so the capability applies and Python=none is a real miss, not n/a. Build a third branch in lint_ph…
- **phantom-symbol (imported name exists in target's exports)** (TS ✅ · Ruby — · Py ❌) — Close the gap for Python (not Ruby). Python's `from m import a` binds a name `a` that must exist in module m's module-level namespace — the exact shape the TS check targets — so this is a real, closeable parity gap, not a legitimate exclusive. Build, in order: (1) libcst_dump.py emits per-file module-level export name…
- **cross-file-importers (edit-time blast-radius advisory)** (TS ✅ · Ruby — · Py ❌) — Close the Python gap (true parity gap; Python's `from module import name` is a near-exact analog of TS named imports). Four coordinated changes: (1) libcst_dump / extractors/python.py: emit `import_symbols` rows ({name, module, line}) for `from mod import name` and `import mod as alias`, plus the module's defined top-…
- **removed-export-breaks-importers (deterministic existence break)** (TS ✅ · Ruby — · Py ❌) — Close the Python parity gap; Ruby stays n/a. The claim's TS=full/Ruby=n/a/Python=none are all correct in code, but is_legitimate_exclusive is FALSE: the exclusive is legitimate only versus Ruby (no static named-import construct, autoload/require resolution). Python's `from module import name` is the exact static analo…
- **Calls index — import grade (cross-file named/namespace-import call edges)** (TS ✅ · Ruby — · Py ❌) — Close the parity gap — not a legitimate exclusive. Python has the exact constructs the import grade resolves (`from m import x` = named import, `import m as a` = namespace import), it just captures them in the wrong shape today. Multi-part build, NOT a gate flip: (1) libcst_dump.py — emit `import_symbols` rows {name, …
- **get_callers / get_drift caller facts (tool read over calls index)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap by adding a Python `import` grade (analog of the TS one). Two pieces: (1) have libcst_dump.py emit `import_symbols` (and namespace/aliased forms) for `from module import name [as local]` / `import module [as alias]`, and `named_export_names` + `export_set_open` per module (a Python module's importable na…
- **Callable signatures index (per-symbol params/return/span)** (TS ✅ · Ruby ⚠️ · Py ⚠️) — Build the Python type-text path; do not build Ruby. The gap is real and the claimed statuses (TS=full, Ruby=partial, Python=partial) are all correct, but it is only PARTLY a legitimate exclusive. Ruby=partial is legitimate: Ruby has no inline static type annotations the prism dump can read (RBS/sorbet sigs are out-of-…
- **Forward definition hydration (definitions of imported symbols for the judge)** (TS ✅ · Ruby ❌ · Py ❌) — Gap is real and confirmed in code: TS=full, Ruby=none, Python=none. NOT a uniform legitimate exclusive — split by language. PYTHON = true parity gap, build it. `from module import name` maps cleanly onto the TS named-import model this capability resolves, and the producer half is already done (Python emits callable_si…
- **Signature contract-diff / contract-breaks (narrowed positional contract)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap (small, additive). 1) Add ".py" to _CONTRACT_DIFF_EXTS (tools.py:6092). 2) Add a `.py` branch to signature_diff._extractor_for_ext (signature_diff.py:181-199) returning PythonExtractor(), and include ".py" in the _batch_parse lang grouping (signature_diff.py:259-266, which currently only maps "rb"/"ts").…

#### 12. Framework awareness

- **Stored framework tag in profile** (TS ❌ · Ruby ❌ · Py ❌) — no action - already at parity. The capability is genuinely absent for all three languages (none/none/none verified), so there is no inter-language asymmetry to close. It is NOT a legitimate exclusive: frameworks exist in every supported language (Next/Nest for TS, Rails for Ruby, Django/Flask/FastAPI for Python), so a…
- **Hybrid frontend handling (language_hint envelope)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap, both directions (a one-sided fix is not parity). (1) Python-primary + JS sidecar: add a _is_python_with_frontend predicate + a Python branch in the envelope that runs _count_ts_files_under over the common Python-app frontend dirs (frontend/, static/, assets/, client/) and emits {primary:"python", second…
- **Companion-artifact co-change rules (framework pairings)** (TS ⚠️ · Ruby ✅ · Py ❌) — Close the gap: add Python (Django/Alembic) co-change rules. (1) Flip _normalize_language (cochange.py:48-59) to also map "python"->"python" so .py files survive the lang gate at line 499-501. (2) Add to _COCHANGE_RULES a cochange-django-model-migration rule (language "python"): trigger = a concrete app models module (…
- **Stale-test / test-pairing advisory eligibility** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap - this is a true parity gap, not a legitimate exclusive. Stale-test/test-pairing advisory applies to Python exactly as to TS/Ruby; pytest (test_*.py, *_test.py, tests/ mirroring) and Django (app/tests/) are universal Python conventions. The gap exists only because the dispatch helpers branch ruby-vs-ever…
- **Inheritance-convention derivation (dominant_base / known_bases)** (TS ❌ · Ruby ✅ · Py ❌) — Close the gap, prioritizing Python. The fix is to generalize, not rebuild — the base-class signal already exists for all three languages via class_shapes (TS heritageOf ts_dump.mjs:594-663; Python class_shapes python.py:200-216). The Ruby comment at conventions.py:1819 ("Ruby/Rails-specific") only justifies that the R…
- **Class-contract decorator/base recognition (framework heritage)** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap (one-line fix). In _collect_contract_classes (conventions.py ~1038) read both keys, e.g. `ext = shape.get("extends") or (shape.get("bases") or [None])[0]`, OR normalize libcst's `bases` to `extends` (first base) so it matches the TS-shaped class_shapes the consumer already expects. This makes Python base…

#### 13. Teach / idioms / counterexamples / class contracts

- **Per-edit counterexample capture (build counterexamples.json from a real off-pat…** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap (real parity gap, not a legitimate exclusive — counterexample capture applies to every language whose competing-import enforcement already works, i.e. all three). Build: add a Python branch to `_find_import_line` (counterexamples.py:168-215) that mirrors `_PY_IMPORT_RE` (lint_engine.py:2210-2212) and the…
- **Per-edit counterexample render ('do NOT write it this way' paired with witness)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. The capability applies to Python (e.g. "use our http wrapper / httpx, not raw requests") — the exclusion is an accidental artifact of a quoted-specifier-only regex, not a real language limitation. Extend the capture matcher (_import_of / _find_import_line in counterexamples.py) to recognize Python's UNQ…
- **Multi-off-pattern-per-archetype counterexamples (schema v2 list)** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. It is a true parity gap, not a legitimate exclusive: wrapper-preference ("prefer httpx over requests", "use the project db module not raw sqlalchemy") is a normal Python convention, and every layer except capture (schema, teach, append, render, archetype keying — all verified agnostic and reachable for …
- **Class-body-contract derivation: base class annotation** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the Python gap with a one-line fix at mcp/chameleon_mcp/conventions.py:1038: `ext = shape.get("extends") or next(iter(shape.get("bases") or []), None)`. TS keeps using `extends`; Python begins reading `bases[0]` from class_shapes (the data is already captured by libcst_dump.py:234, just under the wrong key — ver…
- **Class-contract used as a base anchor for the cohort** (TS ✅ · Ruby ✅ · Py ⚠️) — Close the gap for BOTH Python and Ruby (the claim's Ruby=full is wrong; Ruby shares the exact method-less limitation). Consequence to flag: a pure-macro/declarative model archetype (Django models.Model subclass with only fields, or a Rails ApplicationRecord subclass with only has_many/validates) has no methods and usu…
- **Idiom novelty/coverage: covered-by-inheritance dedup** (TS ❌ · Ruby ✅ · Py ❌) — Close the gap. The base-inheritance-restatement dedup is NOT a legitimate Ruby exclusive: the "inherit from BaseX" base is derived for all three languages (Ruby in conventions["inheritance"], TS/Python in conventions["class_contract"]["base"]). Only the Ruby DSL-macro contract is genuinely Ruby-specific. Concrete fix,…

#### 14. Enforcement, block-eligibility & calibration

- **Block-eligible rule set (which rules may ever block)** (TS ✅ · Ruby ✅ · Py ⚠️) — Two-part. (A) Legitimate exclusives needing no action: jsx-presence-mismatch (TS/JS-only concept), naming-convention-violation interface-prefix (TS) / Ruby casing (Ruby), inheritance-convention-violation (Rails class-inheritance) genuinely do not map to Python the same way -- Python's effective set being smaller for t…
- **phantom-import block rule** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap. Two-part fix. (1) Immediate stopgap for the vacuous-active certification bug: flip BLOCK_RULE_LANGUAGES['phantom-import'] (violation_class.py:226) from None to frozenset({"typescript","ruby"}). This makes lang_ok=False for a Python profile so calibration reports inert_reason="no-signal-for-language" ins…
- **naming-convention-violation block rule** (TS ✅ · Ruby ✅ · Py ❌) — CLOSE the gap for the CASING sub-conventions (not the interface-prefix sub-part, which is correctly TS-exclusive — Python has no interfaces). Three sites, cheap because the calibration/inert-signal layer reuses the same keys:  1. conventions.py:280 — add a `language == "python"` branch to extract_declarations_from_con…
- **file-naming-convention-violation block rule** (TS ✅ · Ruby ✅ · Py ❌) — Close the gap in two parts (the cited fix is correct but incomplete). PART 1 (casing parity, clean one-liner, high value): add _PY_EXTENSIONS to the lint_engine.py:2829 allowlist -> _file_naming_violations stops dropping .py/.pyi and the casing branch fires; _classify_casing already recognizes snake_case (PEP 8 module…

---

_Generated from a full-codebase parity audit. Re-run the audit and regenerate
when the language pipelines change; keep this file as the basis for parity work._
