"""AST extractors for chameleon.

Each language extractor implements the Extractor protocol defined in _base.py.
Extractors are subprocess-based (the actual parsing happens in a vendored
parser binary or interpreter, not in the Python process).

Supported: typescript (TS Compiler API via Node subprocess),
ruby (Prism via Ruby subprocess).
Planned: python (libcst), go (go/parser), rust (syn), php (nikic).

See docs/architecture.md "TypeScript-first extractor" + "Cluster signature function".
"""
