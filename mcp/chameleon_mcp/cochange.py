"""Source-to-test and companion-artifact co-change detection for the turn-end
advisory.

Two related checks live here, both turn-end and advisory only:

1. Stale-test: when a turn edits a source file that conventionally ships with a
   paired test but leaves that test untouched, the test is at risk of going
   stale. This turns the bootstrap-derived ``test_pairing`` statistic into an
   advisory naming the test and the exports the edit may have moved out from
   under it.

2. Change-set completeness: when a turn creates a NEW file of a kind that
   structurally cannot stand alone (a Rails model needs a migration, a new
   controller needs a route), but the turn's change-set contains no matching
   companion, surface a nudge to add it. The trigger->companion pairs are a
   small hand-curated table of framework conventions, not learned, and each is
   silenced for a repo whose own committed files already break the pairing
   often enough that firing it would nag.

Both checks are advisory only. The test-pairing floor admits a sizable fraction
of legitimately untested files, and a partial edit may legitimately defer its
companion to a follow-up commit, so neither blocks. Everything here is
filesystem-stat and regex over already-read bytes; no repo code runs and no
parser spawns.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.conventions import (
    _RUBY_CLASS_NAME_RE,
    _RUBY_MODULE_NAME_RE,
    _TS_EXPORT_NAME_RE,
    _candidate_test_paths,
    _is_test_path,
)

# Names that the export extractor treats as structural noise rather than the
# file's public surface, kept in sync with the bootstrap key-export derivation so
# the advisory cites the same vocabulary the profile stored.
_EXPORT_NAME_SKIP = frozenset(
    {"default", "module", "class", "React", "Component", "ApplicationRecord", "Base"}
)


def _normalize_language(language: str | None) -> str | None:
    """Collapse the lint-engine language tag to the test-pairing helpers' tag.

    The path-derivation helpers branch on ``"ruby"`` versus everything else (the
    TypeScript family), so a JavaScript/TSX file resolves through the TS branch.
    Unsupported languages return None and the caller skips the file.
    """
    if language == "ruby":
        return "ruby"
    if language == "typescript":
        return "typescript"
    if language == "python":
        return "python"
    return None


def changed_exports_in_content(content: str, *, language: str) -> list[str]:
    """Exported names declared in ``content``, in source order, deduped.

    Mirrors the bootstrap key-export extraction so the advisory names symbols the
    same way the profile would have recorded them. Returns the file's own
    exported surface; the caller intersects it with the archetype's stored
    key_exports to cite only the names the team treats as the public contract.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen and name not in _EXPORT_NAME_SKIP:
            seen.add(name)
            names.append(name)

    if language == "typescript":
        for m in _TS_EXPORT_NAME_RE.finditer(content):
            name = m.group(1)
            if len(name) > 1:
                _add(name)
    elif language == "ruby":
        for m in _RUBY_CLASS_NAME_RE.finditer(content):
            _add(m.group(1).split("::")[-1])
        for m in _RUBY_MODULE_NAME_RE.finditer(content):
            _add(m.group(1).split("::")[-1])
    return names


class StaleTestItem:
    """One edited source file whose paired test exists but went untouched."""

    __slots__ = ("source_rel", "test_rel", "mapping", "exports")

    def __init__(
        self, source_rel: str, test_rel: str, mapping: str | None, exports: list[str]
    ) -> None:
        self.source_rel = source_rel
        self.test_rel = test_rel
        self.mapping = mapping
        self.exports = exports


