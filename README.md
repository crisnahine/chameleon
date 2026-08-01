# chromatophore

A universal code-convention engine: one tree-sitter parse layer over 24
languages, built for [chameleon](../chameleon) to use.

The organ that actually changes a chameleon's colour is the chromatophore. This
is the equivalent layer for the plugin: it does the compute-heavy,
language-bound work — parse, extract, index, mine, match — and leaves policy,
trust, hooks, and artifact lifecycle to the host.

## Why

Chameleon derives a repo's own conventions and enforces them per edit. It
already parses with tree-sitter in-process, and it supports three languages:
TypeScript/JavaScript, Ruby, Python. The ceiling is not the parser, it is the
**per-language code** above it — roughly 2,500 lines of hand-written Python
tables and callables for those three, plus a core edit for a fourth. Chameleon's
own `docs/parity-progress.md` records what adding Python cost: 12 work packages,
about 90 tracked items.

This engine moves that boundary. A language here is a TOML file — node-kind
sets, a kind-translation table, field names, a few flags — and the walker never
learns a language's name.

## What it does

| Layer | Module | What it gives you |
|---|---|---|
| Wire contract | `core` | The `ParsedFile` record chameleon's consumers already read |
| Languages | `lang` | 24 declarative specs bound to tree-sitter grammars |
| Extraction | `extract` | One iterative walk producing a record per file |
| Intelligence | `index` | Symbols, imports, call graph, blast radius |
| Mining | `mine` | Archetype clustering, convention voting, witness selection |
| Rules | `rules` | The universal rule schema and its evaluator |

## Measured

All figures from this machine (M-series, 11 cores), on chameleon's own
`plugin/mcp/chameleon_mcp` tree — 126 Python files.

| | Wall clock | Per file |
|---|---|---|
| `libcst_dump.py` (chameleon's fallback) | 10.08 s | ~80 ms |
| chameleon's in-process tree-sitter path | 0.81 s | 6.5 ms |
| `chromatophore dump` | **0.097 s** | **0.77 ms** |

That is 8.4x chameleon's current extractor and 104x its libcst fallback. The
gap is native traversal plus all cores; the engine is also the only one of the
three that parses `tools.py`, which libcst refuses at its node ceiling.

Build: 24 grammars compile in ~11 s cold. Binary: 39 MB unstripped.

### Parity

`tests/parity.py` runs the engine and chameleon's reference dumper over the same
corpus and reports per-field agreement. On those 126 files, **13 of 13 compared
fields match on 125 of 125 files** (the 126th is the file libcst refuses, so
there is nothing to compare).

```
top_level_node_kinds   125/125  100.0%     function_scopes       125/125  100.0%
named_export_count     125/125  100.0%     callable_signatures   125/125  100.0%
export_set_open        125/125  100.0%     class_shapes          125/125  100.0%
import_specifiers      125/125  100.0%     call_sites            125/125  100.0%
import_symbols         125/125  100.0%     call_sites_total      125/125  100.0%
namespace_imports      125/125  100.0%     call_sites_truncated  125/125  100.0%
has_jsx                125/125  100.0%
```

Independent cross-check: on chameleon's own tree the engine derives **232
callers** for `threshold_int`; chameleon's own committed index says 233. Two
implementations, one edge apart.

## Using it from chameleon

`dump` speaks the protocol chameleon's extractors already use — absolute paths
on stdin, one NDJSON record per file on stdout — so it is a drop-in for
`libcst_dump.py`, `prism_dump.rb`, and `ts_dump.mjs` at once:

```bash
find . -name '*.py' | chromatophore dump
```

Wiring it in is a `parse_repo` that spawns this binary instead, registered
through the existing seam:

```python
from chameleon_mcp.extractors.registry import register

class ChromatophoreExtractor:
    language = "python"
    def can_handle(self, repo_root): ...
    def parse_repo(self, repo_root, paths=None): ...  # spawn `chromatophore dump`

register(ChromatophoreExtractor)
```

Adding a language chameleon does not yet support needs one edit in its core —
`_EXTENSIONS_BY_LANGUAGE` in `bootstrap/orchestrator.py`, which is the single
source of truth for which extensions an extractor owns.

## Other commands

```bash
chromatophore languages                      # what it can parse, and at which ABI
chromatophore parse FILE                     # one record, pretty-printed
chromatophore index DIR                      # symbols, imports, call graph
chromatophore blast DIR FILE SYMBOL          # who breaks if this changes
chromatophore mine DIR                       # archetypes and derived conventions
chromatophore check DIR RULES.toml           # evaluate a rule document
```

## The rule schema

One shape encodes every kind of knowledge the taxonomy names — conventions,
idioms, anti-patterns, smells, contracts, secrets, dependency policy. See
`examples/rules.toml` for four worked rules.

Blocking is **earned, not declared**. A rule carries the confidence measured
against code the team already accepted, plus its drift direction, and
`may_block()` refuses below the bar however the mode is set:

```rust
mode == Enforce && drift != Weakening && calibrated_confidence >= threshold(kind)
```

Thresholds: secrets, coupling, and contracts need 0.99; conventions,
dependency policy, and anti-patterns need 0.90; smells, idioms, and patterns
are judgment calls and can never hard-block at any confidence.

## Honest boundaries

- **An empty caller set is not evidence of dead code.** Dynamic dispatch,
  reflection, and calls through an instance are invisible to a static
  snapshot. Grep before you delete.
- **Precision over recall in the call graph.** A call site that cannot be
  resolved deterministically records no edge rather than a name-matched guess.
  Recall is deliberately sacrificed: a wrong edge makes a blast radius lie.
- **A spec expresses what node kinds and field names can say.** That covers
  the bulk of the contract. Where a language needs real per-node logic, the
  spec degrades to reporting less rather than reporting wrong.
- **Parity is measured on Python.** The other 23 languages are verified to
  parse and extract structurally (`cargo test`), not compared against a
  reference implementation, because for most of them none exists.
- **Spec depth varies by language, and the difference is measurable.** Every
  shipped language parses, and extracts functions, classes, and body shape.
  Call-site classification — separating `obj.method()` from a bare `method()`
  and keeping the receiver — is verified for C, C++, C#, Go, Java, Lua, PHP,
  Python, Rust, Scala, and TypeScript. Kotlin, Swift, Ruby, and Bash currently
  record bare calls but lose the receiver on member calls, and Elixir reports
  its own `def`/`defmodule` macros as calls because in that grammar they are.
  Those are spec gaps, not engine gaps: closing one is editing a TOML file,
  which is exactly the property this design is for.
- **`AstGrep`, `Semgrep`, and `Coupling` are declared but not implemented.**
  Evaluating one returns an explicit `Unsupported` rather than silently
  matching nothing — a rule that quietly never fires is worse than one that
  says it cannot run.

## Adding a language

1. Add the grammar to `Cargo.toml`.
2. Write `languages/<name>.toml`.
3. Add one line to `EMBEDDED_SPECS` and one arm to `grammar_for` in `src/lang.rs`.

No walker change, no extraction logic. `cargo test` then asserts the grammar
loads, sits inside the ABI window tree-sitter accepts, and parses.

## Development

```bash
cargo test                    # 70 tests
cargo clippy --all-targets -- -D warnings
cargo fmt --check
python3 tests/parity.py --chameleon /path/to/chameleon
```
