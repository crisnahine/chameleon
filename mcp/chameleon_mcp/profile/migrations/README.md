# Profile schema migrations

This directory holds migration scripts that transform `profile.json` (and
sibling artifacts) between schema versions.

See `docs/architecture.md#migration-correctness-contract` for the complete
specification. Brief summary of the contract every migration must satisfy:

1. **Idempotence** — running a migration `v_k → v_{k+1}` twice on the
   same input produces the same output as running it once.
2. **Round-trip preservation** — if a migration is reversible, the inverse
   migration MUST exist and `migrate_back(migrate(p)) == p`. If not
   reversible, document explicitly in the migration's docstring.
3. **Partial-write atomicity** — migrations MUST use `bootstrap.transaction.atomic_profile_commit`
   to ensure either the original profile is unchanged OR the migrated
   profile is fully written. No half-migrated state.
4. **No-op detection** — if a profile is already at the target schema,
   the migration is a no-op (zero writes, zero side effects).
5. **Test obligation** — every migration ships with a test fixture pair
   `(input_v_k.json, expected_output_v_{k+1}.json)`. CI runs the migration
   on the input and asserts byte-equality with the expected output.

## Migration script naming

`v<from>_to_<to>.py` — e.g., `v3_to_v4.py`. Migration applies cleanly when
profile.json has `schema_version == <from>` and produces a profile with
`schema_version == <to>`.

## File-level vs database-level migrations

- **JSON profile artifacts** (profile.json, archetypes.json, etc.):
  forward-migration scripts here. Use `bootstrap.transaction.atomic_profile_commit`.
- **drift.db** (per-repo SQLite cache): drop-and-recreate is permitted on
  schema bumps. `/chameleon-refresh` rebuilds in <60s on typical repos.
- **index.db** (single SQLite registry): additive-only `ALTER TABLE` for
  new columns. Breaking changes require explicit migration script in
  `index_db_<from>_to_<to>.py`.

## v1 status

No migration scripts exist yet — the engine is at schema_version 8. The
v7→v8 bump (cluster-signature metric change) intentionally ships no migration:
an older-schema profile still loads, and re-bootstrap re-clusters it under the
new metric. A profile written by a *newer* schema is refused at load time; the
first migration script will be authored when a bump needs one.
