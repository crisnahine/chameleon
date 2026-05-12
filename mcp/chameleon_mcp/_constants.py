"""Central definitions for status / state enum strings used across the package.

v0.5.7 audit (item #4: Magic strings) flagged that "profile_corrupted",
"already_bootstrapped", "failed_unsupported_language", "untrusted",
"stale", "trusted", "n/a", etc. were scattered across files with no
single source of truth. A typo in one place would surface only at
the consumer (skill / hook / external tool). Centralizing the values
here makes drift visible at import time.

Importers SHOULD use these constants. Existing string-literal usage is
not (yet) rewritten en masse; the constants and the literals carry
identical values so both forms remain wire-compatible.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# detect_repo profile_status values
# ---------------------------------------------------------------------------
PROFILE_STATUS_NO_REPO: Final[str] = "no_repo"
"""``detect_repo`` resolved no repo root for the given file path."""

PROFILE_STATUS_NO_PROFILE: Final[str] = "no_profile"
"""Repo root resolved but ``.chameleon/`` does not exist."""

PROFILE_STATUS_PROFILE_PRESENT: Final[str] = "profile_present"
"""A valid ``.chameleon/`` profile exists and is loadable."""

PROFILE_STATUS_PROFILE_CORRUPTED: Final[str] = "profile_corrupted"
"""``.chameleon/profile.json`` exists but is unparseable JSON."""

PROFILE_STATUS_PROFILE_UNSUPPORTED_SCHEMA: Final[str] = (
    "profile_unsupported_schema_version"
)
"""Profile schema_version > MAX_SUPPORTED_SCHEMA_VERSION; engine refuses to load it."""

PROFILE_STATUS_VALUES: Final[tuple[str, ...]] = (
    PROFILE_STATUS_NO_REPO,
    PROFILE_STATUS_NO_PROFILE,
    PROFILE_STATUS_PROFILE_PRESENT,
    PROFILE_STATUS_PROFILE_CORRUPTED,
    PROFILE_STATUS_PROFILE_UNSUPPORTED_SCHEMA,
)


# ---------------------------------------------------------------------------
# detect_repo trust_state values
# ---------------------------------------------------------------------------
TRUST_STATE_NA: Final[str] = "n/a"
"""No profile present, or profile is corrupted / unsupported: trust is meaningless."""

TRUST_STATE_UNTRUSTED: Final[str] = "untrusted"
"""Profile exists; no trust grant from the current user."""

TRUST_STATE_TRUSTED: Final[str] = "trusted"
"""Profile exists; trust grant present AND profile sha matches the grant."""

TRUST_STATE_STALE: Final[str] = "stale"
"""Trust grant exists but profile sha has changed since the grant.

User must re-run ``/chameleon-trust`` to re-confirm. Pre-v0.5.7 this
value was not in the published trust_state schema (only the docstring);
v0.5.7 documents it explicitly via this constant.
"""

TRUST_STATE_VALUES: Final[tuple[str, ...]] = (
    TRUST_STATE_NA,
    TRUST_STATE_UNTRUSTED,
    TRUST_STATE_TRUSTED,
    TRUST_STATE_STALE,
)


# ---------------------------------------------------------------------------
# bootstrap_repo status values
# ---------------------------------------------------------------------------
BOOTSTRAP_STATUS_SUCCESS: Final[str] = "success"
BOOTSTRAP_STATUS_FAILED: Final[str] = "failed"
BOOTSTRAP_STATUS_FAILED_UNSUPPORTED_LANGUAGE: Final[str] = (
    "failed_unsupported_language"
)
BOOTSTRAP_STATUS_FAILED_TOO_MANY_FILES: Final[str] = "failed_too_many_files"
BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED: Final[str] = "already_bootstrapped"
BOOTSTRAP_STATUS_NOOP: Final[str] = "noop"

BOOTSTRAP_STATUS_VALUES: Final[tuple[str, ...]] = (
    BOOTSTRAP_STATUS_SUCCESS,
    BOOTSTRAP_STATUS_FAILED,
    BOOTSTRAP_STATUS_FAILED_UNSUPPORTED_LANGUAGE,
    BOOTSTRAP_STATUS_FAILED_TOO_MANY_FILES,
    BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED,
    BOOTSTRAP_STATUS_NOOP,
)


# ---------------------------------------------------------------------------
# Confidence bands (archetype match strength)
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH: Final[str] = "high"
CONFIDENCE_MEDIUM: Final[str] = "medium"
CONFIDENCE_LOW: Final[str] = "low"

CONFIDENCE_VALUES: Final[tuple[str, ...]] = (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)


# ---------------------------------------------------------------------------
# content_signal_match values (matches signatures.content_signal_match_for)
# ---------------------------------------------------------------------------
CONTENT_SIGNAL_STRONG: Final[str] = "strong"
CONTENT_SIGNAL_WEAK: Final[str] = "weak"
CONTENT_SIGNAL_NONE: Final[str] = "none"

CONTENT_SIGNAL_VALUES: Final[tuple[str, ...]] = (
    CONTENT_SIGNAL_STRONG,
    CONTENT_SIGNAL_WEAK,
    CONTENT_SIGNAL_NONE,
)
