"""The lens contract: one shared result shape, and the registry + gating that
tell the job runner (Task 4) which lenses apply to a turn.

A "lens" is one independent review pass over a turn's diff -- correctness
(this package's first real runner, ``stop/lenses/correctness.py``),
duplication, and idiom (Task 3). Every lens returns a :class:`LensResult`:
canonical ``core.finding.Finding`` objects (never a lens-specific dict shape)
plus the check events it recorded along the way, so the job runner can fold
every lens's telemetry into one attestation without knowing each lens's
internals.

``LENSES`` maps a lens name to ``(config_key, module_path)`` rather than a
live callable, so this package never has to import all three runner modules
just to answer "which lenses are active" -- ``active_lenses`` only reads the
repo's enforcement config. Each registered module (``correctness.py``,
``duplication.py``, ``idiom.py``) exposes a ``run`` callable; the job runner
resolves and imports it lazily, at the point it actually schedules that
lens, via ``resolve_runner``.

Top-level imports stay stdlib-only; every non-stdlib symbol (the lens runner
modules, ``core.finding.Finding``) is resolved via a deferred import inside
the function that needs it, mirroring the rest of the ``stop/`` package.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.core.finding import Finding


@dataclass(frozen=True)
class LensResult:
    """One lens's output for a turn: canonical findings plus its check events.

    ``check_events`` is ``(kind, detail)`` pairs -- the same two-part shape
    every lens's ``event_sink`` callback already receives, collected here
    instead of (or in addition to) being forwarded live, so the job runner
    can persist one flat event list per lens without each lens needing to
    know how events are recorded.
    """

    findings: list[Finding] = field(default_factory=list)
    check_events: list[tuple[str, str]] = field(default_factory=list)


# name -> (EnforcementConfig field gating this lens, "module:callable" the job
# runner imports lazily to get the runner). Insertion order is the lens run
# order: stop/scheduler.py builds a turn's lens_names via active_lenses, so a
# caller that needs a stable iteration order gets one for free.
LENSES: dict[str, tuple[str, str]] = {
    "correctness": ("correctness_judge", "chameleon_mcp.stop.lenses.correctness:run"),
    "duplication": ("duplication_review", "chameleon_mcp.stop.lenses.duplication:run"),
    "idiom": ("idiom_review", "chameleon_mcp.stop.lenses.idiom:run"),
}


def active_lenses(cfg) -> list[str]:
    """Which registered lenses this repo's config has switched on, in
    ``LENSES`` order. ``cfg`` is ``profile.config.EnforcementConfig`` (or
    anything duck-typed with the same boolean fields); a missing attribute
    reads as enabled, matching every other lens flag's fail-open default.
    """
    return [name for name, (config_key, _path) in LENSES.items() if getattr(cfg, config_key, True)]


def resolve_runner(name: str) -> Callable:
    """Import and return the registered lens's ``run`` callable.

    Raises ``KeyError`` for an unregistered lens name, and whatever import
    error the target module raises for a lens registered but not yet built
    -- this function does no fail-open swallowing of its own; the caller
    (the job runner) decides how to handle a lens that isn't ready yet.
    """
    _config_key, path = LENSES[name]
    module_path, _, attr = path.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)