def stale_test_items(
    *,
    repo_root: Path,
    test_pairing: dict,
    key_exports: dict,
    edited_abs: set[str],
    archetype_of,
    language_of,
    read_content,
) -> list[StaleTestItem]:
    """Compute the turn's stale-test advisory items.

    For each edited file: resolve its archetype, require that archetype to carry
    a ``test_pairing`` entry (the bootstrap only records archetypes already above
    the dominance floor, so presence is the high-pairing signal). Derive the
    paired test path; if a candidate exists on disk and is NOT itself edited this
    turn, record an item naming that test and the archetype's key_exports the
    edited file actually declares.

    Callables are injected so the hook owns archetype resolution (daemon-or
    in-process) and the byte cap, keeping this module a pure, testable computation
    over the profile data:

    - ``archetype_of(abs_path) -> archetype name or None``
    - ``language_of(abs_path) -> lint-engine language tag or None``
    - ``read_content(abs_path) -> decoded text or None``

    Returns an empty list when no edited file is a high-pairing source with an
    unsynced existing test. Never raises for a single bad file; a file that fails
    any step is skipped.
    """
    if not test_pairing or not edited_abs:
        return []

    # Repo-relative POSIX paths of every file edited this turn, so a paired test
    # that was itself edited is recognised regardless of how it was keyed.
    edited_rel: set[str] = set()
    for ap in edited_abs:
        try:
            edited_rel.add(Path(ap).relative_to(repo_root).as_posix())
        except ValueError:
            continue

    max_exports = threshold_int("STALE_TEST_ADVISORY_MAX_EXPORTS")
    items: list[StaleTestItem] = []
    for ap in sorted(edited_abs):
        try:
            lang = _normalize_language(language_of(ap))
            if lang is None:
                continue
            try:
                rel = Path(ap).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            # A test file editing itself is the synced case, not a stale one.
            if _is_test_path(rel, language=lang):
                continue
            archetype = archetype_of(ap)
            if not archetype:
                continue
            pairing = test_pairing.get(archetype)
            if not isinstance(pairing, dict):
                continue

            existing_test: str | None = None
            mapping: str | None = None
            for label, candidate in _candidate_test_paths(rel, language=lang):
                try:
                    if (repo_root / candidate).is_file():
                        existing_test = candidate
                        mapping = label
                        break
                except OSError:
                    continue
            if existing_test is None:
                # No paired test exists: that is the missing-test case, which is
                # intentionally not surfaced here (advisory scope is the existing
                # test going stale, never a hard "you must add a test").
                continue
            if existing_test in edited_rel:
                # The paired test was touched this turn; nothing is going stale.
                continue

            arch_exports = key_exports.get(archetype) or []
            exports: list[str] = []
            if arch_exports:
                content = read_content(ap)
                if content:
                    declared = set(changed_exports_in_content(content, language=lang))
                    arch_set = set(arch_exports)
                    # Keep the archetype's documented ordering for stable output.
                    exports = [n for n in arch_exports if n in declared and n in arch_set][
                        :max_exports
                    ]
            items.append(StaleTestItem(rel, existing_test, mapping, exports))
        except Exception:
            continue
    return items


# --- Change-set completeness (companion-artifact co-change) ---------------
#
# A small, hand-curated table of directional framework conventions: a new file
# matching ``trigger`` cannot stand alone, so the change-set is expected to also
# carry a file matching ``companion``. The direction is one-way (a new model
# wants a migration, but a migration does not require a model), which is why the
# trigger and companion predicates are separate. Co-presence is never derived
# from the repo; only this table fires, and only the rules a repo's own history
# vouches for (see ``cochange_rule_disabled``).
#
# Matching is on the repo-relative POSIX path so it is independent of where the
# repo lives on disk. Patterns are deliberately narrow (a Rails-shaped tree, a
# top-level model/controller directory) to keep the trigger from catching files
# that legitimately stand alone.


class CoChangeRule:
    """One directional companion-artifact convention.

    ``rule_id`` is the stable advisory name (also the ``chameleon-ignore`` token).
    ``language`` keys the rule to a source family so a Ruby rule never evaluates a
    TypeScript change-set. ``trigger`` decides whether a NEW file demands the
    companion; ``companion`` decides whether some other file in the change-set
    satisfies it. ``message`` is the human-readable expectation surfaced when the
    companion is absent.
    """

    __slots__ = ("rule_id", "language", "trigger", "companion", "message")

    def __init__(self, rule_id, language, trigger, companion, message) -> None:
        self.rule_id = rule_id
        self.language = language
        self.trigger = trigger
        self.companion = companion
        self.message = message


def _is_rails_model(rel: str) -> bool:
    # A concrete model under app/models, excluding concerns (mixins, not tables)
    # and the abstract ApplicationRecord base, which never gets its own migration.
    if not (rel.startswith("app/models/") and rel.endswith(".rb")):
        return False
    if "/concerns/" in rel:
        return False
    return rel.rsplit("/", 1)[-1] != "application_record.rb"


def _is_rails_migration(rel: str) -> bool:
    return rel.startswith("db/migrate/") and rel.endswith(".rb")


def _is_rails_controller(rel: str) -> bool:
    return (
        rel.startswith("app/controllers/")
        and rel.endswith("_controller.rb")
        and "/concerns/" not in rel
        and rel.rsplit("/", 1)[-1] != "application_controller.rb"
    )


