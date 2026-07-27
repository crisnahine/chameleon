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
            "order, each in its own file:\n"
            "1. StockBadge - takes units in stock, renders 'Out of stock' at "
            "zero, 'Low stock' under 5, otherwise 'In stock'.\n"
            "2. PriceTag - takes an amount in cents, renders it as currency.\n"
            "3. RatingStars - takes a rating 0-5, renders that many filled "
            "stars out of five.\n"
            "4. StatusPill - takes a status string, renders it capitalized.\n"
            "5. QuantityStepper - takes a value and min/max, renders the value "
            "with increment and decrement buttons.\n"
            "6. ShippingNote - takes a number of days, renders 'Arrives in N "
            "days' or 'Arrives today' at zero.\n"
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
            "Add six small service objects to this project, in this order, "
            "each in its own file:\n"
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
