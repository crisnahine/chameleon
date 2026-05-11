"""Archetype name proposal — derive human-meaningful names from cluster signals.

Phase 2D.2: replaces the ``cluster-<16hex>`` placeholder names emitted in
Phase 2B with names like ``controller``, ``service``, ``react-component``,
``migration`` derived from the cluster's signature plus its witness paths.

Design

The heuristic is intentionally rule-based and non-AI. It reads only the
cluster's already-computed signature (paths_pattern_bucket, top_level
node kinds, default_export_kind, jsx_present) and the relative paths of
its members. It returns an ``[a-z][a-z0-9-]{0,63}`` string that the
schema-level regex accepts.

Two clusters can legitimately produce the same base name (e.g., two
``controller`` clusters in different namespaces or test trees). The
public entry point ``propose_archetype_name`` is given the set of
already-assigned names and appends a path-tail suffix on collision so
the final names remain stable and disambiguated.

Falls back to a short ``cluster-<hash8>`` form when no signal matches
so the result is still readable but obviously generic.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Public regex mirror — see ``profile.schema.ARCHETYPE_NAME_RE``. Kept in
# sync by hand because importing the schema module from a bootstrap pure-
# function helper would create a small cycle.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Path tail tokens we strip when suffixing a collision because they carry
# no namespace information (a controller in ``app/controllers/foo.rb`` and
# one in ``app/controllers/bar.rb`` aren't usefully named ``controller-foo``
# and ``controller-bar`` — they're both just ``controller``). Anything not
# in this set is treated as namespace-bearing and used as the suffix.
_GENERIC_TAIL_SEGMENTS = frozenset({
    "controllers",
    "models",
    "services",
    "policies",
    "serializers",
    "jobs",
    "mailers",
    "workers",
    "components",
    "hooks",
    "utils",
    "util",
    "lib",
    "src",
    "app",
    "spec",
    "test",
    "tests",
    "queries",
    "mutations",
    "migrate",
    "migrations",
    "config",
    "initializers",
    "types",
})

# Path segments that strongly indicate test files regardless of language.
_TEST_DIR_TOKENS = frozenset({"spec", "specs", "test", "tests", "__tests__"})

# Filename suffixes that strongly indicate test files.
_TEST_FILE_SUFFIXES = (
    "_spec.rb",
    "_test.rb",
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".test.mjs",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.mjs",
)


def _segments(paths_pattern: str) -> list[str]:
    """Split a paths_pattern bucket (e.g., 'app/controllers/api') into segments.

    Filters out empty fragments produced by leading slashes or repeated
    separators so callers can rely on every element being a non-empty
    segment.
    """
    if not paths_pattern:
        return []
    return [s for s in paths_pattern.split("/") if s]


def _cluster_attr(cluster: Any, attr: str, default: Any = None) -> Any:
    """Reach into ``cluster.key.<attr>`` first, falling back to ``cluster.<attr>``.

    The orchestrator passes ``Cluster`` instances whose signals live on
    ``cluster.key``; tests can also pass a lightweight dict-like stand-in
    for unit testing. This helper keeps both paths working without leaking
    test concerns into the production call site.
    """
    key = getattr(cluster, "key", None)
    if key is not None:
        val = getattr(key, attr, None)
        if val is not None:
            return val
    if isinstance(cluster, dict):
        return cluster.get(attr, default)
    return getattr(cluster, attr, default)


def _member_relpaths(cluster: Any) -> list[str]:
    """Return the cluster members' paths as POSIX-style strings.

    Members are ParsedFile instances during real bootstraps and arbitrary
    dicts/strings during unit tests. We tolerate both shapes; callers use
    the result only for filename and segment inspection, never to read
    files.
    """
    members = getattr(cluster, "members", None)
    if members is None and isinstance(cluster, dict):
        members = cluster.get("members", [])
    if not members:
        return []

    paths: list[str] = []
    for m in members:
        # Real members are ParsedFile; .path is a pathlib.Path.
        p = getattr(m, "path", None)
        if p is None:
            if isinstance(m, str):
                paths.append(m.replace("\\", "/"))
            continue
        # ``Path`` and ``PurePath`` both stringify cleanly; use POSIX form so
        # the heuristic behaves the same on win32 if it's ever wired up.
        paths.append(str(p).replace("\\", "/"))
    return paths


def _looks_like_test(paths_pattern: str, member_paths: Iterable[str]) -> bool:
    """Return True if the cluster's paths or filenames smell like tests.

    Three signals — any one is enough:
      (a) the paths_pattern contains a known test directory (``spec``,
          ``__tests__``, ``test``, ``tests``);
      (b) at least half the members' filenames end in a known test suffix;
      (c) every member sits under a path component named ``spec`` or
          ``__tests__`` (catches Jest colocated tests).
    """
    pattern_segs = _segments(paths_pattern)
    if any(seg in _TEST_DIR_TOKENS for seg in pattern_segs):
        return True

    member_list = list(member_paths)
    if not member_list:
        return False

    suffix_hits = sum(
        1 for p in member_list if any(p.endswith(suf) for suf in _TEST_FILE_SUFFIXES)
    )
    if suffix_hits * 2 >= len(member_list):  # ≥50% of members look like tests
        return True

    if all(
        any(seg in _TEST_DIR_TOKENS for seg in p.split("/"))
        for p in member_list
    ):
        return True

    return False


def _pattern_contains(paths_pattern: str, token: str) -> bool:
    """Does the paths_pattern bucket contain ``token`` as a path segment?

    Substring matching gives false positives (``components`` matches
    ``componentsx``); segment matching is the intent. ``token`` is treated
    as a single segment.
    """
    return token in _segments(paths_pattern)


def _members_contain(member_paths: Iterable[str], token: str) -> bool:
    """Does at least one member's path contain ``token`` as a directory segment?

    The signature v5 ``path_pattern_bucket_for`` collapses inner directory
    segments for deep paths — ``app/controllers/api/v1/foo.rb`` becomes the
    bucket ``app/api/v1`` and entirely loses the load-bearing ``controllers``
    segment. Naming the cluster therefore can't rely on the bucket alone;
    we also inspect the raw member paths.

    A cluster is considered to "be a controllers cluster" only if a strict
    majority of its members live under a ``controllers/`` directory, which
    keeps the rule robust against a single rogue file dropped into the
    cluster by a coarse signature.
    """
    members = list(member_paths)
    if not members:
        return False
    matches = sum(1 for p in members if token in p.split("/"))
    # Require majority membership. The threshold matches the cluster-purity
    # ratios used elsewhere (e.g., ``_looks_like_test`` uses 50%).
    return matches * 2 >= len(members)


def _filenames(member_paths: Iterable[str]) -> list[str]:
    """Return just the basename for each path string."""
    return [p.rsplit("/", 1)[-1] for p in member_paths]


def _base_name_for(cluster: Any) -> str | None:
    """Return a derived base name from cluster signals, or None for fallback.

    The order of rules is significant: more specific patterns are tried
    before more general ones (e.g., ``rails-initializer`` before the bare
    ``initializer`` token).
    """
    paths_pattern = _cluster_attr(cluster, "path_pattern_bucket") or ""
    default_export = _cluster_attr(cluster, "default_export_kind")
    top_level_kinds = _cluster_attr(cluster, "top_level_node_kinds") or ()
    jsx_present = bool(_cluster_attr(cluster, "jsx_present", False))
    member_paths = _member_relpaths(cluster)
    file_names = _filenames(member_paths)

    is_class_default = default_export in {
        "ClassNode",
        "ClassDeclaration",
        "ModuleNode",  # treat Ruby modules with a single top-level body as classes
    }
    is_arrow_default = default_export in {"ArrowFunction", "FunctionExpression"}

    def _has(token: str) -> bool:
        """Either the bucket OR the majority of member paths contains ``token``.

        Signature v5 collapses ``app/controllers/api/v1/foo.rb`` into the
        bucket ``app/api/v1``, dropping the load-bearing ``controllers``
        segment. We fall back to inspecting the raw member paths so the
        heuristic still recognizes Rails clusters with deep namespaces.
        """
        return _pattern_contains(paths_pattern, token) or _members_contain(
            member_paths, token
        )

    # Test detection runs first — a "spec/controllers/..." cluster should
    # be named ``test``, not ``controller``. Without this ordering the model
    # would see two ``controller`` clusters and the disambiguator would
    # suffix one ``controller-spec``, which is misleading.
    if _looks_like_test(paths_pattern, member_paths):
        return "test"

    # --- Rails-flavored buckets (paths_pattern starts with ``app/...``) ---
    if _has("controllers"):
        return "controller"
    if _has("models"):
        return "model"
    if _has("policies"):
        return "policy"
    if _has("serializers"):
        return "serializer"
    if _has("jobs"):
        return "job"
    if _has("mailers"):
        return "mailer"
    if _has("workers"):
        return "worker"

    # config/initializers/*.rb — these tend to be top-level Rails.application.configure blocks.
    if _has("initializers") and _has("config"):
        return "rails-initializer"

    # Migrations: ``db/migrate/...`` is the canonical Rails path; ``migrations``
    # also catches other ORMs (knex, sequelize). Check before ``services`` so
    # a ``db/migrate`` cluster isn't shadowed.
    if _has("migrate") or _has("migrations"):
        return "migration"

    if _has("services"):
        return "service"

    # --- Front-end / TS-flavored buckets ---
    # React components: explicit ``components`` dir AND JSX present.
    if _has("components") and jsx_present:
        return "react-component"

    # React hooks: ``hooks`` directory AND filename starts with ``use``.
    if _has("hooks") and any(n.startswith("use") for n in file_names):
        return "react-hook"

    if _has("queries"):
        return "query"
    if _has("mutations"):
        return "mutation"

    # ``lib/utils`` and ``utils`` directories — utility / pure functions.
    if _has("utils") or _has("util"):
        return "utility"

    # ``types`` directory or paths_pattern ending in ``/types``.
    if _has("types"):
        return "types"

    # Style fallback: a JSX-present cluster whose default export is arrow-y
    # is almost always a component.
    if jsx_present and is_arrow_default:
        return "react-component"

    # TS class default with no JSX — generic ``class`` is more informative
    # than the cluster-hash fallback.
    if is_class_default and not jsx_present:
        return "class"

    return None


def _short_hash_for(cluster: Any) -> str:
    """Derive a short fallback identifier from the cluster signature.

    Prefers the existing cluster_id attribute when callers attach one (the
    orchestrator does); otherwise hashes the cluster key dict so unit
    tests don't have to fabricate ids.
    """
    cluster_id = getattr(cluster, "cluster_id", None)
    if cluster_id is None and isinstance(cluster, dict):
        cluster_id = cluster.get("cluster_id")
    if cluster_id:
        return str(cluster_id)[:8]

    key = getattr(cluster, "key", None)
    if key is not None and hasattr(key, "to_dict"):
        import hashlib
        import json

        canonical = json.dumps(key.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return "unknown"


def _disambiguation_suffix(cluster: Any) -> str | None:
    """Pick a stable, name-safe suffix from the cluster's path metadata.

    Consults two sources in order:
      1. the paths_pattern bucket — usually the most concise namespace hint;
      2. the member paths themselves — needed because signature v5's bucket
         collapses inner segments (``app/controllers/api/v1/foo.rb`` →
         ``app/api/v1``) and so loses subdirectory-of-controllers namespaces
         like ``admin`` in ``app/controllers/admin/dashboard_controller.rb``.

    Walks each source's segments from right to left, returning the first
    one that isn't a generic noise token (``controllers``, ``app``, ...)
    and isn't a version-style id (``v1``). Returns None when nothing
    useful is left — caller falls back to a numeric suffix.
    """
    paths_pattern = _cluster_attr(cluster, "path_pattern_bucket") or ""
    candidate_streams: list[list[str]] = []
    bucket_segs = _segments(paths_pattern)
    if len(bucket_segs) > 1:
        candidate_streams.append(bucket_segs[1:])  # drop the noisy top-level
    elif bucket_segs:
        candidate_streams.append(bucket_segs)

    # Also consult the first member's directory chain (without the filename)
    # so collisions inside ``app/controllers/admin/...`` reliably pick up
    # ``admin``.
    member_paths = _member_relpaths(cluster)
    if member_paths:
        first_dirs = member_paths[0].rsplit("/", 1)[0].split("/")
        # Skip the very first segment (``app``/``spec``); it's generic.
        if len(first_dirs) > 1:
            candidate_streams.append(first_dirs[1:])

    for segs in candidate_streams:
        for seg in reversed(segs):
            normalized = re.sub(r"[^a-z0-9-]+", "-", seg.lower()).strip("-")
            if not normalized:
                continue
            # Drop pure version tokens (v1, v2.3) and well-known generic tails.
            if re.fullmatch(r"v\d+(?:\.\d+)*", normalized):
                continue
            if normalized in _GENERIC_TAIL_SEGMENTS:
                continue
            # Must still match the leading-letter requirement of the schema.
            if not normalized[0].isalpha():
                continue
            return normalized
    return None


def _sanitize(name: str) -> str | None:
    """Coerce ``name`` to the archetype-name shape, or return None.

    Public callers should treat ``None`` as "the chosen base name was
    invalid, fall back to the short-hash form". This belt-and-suspenders
    check protects the schema regex against a future heuristic addition
    that accidentally produces ``Camel`` or ``snake_case``.
    """
    candidate = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not candidate:
        return None
    if not candidate[0].isalpha():
        return None
    candidate = candidate[:64]
    return candidate if _NAME_RE.match(candidate) else None


def propose_archetype_name(cluster: Any, existing_names: set[str]) -> str:
    """Propose a meaningful archetype name for ``cluster``.

    Args:
        cluster: a ``Cluster`` instance (or dict-like with ``key`` and
            ``members``) exposing ``key.path_pattern_bucket``,
            ``key.default_export_kind``, ``key.top_level_node_kinds``,
            ``key.jsx_present``, plus iterable ``members`` whose entries
            expose a ``.path`` attribute (or are strings).
        existing_names: the set of names already assigned by previous
            calls in this bootstrap run. Used for collision disambiguation
            and never mutated.

    Returns:
        A string matching ``^[a-z][a-z0-9-]{0,63}$``. Guaranteed unique
        with respect to ``existing_names``.

    The function is pure (no I/O) and idempotent: given the same cluster
    and ``existing_names`` it always returns the same name.
    """
    base = _base_name_for(cluster)
    if base is not None:
        sanitized = _sanitize(base)
        if sanitized is not None:
            base = sanitized
        else:
            base = None

    if base is None:
        # Fallback: ``cluster-<hash8>`` keeps the schema invariant satisfied
        # AND signals to the user that nothing meaningful was inferred.
        base = f"cluster-{_short_hash_for(cluster)}"
        sanitized = _sanitize(base)
        if sanitized is not None:
            base = sanitized
        else:
            base = "cluster-unknown"

    if base not in existing_names:
        return base

    # Collision path. First try the path-tail suffix because it carries
    # real namespace info (e.g., ``controller-admin`` vs ``controller-api``).
    suffix = _disambiguation_suffix(cluster)
    if suffix is not None:
        candidate = _sanitize(f"{base}-{suffix}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    # As a last resort, append a numeric counter. We try ``-2`` first because
    # ``foo`` already exists, so the second instance is logically the second.
    for i in range(2, 1000):
        candidate = _sanitize(f"{base}-{i}")
        if candidate is not None and candidate not in existing_names:
            return candidate

    # The schema allows 64 chars, so 998 numeric suffixes is unreachable
    # under any realistic codebase. Defensive fallback: include the short
    # hash to guarantee uniqueness without overflowing the regex.
    return _sanitize(f"{base}-{_short_hash_for(cluster)}") or f"cluster-{_short_hash_for(cluster)}"
