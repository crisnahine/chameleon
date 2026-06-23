# Python parity — implementation progress

Closing the 77 Python-parity gaps from [language-support-matrix.md](./language-support-matrix.md).
Dependency-ordered work packages; hot files (`lint_engine.py`, `conventions.py`)
are edited serially in one pass per package to avoid breaking the TS/Ruby paths.
The full unit suite must pass before every commit; for any shared
`if language ==` function touched, a TS and a Ruby fixture must still produce
identical output.

**Excluded (not Python parity — surfaced, not built on this branch):** TS
per-method decorators, TS `base_class`/`enclosing_class_path` on signatures, TS
`NODE_OPTIONS` scrub, Ruby `tautological-assertion`, `stored-framework-tag`
(no language has it — net-new for all), `import_module_set_hash` (intentionally
dead for all three).

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

## PKG-0 — P0 wiring bugs (correctness, tiny)
- [x] class_shapes base: consumer reads `extends` but Python emits `bases` (conventions.py:1038 one-line `extends or bases[0]`)
- [x] file-naming enforcement gate omits `_PY_EXTENSIONS` (lint_engine.py:2829)
- [x] vacuous-active calibration: scope phantom-import + file-naming `BLOCK_RULE_LANGUAGES` so Python doesn't certify inert rules active (violation_class.py:226,231)

## PKG-1 — Extractor foundation (libcst_dump.py + python.py); re-bootstrap to validate
- [x] emit `import_symbols` rows `{name, local, module, line}` (match ts_dump shape)
- [x] emit `namespace_imports` rows `{alias, module, line}`
- [x] emit `named_export_names` + `export_set_open`
- [x] emit `return_type` on callable_signatures
- [x] emit param `type` annotation on param shapes
- [x] class_shapes also carry `extends` (first base) to match TS consumer

## PKG-2 — Conventions derivation (conventions.py + import_graph.py)
- [x] naming casing conventions for Python (PEP8 snake/Pascal/UPPER)
- [x] class_contract base fix (read `extends`/`bases`) + accept staticmethod/classmethod kinds
- [x] key_exports (top-level public names / `__all__`)
- [x] error_handling (`try:` shape)
- [x] doc_coverage (docstring detection — scan line AFTER def/class, not before)
- [x] test_pairing: `_is_test_path` + `_candidate_test_paths` Python branches (test_*.py, *_test.py, conftest.py)
- [ ] inheritance derivation (dominant_base/known_bases for Python)
- [x] layering: `_resolve_python` dotted-relative resolver in import_graph.py

## PKG-3 — Cross-file intelligence (depends on PKG-1)
- [ ] exports index for Python (named_export_names → exports_index.json)
- [ ] reverse index for Python (import_symbols → reverse_index.json)
- [ ] phantom-import (relative import → file on disk) Python branch
- [ ] phantom-symbol (imported name in target exports) Python
- [ ] cross-file-importers (blast-radius advisory) Python
- [ ] removed-export-breaks-importers Python
- [ ] calls index import grade (named/namespace import call edges) Python
- [ ] forward definition hydration Python
- [ ] signature contract-diff (.py in _CONTRACT_DIFF_EXTS + signature_diff extractor)
- [ ] get_callers/get_drift caller facts (unlocked by import grade)
- [ ] signature index param/return type text (Python)
- [ ] doctor advisory-emission source-edit check (.py in _source_exts)

## PKG-4 — Security lint (lint_engine.py)
- [x] weak-hash (add python to gate — hashlib.md5/sha1)
- [x] insecure-random (random.* vs secrets in crypto context) Python (+Ruby)
- [x] command-injection sink (os.system / subprocess shell=True / os.popen) [net-new, Python]
- [x] insecure-deserialization sink (pickle.load(s) / yaml.load non-Safe) [net-new, Python]

## PKG-5 — Style lint (lint_engine.py + tool_config.py)
- [x] Python formatter-config reader at bootstrap (black/ruff/flake8: line-length, quote)
- [x] scan_style_rules gate + python stripper branch
- [x] indent rule (editorconfig fallback)
- [x] quote rule (+ _PY_TOKEN_RE for triple/prefixed strings)
- [x] max-line-length rule
- [x] (emission cap + summary tail come free once the scan runs)

## PKG-6 — Naming + file-naming lint (lint_engine.py + conventions.py + violation_class.py)
- [x] naming-convention-violation identifier scan (Python snake/Pascal/UPPER, mirror Ruby path)
- [x] naming-convention block-eligibility (BLOCK_RULE_LANGUAGES add python)
- [ ] (file-naming enforcement — done in PKG-0)

## PKG-7 — Test-quality lint (lint_engine.py + conventions.py)
- [ ] test-quality pass language gate + python branch
- [ ] skipped-test (pytest mark.skip/skipif/xfail, unittest skip)
- [ ] tautological-assertion (assert True==True / assertEqual(1,1))
- [ ] real-sleep-in-test (time.sleep/asyncio.sleep)
- [ ] random-in-test (random.*/secrets.*/uuid4 in test)
- [ ] assertion-free-test (_PY_ASSERTION_RE + test-block scanner)
- [ ] unstubbed-network (requests/httpx/... vs responses/respx/vcrpy)
- [ ] unfrozen-clock (datetime.now/time.time vs freezegun/time-machine)
- [ ] witness assertion-helper self-calibration (Python)
- [ ] test-archetype naming (_looks_like_test Python predicate, not just suffix list)

## PKG-8 — Import lint + counterexamples (lint_engine.py + counterexamples.py)
- [ ] string-embedded-import false-positive guard (Python)
- [ ] inline-ignore directive Python string blanker (_blank_string_literals)
- [x] off-pattern counterexample capture (_import_of Python branch: import x / from x import)
- [x] counterexample render + multi-off-pattern (free once capture works)

## PKG-9 — Framework awareness (cochange.py + orchestrator.py + naming.py)
- [ ] hybrid frontend (python<->ts language_hint, both directions)
- [x] Django model→migration co-change rule (+ _normalize_language python)
- [x] stale-test advisory eligibility (python in _normalize_language)
- [ ] inheritance-convention derivation (Django models.Model / DRF APIView)
- [x] class-contract decorator/base recognition (one-line extends/bases — overlaps PKG-2)
- [x] AST-shape fallback: add ClassDef to is_class_default (naming.py)

## PKG-10 — Calibration (violation_class.py + enforcement_calibration.py)
- [ ] phantom-import block rule Python (after PKG-3 phantom-import lands)
- [ ] naming-convention block rule Python (after PKG-6)
- [ ] file-naming block rule Python (after PKG-0)
- [ ] block-eligible rule set parity audit (scope correctly)

## PKG-11 — Idioms (idiom_coverage.py + conventions.py)
- [ ] covered-by-inheritance dedup for non-Ruby (consult class_contract.base)
- [ ] covered-by-class-contract content for non-Ruby