def _is_rails_routes(rel: str) -> bool:
    # The central routes file or a nested route fragment under config/routes/.
    return rel == "config/routes.rb" or (rel.startswith("config/routes/") and rel.endswith(".rb"))


def _is_django_model(rel: str) -> bool:
    # A Django model module: models.py or a file in a models/ package (the
    # cross-app role form), excluding migrations and the package __init__. The
    # package form additionally consults the role classifier so a co-located
    # managers.py / querysets.py / signals.py -- which has no table and needs no
    # migration -- is not mistaken for a model.
    if not rel.endswith(".py") or "/migrations/" in rel:
        return False
    name = rel.rsplit("/", 1)[-1]
    if name == "models.py":
        return True
    if "/models/" not in rel or name == "__init__.py":
        return False
    from chameleon_mcp.signatures import python_role_for_path

    return python_role_for_path(rel) == "model"


def _is_django_migration(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return "/migrations/" in rel and rel.endswith(".py") and name != "__init__.py"


def _is_ts_migration_dir(rel: str) -> bool:
    # ORM migrations (Prisma, TypeORM, Knex, Sequelize) live under a migrations
    # directory; the leaf extension is the source family's, so this only needs the
    # directory component, which keeps it agnostic to the per-tool filename shape.
    parts = rel.split("/")
    return "migrations" in parts or "migration" in parts


def _is_prisma_schema(rel: str) -> bool:
    return rel.endswith(".prisma")


def _is_redux_slice(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return rel.endswith((".ts", ".tsx")) and (name.endswith("slice.ts") or "slice" in name)


def _is_store_registration(rel: str) -> bool:
    # The slice has to be wired into a store/reducers aggregation to take effect; a
    # slice file added with no store/reducer edit is the incomplete-change shape.
    low = rel.lower()
    return low.endswith((".ts", ".tsx")) and any(
        token in low for token in ("/store", "store.ts", "reducer", "rootreducer")
    )


# Ordered most-broadly-applicable first; the disable gate prunes the rest.
_COCHANGE_RULES: tuple[CoChangeRule, ...] = (
    CoChangeRule(
        "cochange-model-migration",
        "ruby",
        _is_rails_model,
        _is_rails_migration,
        "new model added without a db/migrate migration in the same change",
    ),
    CoChangeRule(
        "cochange-controller-route",
        "ruby",
        _is_rails_controller,
        _is_rails_routes,
        "new controller added without a config/routes change wiring it up",
    ),
    CoChangeRule(
        "cochange-django-model-migration",
        "python",
        _is_django_model,
        _is_django_migration,
        "new Django model added without a migrations/*.py migration in the same change "
        "(run makemigrations)",
    ),
    CoChangeRule(
        "cochange-prisma-migration",
        "typescript",
        _is_prisma_schema,
        _is_ts_migration_dir,
        "prisma schema changed without a migration in the same change",
    ),
    CoChangeRule(
        "cochange-slice-store",
        "typescript",
        _is_redux_slice,
        _is_store_registration,
        "new state slice added without wiring it into the store/reducers",
    ),
)


class ChangeSetItem:
    """One NEW file whose required companion is missing from the change-set."""

    __slots__ = ("source_rel", "rule_id", "message")

    def __init__(self, source_rel: str, rule_id: str, message: str) -> None:
        self.source_rel = source_rel
        self.rule_id = rule_id
        self.message = message


def _iter_repo_files(repo_root: Path, max_files: int):
    """Yield repo-relative POSIX paths of tracked-looking source files, bounded.

    A plain bounded walk that skips the usual heavy/irrelevant directories. Used
    only by the per-rule disable check, never on the turn hot path. Returns at
    most ``max_files`` paths so a giant monorepo cannot turn the disable check
    into an unbounded scan.
    """
    skip_dirs = {
        ".git",
        "node_modules",
        ".chameleon",
        "vendor",
        "tmp",
        "log",
        "coverage",
        "dist",
        "build",
        ".next",
        # Static-asset and test trees are never a source companion for any
        # co-change rule (a model's migration, a controller's route). On a large
        # monolith these dirs hold thousands of files; without skipping them the
        # bounded walk exhausts its file budget inside `public/` and `spec/`
        # (reverse-alphabetical pop order visits them first) and never reaches
        # `app/`, `config/`, or `db/`, silently disabling every Rails rule.
        "public",
        "spec",
        "test",
        "tests",
        "__tests__",
        "e2e",
        "cypress",
        "storybook",
        ".storybook",
        "fixtures",
        "assets",
        "docs",
        "doc",
        "public_html",
        "static",
    }
    count = 0
    stack = [repo_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in skip_dirs and not entry.is_symlink():
                        stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            try:
                rel = entry.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            yield rel
            count += 1
            if count >= max_files:
                return


def cochange_rule_disabled(rule: CoChangeRule, repo_root: Path) -> bool:
    """True when ``rule`` should not fire for this repo.

    Walks the repo's committed-looking files once, counts how many match the
    rule's trigger, and how many of those would be flagged were the rule applied
    repo-wide (a trigger file in a repo that carries no companion file at all). A
    rule whose committed violation rate exceeds the tolerance is treated as one
    this repo does not follow, so firing it on a new file would nag rather than
    catch a real gap. Stays silent (disabled) when the trigger sample is too thin
    to trust. Fails safe to disabled on any error: a check that cannot measure
    the repo must not let an unvetted rule fire.

    This is a coarse repo-applicability gate, not the per-entity check: it asks
    "does this repo keep triggers and companions together at all", which is the
    signal that decides whether the rule is worth surfacing here.
    """
    try:
        max_files = threshold_int("COCHANGE_MAX_FILES_SCANNED")
        min_trigger = threshold_int("COCHANGE_MIN_TRIGGER_FILES")
        max_rate = threshold_float("COCHANGE_MAX_VIOLATION_RATE")

        triggers = 0
        has_companion = False
        for rel in _iter_repo_files(repo_root, max_files):
            if not has_companion and rule.companion(rel):
                has_companion = True
            if rule.trigger(rel):
                triggers += 1
        if triggers < min_trigger:
            # Too few committed trigger files to trust the pairing signal.
            return True
        # With no companion file anywhere in the repo, every committed trigger is a
        # violation (rate 1.0); the rule plainly does not apply here. With at least
        # one companion present, the repo follows the convention and the rule is
        # kept. A finer per-trigger rate is intentionally avoided: companions are
        # not co-located with triggers (migrations are not per-model), so the only
        # honest repo-level signal is whether the companion category exists at all.
        violation_rate = 0.0 if has_companion else 1.0
        return violation_rate > max_rate
    except Exception:
        return True


def changeset_completeness_items(
    *,
    repo_root: Path,
    new_files_abs: set[str],
    edited_abs: set[str],
    language_of,
    rule_enabled=None,
) -> list[ChangeSetItem]:
    """Compute the turn's change-set-completeness advisory items.

    For each NEW file created this turn, find any co-change rule whose trigger it
    matches and whose companion no file in the whole change-set satisfies; emit an
    item per such (file, rule). Only newly created files trigger: editing a method
    on an existing model must not demand a fresh migration.

    Callables are injected so the hook owns repo I/O and the per-rule disable
    decision, keeping this a pure computation over the change-set:

    - ``language_of(abs_path) -> lint-engine language tag or None``
    - ``rule_enabled(CoChangeRule) -> bool`` (default: every rule enabled), the
      hook's wrapper around ``cochange_rule_disabled`` so the repo scan is cached
      across rules within one turn.

    ``edited_abs`` is the full change-set (created + modified); the companion may
    have been an edit to an existing file, so satisfaction is checked against the
    whole set, not just the new files. Returns [] when nothing applies; never
    raises for a single bad file.
    """
    if not new_files_abs:
        return []

    def _rel_set(abs_paths) -> set[str]:
        rels: set[str] = set()
        for ap in abs_paths:
            try:
                rels.add(Path(ap).relative_to(repo_root).as_posix())
            except ValueError:
                continue
        return rels

    changeset_rel = _rel_set(edited_abs | new_files_abs)
    if rule_enabled is None:
        rule_enabled = lambda _rule: True  # noqa: E731

    items: list[ChangeSetItem] = []
    for ap in sorted(new_files_abs):
        try:
            lang = _normalize_language(language_of(ap))
            if lang is None:
                continue
            try:
                rel = Path(ap).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            for rule in _COCHANGE_RULES:
                if rule.language != lang:
                    continue
                if not rule.trigger(rel):
                    continue
                # Some file in the change-set already satisfies the companion: the
                # change is coherent, nothing to surface.
                if any(rule.companion(c) for c in changeset_rel):
                    continue
                if not rule_enabled(rule):
                    continue
                items.append(ChangeSetItem(rel, rule.rule_id, rule.message))
        except Exception:
            continue
    return items
