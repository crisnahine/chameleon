"""Convention schema, serialization, and extraction for Smart Injection v0.9.0."""
from __future__ import annotations
import json

CONVENTIONS_SCHEMA_VERSION = 1
MIN_SAMPLE_SIZE = 10
MIN_SAMPLE_SIZE_NAMING = 5


def empty_conventions(*, generation: int) -> dict:
    return {
        "schema_version": CONVENTIONS_SCHEMA_VERSION,
        "generation": generation,
        "min_sample_size": MIN_SAMPLE_SIZE,
        "conventions": {
            "imports": {},
            "naming": {},
        },
    }


def serialize_conventions(conventions: dict) -> str:
    return json.dumps(conventions, indent=2, sort_keys=False, ensure_ascii=False)
