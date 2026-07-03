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

import re
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

# A top-level public def/class: the name is what the module exports. Anchored at
# column zero (no leading whitespace) so nested methods and class-body members,
# which are not part of the importable surface, are excluded. The bootstrap
# key-export derivation drops underscore-prefixed names, so the leading-letter
# class restricts this to the same public set.
_PY_TOP_LEVEL_PUBLIC_DECL_RE = re.compile(
    r"^(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)", re.MULTILINE
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
    elif language == "python":
        # Python has no export keyword: a module's public surface is its
        # top-level def/class names. Strip strings/comments first so a
        # ``def``/``class`` inside a docstring or string literal is not counted.
        from chameleon_mcp.lint_engine import _strip_python_strings_and_comments

        scan = _strip_python_strings_and_comments(content)
        for m in _PY_TOP_LEVEL_PUBLIC_DECL_RE.finditer(scan):
            name = m.group(1)
            if len(name) > 1:
                _add(name)
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
    companion is absent. ``framework`` (optional) gates the rule to a single
    framework family: a filename suffix that is not unique to one framework (a
    ``.controller.ts`` is NestJS, routing-controllers or Express MVC; a
    ``.module.ts`` is NestJS or Angular) only fires where the repo's dependency
    manifest names that framework, so it never nags a repo that merely shares the
    suffix.
    """

    __slots__ = (
        "rule_id",
        "language",
        "trigger",
        "companion",
        "message",
        "framework",
        "min_trigger",
        "fires_on_edit",
    )

    def __init__(
        self,
        rule_id,
        language,
        trigger,
        companion,
        message,
        framework=None,
        min_trigger=None,
        fires_on_edit=False,
    ) -> None:
        self.rule_id = rule_id
        self.language = language
        self.trigger = trigger
        self.companion = companion
        self.message = message
        self.framework = framework
        # Per-rule committed-trigger floor override (default: the global
        # COCHANGE_MIN_TRIGGER_FILES). A single-file-convention artifact like a
        # Prisma schema (repos carry exactly one) needs a floor of 1, not 8.
        self.min_trigger = min_trigger
        # When True, an EDIT to an existing trigger file (not just a new file)
        # fires the rule. A Prisma schema change is almost always an edit to the
        # single existing schema.prisma, so a new-files-only rule never sees it.
        self.fires_on_edit = fires_on_edit


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
    if not rel.endswith(".py") or name == "__init__.py":
        return False
    if "/migrations/" in rel:
        return True
    # Alembic (the standard SQLAlchemy/FastAPI layout) keeps revisions in a
    # `versions/` dir under `alembic/` (or `migrations/`), which has no
    # `/migrations/` path segment. Mirror python_role_for_path's migration-role
    # classifier so the model-migration coupling rule the message advertises for
    # SQLAlchemy actually recognizes the companion.
    dirs = rel.split("/")[:-1]
    return "versions" in dirs and ("alembic" in dirs or "migrations" in dirs)


def _is_ts_migration_dir(rel: str) -> bool:
    # ORM migrations (Prisma, TypeORM, Knex, Sequelize) live under a migrations
    # directory; the leaf extension is the source family's, so this only needs the
    # directory component, which keeps it agnostic to the per-tool filename shape.
    parts = rel.split("/")
    return "migrations" in parts or "migration" in parts


def _is_prisma_schema(rel: str) -> bool:
    return rel.endswith(".prisma")


def _is_redux_slice(rel: str) -> bool:
    # The Redux Toolkit convention is a file named `fooSlice.ts` / `fooSlice.tsx`
    # with a CAPITAL S (createSlice's convention). Match that exact suffix token
    # (a name char + `Slice` + ext) so `userSlice.ts` matches but the over-broad
    # substring cases -- `imageSlicer.ts`, `pizzaSlices.ts`, `backslice.ts`
    # (lowercase), `sliceUtils.ts` -- do not.
    name = rel.rsplit("/", 1)[-1]
    return bool(re.search(r"[A-Za-z0-9]Slice\.tsx?$", name))


def _is_store_registration(rel: str) -> bool:
    # The slice has to be wired into a store/reducers aggregation to take effect; a
    # slice file added with no store/reducer edit is the incomplete-change shape.
    low = rel.lower()
    return low.endswith((".ts", ".tsx")) and any(
        token in low for token in ("/store", "store.ts", "reducer", "rootreducer")
    )


def _is_nestjs_controller(rel: str) -> bool:
    # A NestJS HTTP controller. Test files end in `.controller.spec.ts` /
    # `.controller.e2e-spec.ts`, i.e. `.spec.ts`, so the `.controller.ts` suffix
    # match excludes them. The suffix alone is not NestJS-unique (routing-
    # controllers, tsoa, Express MVC), so the rule is `framework="nestjs"`-gated.
    return rel.endswith(".controller.ts")


def _is_nestjs_module(rel: str) -> bool:
    # A NestJS module declaration: its `controllers: [...]` array is where a new
    # controller is registered to be routed. `.module.ts` is also Angular's
    # NgModule suffix, hence the framework gate on the rule.
    return rel.endswith(".module.ts")


# Dependency-manifest markers that confirm a framework family for a framework-
# gated rule. Cheap substring probe of package.json (no code execution); mirrors
# the nestjs arm of the bootstrap classifier (orchestrator._classify_framework).
_FRAMEWORK_DEP_MARKERS: dict[str, tuple[str, ...]] = {
    "nestjs": ('"@nestjs/core"', '"@nestjs/common"'),
}


def _manifest_declares(pkg_path: Path, framework: str) -> bool:
    markers = _FRAMEWORK_DEP_MARKERS.get(framework)
    if not markers:
        return False
    try:
        text = pkg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(m in text for m in markers)


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
        "new model added without a migrations/*.py migration in the same change "
        "(Django: run makemigrations; SQLAlchemy: add the Alembic revision)",
    ),
    CoChangeRule(
        "cochange-prisma-migration",
        "typescript",
        _is_prisma_schema,
        _is_ts_migration_dir,
        "prisma schema changed without a migration in the same change",
        # A repo carries exactly one schema.prisma (floor of 1, not 8), and the
        # change is an EDIT to that existing schema, not a new file.
        min_trigger=1,
        fires_on_edit=True,
    ),
    CoChangeRule(
        "cochange-slice-store",
        "typescript",
        _is_redux_slice,
        _is_store_registration,
        "new state slice added without wiring it into the store/reducers",
    ),
    CoChangeRule(
        "cochange-nestjs-controller-module",
        "typescript",
        _is_nestjs_controller,
        _is_nestjs_module,
        "new NestJS controller added without registering it in a @Module "
        "(add it to a module's controllers: [...] array, or it is never routed)",
        framework="nestjs",
    ),
)


class ChangeSetItem:
    """One NEW file whose required companion is missing from the change-set."""

    __slots__ = ("source_rel", "rule_id", "message")

    def __init__(self, source_rel: str, rule_id: str, message: str) -> None:
        self.source_rel = source_rel
        self.rule_id = rule_id
        self.message = message


# Directory names the co-change rules care about (a model's migration, a
# controller's route, a source-and-migration pairing) across Rails/Django/Next,
# with a visit-order RANK. The bounded walk visits higher-rank dirs first so a
# giant monorepo does not spend its file budget on unrelated trees before
# reaching the rule-relevant ones. `app` outranks `db` so the trigger side
# (models/controllers under app/) is reached even when db/ alone is large; both
# still fit under the (raised) budget. `lib` is deliberately absent -- it is
# large on a monolith and holds no cochange trigger/companion.
_COCHANGE_DIR_RANK: dict[str, int] = {
    "app": 5,
    "models": 5,
    "controllers": 5,
    "config": 4,
    "routes": 4,
    "db": 3,
    "migrations": 3,
    "migrate": 3,
    "src": 2,
    "api": 2,
}


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
        # Visit the co-change-relevant trees FIRST so the bounded budget reaches
        # them before it is spent. The stack is LIFO (pop takes the last-pushed),
        # so pushing entries in ascending sort order makes the walk explore the
        # LAST-sorted dir first -- default alphabetical order visits
        # `workhorse`/`qa`/`gems`/`ee` first on a large monolith (gitlabhq) and
        # the budget is gone before `app`/`db`/`config` are reached, silently
        # disabling every Rails cochange rule. Sort by RANK ascending (higher rank
        # last -> popped first): app/models/controllers win, then config/routes,
        # then db/migrate. Pure traversal-order change.
        entries.sort(key=lambda e: (_COCHANGE_DIR_RANK.get(e.name, 0), e.name))
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
        # A single-file-convention rule (Prisma: exactly one schema.prisma) sets
        # its own floor; the global 8-file floor is right for Rails/Django where
        # repos carry dozens of models but would permanently disable a one-schema
        # rule.
        min_trigger = rule.min_trigger or threshold_int("COCHANGE_MIN_TRIGGER_FILES")
        max_rate = threshold_float("COCHANGE_MAX_VIOLATION_RATE")

        triggers = 0
        has_companion = False
        # A framework-gated rule fires only where the repo's manifest names that
        # framework, so a shared filename suffix (.controller.ts / .module.ts) does
        # not arm it on an Angular / routing-controllers / Express repo. Resolved
        # in the same single walk as triggers/companions (no extra repo scan).
        framework_ok = rule.framework is None
        for rel in _iter_repo_files(repo_root, max_files):
            if not has_companion and rule.companion(rel):
                has_companion = True
            if rule.trigger(rel):
                triggers += 1
            if not framework_ok and rel.rsplit("/", 1)[-1] == "package.json":
                framework_ok = _manifest_declares(repo_root / rel, rule.framework)
        if not framework_ok:
            # The rule names a framework this repo does not declare.
            return True
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
    # Files edited but not newly created; only rules that opt into `fires_on_edit`
    # (Prisma) evaluate these, so every other rule stays strictly new-files-only.
    edit_only_abs = set(edited_abs) - set(new_files_abs)
    if not new_files_abs and not edit_only_abs:
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

    def _cochange_lang(ap: str):
        lang = _normalize_language(language_of(ap))
        if lang is None and ap.endswith(".prisma"):
            # A `.prisma` schema is a TypeScript/JS-ecosystem artifact language_of
            # does not recognize; the prisma rule (keyed typescript) triggers on
            # it, so treat it as typescript here rather than skipping it entirely.
            return "typescript"
        return lang

    items: list[ChangeSetItem] = []
    seen: set[tuple[str, str]] = set()
    # (abs_path, is_new); new files evaluate every rule, edited-only files only
    # the fires_on_edit rules.
    candidates = [(ap, True) for ap in new_files_abs] + [(ap, False) for ap in edit_only_abs]
    for ap, is_new in sorted(candidates):
        try:
            lang = _cochange_lang(ap)
            if lang is None:
                continue
            try:
                rel = Path(ap).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            for rule in _COCHANGE_RULES:
                if rule.language != lang:
                    continue
                if not is_new and not rule.fires_on_edit:
                    continue
                if not rule.trigger(rel):
                    continue
                # Some file in the change-set already satisfies the companion: the
                # change is coherent, nothing to surface.
                if any(rule.companion(c) for c in changeset_rel):
                    continue
                if not rule_enabled(rule):
                    continue
                key = (rel, rule.rule_id)
                if key in seen:
                    continue
                seen.add(key)
                items.append(ChangeSetItem(rel, rule.rule_id, rule.message))
        except Exception:
            continue
    return items
