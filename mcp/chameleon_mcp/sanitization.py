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


# Trojan Source / CVE-2021-42574 — bidirectional formatting controls that
# can re-order code visually one way while the parser/LLM reads it another
# way. v0.5.1 dogfood revealed these were reaching model context verbatim.
# The full character set per the CVE:
#   U+202A LRE — left-to-right embedding
#   U+202B RLE — right-to-left embedding
#   U+202C PDF — pop directional formatting
#   U+202D LRO — left-to-right override
#   U+202E RLO — right-to-left override
#   U+2066 LRI — left-to-right isolate
#   U+2067 RLI — right-to-left isolate
#   U+2068 FSI — first strong isolate
#   U+2069 PDI — pop directional isolate
# Each is stripped byte-for-byte (no normalization, no replacement marker)
# so attacker-shaped strings get their original visual order back.
_BIDI_CONTROLS = (
    "‪‫‬‭‮"
    "⁦⁧⁨⁩"
)
_BIDI_RE = re.compile(f"[{_BIDI_CONTROLS}]")


def sanitize_for_chameleon_context(content: str) -> str:
    """Replace dangerous tag-boundary tokens with neutral text.

    Order matters: zero-width characters, bidi controls, and ANSI escapes
    are stripped FIRST so an attacker cannot hide a tag-boundary token by
    sandwiching invisible characters inside it (e.g.,
    ``<\\u200b/chameleon-context>`` or ``<\\u202e/chameleon-context>``).
    Once these obfuscators are gone, NFC normalization runs, then the
    literal tag-boundary replacement.

    Defensive transformations:
    1. Strip zero-width unicode (U+200B–U+200D, U+FEFF, U+2060) — must be first.
    2. Strip bidi formatting controls (U+202A–U+202E + U+2066–U+2069) — the
       Trojan Source / CVE-2021-42574 character set. Removed byte-for-byte
       (no replacement marker) so the underlying logical order is restored.
    3. Strip ANSI CSI/OSC escapes.
    4. NFC normalize (defeat decomposed `<`, `>` variants).
    5. Replace each dangerous token with a `[chameleon-sanitized: <text>]`
       annotation so the meaning is preserved but the structure is broken.
    """
    cleaned = re.sub(r"[​-‍﻿⁠]", "", content)
    cleaned = _BIDI_RE.sub("", cleaned)
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"\x1b\][^\x07]*\x07?", "", cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)

    for token in _DANGEROUS_TOKENS:
        replacement = f"[chameleon-sanitized: {token.strip('<>')}]"
        cleaned = cleaned.replace(token, replacement)
        cleaned = cleaned.replace(token.lower(), replacement)

    return cleaned
