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

    Order matters: zero-width characters and ANSI escapes are stripped FIRST
    so an attacker cannot hide a tag-boundary token by sandwiching invisible
    characters inside it (e.g., `<\\u200b/chameleon-context>`). Once these
    obfuscators are gone, NFC normalization runs, then the literal tag-boundary
    replacement.

    Defensive transformations:
    1. Strip zero-width unicode (U+200B–U+200D, U+FEFF, U+2060) — must be first.
    2. Strip ANSI CSI/OSC escapes.
    3. NFC normalize (defeat decomposed `<`, `>` variants).
    4. Replace each dangerous token with a `[chameleon-sanitized: <text>]`
       annotation so the meaning is preserved but the structure is broken.
    """
    cleaned = re.sub(r"[​-‍﻿⁠]", "", content)
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"\x1b\][^\x07]*\x07?", "", cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)

    for token in _DANGEROUS_TOKENS:
        replacement = f"[chameleon-sanitized: {token.strip('<>')}]"
        cleaned = cleaned.replace(token, replacement)
        cleaned = cleaned.replace(token.lower(), replacement)

    return cleaned
