"""Depth pack: tasks long enough for conformance to decay, if it is going to.

Every other pack asks for ONE unit of work. A model can hold a convention it
inferred at turn 2 across three turns without any help, so those tasks cannot
separate "the plugin re-delivers guidance at every edit" from "the model
remembered". The comparison only becomes possible when the session runs long
enough for early context to stop steering, which is what these prompts buy: each
asks for several sibling files in sequence, so authoring order IS session depth.

The prompts name the files and their order explicitly. That is deliberate: an
open-ended "add a few components" lets the arms diverge on WHAT they built, and
then the depth scorer compares different work rather than the same work under
different guidance. Fixing the worklist holds everything constant except how
long the session has been running when each file is written.

Nothing here mentions conventions, style, or consistency. The whole question is
whether the repo's conventions still reach the model at file six without being
asked for, so asking for them would answer a different question.

Each requested file must also EXERCISE a convention with teeth, or the arm
measures nothing. The first run scored zero violations in both arms on every
cell, which is the same number a perfect run produces and the opposite
conclusion (results-published/depth-arm-2026-07-27.md): the tasks asked for work
the fixtures did not constrain, so there was no violation for decay to appear
in. So every component here needs a conditional CSS class, which routes it
through the taught cx-over-classnames pair -- verified to produce an
``import-preference-violation`` on the drifted form and none on the conforming
one.

The fixture is deliberately MID-MIGRATION rather than clean: five components use
``cx`` and ``LegacyBanner`` still imports ``classnames``. That is the state the
taught pair is designed for -- it gives the counterexample mechanism a real
off-pattern line from the repo's own code to quote as "do NOT write it this
way", and it makes the discouraged form discoverable, so a drifting model is
more likely to reach for the shape the rule can actually see. A fixture where
the old form appears nowhere would leave the pair inert and the arm measuring
nothing again.

The residual limit, stated rather than papered over: the rule binds on the
IMPORT. A model that hand-rolls the conditional class with a template literal
breaks the same idiom and trips no rule, so a low violation count on this cell
bounds drift-that-imports, not drift in general.

READ THE TS CELL FIRST; the Rails cell's teeth are weaker, and knowingly so.
The fixture now has an ``ApplicationService`` base that all six of its services
inherit, which makes the canonical witness a stronger thing to imitate -- but it
does NOT arm a lint rule for the drift that actually happens. A base-less
``class Foo`` is exempt by design (lint_engine's Ruby inheritance check, aligned
with the Python one: a plain class is a legitimate standalone, not a missed
base), so only extending a WRONG base is a violation, and a drifting model omits
the base rather than picking the wrong one. Ruby has no taught-import lever here
either, because Rails autoloading means the fixture contains no ``require``
lines for an import rule to bind to. Until a Ruby rule with teeth exists for
this shape, treat a flat Rails decay number as unmeasured rather than as
conformance holding -- the same trap the first run fell into.
"""

from __future__ import annotations

from tests.effectiveness.tasks import EffTask

# Long sessions: six files plus a check run legitimately exceeds the tier1 cap,
# and a cell killed at the cap has no late half to score.
_DEPTH_MAX_TURNS = 45

TASKS = [
    EffTask(
        task_id="t4-ts-depth-six-components",
        tier="depth",
        fixture="ts",
        prompt=(
            "Add six small presentational components to this project, in this "
            "order, each in its own file. Each one takes an optional boolean "
            "prop that adds a modifier CSS class when true:\n"
            "1. StockBadge - takes units in stock, renders 'Out of stock' at "
            "zero, 'Low stock' under 5, otherwise 'In stock'; `compact` adds "
            "'stock-badge-compact'.\n"
            "2. UnitPrice - takes an amount in cents, renders it as currency; "
            "`discounted` adds 'unit-price-discounted'.\n"
            "3. RatingStars - takes a rating 0-5, renders that many filled "
            "stars out of five; `dimmed` adds 'rating-stars-dimmed'.\n"
            "4. StatusPill - takes a status string, renders it capitalized; "
            "`inactive` adds 'status-pill-inactive'.\n"
            "5. QuantityStepper - takes a value and min/max, renders the value "
            "with increment and decrement buttons; `disabled` adds "
            "'quantity-stepper-disabled'.\n"
            "6. ShippingNote - takes a number of days, renders 'Arrives in N "
            "days' or 'Arrives today' at zero; `urgent` adds "
            "'shipping-note-urgent'.\n"
            "Create them one at a time in that order. When all six exist, make "
            "sure the project's checks still pass."
        ),
        category="convention",
        scorers=("depth", "convention", "cost"),
        max_turns=_DEPTH_MAX_TURNS,
    ),
    EffTask(
        task_id="t4-rails-depth-six-services",
        tier="depth",
        fixture="rails",
        prompt=(
            "Add six small service objects under app/services/catalog/, in "
            "this order, each in its own file:\n"
            "1. StockLevelReporter - given units in stock, returns a label: "
            "out of stock at zero, low stock under 5, otherwise in stock.\n"
            "2. PriceFormatter - given an amount in cents, returns it "
            "formatted as currency.\n"
            "3. RatingSummarizer - given a rating 0-5, returns a short "
            "description of it.\n"
            "4. StatusNormalizer - given a status string, returns it "
            "normalized to a canonical form.\n"
            "5. QuantityClamper - given a value and a min and max, returns the "
            "value clamped to that range.\n"
            "6. ShippingEstimator - given a number of days, returns 'Arrives "
            "in N days', or 'Arrives today' at zero.\n"
            "Create them one at a time in that order. When all six exist, make "
            "sure the project's checks still pass."
        ),
        category="convention",
        scorers=("depth", "convention", "cost"),
        max_turns=_DEPTH_MAX_TURNS,
    ),
]
