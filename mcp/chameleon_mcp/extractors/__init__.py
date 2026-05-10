"""AST extractors for chameleon. v1.0 ships TypeScript only.

Each language extractor implements the Extractor protocol defined in _base.py.
Extractors are subprocess-based (the actual parsing happens in a vendored
parser binary or interpreter, not in the Python process).

v1.0: typescript (TS Compiler API via Node subprocess)
v1.5: ruby (Prism via Ruby subprocess)
v2.0+: python (libcst), go (go/parser), rust (syn), php (nikic)

See ARCHITECTURE.md "TypeScript-first extractor" + "Cluster signature function".
"""
