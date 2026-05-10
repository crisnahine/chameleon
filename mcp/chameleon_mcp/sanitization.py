"""Tag-boundary sanitization for content injected into <chameleon-context>.

Round 4 Anthropic Engineer Round 5 AppSec Specialist critical security
mitigation: prevent canonical excerpts or idioms from containing literal
`</chameleon-context>` (or near-tag-boundary tokens) that would be parsed
as the closing tag and let attacker-controlled content escape.

Per ARCHITECTURE.md "Security mitigations" #2.
"""

from __future__ import annotations

import re
import unicodedata

# Tag-boundary tokens to sanitize. We replace each with a zero-width-stripped
# placeholder so the model still reads the intent but cannot exit the tag.
_DANGEROUS_TOKENS = (
    "</chameleon-context>",
    "</chameleon",
    "<chameleon-context>",
    "<chameleon",
    # Common system-prompt boundary tokens (defense in depth — not strictly
    # chameleon's tag, but injecting these into model context is suspicious)
    "</system>",
    "<system>",
    "<|im_start|>",
    "<|im_end|>",
    "<|endoftext|>",
)


def sanitize_for_chameleon_context(content: str) -> str:
    """Replace dangerous tag-boundary tokens with neutral text.

    Defensive transformations applied:
    1. NFC unicode normalization (defeat NFD-encoded variants of `<`, `>`, etc.)
    2. Replace each dangerous token with a `[chameleon: <text>]`-style annotation
       so the meaning is preserved but the structure is broken.
    3. Strip ANSI escapes (visual injection vector).
    4. Strip zero-width unicode characters (used to hide real tokens).
    """
    # 1. Normalize unicode to NFC
    normalized = unicodedata.normalize("NFC", content)

    # 2. Tag-boundary token replacement (case-insensitive for safety)
    for token in _DANGEROUS_TOKENS:
        replacement = f"[chameleon-sanitized: {token.strip('<>')}]"
        # Replace both literal case and lowercased
        normalized = normalized.replace(token, replacement)
        normalized = normalized.replace(token.lower(), replacement)

    # 3. Strip ANSI CSI/OSC escapes
    normalized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", normalized)
    normalized = re.sub(r"\x1b\][^\x07]*\x07?", "", normalized)

    # 4. Strip zero-width characters (U+200B–U+200D, U+FEFF, U+2060)
    normalized = re.sub(r"[​-‍﻿⁠]", "", normalized)

    return normalized
