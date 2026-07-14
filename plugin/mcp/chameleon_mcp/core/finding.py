"""Canonical review finding: one shape for every lens, VERIFY, and the ledger.

Findings are immutable; stages derive successors with dataclasses.replace().
The single severity vocabulary and normalizer live here so no consumer can
grow its own mapping. match_key is the exact-match identity used for
cross-session recurrence (semantic matching is deliberately out of scope).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields

KINDS: tuple[str, ...] = ("correctness", "duplication", "idiom", "intent", "advisory")
SEVERITIES: tuple[str, ...] = ("blocker", "high", "medium", "low")
STATUSES: tuple[str, ...] = (
    "pending",
    "delivered",
    "addressed",
    "resurfaced",
    "shelved",
    "expired",
)

_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "blocker",
    "block": "blocker",
    "error": "high",
    "warn": "medium",
    "warning": "medium",
    "info": "low",
    "nit": "low",
}

_WS_RE = re.compile(r"\s+")


def normalize_severity(raw: str | None) -> str:
    """Fold any producer's severity word onto the canonical vocabulary.

    Unknown or missing values land on "medium": low enough not to block,
    high enough not to vanish below a default surface bar.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "medium"
    word = raw.strip().lower()
    if word in SEVERITIES:
        return word
    return _SEVERITY_ALIASES.get(word, "medium")


def compute_match_key(claim: str, file: str, kind: str) -> str:
    """Exact-match identity: sha256 over whitespace-collapsed lowercased claim
    + file path + kind. Trailing sentence punctuation is stripped so a
    re-phrased period doesn't fork the identity."""
    norm = _WS_RE.sub(" ", (claim or "").strip().lower()).rstrip(".!?")
    payload = "\x00".join((norm, file or "", kind or ""))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Finding:
    id: str
    kind: str
    severity: str
    confidence: float
    file: str
    span: tuple[int, int]
    claim: str
    evidence: str
    excerpt_sha: str
    excerpt: str
    source_lens: str
    status: str
    created_at: str
    intent_tokens: tuple[str, ...] = ()
    verified: str = "unverified"  # "unverified" | "confirmed" | "refuted"
    stale: bool = False
    match_key: str = field(default="")

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown finding kind: {self.kind!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r} (normalize first)")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if not self.match_key:
            object.__setattr__(
                self, "match_key", compute_match_key(self.claim, self.file, self.kind)
            )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "confidence": self.confidence,
            "file": self.file,
            "span": list(self.span),
            "claim": self.claim,
            "evidence": self.evidence,
            "excerpt_sha": self.excerpt_sha,
            "excerpt": self.excerpt,
            "source_lens": self.source_lens,
            "status": self.status,
            "created_at": self.created_at,
            "intent_tokens": list(self.intent_tokens),
            "verified": self.verified,
            "stale": self.stale,
            "match_key": self.match_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Finding:
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        span = kwargs.get("span") or (0, 0)
        kwargs["span"] = (int(span[0]), int(span[1]))
        kwargs["intent_tokens"] = tuple(kwargs.get("intent_tokens") or ())
        return cls(**kwargs)
