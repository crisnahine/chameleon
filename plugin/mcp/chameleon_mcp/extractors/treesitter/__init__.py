"""In-process tree-sitter extraction.

Replaces the three dump-script subprocesses (ts_dump.mjs, prism_dump.rb,
libcst_dump.py) with one grammar-driven extractor running inside the MCP
server. The ``ParsedFile`` contract is unchanged, so every downstream consumer
-- cluster signature, ast_query witnesses, kind labels, phantom imports --
reads the same shape it always has.

Node kinds are translated back to the strings the dump scripts emitted
(``function_declaration`` -> ``FunctionDeclaration``, ``method`` -> ``DefNode``)
so existing profiles neither re-cluster nor rename their archetypes.
"""

from __future__ import annotations

from chameleon_mcp.extractors.treesitter.grammars import (
    TreeSitterUnavailableError,
    grammar_for_path,
    language_for_path,
)

__all__ = [
    "TreeSitterUnavailableError",
    "grammar_for_path",
    "language_for_path",
]
