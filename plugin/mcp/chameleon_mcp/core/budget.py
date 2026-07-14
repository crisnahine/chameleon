"""Turn budget: one wall-clock deadline + one token ceiling, anchored at entry.

Constructed at hook or job entry and passed down explicitly. Never anchor to
module import time: a long-lived host would inherit a stale anchor and every
downstream stage would see an exhausted budget (the silent-skip failure mode
this type exists to kill). No consumer may hold its own seconds constant;
totals come from _thresholds.py at the construction site.
"""

from __future__ import annotations

import time


def approx_tokens(text: str) -> int:
    """chars/4, rounded up — the same coarse estimate every emission surface uses."""
    if not text:
        return 0
    return -(-len(text) // 4)


class TurnBudget:
    def __init__(self, *, deadline: float, token_ceiling: int) -> None:
        self._deadline = deadline
        self._token_ceiling = max(0, int(token_ceiling))
        self._tokens_spent = 0

    @classmethod
    def for_hook(cls, *, total_seconds: float, token_ceiling: int) -> TurnBudget:
        return cls(deadline=time.monotonic() + float(total_seconds), token_ceiling=token_ceiling)

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def tokens_remaining(self) -> int:
        return max(0, self._token_ceiling - self._tokens_spent)

    def would_fit(self, text: str) -> bool:
        return approx_tokens(text) <= self.tokens_remaining()

    def charge_tokens(self, text: str) -> bool:
        """Spend the text's tokens; refuse (and spend nothing) when it exceeds
        the remainder — the caller defers the item whole rather than truncating."""
        cost = approx_tokens(text)
        if cost > self.tokens_remaining():
            return False
        self._tokens_spent += cost
        return True
