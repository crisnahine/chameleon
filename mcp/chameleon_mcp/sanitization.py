"""Tag-boundary sanitization for content injected into <chameleon-context>.

Round 4 Anthropic Engineer Round 5 AppSec Specialist critical security
mitigation: prevent canonical excerpts or idioms from containing literal
`</chameleon-context>` (or near-tag-boundary tokens) that would be parsed
as the closing tag and let attacker-controlled content escape.

Per docs/architecture.md "Security mitigations" #2.
"""

from __future__ import annotations

import re
import unicodedata

_DANGEROUS_TOKENS = (
    "</chameleon-context>",
    "</chameleon",
    "<chameleon-context>",
    "<chameleon",
    "</system>",
    "<system>",
    "</system-reminder>",
    "<system-reminder>",
    "</system_reminder>",
    "<system_reminder>",
    "</im_start>",
    "<im_start>",
    "</im_end>",
    "<im_end>",
    "<|im_start|>",
    "<|im_end|>",
    "<|endoftext|>",
)


_BIDI_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩"
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
    4. Strip C0 control bytes (U+0000–U+001F) except whitespace (tab U+0009,
       LF U+000A, CR U+000D). NUL and other C0 bytes cannot escape the tag,
       but they can corrupt downstream parsers, loggers, and metrics.
    5. NFC normalize (defeat decomposed `<`, `>` variants).
    6. Replace each dangerous token with a `[chameleon-sanitized: <text>]`
       annotation so the meaning is preserved but the structure is broken.
    """
    cleaned = re.sub(r"[​-‍﻿⁠]", "", content)
    cleaned = _BIDI_RE.sub("", cleaned)
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"\x1b\][^\x07]*\x07?", "", cleaned)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)

    for token in _DANGEROUS_TOKENS:
        replacement = f"[chameleon-sanitized: {token.strip('<>')}]"
        cleaned = re.sub(re.escape(token), replacement, cleaned, flags=re.IGNORECASE)

    return cleaned
